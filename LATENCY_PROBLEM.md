# Latency — what this design can and cannot do

## The structural ceiling

This pipeline is **phrase-complete-first**: a caption can only be produced after
three things finish in sequence.

```
┌─────────────────────────────────────────────────────────┐
│  1. HLS buffering   ~8–10 s  fixed, set by the channel  │
│  2. Phrase ends     + 0–5 s  phrase length + VAD silence │
│  3. ASR + translate + 0.3–8 s  depending on backend     │
└─────────────────────────────────────────────────────────┘
         │
         ▼
   caption arrives at the server
         │
         ▼
   player shows it  ←── Atraso do vídeo must cover all three rows above
```

No backend swap, tuning tweak, or code patch changes that structure.
**The `Atraso do vídeo` slider is the real control.** It has to be set to at
least the sum of all three rows or captions arrive late and the status bar
turns red.

---

## What each row actually costs

### Row 1 — HLS buffering (~8–10 s, unavoidable)

The TV Cidade 10 stream uses `#EXT-X-TARGETDURATION: 19` with actual segments
of ~8–10 s. ffmpeg cannot begin decoding a segment until it is fully downloaded.
This delay exists before any processing starts. It cannot be shortened without
a different stream source or a different capture strategy (e.g. reading the MPEG-TS
feed directly at a lower level).

### Row 2 — Phrase must end (0–5 s, partially controllable)

VAD waits for a silence gap (`VAD_SILENCE_MS=500`) before closing a segment.
During continuous speech the forced-cut ceiling applies (`VAD_MAX_SEGMENT_MS=5000`).
Lowering `VAD_MAX_SEGMENT_MS` trades caption accuracy for less worst-case wait.
This row cannot be eliminated — ASR needs a complete utterance to produce
meaningful text.

### Row 3 — ASR + translation (0.3–8 s, controllable by backend)

| Backend | ASR | Translation | Row 3 total |
|---|---|---|---|
| Groq | ~0.1–0.3 s | ~0.3–0.7 s | **~0.5–1 s** |
| Local CPU (small) | ~3–8 s | ~0.5–2 s | **~4–10 s** |
| Local GPU (RTX 3060+) | ~1–2 s | ~0.3–1 s | **~1–3 s** |

Groq is the best option here, but it only removes Row 3. Rows 1 and 2 remain.

---

## What "Patch 6" and previous changes actually fixed

Each patch reduced Row 3 or improved reliability. None changed Rows 1 or 2.

| Change | What it fixed | Row affected |
|---|---|---|
| Groq ASR + translation | 4–10 s → 0.5–1 s inference time | Row 3 |
| `groq_client.py` shared pool | Removes per-request TLS handshake overhead | Row 3 |
| `reasoning_effort=low` | Removes chain-of-thought latency on `gpt-oss` | Row 3 |
| `VAD_MAX_SEGMENT_MS` 8000→5000 | Worst-case phrase wait 8 s → 5 s | Row 2 |
| Exponential backoff in `capture.py` | Stream-down recovery; no latency impact | None |
| WebSocket slow-client timeout | Prevents one browser stalling others | None |

The 3–4 s you observe even with Groq is Row 2: the phrase is still being spoken
or the VAD silence gap is still running. That is not a bug.

---

## Setting `Atraso do vídeo` correctly

Each caption logs the full timing when it arrives:

```
caption ready: speech started 7.4s ago … (seg 5.1s, ASR 0.3s, translation 0.7s)
```

`age_start` (the "7.4s ago" value) is the total of all three rows for that
caption. Set the slider to slightly above your observed maximum `age_start`.
The player status bar warns when more than 20 % of captions arrive late — that
is the signal to increase the delay, not to change the backend.

Typical values measured with Groq on this channel:

| Condition | Recommended delay |
|---|---|
| Normal speech, Groq | 10–12 s |
| Music / jingle segments | 12–15 s (VAD cuts more often at max) |
| Local CPU (no Groq) | 18–25 s |

---

## How to get below ~8 s total latency (not implemented)

The HLS buffer in Row 1 is the hard floor. To get below it would require one of:

- **Direct MPEG-TS capture** — connect to the transport stream before HLS
  packetizes it into ~10 s segments. Reduces Row 1 to near zero but requires
  a different source URL and ffmpeg pipeline.
- **Streaming ASR** — emit partial words in real time instead of waiting for a
  complete phrase. Requires a different architecture (WebSocket to an ASR
  provider, partial caption updates in the browser). Providers: Deepgram Nova
  (~500 ms), AssemblyAI real-time (~1 s). Row 2 disappears; Row 1 still applies
  unless combined with direct TS capture.

Neither is implemented. The current design is intentionally simple: offline
phrase-based ASR over HLS, with the player delay as the sync mechanism.

---

## Quick reference

```
Goal                          Action
─────────────────────────────────────────────────────────────
Captions always on time       Increase Atraso do vídeo
Captions cut mid-sentence     Install silero-vad (pip install silero-vad)
                              or lower VAD_MAX_SEGMENT_MS
Row 3 too slow                Add GROQ_API_KEY to .env
Below 8 s total latency       Not possible with this design (HLS floor)
```
