# How to solve the latency problem

## The hard reality: phrase-based ASR has a ceiling

Local Whisper (phrase-based) can't emit captions until a full sentence is spoken and silence is
detected. A 5 s sentence + 0.5 s silence + VAD/ASR processing adds up to ~6–8 s of fixed,
unavoidable latency on any CPU hardware.

**Quick answer:** Use Groq instead of local Whisper. ASR + translation drop to ~0.5–1 s combined,
though total end-to-end latency is still ~10–15 s because of HLS segment buffering (see below).

---

## Option 1: Groq (recommended for old hardware) ✓ IMPLEMENTED

**Setup (1 minute)**

1. Get a free API key: https://console.groq.com/keys
2. Add to `.env`:
   ```
   GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx
   ```
3. Run `python main.py`.

**What you get**

| Metric | Local `whisper-small` CPU | Groq `whisper-large-v3-turbo` |
|---|---|---|
| Segment transcription | ~4–8 s | ~0.1–0.3 s |
| Translation PT→ES | ~0.5–2 s | ~0.3–0.7 s (`openai/gpt-oss-20b` with `reasoning_effort=low`) |
| **Total end-to-end latency** | **~10–15 s** | **~10–15 s measured** (dominated by HLS buffering, not ASR) |
| **GPU required?** | No | No |
| **Cost** | Free (your CPU) | Free tier (see console.groq.com/docs/rate-limits) or pay-as-you-go |

**Why is total latency still ~10–15 s with Groq?**

The bottleneck is not ASR — it's the HLS stream itself. The channel uses ~8–10 s segments
(`#EXT-X-TARGETDURATION: 19`). ffmpeg can't start decoding a segment until it's fully downloaded,
so there is an unavoidable ~8–10 s delay at the source before a word even reaches the VAD.
ASR + translation with Groq add only ~0.5–1 s on top. To see captions closer to real time,
you'd need a stream with shorter segments, or a different capture strategy.

**How it works**

- `groq_client.py` holds one shared `Groq()` instance (thread-safe, reused across both ASR and
  translation to avoid per-request TLS handshakes).
- `transcribe.py` sends a WAV buffer to `whisper-large-v3-turbo` at Groq's inference endpoint
  and parses per-segment timestamps from the `verbose_json` response.
- `translate.py` calls the same client for `openai/gpt-oss-20b` with `reasoning_effort=low`
  (suppresses chain-of-thought, cuts latency by ~2 s on translation-only tasks).
- Both honour `GROQ_TIMEOUT_S=8` and `GROQ_MAX_RETRIES=1` to avoid a hung call stalling
  the whole pipeline.
- Startup log shows: `ASR backend: Groq (whisper-large-v3-turbo) ✓`.

**Tuning Groq**

```env
GROQ_TRANSLATE_MODEL=openai/gpt-oss-20b   # default; try other models from groq.models.list()
GROQ_REASONING_EFFORT=low                  # low | medium | high | none (gpt-oss only)
GROQ_TIMEOUT_S=8                           # abort a hung request after this many seconds
GROQ_MAX_RETRIES=1                         # SDK auto-retries on 429 / 5xx
```

**Note on VAD**

When `torch` is not installed, `vad.py` automatically falls back to `EnergyDetector`
(RMS-based segmentation — no model needed). This is sufficient for the Groq path.
For better segmentation accuracy, install the full Silero VAD stack:

```bash
pip install silero-vad   # ~800 MB torch download
```

---

## Option 2: Local `whisper-small` CPU (free, works without internet)

Just run `python main.py` without `GROQ_API_KEY`. The code auto-falls back to `faster-whisper`.

**Latency breakdown**

```
HLS segment buffering    ~8–10 s  (channel uses ~10 s segments; ffmpeg waits for full segment)
Max phrase length          3–5 s  (VAD_MAX_SEGMENT_MS=5000)
VAD silence margin         0.5 s  (VAD_SILENCE_MS=500)
Whisper inference          3–8 s  (on a ~10-year-old CPU, even with int8)
Translation (NLLB CPU)   0.5–2 s  (if enabled)
══════════════════════════════════
Total                   ~15–25 s
```

**When to use local**

- No internet access
- Want zero cloud dependencies
- Have a GPU (cuts Whisper inference to ~1–3 s)

---

## Option 3: GPU (local or cloud instance)

**Local GPU (NVIDIA only)**

```env
WHISPER_DEVICE=cuda
WHISPER_COMPUTE=float16
```

For RTX 3060+: expect ~1–3 s per segment. Total latency ~10–13 s (still bounded by HLS buffering).

**Cloud GPU instance**

Rent a cheap GPU instance (~$0.30/hour) and run `faster-whisper` on it:
- Whisper `small` on V100: ~1–2 s per segment
- Total latency: ~10–12 s
- Worth it if you're already running on a server and don't want a Groq API dependency.

---

## Comparison table

| Option | End-to-end latency | Cost | Setup | Internet | GPU | ASR quality |
|---|---|---|---|---|---|---|
| **Groq** | ~10–15 s | Free tier | 2 min | Yes | No | Highest (v3-turbo) |
| Local `small` CPU | ~15–25 s | Free | 0 min | No | No | Medium |
| Local `medium` CPU | ~20–30 s | Free | 0 min | No | No | Good |
| Local `small` GPU | ~10–13 s | GPU cost | 5 min | No | Yes | Medium |
| Cloud GPU instance | ~10–12 s | ~$0.30/h | 10 min | Yes | Yes | Medium |

---

## Reducing player delay

The `Atraso do vídeo` slider (default 10 s) in the player needs to cover the full pipeline time.
Each caption logs its timing:

```
caption ready: speech started 7.4s ago … (seg 5.1s, ASR 0.3s, translation 0.7s)
```

Set the delay to slightly above the largest `age_start` you see in the logs. If more than 20 %
of captions arrive late (the status bar will warn you), increase the delay.

---

## The hard ceiling: streaming ASR

The real way to get captions below ~3 s total latency is **streaming ASR** — a model that emits
partial words as you're speaking and corrects them in real time. This requires a different
architecture (WebSocket to the ASR provider, partial caption updates) and is not implemented here.

Options (not implemented):
- Deepgram Nova streaming API (~500 ms latency)
- AssemblyAI real-time API (~1 s latency)

**Recommendation for old hardware without a GPU:** Use Groq. Get the free key at
https://console.groq.com/keys, add it to `.env`, and run `python main.py`.
