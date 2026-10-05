# Roadmap — Current State & Future Work

This document outlines what is working, what's in progress, and what's left to do.

---

## ✅ Completed

### Core Pipeline
- **Live ASR (PT)** — Whisper via Groq (fast) or faster-whisper locally
- **PT→ES Translation** — Groq LLM or NLLB locally
- **Caption synchronization** — Age-based scheduling with viewer-configurable video delay
- **Caption size** — 30% larger than standard for TV viewing (~1.5rem PT, ~1.3rem ES)
- **Spanish visibility** — Hidden by default; shown only via "Traducción - Minuto" button (60s), pause, or checkbox
- **Offline detection** — Stream down → 503 proxy response → prominent red banner + monitoring script
- **Fullscreen (desktop)** — Captions remain visible when entering fullscreen (fallback to CSS fullscreen if needed)

### Testing
- **39 unit tests** — VAD, ASR, translation, caption scheduling, proxy SSRF, WebSocket
- **7 page tests** — Caption scheduling in jsdom (HLS.js/WebSocket mocked), offline banner, fullscreen
- **End-to-end test** — ffmpeg + local HLS → capture + VAD → captions (30 s)
- **Smoke test** — `main.py` + server + WebSocket + manual Ctrl+C

### Documentation
- **README.md** — Installation, configuration, backends, known risks, security
- **YUNOHOST.md** — Step-by-step Debian/YunoHost deployment with nginx, systemd, troubleshooting
- **LATENCY_PROBLEM.md** — Why 10–15 s atraso is needed, where latency comes from
- **CHANGELOG.md** — Audit findings and bug fixes
- **start.sh** — One-command setup (venv, deps, .env, starts app)
- **check_stream.sh** — Monitor when stream comes back online

### Deployment
- **YunoHost on Debian** — YunoHost Reverse-Proxy app + Let's Encrypt HTTPS + systemd auto-start/restart + dedicated `tv10svc` user. Public access via `yunohost user permission add`. Step 5 (Reverse-Proxy install) not yet run on the target server.
- **Local testing** — `test_hls/` folder with 60 s synthetic stream
- **Groq integration** — Viewer-gated (zero API calls when no clients connected)
- **Cota management** — ASR cap is generous (~14k req/day free tier); translation (`gpt-oss-20b`)
  is tighter (~1k req/day free tier). Can switch translation to local NLLB if quota exhausted.

---

## 🟡 Known Limitations & Workarounds

### iPhone Fullscreen — Captions Vanish
**Symptom:** On iOS Safari, entering fullscreen hides captions (same issue as desktop before the fix).

**Root cause:** iOS Safari uses its own native fullscreen player and ignores `controlsList`
and custom overlays. The only way to keep captions visible is to catch the
`webkitbeginfullscreen` event and switch to CSS fullscreen (no native player).

**Workaround today:** None. Captions vanish on iPhone fullscreen.

**Future fix:** Implement `webkitbeginfullscreen` → CSS fullscreen switch in `static/captions.js`.
This requires:
1. Listen to `webkitbeginfullscreen` instead of (or in addition to) `fullscreenchange`
2. Exit native fullscreen and re-enter CSS fullscreen
3. Test on real iPhone (Safari and Chrome on iOS, since Chrome on iOS uses WebKit)

**Effort:** Medium. Needs testing on real devices.

---

### YunoHost Deployment — Reverse-Proxy App Not Yet Tested
**Current approach (YUNOHOST.md):** Use YunoHost's own Reverse-Proxy app instead of a
hand-written nginx snippet. This registers the app with YunoHost's permission system
(SSOwat), so:
- Visitors are blocked behind the YunoHost login by default
- Public access is granted explicitly: `yunohost user permission add <app>.main visitors`
- The domain, certificate, and nginx config are all managed by YunoHost

**Status:** The domain/cert/systemd steps match what already works on `new.kilombo.top`.
The Reverse-Proxy app install (Step 5 of YUNOHOST.md) has **not been run on this server
yet**. Marked ⚠ in the guide.

**What to confirm before relying on it:**
- Does `yunohost app search reverse` find the Reverse-Proxy app in the catalog?
- Does the app's nginx config include WebSocket headers (`Upgrade`, `Connection`,
  `proxy_read_timeout 3600s`)? If not, add them manually and note that a YunoHost
  upgrade of the proxy app may overwrite the file.
- Does `curl` to `/captions` return HTTP 101 after Step 5?

**Direct YunoHost package** — Writing a full `.yunohost.yml` manifest would automate
installation, updates, backups, and SSO without any manual nginx edits. Not done; high effort.

---

## 🔴 Not Implemented

### Advanced Features
- **Multi-language support** — Currently PT → ES only. Adding FR, EN, etc. requires:
  - UI dropdown to pick target language(s)
  - Parallel translation calls (one per language)
  - Player display (tabs or stacked overlays)
  - **Effort:** Medium

- **Fallback caption sources** — If Groq is down, fall back to local models automatically
  - **Effort:** Low (already has local fallback; just needs graceful degradation in UI)

- **Caption export** — Save captions as `.srt`, `.vtt`, or archive for future reference
  - **Effort:** Low

- **Viewer chat/reactions** — Adding engagement features beyond captions
  - **Effort:** High (needs user auth, message storage, moderation)

- **Recording** — Archive the stream + captions for on-demand playback
  - **Effort:** High (ffmpeg multi-bitrate, storage, indexing)

### Mobile Optimizations
- **Responsive UI** — Caption size, button layout on small screens
  - **Effort:** Low (CSS media queries)
- **Offline captions** (service worker) — Let viewers see past captions on weak connections
  - **Effort:** Medium
- **iOS fullscreen fix** (see above)

### DevOps
- **Docker image** — Containerize for easy deployment anywhere
  - **Effort:** Low (Dockerfile, compose for local dev)
- **Ansible playbook** — Automate YunoHost deployment
  - **Effort:** Low–Medium
- **Auto-scaling** — Not applicable (single-tenant app), but could shard across multiple servers
- **Monitoring dashboard** — Uptime, quota usage, latency percentiles
  - **Effort:** Medium (Prometheus + Grafana integration)

### Accessibility
- **Keyboard navigation** — Full control of video and captions
  - **Effort:** Low
- **Audio descriptions** — Separate track for visually impaired
  - **Effort:** High (manual or AI narration)
- **WCAG 2.1 AA audit** — Full compliance testing with assistive tech
  - **Effort:** Medium (manual testing required)

### Quality
- **Speaker diarization** — Identify who's talking (e.g., "Host:" vs "Guest:")
  - **Effort:** High (requires separate model, complicates pipeline)
- **Punctuation restoration** — Whisper/faster-whisper don't add periods or commas
  - **Effort:** Medium (post-ASR LLM call or training data tricks)
- **Accent/dialect adaptation** — Improve accuracy for regional accents
  - **Effort:** High (custom Whisper fine-tuning or better prompts)

---

## 📋 Quick Reference: What to Do Next

**If you have users watching right now:**
1. Monitor the stream daily — run as `tv10svc` so `.env` is readable and the correct
   `STREAM_URL` is used (otherwise the script silently falls back to the default URL):
   `sudo -u tv10svc /opt/tvcidade10/check_stream.sh`
2. Watch Groq quota with `sudo journalctl -u tvcidade10 -f` (look for 429 errors)
3. If translation quota hits limit, switch to local NLLB: `TRANSLATION_BACKEND=nllb` in `.env`

**If you want to improve UX:**
1. Responsive CSS for mobile (media queries for caption size + buttons)
2. iOS fullscreen fix (catch `webkitbeginfullscreen`, test on real device)
3. Caption export (`.srt` download button)

**If you want to complete the YunoHost deployment:**
1. Follow YUNOHOST.md Step 5 (Reverse-Proxy app install) — this is the untested part
2. Verify WebSocket headers in the generated nginx config
3. Run the `curl` WebSocket test from Step 5 before opening to the public

**If you want to scale:**
1. Containerize with Docker
2. Set up monitoring (Prometheus scrape `/health` endpoint — add this first)
3. Automate with Ansible
4. Plan for multi-stream or CDN

---

## 🧪 Testing Checklist Before Wider Deployment

- [ ] Stream stays up for 24+ hours without restarts (`systemctl status tvcidade10`)
- [ ] Groq quota does not hit limit on a typical day of viewing
- [ ] Captions are synchronized within 10–15 s and readable at TV distance
- [ ] Fullscreen works on Chrome, Firefox, Safari (desktop)
- [ ] Fullscreen on iOS Safari noted as limitation (captions vanish — acceptable for now?)
- [ ] WebSocket reconnects if server restarts mid-stream
- [ ] Offline banner appears when stream goes down (and disappears when it comes back)
- [ ] Translation works correctly (PT → ES) with real speech (not synthetic)
- [ ] Viewer-gated ASR works: open player tab → ASR starts; close all tabs → ASR stops

---

## 📞 Known Issues to Report / Track

1. **iPhone fullscreen** — Captions vanish (by design, iOS limitation)
2. **YunoHost Reverse-Proxy app** — Step 5 of YUNOHOST.md not yet run on this server; WebSocket headers need manual verification after install
3. **Groq quota management** — No built-in quota warning or auto-fallback (manual `.env` edit needed)
4. **Whisper hallucinations** — Music/noise/overlapping speakers can cause false text (acceptable risk)
5. **No speaker diarization** — Can't tell who's talking (low priority for news channel)

---

## 📖 Related Documents

- **README.md** — Full feature list and configuration reference
- **YUNOHOST.md** — Deployment guide (Debian, nginx, systemd)
- **LATENCY_PROBLEM.md** — Why streaming latency is 10–15 s
- **CHANGELOG.md** — Recent bug fixes and improvements

