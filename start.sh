#!/usr/bin/env bash
# start.sh — first-time setup + launch for TV Cidade 10 live subtitles
#
# Usage:
#   ./start.sh                  # uses GROQ_API_KEY from .env if present
#   GROQ_API_KEY=gsk_... ./start.sh   # pass key inline
#
# What it does:
#   1. Creates a Python venv (once, skipped on subsequent runs)
#   2. Installs the right dependencies (Groq-only by default, ~50 MB)
#   3. Copies .env.example → .env if no .env exists yet
#   4. Writes GROQ_API_KEY into .env if passed via environment
#   5. Starts the app

set -e
cd "$(dirname "$0")"

# ── 1. Python check ───────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install Python 3.10+ and try again."
  exit 1
fi

PYTHON=$(command -v python3)

# ── 2. ffmpeg check ───────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
  echo "ERROR: ffmpeg not found."
  echo "  Ubuntu/Debian: sudo apt install ffmpeg"
  echo "  macOS:         brew install ffmpeg"
  echo "  Windows:       https://ffmpeg.org/download.html"
  exit 1
fi

# ── 3. Virtual environment ────────────────────────────────────────────────────
if [ ! -d venv ]; then
  echo "Creating virtual environment…"
  "$PYTHON" -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

# ── 4. Dependencies ───────────────────────────────────────────────────────────
# Only reinstall when requirements.groq.txt is newer than the venv marker.
MARKER=venv/.installed_groq
if [ ! -f "$MARKER" ] || [ requirements.groq.txt -nt "$MARKER" ]; then
  echo "Installing dependencies (~50 MB, Groq-only — no torch)…"
  pip install -q -r requirements.groq.txt
  touch "$MARKER"
fi

# ── 5. .env setup ─────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
fi

# If GROQ_API_KEY was passed in the environment, write it into .env
if [ -n "$GROQ_API_KEY" ]; then
  if grep -q "^GROQ_API_KEY=" .env; then
    # Replace existing line
    sed -i "s|^GROQ_API_KEY=.*|GROQ_API_KEY=$GROQ_API_KEY|" .env
  else
    # Uncomment placeholder or append
    sed -i "s|^# GROQ_API_KEY=.*|GROQ_API_KEY=$GROQ_API_KEY|" .env
    grep -q "^GROQ_API_KEY=" .env || echo "GROQ_API_KEY=$GROQ_API_KEY" >> .env
  fi
  echo "GROQ_API_KEY written to .env."
fi

# Warn if no key is set
if ! grep -qE "^GROQ_API_KEY=gsk_[^x]" .env 2>/dev/null; then
  echo ""
  echo "NOTE: No GROQ_API_KEY found in .env."
  echo "  Without it the app uses local Whisper + NLLB models (~800 MB download on"
  echo "  first run). For fast cloud ASR, get a free key at https://console.groq.com/keys"
  echo "  and add it to .env:  GROQ_API_KEY=gsk_..."
  echo ""
fi

# ── 6. Launch ─────────────────────────────────────────────────────────────────
echo "Starting TV Cidade 10 live subtitles…"
echo "Open http://localhost:8000 in your browser."
echo "(Press Ctrl+C to stop)"
echo ""
exec python main.py "$@"
