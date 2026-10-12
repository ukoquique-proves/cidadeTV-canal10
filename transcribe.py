"""
transcribe.py — Transcription with faster-whisper (local) or Groq (cloud).

Reads ``(pcm_bytes, audio_start_s, emit_wall)`` tuples from the VAD queue,
runs ASR, and puts one dict per sentence-level segment onto the output queue::

    {
        "pt":          str,    # Portuguese text
        "audio_start": float,  # start on the capture timeline (seconds)
        "audio_end":   float,  # end   on the capture timeline (seconds)
        "t_emit":      float,  # wall time the VAD segment was complete
        "t_asr":       float,  # wall time ASR finished
    }

Backend selection
-----------------
GROQ_API_KEY set  →  Groq cloud ASR (whisper-large-v3-turbo, ~216× real-time).
                     No local model is loaded at all.
GROQ_API_KEY unset →  local faster-whisper (auto-selects device/compute/size).

Groq ASR
--------
Sends a WAV-encoded buffer to https://api.groq.com/openai/v1/audio/transcriptions.
Returns per-segment timestamps (verbose_json).  A 5 s segment typically
completes in ~100–300 ms round-trip, including network.  Free-tier rate limit
is generous for personal use.

Local faster-whisper env vars (ignored when Groq is active)
-----------------------------------------------------------
WHISPER_MODEL    tiny | small | medium | large-v3  (auto: tiny/medium by device)
WHISPER_DEVICE   cpu | cuda | auto  (default: auto-detect)
WHISPER_COMPUTE  int8 | float16 | float32 | auto
WHISPER_BEAM     1 = greedy/fastest; 2-5 more accurate
WHISPER_PROMPT   initial prompt (channel names improve accuracy a lot)
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
import wave
from typing import Callable

import numpy as np

from groq_client import is_transient as _is_transient

logger = logging.getLogger(__name__)

_whisper_model = None

# Phrases Whisper tends to invent on music / noise / silence.
_ALWAYS_HALLUCINATION = re.compile(r"amara\.org|legendas pela comunidade|legendado por", re.I)
_MAYBE_HALLUCINATION = re.compile(
    r"^\W*(obrigad[oa]( por assistir)?|tchau|inscreva-se.*|até (a )?próxima)\W*$", re.I
)


# ---------------------------------------------------------------------------
# Helpers shared by both backends
# ---------------------------------------------------------------------------

def _pcm_to_float32(pcm_bytes: bytes) -> np.ndarray:
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16_000) -> bytes:
    """Wrap raw s16le PCM bytes in a WAV container (in memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)       # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _is_hallucination_text(text: str, no_speech_prob: float = 0.0,
                            avg_logprob: float = 0.0,
                            compression_ratio: float = 1.0) -> bool:
    """Filter likely hallucinations from any backend."""
    if not text:
        return True
    if _ALWAYS_HALLUCINATION.search(text):
        return True
    if no_speech_prob > 0.6 and avg_logprob < -1.0:
        return True
    if compression_ratio > 2.4:
        return True
    if avg_logprob < -1.5:
        return True
    if no_speech_prob > 0.3 and _MAYBE_HALLUCINATION.match(text):
        return True
    return False


# ---------------------------------------------------------------------------
# Groq backend
# ---------------------------------------------------------------------------

_GROQ_ASR_MODEL = "whisper-large-v3-turbo"


def _transcribe_segment_groq(pcm_bytes: bytes) -> list[dict]:
    """
    Send a WAV buffer to Groq's transcription endpoint and return
    [{"text", "start", "end"}, ...] with per-segment timestamps.

    Uses the synchronous groq SDK (called from a thread executor).
    """
    from groq_client import get_client  # lazy — not needed when using local Whisper

    client = get_client()

    wav_bytes = _pcm_to_wav(pcm_bytes)
    prompt = os.environ.get("WHISPER_PROMPT") or None

    response = client.audio.transcriptions.create(
        file=("audio.wav", wav_bytes, "audio/wav"),
        model=_GROQ_ASR_MODEL,
        language="pt",
        response_format="verbose_json",
        timestamp_granularities=["segment"],
        temperature=0.0,
        prompt=prompt,
    )

    out = []
    segments = getattr(response, "segments", None) or []
    for seg in segments:
        text = (seg.get("text") if isinstance(seg, dict) else getattr(seg, "text", "")).strip()
        start = float(seg.get("start") if isinstance(seg, dict) else getattr(seg, "start", 0))
        end = float(seg.get("end") if isinstance(seg, dict) else getattr(seg, "end", start + 0.5))
        no_sp = float(seg.get("no_speech_prob", 0) if isinstance(seg, dict) else getattr(seg, "no_speech_prob", 0))
        logp = float(seg.get("avg_logprob", 0) if isinstance(seg, dict) else getattr(seg, "avg_logprob", 0))
        comp = float(seg.get("compression_ratio", 1) if isinstance(seg, dict) else getattr(seg, "compression_ratio", 1))
        if _is_hallucination_text(text, no_sp, logp, comp):
            logger.debug("Groq: dropped suspicious segment: %r", text)
            continue
        out.append({"text": text, "start": start, "end": end})

    # Fall back to the full text ONLY if the API returned no per-segment data at
    # all.  If it returned segments and we filtered them all out as hallucinations,
    # the result must stay empty -- otherwise the fallback would re-add them.
    if not segments and not out and (getattr(response, "text", "") or "").strip():
        text = response.text.strip()
        dur = len(pcm_bytes) / (16_000 * 2)
        if not _is_hallucination_text(text):
            out.append({"text": text, "start": 0.0, "end": dur})

    return out


# ---------------------------------------------------------------------------
# Local faster-whisper backend
# ---------------------------------------------------------------------------

def _auto_device() -> str:
    """Return 'cuda' if a CUDA GPU is available, else 'cpu'."""
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _resolve_config() -> tuple[str, str, str]:
    """
    Resolve (device, compute_type, model_size) with auto-detection.
    Priority: explicit env var > auto-detected default.
    """
    raw_device = os.environ.get("WHISPER_DEVICE", "auto").lower()
    device = _auto_device() if raw_device == "auto" else raw_device

    raw_compute = os.environ.get("WHISPER_COMPUTE", "auto").lower()
    compute = ("float16" if device == "cuda" else "int8") if raw_compute == "auto" else raw_compute

    raw_model = os.environ.get("WHISPER_MODEL", "auto").lower()
    # Default CPU model is "tiny": measured 0.25-0.76× real-time on i7-4600U.
    # "small" is 2.5-5× real-time on the same CPU — segments queue faster than
    # they are processed, causing cascading drops.  Set WHISPER_MODEL=small
    # explicitly if you have a faster CPU.
    model_size = ("medium" if device == "cuda" else "tiny") if raw_model == "auto" else raw_model

    return device, compute, model_size


def _ensure_av_stub() -> None:
    """Apply an in-process av stub so faster-whisper 0.10.x loads on Python 3.8.

    faster-whisper 0.10.x does ``import av`` at module level in audio.py, which
    is re-exported by __init__.py.  av has no Python 3.8 wheel and cannot be
    built without libavcodec dev headers.  We never call decode_audio() — we
    always pass numpy float32 arrays directly — so av is unused at runtime.

    This function injects a minimal stub into sys.modules before the first
    ``import faster_whisper`` so the package loads cleanly.  It is idempotent
    and a no-op when av is already installed or when faster-whisper is not
    present.  The stub only needs av.audio.resampler.AudioResampler,
    av.audio.fifo.AudioFifo, av.open, and av.error.InvalidDataError — all used
    only inside decode_audio() which we never call.
    """
    import sys
    if "av" in sys.modules:
        return  # real av (or stub) already present

    try:
        import importlib.util
        if importlib.util.find_spec("faster_whisper") is None:
            return  # faster_whisper not installed — nothing to stub for
    except Exception:
        return

    # Build a minimal stub that satisfies faster_whisper.audio's top-level imports
    # without providing any real functionality (decode_audio will raise if called).
    import types

    class _NotAvailable:
        """Raised when decode_audio() is called without av installed."""
        def __init__(self, *a, **kw):
            raise RuntimeError(
                "av is not installed; decode_audio() cannot be used. "
                "Pass a numpy float32 array to WhisperModel.transcribe() instead."
            )

    # av.error.InvalidDataError (caught in _ignore_invalid_frames, a generator
    # that is only entered from decode_audio — safe to stub as a plain Exception)
    _error_mod = types.ModuleType("av.error")
    _error_mod.InvalidDataError = Exception  # type: ignore[attr-defined]

    # av.audio.resampler and av.audio.fifo — only used inside decode_audio()
    _resampler_mod = types.ModuleType("av.audio.resampler")
    _resampler_mod.AudioResampler = _NotAvailable  # type: ignore[attr-defined]
    _fifo_mod = types.ModuleType("av.audio.fifo")
    _fifo_mod.AudioFifo = _NotAvailable  # type: ignore[attr-defined]
    _audio_mod = types.ModuleType("av.audio")
    _audio_mod.resampler = _resampler_mod  # type: ignore[attr-defined]
    _audio_mod.fifo = _fifo_mod  # type: ignore[attr-defined]

    _av_mod = types.ModuleType("av")
    _av_mod.audio = _audio_mod  # type: ignore[attr-defined]
    _av_mod.error = _error_mod  # type: ignore[attr-defined]
    _av_mod.open = _NotAvailable  # type: ignore[attr-defined]

    sys.modules["av"] = _av_mod
    sys.modules["av.audio"] = _audio_mod
    sys.modules["av.audio.resampler"] = _resampler_mod
    sys.modules["av.audio.fifo"] = _fifo_mod
    sys.modules["av.error"] = _error_mod
    logger.debug("av stub injected (faster-whisper on Python 3.8 without av wheel)")


def _load_model():
    global _whisper_model
    if _whisper_model is None:
        _ensure_av_stub()          # no-op if av is present or fw not installed
        from faster_whisper import WhisperModel

        device, compute, size = _resolve_config()
        if device == "cpu" and size in ("small", "medium", "large-v2", "large-v3", "large-v3-turbo"):
            logger.warning(
                "WHISPER_MODEL=%s on CPU will be slow (measured %s on i7-4600U). "
                "Consider WHISPER_MODEL=tiny, or add GROQ_API_KEY to use cloud ASR.",
                size,
                ">5× real-time" if size != "small" else "2.5-5× real-time",
            )
        logger.info("Loading faster-whisper '%s' on %s (%s)…", size, device, compute)
        _whisper_model = WhisperModel(size, device=device, compute_type=compute)
        logger.info("faster-whisper model ready (device=%s, compute=%s)", device, compute)
    return _whisper_model


def _preload_fallback() -> None:
    """Best-effort background load of the local model used as Groq fallback."""
    try:
        _load_model()
    except ImportError:
        logger.warning("faster-whisper not installed — no local fallback if Groq "
                       "hits its quota (pip install faster-whisper)")
    except Exception as exc:  # noqa: BLE001 — download/offline problems
        logger.warning("Could not preload local fallback model: %s", exc)


def _is_hallucination(seg) -> bool:
    return _is_hallucination_text(
        seg.text.strip(),
        getattr(seg, "no_speech_prob", 0.0),
        getattr(seg, "avg_logprob", 0.0),
        getattr(seg, "compression_ratio", 1.0),
    )


def _transcribe_segment_local(pcm_bytes: bytes) -> list[dict]:
    """Local faster-whisper transcription."""
    model = _load_model()
    audio = _pcm_to_float32(pcm_bytes)
    segments, _info = model.transcribe(
        audio,
        language="pt",
        task="transcribe",
        beam_size=int(os.environ.get("WHISPER_BEAM", "1")),
        temperature=0.0,
        vad_filter=False,
        condition_on_previous_text=False,
        initial_prompt=os.environ.get("WHISPER_PROMPT") or None,
        word_timestamps=False,
    )
    out = []
    for seg in segments:
        if _is_hallucination(seg):
            logger.debug("Local: dropped suspicious segment: %r", seg.text)
            continue
        out.append({"text": seg.text.strip(), "start": float(seg.start), "end": float(seg.end)})
    return out


# ---------------------------------------------------------------------------
# Unified transcription entry point
# ---------------------------------------------------------------------------

def _transcribe_segment(pcm_bytes: bytes) -> list[dict]:
    """
    Route to the right backend based on whether GROQ_API_KEY is set.

      GROQ_API_KEY present  → Groq cloud  (whisper-large-v3-turbo, ~100-300 ms)
                               If Groq returns 429 / quota-exhausted, fall back
                               to local faster-whisper automatically and re-try
                               Groq after a cooldown period.
      GROQ_API_KEY absent   → local faster-whisper

    Returns [{"text", "start", "end"}, ...] relative to the segment start.

    ── CLOUD ASR SWAP-IN POINT ───────────────────────────────────────────────
    To use a different cloud service, replace _transcribe_segment_groq() or
    add another branch here.  Keep the return type identical.
    ─────────────────────────────────────────────────────────────────────────
    """
    if os.environ.get("GROQ_API_KEY"):
        return _transcribe_segment_groq(pcm_bytes)
    return _transcribe_segment_local(pcm_bytes)


# ---------------------------------------------------------------------------
# Groq quota / rate-limit fallback state
# ---------------------------------------------------------------------------

# When Groq returns a quota-exhausted 429, we switch to local for a cooldown
# period before retrying Groq.  This avoids hammering the API and means PT
# subtitles continue uninterrupted via faster-whisper.
_groq_blocked_until: float = 0.0          # wall time; 0 = not blocked
_GROQ_COOLDOWN_S: float = 120.0           # retry Groq after this many seconds

# Counters for fallback failure reporting (reset each time Groq is re-blocked).
_fallback_skipped: int = 0          # segments dropped while local ASR unavailable
_fallback_error_logged: bool = False  # log the root cause only once per cooldown


def _groq_rate_limited(exc: BaseException) -> bool:
    """True for any Groq 429 (per-minute, per-HOUR audio-seconds, per-day...).

    Free-tier Whisper is capped at 20 req/min, 2 000 req/day, 7 200 audio-sec/HOUR
    and 28 800 audio-sec/day (each request counts as >= 10 s).  A 24/7 stream hits
    the hourly audio cap first, whose message does not say "requests per day" --
    so match on the status code, not on the wording.
    """
    if getattr(exc, "status_code", None) == 429:
        return True
    if any(c.__name__ == "RateLimitError" for c in type(exc).__mro__):
        return True
    return "rate limit" in str(exc).lower()


def _groq_cooldown_s(exc: BaseException) -> float:
    """How long to stay on local ASR: Groq's own Retry-After when present."""
    try:
        headers = exc.response.headers  # type: ignore[attr-defined]
        retry_after = float(headers.get("retry-after"))
    except Exception:  # noqa: BLE001 — no response/header/garbage: use the default
        return _GROQ_COOLDOWN_S
    return max(15.0, min(retry_after + 5.0, 3600.0))


def _transcribe_with_fallback(pcm_bytes: bytes) -> list[dict]:
    """
    Try Groq first.  If it returns a rate-limit 429, switch to local
    faster-whisper for the cooldown (Retry-After, default _GROQ_COOLDOWN_S), then retry Groq.
    Falls through to Groq error re-raise for non-quota failures (timeout, 5xx…)
    so the Transcriber's outer except clause can log them normally.
    """
    global _groq_blocked_until, _fallback_skipped, _fallback_error_logged

    now = time.time()

    # Still in cooldown — go straight to local
    if now < _groq_blocked_until:
        try:
            result = _transcribe_segment_local(pcm_bytes)
            # Local succeeded: if we had been skipping, report the count now.
            if _fallback_skipped:
                logger.error(
                    "Local ASR back online after %d skipped segment(s) during Groq cooldown.",
                    _fallback_skipped,
                )
                _fallback_skipped = 0
                _fallback_error_logged = False
            return result
        except ImportError as exc:
            # faster-whisper is not installed — nothing to fall back to.
            _fallback_skipped += 1
            if not _fallback_error_logged:
                logger.error(
                    "Local ASR fallback is unavailable (faster-whisper not installed): %s. "
                    "Subtitles will be SILENT for the rest of the Groq cooldown "
                    "(%.0f s remaining). "
                    "Fix: pip install faster-whisper",
                    exc,
                    max(0.0, _groq_blocked_until - now),
                    exc_info=True,
                )
                _fallback_error_logged = True
            else:
                logger.warning(
                    "Segment skipped — local ASR unavailable (%d skipped so far).",
                    _fallback_skipped,
                )
            return []
        except Exception as exc:
            _fallback_skipped += 1
            if not _fallback_error_logged:
                logger.error(
                    "Local ASR fallback failed: %s. "
                    "Subtitles will be SILENT for the rest of the Groq cooldown "
                    "(%.0f s remaining).",
                    exc,
                    max(0.0, _groq_blocked_until - now),
                    exc_info=True,
                )
                _fallback_error_logged = True
            else:
                logger.warning(
                    "Segment skipped — local ASR still failing (%d skipped so far).",
                    _fallback_skipped,
                )
            return []

    # Try Groq
    try:
        result = _transcribe_segment_groq(pcm_bytes)
        # Groq recovered — clear any stale fallback state from a previous cooldown.
        _fallback_skipped = 0
        _fallback_error_logged = False
        return result
    except Exception as exc:
        if _groq_rate_limited(exc):
            cooldown = _groq_cooldown_s(exc)
            if now >= _groq_blocked_until:   # don't extend an existing cooldown
                _groq_blocked_until = time.time() + cooldown
                _fallback_skipped = 0
                _fallback_error_logged = False
                logger.warning(
                    "Groq ASR rate-limited — switching to local faster-whisper "
                    "for %.0fs. Groq will be retried automatically after that.",
                    cooldown,
                )
            try:
                return _transcribe_segment_local(pcm_bytes)
            except ImportError as local_exc:
                _fallback_skipped += 1
                logger.error(
                    "Groq rate-limited AND local ASR unavailable "
                    "(faster-whisper not installed): %s. "
                    "Subtitles will be SILENT until the cooldown ends (%.0f s). "
                    "Fix: pip install faster-whisper",
                    local_exc,
                    max(0.0, _groq_blocked_until - now),
                    exc_info=True,
                )
                _fallback_error_logged = True
                return []
            except Exception as local_exc:
                _fallback_skipped += 1
                logger.error(
                    "Groq rate-limited AND local ASR fallback failed: %s. "
                    "Subtitles will be SILENT until the cooldown ends (%.0f s).",
                    local_exc,
                    max(0.0, _groq_blocked_until - now),
                    exc_info=True,
                )
                _fallback_error_logged = True
                return []
        # Non-rate-limit error — let the Transcriber log it
        raise


# ---------------------------------------------------------------------------
# Transcriber stage
# ---------------------------------------------------------------------------

class Transcriber:
    """Consumes VAD segments, produces timed Portuguese text segments."""

    def __init__(
        self,
        in_queue: asyncio.Queue,
        out_queue: asyncio.Queue,
        has_viewers: "Callable[[], bool] | None" = None,
    ) -> None:
        self.in_queue = in_queue
        self.out_queue = out_queue
        # Optional callable that returns True when at least one WebSocket client
        # is connected.  When it returns False the segment is discarded without
        # calling the ASR or translation backends, saving Groq quota.
        self._has_viewers = has_viewers or (lambda: True)
        self._task: asyncio.Task | None = None
        self._idle_logged = False   # log once per idle run, not per segment

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._run(), name="transcribe")
        return self._task

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()

        if not os.environ.get("GROQ_API_KEY"):
            # Local-only: the model must be ready before the first segment.
            await loop.run_in_executor(None, _load_model)
        else:
            # Groq primary: warm the fallback model in the background so the first
            # 429 doesn't stall live captions on a model download/load.
            loop.run_in_executor(None, _preload_fallback)

        logger.info(
            "Transcriber started (backend=%s)",
            "groq:" + _GROQ_ASR_MODEL if os.environ.get("GROQ_API_KEY") else "local",
        )

        try:
            while True:
                pcm_bytes, audio_start, t_emit = await self.in_queue.get()

                if not self._has_viewers():
                    if not self._idle_logged:
                        logger.info("No viewers — ASR paused (segment discarded)")
                        self._idle_logged = True
                    continue
                if self._idle_logged:
                    logger.info("Viewer connected — ASR resumed")
                    self._idle_logged = False

                try:
                    subs = await loop.run_in_executor(
                        None,
                        _transcribe_with_fallback if os.environ.get("GROQ_API_KEY") else _transcribe_segment,
                        pcm_bytes,
                    )
                except Exception as exc:  # noqa: BLE001
                    # rate limit / timeout / network: one line, no traceback flood
                    logger.error("Transcription error: %s", exc,
                                 exc_info=not _is_transient(exc))
                    continue

                t_asr = time.time()
                dur = len(pcm_bytes) / 32000
                logger.debug("ASR: %.1f s audio in %.2f s wall", dur, t_asr - t_emit)

                for sub in subs:
                    result = {
                        "pt": sub["text"],
                        "audio_start": audio_start + sub["start"],
                        "audio_end": audio_start + max(sub["end"], sub["start"] + 0.5),
                        "t_emit": t_emit,
                        "t_asr": t_asr,
                    }
                    try:
                        self.out_queue.put_nowait(result)
                    except asyncio.QueueFull:
                        try:
                            self.out_queue.get_nowait()
                            logger.warning("Translator is behind — dropped the oldest caption")
                        except asyncio.QueueEmpty:
                            pass
                        self.out_queue.put_nowait(result)

        except asyncio.CancelledError:
            logger.info("Transcriber stopped")
            raise
