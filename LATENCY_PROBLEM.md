# How to solve the latency problem

## The hard reality: phrase-based ASR has a ceiling

Local Whisper (phrase-based) can't emit captions until a full sentence is spoken and silence is detected. A 5 s sentence + 0.5 s silence + VAD/ASR processing adds up to ~6–8 s of fixed, unavoidable latency on any CPU hardware.

**Quick answer:** Use Groq instead of local Whisper. It reduces this to ~0.5–1 s.

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
| Translation (if enabled) | ~0.5–2 s | ~0.2–0.4 s |
| **Total latency** | **~10–15 s** | **~5–7 s** |
| **GPU required?** | No | No |
| **Cost** | Free (your CPU hours) | Free tier (up to 10k requests/day) or pay-as-you-go |

**How it works**

- `transcribe.py` detects `GROQ_API_KEY` and routes to Groq's `whisper-large-v3-turbo` API.
- `translate.py` (if `ENABLE_TRANSLATION=true`) also uses Groq LLM for PT→ES, same key.
- Model used: `openai/gpt-oss-20b` by default (override with `GROQ_TRANSLATE_MODEL` in `.env`).
- Both run on Groq's inference hardware (~216× real-time speed).
- No local models to download or run.
- Startup log shows: `ASR backend: Groq (whisper-large-v3-turbo) ✓`.

**When to use Groq**

- Old laptop with no GPU ← this is you
- Internet access available
- Captions at ~5–7 s latency is good enough
- Don't want to manage large local models (~800 MB torch + model downloads)

**Note on VAD with Groq**

When `torch` is not installed, `vad.py` automatically falls back to `EnergyDetector`
(RMS-based segmentation, no model needed). This is enough to run the Groq path.
For better segmentation accuracy, install the full torch + silero-vad stack:

```bash
pip install silero-vad   # ~800 MB torch download
```

---

## Option 2: Local `whisper-small` CPU (free, works without internet)

**Setup**

Just run `python main.py` without `GROQ_API_KEY`. The code auto-falls back to local `faster-whisper`.

**Latency breakdown**

```
Max phrase length        3–8 s   (depends on how long sentences are)
VAD silence margin       0.5 s   (VAD_SILENCE_MS=500)
Whisper inference        3–8 s   (on a ~10-year-old CPU, even with int8)
Translation (NLLB CPU)   0.5–2 s (if enabled)
═════════════════════════════════
Total                   ~10–15 s
```

The ceiling is real: you can't get below ~8–10 s on CPU without replacing Whisper.

**When to use local**

- No internet access
- Want zero cloud dependencies
- Can tolerate 10–15 s captions
- Have a spare machine with a GPU

---

## Option 3: GPU (local or cloud instance)

**Local GPU (NVIDIA only)**

```bash
# Install CUDA wheel in .env or at install time:
WHISPER_DEVICE=cuda
WHISPER_COMPUTE=float16

# For RTX 3060+, expect ~1–3 s per segment
```

**AWS/GCP/Azure GPU instance**

Rent a cheap GPU instance (~$0.30/hour) and run `faster-whisper` on it:
- Whisper `small` on V100: ~1–2 s per segment
- Total latency: ~6–8 s
- Much cheaper than calling a cloud ASR API if you run 24/7

**Hybrid: GPU transcription + local translation**

```bash
WHISPER_DEVICE=cuda        # GPU transcription
TRANSLATION_BACKEND=nllb   # CPU translation (overlaps with next segment)
```

This is probably not worth the complexity unless you already own the GPU.

---

## Comparison table

| Option | Latency | Cost | Setup | Internet | GPU | Accuracy |
|---|---|---|---|---|---|---|
| **Groq** | ~5–7 s | Free tier | 2 min | Yes | No | Highest (v3-turbo) |
| Local `small` CPU | 10–15 s | Free | 0 min | No | No | Medium |
| Local `medium` CPU | 20–30 s | Free | 0 min | No | No | Good |
| Local `small` GPU | 6–8 s | GPU cost | 5 min | No | Yes | Medium |
| Cloud GPU instance | 6–8 s | ~$0.30/h | 10 min | Yes | Yes | Medium |

---

## The hard ceiling: streaming ASR

The real way to get captions below ~3 s latency is **streaming ASR** — a model that emits partial words as you're speaking and corrects them in real time.

**Options** (not yet implemented):
- Deepgram Nova streaming API (~500 ms latency)
- AssemblyAI real-time API (~1 s latency)
- Whisper Streaming (coming soon from OpenAI)

These require a different architecture (WebSocket connection, partial caption updates) and are more expensive. They're worth it only if latency <2 s is critical.

**Recommendation for your old laptop:** Use Groq. Get the free key at https://console.groq.com/keys, add it to `.env`, and run `python main.py`. You'll have captions at 5–7 s latency with no GPU and no large model downloads required.

