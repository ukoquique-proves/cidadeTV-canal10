"""
translate.py — Portuguese → Spanish translation.

Supports four backends, chosen by the TRANSLATION_BACKEND env var:

  groq    (default when GROQ_API_KEY is set)
            — Groq LLM API (same key as ASR, no extra setup).
              Model: GROQ_TRANSLATE_MODEL (default: openai/gpt-oss-20b).
              ~100-400 ms per segment, highest quality, needs internet.
  nllb    (default when GROQ_API_KEY is unset)
            — facebook/nllb-200-distilled-600M via HuggingFace transformers.
              Runs locally, ~1.3 GB RAM, no API key.
  marian  — MarianMT, PIVOTING THROUGH ENGLISH (PT→EN→ES) because no
              direct PT→ES Marian model exists.  Two ~300 MB models,
              noticeably lower quality and ~2x the latency of NLLB.
              UNTESTED in this repo — prefer groq or nllb.
              Override model names with MARIAN_MODELS="pt_to_en,en_to_es".
  llm     — Any OpenAI-compatible API (set LLM_API_URL and LLM_API_KEY).
              Use this if you have a different LLM provider.

Auto-selection
--------------
If TRANSLATION_BACKEND is not set:
  GROQ_API_KEY present  → groq
  GROQ_API_KEY absent   → nllb

If ENABLE_TRANSLATION=false the module passes Portuguese text through unchanged.

Public API
----------
Translator(in_queue, out_queue)
    Reads {"pt", "audio_start", "audio_end", "t_emit", "t_asr"} dicts from
    in_queue.  Puts {"pt", "es", "audio_start", "audio_end", "t_emit",
    "t_asr", "t_mt"} dicts onto out_queue.

translate_text(text: str) -> str
    Synchronous helper — useful for testing from the CLI.
"""

import asyncio
import logging
import os
import time

from groq_client import is_transient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend auto-selection
# ---------------------------------------------------------------------------

def _default_backend() -> str:
    """groq if GROQ_API_KEY is set, otherwise nllb."""
    return "groq" if os.environ.get("GROQ_API_KEY") else "nllb"


def _active_backend() -> str:
    return (os.environ.get("TRANSLATION_BACKEND") or _default_backend()).strip().lower()


# ---------------------------------------------------------------------------
# Lazy model holders (local backends)
# ---------------------------------------------------------------------------
_nllb_pipeline = None
_marian_pipeline = None


# ---------------------------------------------------------------------------
# Groq LLM backend
# ---------------------------------------------------------------------------

_GROQ_TRANSLATE_MODEL_DEFAULT = "openai/gpt-oss-20b"


def _reasoning_kwargs(model: str) -> dict:
    """Control reasoning on models that support it (GPT-OSS 20B/120B only).

    Translation needs no chain of thought, and thinking time is pure latency on a
    live stream. Only GPT-OSS 20B and 120B support reasoning_effort; other models
    will reject the parameter. Setting reasoning_effort=low reduces reasoning tokens
    spent, but STILL increases total tokens. The API response includes reasoning in
    a separate .reasoning field, and max_completion_tokens does NOT count reasoning
    tokens — only output tokens count toward the limit.

    Groq default for gpt-oss is medium reasoning. We override to low if explicitly
    set, but note that even "low" adds latency and token overhead.

    Set GROQ_REASONING_EFFORT=off in .env to disable reasoning entirely (fastest,
    but uses include_reasoning=False).
    """
    if not model.startswith("openai/gpt-oss"):
        return {}  # parameter would be rejected; ignore it
    
    effort = (os.environ.get("GROQ_REASONING_EFFORT") or "").strip().lower()
    if effort == "off":
        return {"include_reasoning": False}  # disable reasoning output
    if effort in ("low", "medium", "high"):
        return {"reasoning_effort": effort}
    # Default: let Groq use its default (medium for gpt-oss)
    return {}


def _translate_groq_sync(text: str) -> str:
    """
    Translate PT→ES via Groq LLM (synchronous, runs in a thread executor).
    Uses the same GROQ_API_KEY as the ASR backend — no extra credentials.

    Note on token limits: max_tokens counts only OUTPUT tokens, not REASONING
    tokens. gpt-oss-20b includes reasoning in a separate field. With
    reasoning_effort=medium (default), plan for ~200-300 reasoning tokens + your
    actual translation. Set max_tokens high enough (we use 2048) to ensure the
    translation isn't truncated.
    """
    from groq_client import get_client

    model = os.environ.get("GROQ_TRANSLATE_MODEL") or _GROQ_TRANSLATE_MODEL_DEFAULT
    client = get_client()

    # reasoning_effort / include_reasoning are sent via extra_body: the pinned
    # groq==0.13.1 SDK predates them and rejects them as keyword arguments
    # (TypeError), while extra_body is accepted by every SDK version.
    reasoning = _reasoning_kwargs(model)
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional translator. "
                    "Translate the following Portuguese text to Spanish. "
                    "Output ONLY the translation, no explanations, no quotes."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0.2,
        max_tokens=2048,  # Groq SDK 0.13.1 uses max_tokens (not max_completion_tokens)
        **({"extra_body": reasoning} if reasoning else {}),
    )
    return (completion.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Local backends
# ---------------------------------------------------------------------------

def _device() -> int:
    """
    HF pipeline device: -1 = CPU, 0 = first GPU.
    Priority: TRANSLATION_DEVICE env var > auto-detect CUDA.
    """
    raw = os.environ.get("TRANSLATION_DEVICE", "auto").lower()
    if raw == "auto":
        try:
            import torch
            return 0 if torch.cuda.is_available() else -1
        except ImportError:
            return -1
    return 0 if raw.startswith("cuda") else -1


def _translate_nllb(text: str) -> str:
    """Translate PT→ES with NLLB-200-distilled-600M."""
    global _nllb_pipeline
    if _nllb_pipeline is None:
        from transformers import pipeline as hf_pipeline

        logger.info("Loading NLLB-200 model…")
        _nllb_pipeline = hf_pipeline(
            "translation",
            model="facebook/nllb-200-distilled-600M",
            src_lang="por_Latn",
            tgt_lang="spa_Latn",
            max_length=512,
            device=_device(),
        )
        logger.info("NLLB-200 model loaded")

    result = _nllb_pipeline(text, src_lang="por_Latn", tgt_lang="spa_Latn")
    return result[0]["translation_text"].strip()


def _translate_marian(text: str) -> str:
    """Translate PT→ES by pivoting through English with two MarianMT models."""
    global _marian_pipeline
    if _marian_pipeline is None:
        from transformers import pipeline as hf_pipeline

        names = os.environ.get(
            "MARIAN_MODELS", "Helsinki-NLP/opus-mt-ROMANCE-en,Helsinki-NLP/opus-mt-en-es"
        ).split(",")
        logger.info("Loading MarianMT pivot models: %s", names)
        _marian_pipeline = tuple(
            hf_pipeline("translation", model=n.strip(), device=_device()) for n in names
        )
        logger.info("MarianMT models loaded")

    to_en, to_es = _marian_pipeline
    english = to_en(text)[0]["translation_text"]
    return to_es(english)[0]["translation_text"].strip()


async def _translate_llm(text: str) -> str:
    """Translate PT→ES via a generic OpenAI-compatible LLM API (httpx, async)."""
    import httpx

    api_url = os.environ.get("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")

    if not api_key:
        raise ValueError(
            "LLM_API_KEY is not set. "
            "Add it to .env or switch to TRANSLATION_BACKEND=groq."
        )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a professional translator. "
                    "Translate the following Portuguese text to Spanish. "
                    "Output ONLY the translation, nothing else."
                ),
            },
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": 512,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            api_url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def translate_text(text: str) -> str:
    """
    Synchronous translation — run from a thread executor in async code.
    Routes to the active backend.  The 'llm' backend must use translate_text_async().
    """
    backend = _active_backend()
    if backend == "groq":
        return _translate_groq_sync(text)
    elif backend == "nllb":
        return _translate_nllb(text)
    elif backend == "marian":
        return _translate_marian(text)
    else:
        raise ValueError(
            f"translate_text() called with backend='{backend}' but "
            "'llm' backend must be called via translate_text_async()."
        )


async def translate_text_async(text: str) -> str:
    """Async translation — dispatches to all backends including async ones."""
    backend = _active_backend()
    if backend == "llm":
        return await _translate_llm(text)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, translate_text, text)


# ---------------------------------------------------------------------------
# Translator pipeline stage
# ---------------------------------------------------------------------------

class Translator:
    """
    Reads transcript dicts from `in_queue`, adds an "es" key, and puts the
    enriched dict onto `out_queue`.

    If ENABLE_TRANSLATION is "false", the "es" key is set to "" (empty string)
    and the player will only show the Portuguese line.
    """

    def __init__(self, in_queue: asyncio.Queue, out_queue: asyncio.Queue) -> None:
        self.in_queue = in_queue
        self.out_queue = out_queue
        self._enabled = os.environ.get("ENABLE_TRANSLATION", "true").lower() == "true"
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self._run(), name="translate")
        return self._task

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        backend = _active_backend()

        if self._enabled:
            # Eagerly load local models so the first real segment isn't delayed
            if backend in ("nllb", "marian"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, translate_text, "Olá")

        logger.info(
            "Translator started (enabled=%s, backend=%s)",
            self._enabled, backend,
        )

        try:
            while True:
                item: dict = await self.in_queue.get()
                pt_text: str = item["pt"]

                if self._enabled and pt_text:
                    try:
                        es_text = await translate_text_async(pt_text)
                    except Exception as exc:
                        logger.error("Translation error: %s", exc,
                                     exc_info=not is_transient(exc))
                        es_text = ""
                else:
                    es_text = ""

                enriched = {**item, "es": es_text, "t_mt": time.time()}

                try:
                    self.out_queue.put_nowait(enriched)
                except asyncio.QueueFull:
                    try:
                        self.out_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self.out_queue.put_nowait(enriched)

        except asyncio.CancelledError:
            logger.info("Translator stopped")
            raise
