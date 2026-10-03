"""
capture.py — Continuous audio capture from an HLS stream via an ffmpeg pipe.

ffmpeg writes raw signed 16-bit little-endian PCM (16 kHz, mono) to stdout.
The audio is cut into fixed-size chunks and placed on an asyncio.Queue.

Every queue item is a tuple ``(start_byte, pcm_bytes)``:

* ``start_byte``  position of the chunk on the *capture timeline* (bytes since
                  the process started; divide by BYTES_PER_SEC for seconds).
                  The timeline is continuous across ffmpeg restarts.
* ``pcm_bytes``   the audio, or ``None`` as a *discontinuity marker* (ffmpeg was
                  restarted, so audio before and after the marker is not
                  contiguous).

Why positions instead of "just count bytes downstream"
------------------------------------------------------
HLS delivers audio in bursts (a whole 4-10 s segment at once).  The previous
version used a 4 s queue that dropped the oldest chunks on overflow, which
silently lost roughly a third of the audio in a burst simulation.  Now the
queue holds ~2 minutes of audio (a few MB), and if it ever does overflow the
dropped chunks leave a *visible gap* in ``start_byte`` that the VAD handles.

AudioClock
----------
Captions have to be shown when the matching speech is on screen, so we need to
know *when* (wall clock) a given point of the audio was at the live edge.
ffmpeg hands us audio in bursts, so the arrival time of a chunk is only exact
for the *last* bytes of a burst; earlier bytes arrive "late".  Therefore, for
every read we compute ``offset = wall_now - audio_position_of_last_byte`` and
keep the **minimum** over a sliding window: the smallest offset is the one
measured right when the newest audio arrived.  ``clock.live_time(audio_s)``
then returns ``audio_s + offset``.
"""

import asyncio
import logging
import os
import time
from collections import deque

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_SEC = SAMPLE_RATE * 2  # int16 mono

# ~2 minutes of audio at 100 ms per chunk
DEFAULT_QUEUE_CHUNKS = 1200


# ---------------------------------------------------------------------------
# Audio clock
# ---------------------------------------------------------------------------

class AudioClock:
    """Maps positions on the capture timeline to wall-clock 'live' time."""

    def __init__(self, window_s: float = 90.0) -> None:
        self.window_s = window_s
        self._obs: deque[tuple[float, float]] = deque()  # (wall, offset)

    def reset(self) -> None:
        self._obs.clear()

    def observe(self, audio_end_s: float, wall: float | None = None) -> None:
        """Record that the audio up to `audio_end_s` has just arrived."""
        wall = time.time() if wall is None else wall
        self._obs.append((wall, wall - audio_end_s))
        cutoff = wall - self.window_s
        while self._obs and self._obs[0][0] < cutoff:
            self._obs.popleft()

    @property
    def offset(self) -> float | None:
        if not self._obs:
            return None
        return min(o for _, o in self._obs)

    def live_time(self, audio_s: float) -> float | None:
        """Wall-clock time at which `audio_s` was at the live edge (or None)."""
        off = self.offset
        return None if off is None else audio_s + off


clock = AudioClock()


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def _bytes_per_chunk(chunk_ms: int) -> int:
    return int(SAMPLE_RATE * chunk_ms / 1000) * 2


def _build_ffmpeg_cmd(stream_url: str) -> list[str]:
    """
    ffmpeg command: HLS in, raw 16 kHz mono PCM out on stdout.

    * -live_start_index -1  start at the newest segment instead of the default
                            3rd-from-last, so we don't transcribe old audio
                            first and are already near the live edge.
    * -reconnect*           auto-reconnect on network drops.
    * No -re: we WANT segments as fast as they arrive; AudioClock accounts
                            for the burstiness.
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-live_start_index", "-1",
        "-i", stream_url,
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "pipe:1",
    ]


async def _terminate(proc: asyncio.subprocess.Process | None) -> None:
    """Stop an ffmpeg process and reap it (no zombies)."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    except ProcessLookupError:
        pass


def _put(queue: asyncio.Queue, item: tuple, stats: dict) -> None:
    """Put without blocking; on overflow drop the OLDEST item (and say so)."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        queue.put_nowait(item)
        stats["dropped"] += 1
        now = time.time()
        if now - stats["last_warn"] > 5:
            logger.warning(
                "Audio queue overflow — the pipeline is falling behind real time "
                "(%d chunks dropped so far). Use a smaller/faster Whisper model.",
                stats["dropped"],
            )
            stats["last_warn"] = now


# ---------------------------------------------------------------------------
# Reader loop
# ---------------------------------------------------------------------------

async def _reader_loop(queue: asyncio.Queue, stream_url: str, chunk_ms: int) -> None:
    chunk_bytes = _bytes_per_chunk(chunk_ms)
    cmd = _build_ffmpeg_cmd(stream_url)
    emitted = 0  # bytes handed to the queue; monotonic across restarts
    stats = {"dropped": 0, "last_warn": 0.0}
    retry_delay = 2.0  # starts at 2 s, backs off up to 60 s

    logger.info("Starting ffmpeg capture from %s", stream_url)
    logger.debug("Command: %s", " ".join(cmd))

    while True:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("ffmpeg started (pid=%d)", proc.pid)
            clock.reset()  # offset from the previous run is no longer valid
            _put(queue, (emitted, None), stats)  # discontinuity marker
            buf = bytearray()
            got_audio = False

            while True:
                raw = await proc.stdout.read(chunk_bytes * 8)
                if not raw:
                    # Read stderr to surface the real ffmpeg error
                    try:
                        stderr_out = await asyncio.wait_for(
                            proc.stderr.read(2048), timeout=1.0
                        )
                        err_msg = stderr_out.decode(errors="replace").strip()
                    except (asyncio.TimeoutError, Exception):
                        err_msg = ""
                    if err_msg:
                        logger.warning("ffmpeg error: %s", err_msg.splitlines()[-1])
                    if got_audio:
                        # Stream dropped mid-session — short retry
                        retry_delay = 2.0
                        logger.warning("ffmpeg stdout closed, restarting in %.0f s…", retry_delay)
                    else:
                        # Never got audio — stream may be down, back off
                        retry_delay = min(retry_delay * 2, 60.0)
                        logger.warning(
                            "ffmpeg exited without producing audio "
                            "(stream down?), retrying in %.0f s…",
                            retry_delay,
                        )
                    break

                got_audio = True
                retry_delay = 2.0  # reset backoff on successful audio
                buf.extend(raw)
                # Position of the last byte we have received so far
                clock.observe((emitted + len(buf)) / BYTES_PER_SEC)

                while len(buf) >= chunk_bytes:
                    chunk = bytes(buf[:chunk_bytes])
                    del buf[:chunk_bytes]
                    _put(queue, (emitted, chunk), stats)
                    emitted += chunk_bytes

                # Let the consumers run even if ffmpeg delivered a big burst
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            logger.info("Capture loop cancelled — shutting down ffmpeg")
            await _terminate(proc)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in capture loop: %s", exc, exc_info=True)
        finally:
            await _terminate(proc)

        await asyncio.sleep(retry_delay)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_capture(
    queue: asyncio.Queue,
    stream_url: str | None = None,
    chunk_ms: int = 100,
) -> asyncio.Task:
    """Start the ffmpeg capture loop as a background asyncio Task."""
    url = stream_url or os.environ.get(
        "STREAM_URL",
        "https://video10.logicahost.com.br/tvcidade10/tvcidade10/playlist.m3u8",
    )
    return asyncio.create_task(_reader_loop(queue, url, chunk_ms), name="capture")


async def stop_capture(task: asyncio.Task) -> None:
    """Cancel the capture task and wait for it to finish cleanly."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Capture stopped")
