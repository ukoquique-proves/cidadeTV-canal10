"""
groq_client.py — one shared Groq client for ASR and translation.

Building a ``Groq()`` per request throws away the HTTP connection pool, so every
call paid a fresh TLS handshake.  The client is thread-safe, so a single cached
instance serves the executor threads of both pipeline stages.

The SDK defaults (60 s timeout, 2 retries) are far too patient for a live stream:
the stages run one segment at a time, so a hung call stalls everything behind it
and the queues start dropping captions.  Defaults here are tighter and tunable:

  GROQ_TIMEOUT_S    per-request timeout in seconds   (default 8)
  GROQ_MAX_RETRIES  SDK retries on 429/5xx/network   (default 1)
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_cache: dict = {}

_TRANSIENT = {"RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def get_client():
    """Return the shared client (rebuilt only if the key or settings change)."""
    from groq import Groq  # lazy: not needed with local models

    api_key = os.environ["GROQ_API_KEY"]
    timeout = _env_float("GROQ_TIMEOUT_S", 8.0)
    retries = _env_int("GROQ_MAX_RETRIES", 1)
    key = (Groq, api_key, timeout, retries)
    with _lock:
        client = _cache.get(key)
        if client is None:
            _cache.clear()
            client = Groq(api_key=api_key, timeout=timeout, max_retries=retries)
            _cache[key] = client
        return client


def is_transient(exc: BaseException) -> bool:
    """True for rate-limit / timeout / network / 5xx errors (no traceback needed)."""
    return any(c.__name__ in _TRANSIENT for c in type(exc).__mro__)
