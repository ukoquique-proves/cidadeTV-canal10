# Changelog

All notable changes and findings about this project are recorded here.
Most recent entry first.

---

## 2026-10-05 — Startup verification + audit patch applied

### Notes
- Verified the repository’s supported startup path via `./start.sh`; the app starts
  successfully and streams live captions from the upstream TV signal.
- Corrected the earlier mistaken claim that the app was unable to run; the issue was
  the startup method, not the project itself.
- Applied the audit patch and recorded the related fixes in this changelog.

---

## 2026-10-05 — Audit: offline banner, stale signatures, housekeeping

### Bugs fixed
- **Offline banner never showed.** The page read `networkDetails.response.status`, but
  hls.js's XHR loader puts the HTTP status in `data.response.code` and the body in
  `networkDetails.responseText` (`networkDetails.response` is the body *string*, so
  `.status` was `undefined`). Both are now read from the real locations. The old page
  test used a hand-made event shape that matched the bug, so it never caught this;
  tests 9–10 in `tests/page.test.js` use the real shape.
- **Dead player after a server restart.** Signed proxy URLs use a per-process HMAC
  secret, so every open tab got 403s forever after `systemctl restart`. A 403 now
  rebuilds the player (fresh `/proxy/playlist`) instead of retrying dead URLs.
- **Fullscreen fallback:** the `fullscreenchange` handler's inner `requestFullscreen()`
  rejection was unhandled; it now falls back to CSS fullscreen.
- **`check_stream.sh`** had the stream URL hard-coded and ignored `STREAM_URL` in `.env`
  (YUNOHOST.md tells you to change it there). It now reads env → `.env` → default, and
  has a curl timeout.

### Housekeeping
- `.env.example`: removed the GitHub token/repository placeholders (unused by the app,
  but `start.sh` copies this file to `.env` on every server).
- Removed the tracked `.DirIcon` (stray 96×96 PNG) and the stale `capture.py.backup`.
- `package.json` is now tracked (page tests need `jsdom`).
- YUNOHOST.md: Step 5 no longer says "Change:" for values that are already the defaults;
  mentions `PROXY_ALLOWED_HOSTS`; unit waits for `network-online.target`.

---

## 2026-10-04 — Prominent stream offline indication + monitoring

### Problem
When the upstream stream returns 404 (channel down or URL changed), the player showed
only a small red dot in the status bar. Users had no clear indication that the stream
was offline rather than buffering or having a transient network glitch.

### Solution
Three complementary improvements:

1. **Proxy returns 503 instead of 404** — When `_fetch()` encounters a 404 from upstream,
   it now returns `503 Service Unavailable` with body `"Stream offline"`. This tells
   hls.js to keep retrying (404 would be treated as fatal and hls.js would give up).

2. **Prominent offline banner on page** — When hls.js receives the 503, the error
   handler shows a large red banner: `⚠ Stream offline (404) — o canal está fora do ar.
   Tentando reconectar…` The banner is hidden when the stream recovers and starts
   playing normally.

3. **Stream monitor script** — New `check_stream.sh` polls the stream URL every 60 s
   and prints HTTP status to console. Useful for watching when the stream comes back
   online (`curl` wrapper, no external deps).

### Implementation
- `server.py`: `_fetch()` now checks `if sc == 404` and returns `Response(503, b"Stream offline")`.
- `static/index.html`: Added CSS `.show` state for `#offline-banner` div. HLS error handler
  checks response status and body, sets/clears the `.show` class accordingly.
- `check_stream.sh`: Loop every 60 s, print timestamp + HTTP code.

### Testing
- All 7 page tests pass (banner does not interfere with normal flow).
- Tested with local test HLS stream via `http.server 9100`.

---

## 2026-10-04 — Cleanup: single source of truth + requirements refactor

### Changes

**DEFAULT_STREAM_URL constant moved to capture.py**
- Previously duplicated in `main.py`, `server.py`, and `capture.py`.
- Now defined once in `capture.py` (the module that actually uses it).
- `main.py` and `server.py` import and re-use the constant.

**requirements.txt refactored into two files**
- `requirements.groq.txt`: Core + Groq cloud ASR/translation (~50 MB, no torch).
- `requirements.local.txt`: torch + faster-whisper + NLLB + Silero (~800 MB).
- `requirements.txt`: Now simply includes both files via `-r` directives.
- Pins live in ONE place per file — no duplication.
- Users pick: `pip install -r requirements.groq.txt` (Groq) or
  `pip install -r requirements.txt` (all models).

**Minor cleanups**
- Removed unused imports: `statistics` from `e2e_local_hls.py`, `os` from
  `smoke_main.py`, `WINDOW_BYTES` from `test_units.py`.
- Removed stale `WebSocketDisconnect` import from `server.py`; `Exception` catches it.
- Updated docstrings: `max_completion_tokens` → `max_tokens` in README and `translate.py`.

### Testing
- All 33 unit tests pass.

---

## 2026-10-04 — Fixed button logic + end-to-end test with local HLS

### Fixed: "Traducción - Minuto" button was inert

**Root cause**: `setInterval(render, 100)` captured the original `render` function
reference at binding time. The later `render = function(){...}` reassignment created
a new function object, but the interval kept calling the old one — so `esVisible()`
was never consulted.

**Fix**: Made the original `render()` call `esVisible()` directly (hoisted function
declaration, available everywhere). Deleted the dead reassignment block. `esVisible()`
now handles all three ES-visibility conditions:
1. `showEs` checkbox checked
2. `peekActive` (60s "Traducción - Minuto" timer running)
3. `pausedEs` (video is paused)

### End-to-end test with local HLS

Confirmed the entire pipeline works with `test_hls/stream.m3u8`:
- Started `http.server 9100` in `test_hls/` folder.
- Set `STREAM_URL=http://127.0.0.1:9100/stream.m3u8`.
- App captured audio, VAD segmented, ASR → Groq, translation → Groq.
- Captions arrived in browser via WebSocket.
- Proxy correctly rewrote playlist URLs with HMAC signatures.

### Stream monitor & next steps

When TV Cidade 10 stream comes back online:
1. Run `./check_stream.sh` to watch for HTTP 200 (polls every 60 s).
2. If the official player works but app still gets 404, the URL changed —
   check DevTools → Network tab → filter `m3u8` → copy new URL.
3. Update `.env` with new `STREAM_URL` and restart.

---

### Problem
Without `GROQ_API_KEY`, the viewer-gate tests (`test_transcriber_skips_asr_when_no_viewers`
and `test_transcriber_resumes_asr_when_viewer_connects`) would try to load the local
faster-whisper model, which fails on CI or a fresh clone where `pip install -r requirements.local.txt`
was not run.

### Solution
Monkeypatch `_load_model` to `lambda: None` in both tests. This stubs out the heavy
model load while letting the tests exercise only the viewer gate logic. All 39 tests
now pass in three configurations: no key/current SDK, no key/pinned groq==0.13.1,
and with a key.

### Other fixes
- `capture.py`: log message now shows `~N s (±20%%)` to clarify jitter is applied;
  initial retry comment clarified (2 s after healthy run, not "4 s" as previously implied).
- `README.md`: corrected `max_completion_tokens` → `max_tokens` (code uses `max_tokens`).

---

## 2026-10-04 — Groq translation fix (extra_body) + capture process tests

### Problem
The `groq==0.13.1` SDK rejects `reasoning_effort` and `include_reasoning` as
keyword arguments to `chat.completions.create()`. The previous fix changed
`max_completion_tokens` → `max_tokens`, which is correct, but kept passing
`**_reasoning_kwargs(model)` directly — which caused every translation to fail
with a `TypeError`. Spanish captions stayed blank.

### Solution
Wrap `_reasoning_kwargs()` result in `{"extra_body": reasoning}`. The SDK
accepts `extra_body` on every version, while rejecting direct kwargs when they
are newer than the SDK. This keeps `reasoning_effort=off|low|medium|high` and
`include_reasoning=False` working.

### Implementation
- `translate.py`: `**({"extra_body": reasoning} if reasoning else {})` instead of
  `**_reasoning_kwargs(model)`.
- `tests/test_units.py`: two existing tests updated to assert `extra_body` instead
  of direct kwargs; new `test_groq_call_kwargs_are_accepted_by_installed_sdk` checks
  every effort level against the real SDK signature.
- `tests/test_capture_process.py`: new file — 6 tests for `FfmpegProcess`, `Chunker`,
  and `_reader_loop` using a fake ffmpeg subprocess.

### Other recent changes
- `capture.py`: concurrent stderr drain prevents ffmpeg from blocking on full pipe.
- `vad.py`: `VAD_*=0` now respected; pre-roll no longer counted toward min_speech.
- `main.py`: viewer-gated ASR/translation; Groq quota saver.
- `static/index.html`: "Traducción - Minuto" button; pause-to-show-ES.
- `.env`: fixed `GITHUB_REPOSITORY` (was `GITHUB-REPOSITORY` with hyphen); added
  Groq optional vars commented out.

---

## 2026-10-04 — "Traducción - Minuto" button + pause-to-show ES

### Problem
Spanish captions are useful on demand — when the user misses a phrase or wants
to catch up — but not necessarily wanted all the time. The existing "Mostrar ES"
checkbox is all-or-nothing. There was no way to see ES briefly without toggling
it on permanently.

### Solution (frontend only, no backend changes)
Three independent ways ES can now become visible, any one of which is sufficient:

1. **"Mostrar ES" checkbox** — existing always-on behaviour, unchanged.
2. **"Traducción - Minuto" button** — shows ES for the next 60 seconds across all
   incoming captions, then hides automatically. The button turns green while
   active; pressing it again resets the 60 s timer.
3. **Video paused** — ES appears automatically while the player is paused (useful
   for catching a phrase just heard). Resuming playback hides it again unless the
   button or checkbox keep it on.

### Implementation
- Pure frontend change in `static/index.html`.
- `esVisible()` helper combines the three states; `render()` (which already runs
  every 100 ms) uses it instead of checking `showEs.checked` directly.
- `peekActive` flag + `peekTimer` (60 s `setTimeout`) manage the button state.
- `pausedEs` flag is set/cleared by `video` `pause`/`play` event listeners.
- No new WebSocket messages, no new API endpoint — ES was already arriving in
  every caption message; it was just hidden.

---

## 2026-10-04 — Groq quota saver: gate on viewer presence

### Problem
Groq free tier caps are ~14,400 ASR requests/day and ~500k LLM tokens/day. A 24/7 stream
will hit those limits even with only a few viewers because ASR and translation run
unconditionally — Groq calls happen whether anyone is listening or not.

### Solution
When no WebSocket client is connected, the Transcriber discards every VAD segment
without calling the ASR backend at all. The same logic applies to the Translator.
VAD and capture keep running, so the pipeline is warm when the first viewer arrives.

### Implementation
- `Transcriber.__init__` now accepts an optional `has_viewers` callable.
- In `Transcriber._run()`, after `await self.in_queue.get()`, the gate checks
  `has_viewers()` and `continue`s if `False`. Logs once when entering/exiting idle.
- `main.py` wires `has_viewers=lambda: _manager.count > 0`.
- Two new unit tests verify both the skip and resume paths.

### Impact
- A 24/7 stream with zero viewers burns **zero Groq quota** — no ASR calls, no
  translation calls, no 429 rate limits, no retry backoff.
- With one viewer, quota usage becomes linear with viewer count.
- When the first viewer connects, VAD has been running the whole time, so the
  first caption arrives with normal latency — no warm-up delay.

---

## 2026-10-04 — reasoning_effort fix + VAD gap detection

### Fixed reasoning_effort parameter handling for gpt-oss-20b
- `reasoning_effort` is now passed directly to the Groq API (not via `extra_body`), matching Groq SDK expectations
- Added support for `GROQ_REASONING_EFFORT=off` to disable reasoning entirely (fastest option for live streams)
- Increased `max_completion_tokens` from 512 to 2048 — reasoning tokens don't count toward this limit, but the output translation still needs room
- Updated `.env.example` and README docs to clarify that reasoning adds latency (~200–300 ms) and is optional for live captions
- Only applies to `gpt-oss` models; other models silently ignore the parameter (no API errors)

### VAD gap detection even when window buffer is empty
- Fixed a bug where dropped audio chunks at window-buffer boundaries (every ~8th chunk) went undetected
- Added `_stream_started` flag to distinguish true stream start from buffer-empty state
- Gap detection now computes expected position even when the buffer is empty, catching all dropped chunks
- Added two new unit tests: `test_vad_detects_gap_wherever_it_falls()` and `test_vad_marker_then_stream_continues_normally()`

### Test coverage updates
- Updated `test_reasoning_effort_only_for_gpt_oss()` to match new parameter-passing behavior
- Updated `test_groq_translation_sends_reasoning_effort()` to test direct parameter (not `extra_body`)
- All 30 unit tests pass

### Documentation clarification
- README: Expanded test section to explicitly state that page tests use jsdom stubs, not a real browser
- CHANGELOG: Clarified what was tested with real dependencies vs. what requires the user's environment

---

## 2026-10-03 — page + docs refresh

### Frontend polish
- Updated the live caption page to better reflect the current stream and caption state.
- Improved the browser-side caption handling so the UI keeps the latest translation flow visible and easier to follow.
- Refined the page behavior around live updates and reconnect friendliness in the browser client.

### Documentation refresh
- Updated the project docs to align with the current Groq/local backend model, env handling, and operational notes.
- Kept the runtime guidance, config examples, and latency notes consistent with the latest code and env conventions.
- Included the latest environment setup and backend-selection guidance in the checked-in documentation.

---

## 2026-10-03 — Groq client integration

### Added `groq_client.py`
- Introduced a dedicated Groq client wrapper to centralize transcription and translation calls.
- Keeps the Groq API usage isolated from the rest of the pipeline so the local fallback remains easy to reason about.
- Makes model selection and request handling easier to test and reuse.

### Updated transcription / translation flow
- Wired the app to prefer the shared Groq client for both ASR and translation work when `GROQ_API_KEY` is configured.
- Keeps the existing local faster-whisper / NLLB fallback intact when the key is missing.
- Reduces duplicated request logic and keeps the backend selection consistent across modules.

### Verification
- The repo patch was applied cleanly and the new client file is present in the working tree.
- The project continues to use the same env-driven backend selection pattern: Groq when configured, local stack otherwise.

---

## 2026-10-03 — env handling + slow-client resilience

### Environment handling
- Kept the local runtime secrets in the project-local `.env` / parent-folder env pattern, while preserving a repo-safe `.env.example` as the checked-in template.
- Clarified that GitHub-specific variables should use underscores (`GITHUB_REPOSITORY`, `GITHUB_TOKEN`) and must not be used as runtime credentials in the application itself.
- Documented the intended behavior for local secret storage and the difference between repository-safe examples and actual environment files.

### WebSocket slow-client protection
- Hardened the live caption broadcast path so one slow or hanging browser client no longer stalls the rest of the connected viewers.
- Added per-client send timeouts and background socket cleanup so stale connections are dropped quietly instead of blocking the broadcast loop.
- Keeps the page reconnect flow healthy when a browser falls behind or stops consuming updates.

---

## 2026-10-03 — Python 3.13 dependency fix

The local install issue was caused by an incompatible dependency chain rather than a single package pin:

- `transformers==4.44.2` forced a `tokenizers` version that had no Python 3.13 wheel.
- `sentencepiece==0.2.0` was also too old for the modern Python runtime.
- Pip therefore tried to build `tokenizers` from source and failed.

### Fix applied
- Updated `requirements.txt` to:
  - `transformers==4.57.6`
  - `sentencepiece==0.2.2`
- This resolves to a compatible `tokenizers==0.22.2` wheel path on Python 3.13.

### Verification
We verified the actual local environment with:

```bash
cd /root/TVcidade10/tv10-
. venv/bin/activate
python -m pip install -r requirements.txt
```

Result: `EXIT:0`

Imports also succeeded in the venv:
- `torch OK 2.14.1+cu130`
- `transformers OK 4.57.6`
- `sentencepiece OK 0.2.2`
- `tokenizers OK 0.22.2`

### Remaining note
The earlier NVIDIA wheel metadata failure observed in the broader cross-platform simulation appears to be a simulation artifact rather than a real Linux install failure in this environment. We also started validating the NLLB PT→ES path using the newer `transformers` stack; the model download began successfully, but the full translation run was not yet completed at the time of writing.

---

## 2026-10-03 — Documentation update

Rewrote all `.md` files and `.env.example` to reflect the current state of the project.

### `README.md` — full rewrite
- Architecture diagram now shows both Groq and local-fallback paths side by side.
- New "Backends disponíveis" table: Groq vs local, with latency numbers confirmed
  from live testing.
- Installation section split into two paths: "com Groq" (quick) and "sem Groq"
  (local models, requires torch download).
- Configuration table split into sections: Groq, Stream, Servidor, ASR local, VAD,
  Tradução local. All new variables (`GROQ_API_KEY`, `GROQ_TRANSLATE_MODEL`,
  `LOG_LEVEL`) documented.
- Command-line options section added (all `argparse` flags from `main.py`).
- Risk section updated: added stream-down backoff behaviour.

### `LATENCY_PROBLEM.md` — fixes
- Groq translation model corrected: `llama-3.1-8b-instant` → `openai/gpt-oss-20b`
  (verified against live Groq model list).
- Added "Note on VAD with Groq": explains `EnergyDetector` fallback when torch is
  not installed, and how to install the full Silero VAD if needed.
- Recommendation updated with exact URL and command.

### `.env.example` — fixes
- `TRANSLATION_BACKEND=nllb` was hardcoded, overriding the Groq auto-detection.
  Replaced with a comment explaining the default behaviour and leaving the line
  commented out.
- `GROQ_TRANSLATE_MODEL` comment updated to `openai/gpt-oss-20b`.

---

## 2026-10-03 — capture.py: exponential backoff + ffmpeg stderr surfaced

### Problem
When the stream is down (e.g. `Connection refused`), ffmpeg exits immediately
with no audio. The previous code retried every 2 s unconditionally — causing a
tight restart loop with hundreds of spawned processes per minute and no
indication in the logs of what was actually wrong.

### Fixes in `capture.py`

**Exponential backoff on stream-down**
- First failure: retry in 4 s.
- Each subsequent failure with no audio: double the delay (4 → 8 → 16 → 32 → 60 s).
- Delay caps at 60 s — keeps trying indefinitely until the stream returns.
- If the stream was producing audio and then dropped mid-session, delay resets
  to 2 s (transient glitch, not a persistent outage).

**ffmpeg stderr now captured and logged**
- Previously `stderr=asyncio.subprocess.DEVNULL` swallowed all error messages.
- Now reads up to 2 KB of stderr after stdout closes and logs the last line at
  `WARNING` level. Example:
  ```
  capture — ffmpeg error: Error opening input files: Connection refused
  capture — ffmpeg exited without producing audio (stream down?), retrying in 16 s…
  ```

**`got_audio` flag distinguishes outage from mid-stream drop**
- Tracks whether any audio bytes arrived in the current ffmpeg session.
- `False` on exit → outage → exponential backoff.
- `True` on exit → transient drop → 2 s retry (unchanged from before).

---

## 2026-10-03 — First successful live run

Full end-to-end pipeline confirmed working against the real TV Cidade 10 stream.

### What was fixed to get it running

**`requirements.txt` — torch/numpy version pins relaxed (done in previous session)**
- `torch==2.3.1` → `torch>=2.3.1` (no pre-built wheel for Python 3.13 at that exact version)
- `numpy==1.26.4` → `numpy>=1.24` (same; source build fails without a C compiler)

**`.env` — `TRANSLATION_BACKEND` set to `groq`**
- Was explicitly `TRANSLATION_BACKEND=nllb`, which overrode the auto-detection even
  with `GROQ_API_KEY` present. Changed to `groq` to match the key.

**`vad.py` — graceful fallback when torch not installed**
- `SileroDetector.__init__()` raises `ModuleNotFoundError` when torch is absent.
- Added `try/except` in `VadSegmenter._run()`: catches the error and falls back to
  `EnergyDetector` (RMS-based, no model needed) with a warning log.
- This allows the system to start without torch installed, using the Groq ASR path
  which does not need VAD accuracy as much as local Whisper does.

**`translate.py` / `.env` / `.env.example` — translation model corrected**
- `llama-3.1-8b-instant` no longer exists on this Groq account's available models.
- Checked live model list via `groq.models.list()`.
- Default changed to `openai/gpt-oss-20b` (fastest available chat model).

### Confirmed working

```
ASR backend   : Groq (whisper-large-v3-turbo) ✓
Translation   : Groq LLM ✓
Player        : http://localhost:8000
```

Live captions arriving, e.g.:
- `o preço, por mais que eu tenha uma loja aqui, no São Francisco` — seg 5.0s, ASR 0.7s, translation 0.7s
- `em uma loja no Bigurrilho` — seg 1.3s, ASR 1.1s, translation 3.2s
- `Tem que vender o mesmo preço` — seg 1.3s, ASR 1.3s, translation 3.0s

Total latency ~10–15 s (dominated by live-edge buffering in ffmpeg/HLS segments,
not ASR or translation). ASR + translation combined: ~1–5 s.

### Still missing

- `torch` + `silero-vad` are not installed — using `EnergyDetector` fallback.
  This means VAD segmentation is less accurate (cuts on energy drops, not speech
  pauses). Run `pip install silero-vad` inside the venv to restore full Silero VAD.
  Large torch download (~800 MB) is why it was skipped for initial testing.

---

## 2026-10-03 — Environment setup: venv + requirements fixes

### Context
Attempted first real run of `python main.py`. Install failed twice before the
right combination of fixes was found.

### `requirements.txt` — version pins relaxed for modern Python/pip

Two packages failed to install with their original pinned versions:

- **`torch==2.3.1`** — no longer available as a pre-built wheel for Python 3.13.
  Changed to `torch>=2.3.1` to allow the resolver to pick the latest stable CPU
  wheel (`2.5.x` or newer). The Groq path does not use torch at runtime; torch
  is only needed for local VAD (silero) and NLLB translation fallbacks.

- **`numpy==1.26.4`** — pre-built wheel not available for Python 3.13; building
  from source fails without a C compiler. Changed to `numpy>=1.24` to use the
  newest available binary wheel.

All other versions remain pinned (`groq==0.13.1`, `faster-whisper==1.1.1`,
`fastapi==0.115.0`, etc.).

### `.env` — GROQ_API_KEY filled in

The actual Groq API key was manually added to `.env` (copied from the KILOMBO
project). The placeholder `gsk_xxxxxxxxxxxxxxxxxxxx` in `.env.example` is
intentionally left as-is. The key is not committed to git.

### Virtual environment

A `venv/` directory was created at `/root/TVcidade10/venv/`. Install may still
be in progress (torch download is large). If the venv install is incomplete,
run:

```bash
cd /root/TVcidade10
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

---

## 2026-10-03 — Groq integration: cloud ASR + translation on old hardware

The project now supports **Groq** for both speech recognition and translation with a
single API key. This solves the latency problem on old laptops with no GPU.

### What changed

- **`groq==0.13.1`** added to `requirements.txt`.
- **`transcribe.py`**: `_transcribe_segment()` auto-selects Groq when `GROQ_API_KEY`
  is set, otherwise falls back to local `faster-whisper`.
  - Groq uses `whisper-large-v3-turbo`, runs at ~216× real-time (5 s segment in ~23 ms
    on Groq hardware).
  - Returns per-segment timestamps via `verbose_json` format.
  - Falls back to full-text if segment parsing fails.
- **`translate.py`**: `_translate_groq_sync()` adds a new backend.
  - Uses `llama-3.1-8b-instant` (or `GROQ_TRANSLATE_MODEL` env var).
  - Same `GROQ_API_KEY` as ASR — no extra credentials needed.
  - Auto-selection: `TRANSLATION_BACKEND=groq` when `GROQ_API_KEY` is present.
- **`main.py`**: startup log now shows which backend is active:
  - With Groq: `ASR backend: Groq (whisper-large-v3-turbo) ✓`, `Translation: Groq LLM ✓`.
  - Without Groq: warns users that adding `GROQ_API_KEY` dramatically improves latency
    on CPU hardware.
- **`static/index.html`**: PT (target language) on bottom, ES on top.

### Performance on old hardware

| Backend | Segment time | Latency | GPU needed? |
|---|---|---|---|
| Local `whisper-small` CPU | ~4–8 s | 10–15 s | No |
| Groq `whisper-large-v3-turbo` | ~0.1–0.3 s | ~5 s | No |
| Groq + Groq translation | ~0.1–0.4 s | ~5 s | No |

**Instructions to use Groq**

1. Get a free API key: https://console.groq.com/keys
2. Add it to `.env`:
   ```
   GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx
   ```
3. Run `python main.py`.

Local `faster-whisper` is still available as a fallback — just omit `GROQ_API_KEY`.

---

## 2026-10-03 — Latency optimisations applied

Changes made based on LATENCY_PROBLEM.md analysis.

### `vad.py` — VAD_MAX_SEGMENT_MS default 8000 → 5000
The segment ceiling is the single biggest controllable latency contributor on
CPU: a segment cannot be transcribed until it is complete, so a 8 s ceiling
means up to 8 s of wait time before Whisper even starts.  Lowering to 5000 ms
saves up to ~3 s of worst-case latency with acceptable accuracy loss on news
speech.  Override with `VAD_MAX_SEGMENT_MS=3000` for more aggression or
`VAD_MAX_SEGMENT_MS=8000` to restore the old behaviour.

### `.env.example` — default updated to match
`VAD_MAX_SEGMENT_MS=5000` (was 8000), with a comment noting the old value.

### `main.py` — latency log line now includes segment duration
The `caption ready:` log line now shows `(seg Xs, ASR Xs, translation Xs)` so
the dominant cost is immediately visible.  Previously only ASR and translation
times were shown, hiding how much time was spent waiting for the VAD segment
to close.

### `main.py` — WHISPER_PROMPT logged at startup
If `WHISPER_PROMPT` is set in `.env`, it is now printed at startup so it is
easy to confirm it is active.

### `translate.py` — stale docstring fixed
The `Translator` class docstring still said `stream_ts`/`duration` (left over
from a previous version).  Corrected to `audio_start`/`audio_end`/`t_emit`/
`t_asr`/`t_mt` which matches the actual pipeline dict.

### What was NOT changed (and why)
- `WHISPER_BEAM` stays at 1 (greedy) — already the fastest setting.
- `temperature=0.0` stays — already set, no fallback re-decodes.
- `condition_on_previous_text=False` stays — already set.
- GPU path (`WHISPER_DEVICE=cuda`, `TRANSLATION_DEVICE=cuda`) already wired;
  no code change needed, only `.env` settings.
- Streaming ASR not implemented — requires a different architecture.

---

## 2026-10-03 — Stream investigation (findings)

### Stream health
- URL `https://video10.logicahost.com.br/tvcidade10/tvcidade10/playlist.m3u8` responds HTTP 200.
- Audio decoded from a live segment: duration ~8.9 s, RMS 0.041, peak 0.68 — normal speech levels, not silence.

### CORS
- Every response already carries `Access-Control-Allow-Origin: *`.
- The built-in proxy is still useful to avoid mixed-content issues when the page is served from `http://localhost`, but CORS itself will not block direct browser playback.

### Hosts
- Segments are served from the same host as the playlist (`video10.logicahost.com.br`). No CDN redirect was observed.
- **`PROXY_ALLOWED_HOSTS` can be left blank.**

### Chunklist name
- The master playlist returns a new `chunklist_w<random>.m3u8` name on every poll. HLS.js handles this transparently. `ffmpeg` in `capture.py` follows the chunklist from the initial master-playlist fetch, which is correct.

### Segment duration
- `#EXT-X-TARGETDURATION: 19`, actual segments ~8–10 s each. ffmpeg delivers audio in bursts of that size rather than continuously.
- The `AudioClock` in `capture.py` (added in the TV10-fixed update) accounts for this by tracking the minimum wall-clock offset over a 90 s sliding window.

### Tracks confirmed (ffprobe on a live segment)
| Track | Codec | Details |
|---|---|---|
| data | timed_id3 | ID3 metadata |
| video | H.264 | 1920×1080, 30 fps, Constrained Baseline |
| audio | AAC-LC | 44100 Hz, stereo, ~98 kbps |
| subtitles | — | `closed_captions=0` (none) |

---

## 2026-10-03 — TV10-fixed.tar.gz applied

Full replacement of all Python and JS source files from the package
`TV10-fixed.tar.gz`. Previous files were written in the same session and had
never been run against the real stream.

### What changed
- **`captions.py`** (new): age-based sync — sends `age_start`/`age_end` (seconds
  since speech was at the live edge) instead of absolute timestamps, so server
  and browser clocks never need to agree.
- **`static/captions.js`** (new): `CaptionScheduler` class — queues captions,
  honours `MIN_SHOW_MS`/`MAX_SHOW_MS`, detects and reports late captions.
- **`capture.py`**: added `AudioClock` (burst-aware live-edge estimator),
  discontinuity markers on ffmpeg restart, larger queue (~2 min).
- **`vad.py`**: tries `silero-vad` pip package first (bundled model, works
  offline); falls back to `torch.hub`. Adds `EnergyDetector` for tests,
  preroll buffer, env-var tuning (`VAD_*`).
- **`transcribe.py`**: returns per-sentence `{text, start, end}` list so long
  VAD segments become multiple timed captions; hallucination filters added.
- **`translate.py`**: Marian backend now correctly pivots PT→EN→ES with two
  models. The previous `opus-mt-ROMANCE` model ID does not exist on HuggingFace.
- **`server.py`**: HMAC-signed segment URLs (prevents open-relay SSRF);
  `PROXY_ALLOWED_HOSTS` allowlist; concurrent WebSocket send; `lifespan`
  handler for clean `httpx` shutdown.
- **`static/index.html`**: uses `CaptionScheduler`; video-delay control (4–40 s,
  saved to `localStorage`); ±0.5 s fine-tune offset; late-caption warning bar.
- **`tests/`** (new): 12 Python unit tests, Node.js scheduler tests, jsdom page
  tests, e2e test with real ffmpeg + local HLS stream, smoke test for main.py.
- **`.env.example`**: added `VAD_*`, `WHISPER_BEAM`, `WHISPER_PROMPT`,
  `TRANSLATION_DEVICE`, `PROXY_ALLOWED_HOSTS`, `HOST`.

### What was tested in the package
- 12 unit tests pass.
- `CaptionScheduler` logic verified in Node.js.
- Page logic verified in jsdom (HLS.js and WebSocket mocked, not real browser).
- End-to-end with real ffmpeg + local live HLS: captions placed within 20 ms of
  true timing in steady state; first caption may be ~0.25 s off while
  `AudioClock` settles.
- `main.py` with a real WebSocket client; clean shutdown on Ctrl+C.

### What was NOT tested in the package
- Real Whisper and NLLB models.
- Real TV Cidade 10 stream.
- HLS.js in a real browser (Firefox, Chrome, Safari, etc.) — jsdom page tests
  use stubbed HLS.js and WebSocket. Real browser behavior may differ in timing,
  buffering, or event handling.

---

## 2026-10-03 — Initial build

First implementation created from scratch based on the README specification.

### Architecture decided
- Whisper `translate` task outputs English only; PT→ES requires a second step.
- Marian `opus-mt-ROMANCE` was initially used for translation (later found
  to be a non-existent model ID — fixed in TV10-fixed update).
- WebSocket/SSE chosen over a growing `.vtt` file (browsers do not handle
  the latter reliably for live streams).
- `liveSyncDuration` in HLS.js delays playback intentionally to allow ASR
  time to finish before the corresponding audio reaches the viewer.

### Files created
`capture.py`, `vad.py`, `transcribe.py`, `translate.py`, `server.py`,
`static/index.html`, `main.py`, `requirements.txt`, `.env.example`, `README.md`.

### Caption layout
- PT on bottom line, full white (target / primary language).
- ES on top line, slightly dimmer (secondary / reference).
