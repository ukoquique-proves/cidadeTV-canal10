"""
captions.py — turns pipeline items into messages for the browser.

A pipeline item (after ASR + translation) looks like::

    {"pt", "es", "audio_start", "audio_end", "t_emit", "t_asr", "t_mt"}

where audio_start/audio_end are seconds on the capture timeline.

Browser messages carry *ages* instead of absolute times::

    {"pt", "es", "age_start", "age_end"}

``age_start`` = how many seconds ago (wall clock, measured at the moment of
sending) this speech started at the live edge.  The browser knows how far its
player is behind the live edge, so it can show the caption after
``player_latency - age_start`` seconds.  Using *ages* means server and browser
clocks never have to agree.
"""

import math
import re
import time

MAX_CHARS_PER_CAPTION = 84   # ~2 lines of 42 characters


def split_text(text: str, n: int) -> list[str]:
    """Split `text` into `n` pieces of roughly equal length, on word boundaries,
    preferring to break right after punctuation."""
    words = text.split()
    if n <= 1 or len(words) <= 1:
        return [text.strip()]
    n = min(n, len(words))
    total = sum(len(w) + 1 for w in words)
    target = total / n

    pieces, cur, cur_len = [], [], 0
    for i, w in enumerate(words):
        cur.append(w)
        cur_len += len(w) + 1
        remaining_words = len(words) - i - 1
        remaining_pieces = n - len(pieces) - 1
        at_punct = bool(re.search(r"[,.;:!?…]$", w))
        # Break when the pieces are big enough, or when we MUST (one word left per
        # remaining piece) so the result always has exactly n pieces.
        if remaining_pieces > 0 and remaining_words >= remaining_pieces and (
            cur_len >= target * 1.15 or (cur_len >= target * 0.75 and at_punct)
            or remaining_words == remaining_pieces
        ):
            pieces.append(" ".join(cur))
            cur, cur_len = [], 0
    if cur:
        pieces.append(" ".join(cur))
    return pieces


def expand(item: dict, max_chars: int = MAX_CHARS_PER_CAPTION) -> list[dict]:
    """Split one (possibly long) item into subtitle-sized items with their own
    sub-ranges of the audio timeline (time is distributed by character count)."""
    pt = (item.get("pt") or "").strip()
    es = (item.get("es") or "").strip()
    n = max(1, math.ceil(max(len(pt), len(es)) / max_chars))
    # Never ask for more pieces than either text has words: split_text() then
    # returns exactly n pieces for both, so PT and ES stay aligned and no text
    # is lost (the old code truncated ES when it yielded more pieces than PT).
    n = min(n, len(pt.split()) or 1, (len(es.split()) or 1) if es else n)
    if n == 1:
        return [{**item, "pt": pt, "es": es}]

    pt_parts = split_text(pt, n)
    es_parts = split_text(es, n) if es else [""] * len(pt_parts)

    start, end = item["audio_start"], item["audio_end"]
    weights = [max(1, len(p)) for p in pt_parts]
    total = sum(weights)
    out, t = [], start
    for p, e, w in zip(pt_parts, es_parts, weights):
        d = (end - start) * w / total
        out.append({**item, "pt": p, "es": e, "audio_start": t, "audio_end": t + d})
        t += d
    return out


def to_message(item: dict, live_time, now: float | None = None) -> dict:
    """Build the WebSocket message.  `live_time(audio_s)` maps the capture
    timeline to wall-clock live time (see capture.AudioClock)."""
    now = time.time() if now is None else now
    live_start = live_time(item["audio_start"])
    live_end = live_time(item["audio_end"])
    if live_start is None or live_end is None:      # no clock yet: show at once
        age_start = age_end = 0.0
    else:
        age_start = max(0.0, now - live_start)
        age_end = max(0.0, now - live_end)
    return {
        "pt": item["pt"],
        "es": item.get("es", ""),
        "age_start": round(age_start, 2),
        "age_end": round(age_end, 2),
    }
