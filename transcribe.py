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
WHISPER_MODEL    tiny | small | medium | large-v3  (auto: small/medium by device)
WHISPER_DEVICE   cpu | cuda | auto  (default: auto-detect)
WHISPER_COMPUTE  int8 | float16 | float32 | auto
WHISPER_BEAM     1 = greedy/fastest; 2-5 more accurate
WHISPER_PROMPT   initial prompt (channel names improve accuracy a lot)
"""

import asyncio
import io
import logging
import os
import re
import time
import wave

import numpy as np

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
    from groq import Groq  # lazy import — not needed when using local Whisper

    api_key = os.environ["GROQ_API_KEY"]
    client = Groq(api_key=api_key)

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
    model_size = ("medium" if device == "cuda" else "small") if raw_model == "auto" else raw_model

    return device, compute, model_size


def _load_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        device, compute, size = _resolve_config()
        if device == "cpu" and size in ("medium", "large-v2", "large-v3", "large-v3-turbo"):
            logger.warning(
                "WHISPER_MODEL=%s on CPU will be very slow (~10-30 s per segment). "
                "Consider WHISPER_MODEL=small, or add GROQ_API_KEY to use cloud ASR.",
                size,
            )
        logger.info("Loading faster-whisper '%s' on %s (%s)…", size, device, compute)
        _whisper_model = WhisperModel(size, device=device, compute_type=compute)
        logger.info("faster-whisper model ready (device=%s, compute=%s)", device, compute)
    return _whisper_model


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
# Transcriber stage
# ---------------------------------------------------------------------------

class Transcriber:
    """Consumes VAD segments, produces timed Portuguese text segments."""

    def __init__(self, in_queue: asyncio.Queue, out_queue: asyncio.Queue) -> None:
        self.in_queue = in_queue
        self.out_queue = out_queue
        self._task: asyncio.Task | None = None

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

        # Pre-load local model before first segment (no-op when using Groq)
        if not os.environ.get("GROQ_API_KEY"):
            await loop.run_in_executor(None, _load_model)

        logger.info(
            "Transcriber started (backend=%s)",
            "groq:" + _GROQ_ASR_MODEL if os.environ.get("GROQ_API_KEY") else "local",
        )

        try:
            while True:
                pcm_bytes, audio_start, t_emit = await self.in_queue.get()
                try:
                    subs = await loop.run_in_executor(None, _transcribe_segment, pcm_bytes)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Transcription error: %s", exc, exc_info=True)
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
