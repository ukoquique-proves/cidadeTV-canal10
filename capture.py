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

stderr draining
---------------
ffmpeg's stderr is drained concurrently from the moment the process starts.
This prevents the stderr pipe from filling up (~64 KB) and blocking ffmpeg,
which would block our stdout reads and stall the whole pipeline. The last 8 KB
of stderr is kept for error reporting.
"""

import asyncio
import logging
import os
import random
import time
from asyncio.subprocess import DEVNULL, PIPE
from collections import deque

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_SEC = SAMPLE_RATE * 2  # int16 mono

# ~2 minutes of audio at 100 ms per chunk
DEFAULT_QUEUE_CHUNKS = 1200

# Single source of truth for the default stream (main.py and server.py import it).
DEFAULT_STREAM_URL = "https://video10.logicahost.com.br/tvcidade10/tvcidade10/playlist.m3u8"


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
# Process management
# ---------------------------------------------------------------------------

class StallError(Exception):
    """ffmpeg is alive but has produced no audio for too long."""


class FfmpegProcess:
    """Owns one ffmpeg child. stderr is drained from the moment it starts, so the
    child can never block on a full pipe, and stop() can never hang on one."""

    def __init__(self, cmd: list[str], *, grace_s: float = 3.0, tail_bytes: int = 8192):
        self.cmd, self._grace, self._tail_max = cmd, grace_s, tail_bytes
        self._proc: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task | None = None
        self._tail = bytearray()

    async def __aenter__(self):
        self._proc = await asyncio.create_subprocess_exec(*self.cmd, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)
        self._drain = asyncio.create_task(self._drain_stderr(), name="ffmpeg-stderr")
        return self

    async def __aexit__(self, *_exc):
        await self.stop()

    async def _drain_stderr(self) -> None:
        while chunk := await self._proc.stderr.read(4096):
            self._tail += chunk
            if len(self._tail) > self._tail_max:
                del self._tail[: -self._tail_max]          # keep only the newest bytes

    @property
    def stderr_tail(self) -> str:
        lines = [l for l in self._tail.decode(errors="replace").splitlines() if l.strip()]
        return " | ".join(l[:200] for l in lines[-3:])

    @property
    def pid(self): return self._proc.pid

    async def read(self, n: int, timeout: float) -> bytes:
        try:
            return await asyncio.wait_for(self._proc.stdout.read(n), timeout)
        except asyncio.TimeoutError:
            raise StallError(f"no audio for {timeout:.0f}s") from None

    async def exit_code(self, timeout: float = 5.0) -> int | None:
        try:
            return await asyncio.wait_for(self._proc.wait(), timeout)
        except asyncio.TimeoutError:
            return None

    async def stop(self) -> None:
        p = self._proc
        # asyncio only reports an exit once BOTH pipes hit EOF, so while stopping we must
        # keep consuming stdout too (the reader may have been cancelled mid-stream).
        sink = asyncio.create_task(self._discard(p.stdout)) if p is not None else None
        if p is not None and p.returncode is None:
            for sig in ("terminate", "kill"):
                try:
                    getattr(p, sig)()
                except ProcessLookupError:
                    break
                try:
                    await asyncio.wait_for(p.wait(), self._grace)
                    break
                except asyncio.TimeoutError:
                    continue
            else:
                logger.error("ffmpeg pid=%s would not die; abandoning it", p.pid)
        for t in (self._drain, sink):
            if t is not None:
                try:
                    await asyncio.wait_for(t, 1.0)             # both end by themselves at EOF
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

    @staticmethod
    async def _discard(stream) -> None:
        while await stream.read(65536):
            pass


class Chunker:
    """Pure byte-slicing. No I/O, so it is trivially unit-testable."""

    def __init__(self, chunk_bytes: int):
        self.chunk_bytes, self.emitted, self._buf = chunk_bytes, 0, bytearray()

    @property
    def received_end(self) -> int:
        return self.emitted + len(self._buf)

    def discard_partial(self) -> None:
        self._buf.clear()                                   # timeline position is unchanged

    def feed(self, raw: bytes) -> list[tuple[int, bytes]]:
        self._buf += raw
        out = []
        while len(self._buf) >= self.chunk_bytes:
            out.append((self.emitted, bytes(self._buf[: self.chunk_bytes])))
            del self._buf[: self.chunk_bytes]
            self.emitted += self.chunk_bytes
        return out


# ---------------------------------------------------------------------------
# ffmpeg setup
# ---------------------------------------------------------------------------

def _bytes_per_chunk(chunk_ms: int) -> int:
    return int(SAMPLE_RATE * chunk_ms / 1000) * 2


def _build_ffmpeg_cmd(stream_url: str) -> list[str]:
    """
    ffmpeg command: HLS/file in, raw 16 kHz mono PCM out on stdout.

    * -live_start_index -1  start at the newest segment instead of the default
                            3rd-from-last, so we don't transcribe old audio
                            first and are already near the live edge.
                            (HLS only, skipped for local files)
    * -reconnect*           auto-reconnect on network drops.
                            (HLS only, skipped for local files)
    * No -re: we WANT segments as fast as they arrive; AudioClock accounts
                            for the burstiness.
    """
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    
    # Only add HLS-specific options for HLS streams (http/https)
    is_hls = stream_url.startswith("http://") or stream_url.startswith("https://")
    
    if is_hls:
        cmd.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-live_start_index", "-1",
        ])
    
    cmd.extend([
        "-i", stream_url,
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "pipe:1",
    ])
    
    return cmd


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
    chunker = Chunker(chunk_bytes)
    stats = {"dropped": 0, "last_warn": 0.0}
    retry_delay = 2.0  # first retry after a healthy run resets to 2 s; initial is 2 s
    stall_s = 30.0  # no audio for 30 s = timeout

    logger.info("Starting ffmpeg capture from %s", stream_url)
    logger.debug("Command: %s", " ".join(cmd))

    while True:
        ff, t0, got_audio, why = FfmpegProcess(cmd), time.monotonic(), False, "eof"
        try:
            async with ff:
                logger.info("ffmpeg started (pid=%d)", ff.pid)
                clock.reset()
                chunker.discard_partial()
                _put(queue, (chunker.emitted, None), stats)  # discontinuity marker

                while raw := await ff.read(chunk_bytes * 8, stall_s):
                    got_audio = True
                    for emitted, chunk in chunker.feed(raw):
                        _put(queue, (emitted, chunk), stats)
                    clock.observe(chunker.received_end / BYTES_PER_SEC)
                    await asyncio.sleep(0)

                rc = await ff.exit_code()
                why = f"exited rc={rc}"

        except StallError as e:
            why = f"stalled ({e})"
        except asyncio.CancelledError:
            logger.info("Capture loop cancelled — shutting down ffmpeg")
            raise                                            # __aexit__ already reaped the child
        except Exception as exc:                             # noqa: BLE001
            why = f"error: {exc!r}"

        up = time.monotonic() - t0
        # Backoff only resets after a *healthy* run, so a stream that gives 1 s of audio
        # and dies is not retried every 2 s forever.
        healthy_s = 20.0
        retry_delay = 2.0 if (got_audio and up >= healthy_s) else min(retry_delay * 2, 60.0)

        logger.warning("ffmpeg %s after %.0fs; stderr: %s; restart in ~%.0fs (±20%%)",
                       why, up, ff.stderr_tail or "-", retry_delay)

        await asyncio.sleep(retry_delay * random.uniform(0.8, 1.2))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_capture(
    queue: asyncio.Queue,
    stream_url: str | None = None,
    chunk_ms: int = 100,
) -> asyncio.Task:
    """Start the ffmpeg capture loop as a background asyncio Task."""
    url = stream_url or os.environ.get("STREAM_URL", DEFAULT_STREAM_URL)
    return asyncio.create_task(_reader_loop(queue, url, chunk_ms), name="capture")


async def stop_capture(task: asyncio.Task) -> None:
    """Cancel the capture task and wait for it to finish cleanly."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Capture stopped")
