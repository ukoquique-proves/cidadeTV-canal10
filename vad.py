"""
vad.py — Voice Activity Detection and speech segmentation.

Reads ``(start_byte, pcm_bytes)`` items from capture.py, runs a voice detector
on 32 ms windows and emits complete speech segments.

Output queue items are tuples::

    (pcm_bytes, audio_start_s, emit_wall)

* ``audio_start_s`` start of the segment on the capture timeline (seconds,
  exact to one 32 ms window — computed from byte positions, not from timers).
* ``emit_wall``     wall-clock time when the segment was complete (used for
  latency logging).

Why VAD
-------
Fixed-length chunks cut words in half.  Cutting at natural pauses gives
Whisper whole phrases, which is both more accurate and easier to subtitle.

Tuning (environment variables, all optional)
--------------------------------------------
VAD_THRESHOLD        speech probability threshold              (default 0.5)
VAD_MIN_SPEECH_MS    drop segments shorter than this           (default 500)
VAD_MAX_SEGMENT_MS   force a cut after this long without pause (default 5000)
VAD_SILENCE_MS       pause length that ends a segment          (default 500)
VAD_PREROLL_MS       audio kept from before the detected onset (default 300)

Lower MAX_SEGMENT_MS = lower latency (a segment can only be transcribed once it
is complete) but more mid-sentence cuts.  5000 ms is a reasonable default:
saves ~3 s of worst-case latency vs. 8000 ms with acceptable accuracy loss.
"""

import asyncio
import logging
import os
import time
from collections import deque

import numpy as np

from capture import BYTES_PER_SEC, SAMPLE_RATE

logger = logging.getLogger(__name__)

WINDOW_SAMPLES = 512                      # Silero window at 16 kHz = 32 ms
WINDOW_BYTES = WINDOW_SAMPLES * 2


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _ms_to_bytes(ms: int) -> int:
    """Milliseconds → bytes, rounded down to a whole number of windows."""
    n = int(SAMPLE_RATE * ms / 1000) * 2
    return max(WINDOW_BYTES, n - n % WINDOW_BYTES)


# ---------------------------------------------------------------------------
# Detectors  (callable: window_bytes -> bool, plus reset())
# ---------------------------------------------------------------------------

class SileroDetector:
    """Silero VAD.  Prefers the `silero-vad` pip package (model bundled, works
    offline); falls back to torch.hub (downloads from GitHub on first run)."""

    def __init__(self, threshold: float = 0.5) -> None:
        import torch  # imported lazily so tests don't need torch

        self._torch = torch
        self.threshold = threshold
        try:
            from silero_vad import load_silero_vad

            self.model = load_silero_vad()
        except ImportError:
            logger.warning("silero-vad package not found — using torch.hub")
            self.model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
            )
        self.model.eval()
        self.reset()

    def reset(self) -> None:
        self.model.reset_states()

    def __call__(self, window: bytes) -> bool:
        samples = np.frombuffer(window, dtype=np.int16).astype(np.float32) / 32768.0
        with self._torch.no_grad():
            prob = self.model(self._torch.from_numpy(samples), SAMPLE_RATE).item()
        return prob > self.threshold


class EnergyDetector:
    """Trivial RMS detector — used by the tests (no model needed)."""

    def __init__(self, threshold: float = 0.02) -> None:
        self.threshold = threshold

    def reset(self) -> None:
        pass

    def __call__(self, window: bytes) -> bool:
        s = np.frombuffer(window, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(s * s))) > self.threshold


# ---------------------------------------------------------------------------
# VadSegmenter
# ---------------------------------------------------------------------------

class VadSegmenter:
    def __init__(
        self,
        in_queue: asyncio.Queue,
        out_queue: asyncio.Queue,
        detector=None,
        min_speech_ms: int | None = None,
        max_segment_ms: int | None = None,
        silence_ms: int | None = None,
        preroll_ms: int | None = None,
    ) -> None:
        self.in_queue = in_queue
        self.out_queue = out_queue
        self._detector = detector
        self.min_speech_bytes = _ms_to_bytes(min_speech_ms or _env_int("VAD_MIN_SPEECH_MS", 500))
        self.max_segment_bytes = _ms_to_bytes(max_segment_ms or _env_int("VAD_MAX_SEGMENT_MS", 5000))
        self.silence_bytes = _ms_to_bytes(silence_ms or _env_int("VAD_SILENCE_MS", 500))
        self.preroll_windows = _ms_to_bytes(preroll_ms or _env_int("VAD_PREROLL_MS", 300)) // WINDOW_BYTES
        self._task: asyncio.Task | None = None

        # --- state of the current stream ---
        self._win_buf = bytearray()   # audio not yet processed (< 1 window after each chunk)
        self._win_pos = 0             # byte position of _win_buf[0]
        self._reset_segment_state()

    # ------------------------------------------------------------------ API

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._run(), name="vad")
        return self._task

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------ internals

    def _reset_segment_state(self) -> None:
        self._in_speech = False
        self._speech = bytearray()
        self._pending_silence = bytearray()
        self._seg_start_pos = 0
        self._preroll: deque[tuple[int, bytes]] = deque(maxlen=max(1, self.preroll_windows))

    def _detect_many(self, windows: list[bytes]) -> list[bool]:
        # One executor hop per chunk instead of one per 32 ms window
        return [self._detector(w) for w in windows]

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        if self._detector is None:
            logger.info("Loading Silero VAD…")
            thr = float(os.environ.get("VAD_THRESHOLD", "0.5"))
            try:
                self._detector = await loop.run_in_executor(None, SileroDetector, thr)
                logger.info("Silero VAD ready")
            except ModuleNotFoundError:
                logger.warning(
                    "torch/silero-vad not installed — falling back to EnergyDetector. "
                    "Install torch to use the full Silero VAD model."
                )
                self._detector = EnergyDetector()
        logger.info("VAD segmenter started")

        try:
            while True:
                pos, raw = await self.in_queue.get()

                if raw is None:  # discontinuity marker (ffmpeg restarted)
                    self._end_stream()
                    continue

                if self._win_buf and pos != self._win_pos + len(self._win_buf):
                    logger.warning("Audio gap of %.2f s — ending current segment",
                                   (pos - self._win_pos - len(self._win_buf)) / BYTES_PER_SEC)
                    self._end_stream()
                if not self._win_buf:
                    self._win_pos = pos
                self._win_buf.extend(raw)

                windows, positions = [], []
                while len(self._win_buf) >= WINDOW_BYTES:
                    windows.append(bytes(self._win_buf[:WINDOW_BYTES]))
                    positions.append(self._win_pos)
                    del self._win_buf[:WINDOW_BYTES]
                    self._win_pos += WINDOW_BYTES
                if not windows:
                    continue

                flags = await loop.run_in_executor(None, self._detect_many, windows)
                for wpos, window, is_speech in zip(positions, windows, flags):
                    self._step(wpos, window, is_speech)

        except asyncio.CancelledError:
            self._flush()
            logger.info("VAD segmenter stopped")
            raise

    def _end_stream(self) -> None:
        """Flush what we have and forget buffered audio (gap in the stream)."""
        self._flush()
        self._win_buf.clear()
        self._reset_segment_state()
        if self._detector is not None:
            self._detector.reset()

    def _step(self, wpos: int, window: bytes, is_speech: bool) -> None:
        if is_speech:
            if not self._in_speech:
                self._in_speech = True
                # include a little audio from before the detected onset
                self._seg_start_pos = self._preroll[0][0] if self._preroll else wpos
                self._speech = bytearray(b"".join(w for _, w in self._preroll))
                self._preroll.clear()
            if self._pending_silence:
                self._speech.extend(self._pending_silence)
                self._pending_silence.clear()
            self._speech.extend(window)
            if len(self._speech) >= self.max_segment_bytes:
                self._flush()          # long speech without a pause: cut here
        elif self._in_speech:
            self._pending_silence.extend(window)
            if len(self._pending_silence) >= self.silence_bytes:
                self._flush()          # real pause: trailing silence is dropped
        else:
            self._preroll.append((wpos, window))

    def _flush(self) -> None:
        """Emit the buffered speech (if long enough) and reset segment state."""
        if self._in_speech and len(self._speech) >= self.min_speech_bytes:
            self._emit(bytes(self._speech), self._seg_start_pos / BYTES_PER_SEC)
        elif self._in_speech:
            logger.debug("Segment too short (%d bytes) — discarded", len(self._speech))
        self._in_speech = False
        self._speech = bytearray()
        self._pending_silence = bytearray()

    def _emit(self, segment: bytes, audio_start_s: float) -> None:
        item = (segment, audio_start_s, time.time())
        logger.debug("Segment %.1f s at %.2f s", len(segment) / BYTES_PER_SEC, audio_start_s)
        try:
            self.out_queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.out_queue.get_nowait()
                logger.warning("Transcriber is behind — dropped the oldest segment")
            except asyncio.QueueEmpty:
                pass
            self.out_queue.put_nowait(item)
