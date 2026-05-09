#!/bin/bash
# Bootstrap voice-memo sync + transcription on a fresh Mac.
#
# Idempotent — safe to re-run. After running, plug in the IC Recorder and
# the workflow runs automatically: sync new mp3s -> transcribe with the
# OpenAI audio API -> append to a daily ~/Sony/Memos/YYYY-MM-DD.md file.
#
# Manual prerequisites this script does NOT do for you:
#   1. Save your OpenAI key to ~/.config/openai/api_key (chmod 600).
#      Without a key, transcription falls back to local openai-whisper
#      (slower; install with `brew install openai-whisper`).
#   2. Grant Full Disk Access to /bin/bash so launchd can read /Volumes:
#      open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Repo: $REPO_DIR"

# 1. Tools
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Install it first: https://brew.sh" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found (huh, it's normally on macOS)" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2
  exit 1
fi

# ffmpeg lets whisper (local fallback) and various audio tools handle mp3.
if ! brew list --formula 2>/dev/null | grep -qx ffmpeg; then
  echo "==> brew install ffmpeg"
  brew install ffmpeg
fi

# 2. Working directories
mkdir -p \
  "$HOME/bin" \
  "$HOME/Sony/Files" \
  "$HOME/Sony/Memos" \
  "$HOME/Sony/.cache/transcripts" \
  "$HOME/.config/openai" \
  "$HOME/Library/LaunchAgents" \
  "$HOME/Library/Logs"

# Pre-create the API key file (empty) with locked-down permissions so the
# user just has to paste their key into it.
if [ ! -f "$HOME/.config/openai/api_key" ]; then
  touch "$HOME/.config/openai/api_key"
  chmod 600 "$HOME/.config/openai/api_key"
fi

# 3. Symlink scripts into ~/bin
chmod +x "$REPO_DIR/sync-ic-recorder.sh" "$REPO_DIR/transcribe.py"
ln -sfn "$REPO_DIR/sync-ic-recorder.sh" "$HOME/bin/sync-ic-recorder.sh"
ln -sfn "$REPO_DIR/transcribe.py"        "$HOME/bin/transcribe-memos.py"
echo "==> linked scripts into ~/bin"

# 4. Render and (re)load the LaunchAgent
PLIST="$HOME/Library/LaunchAgents/com.maxua.sync-ic-recorder.plist"
sed "s#__HOME__#$HOME#g" "$REPO_DIR/com.maxua.sync-ic-recorder.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "==> LaunchAgent loaded"

cat <<EOF

Setup complete.

REMAINING MANUAL STEPS:

  1) Paste your OpenAI API key into ~/.config/openai/api_key (single line, no
     quotes). Without it, transcription falls back to local openai-whisper —
     install that with: brew install openai-whisper

  2) Grant Full Disk Access to /bin/bash so launchd can read /Volumes/<removable>:
       open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
     Click '+', press Cmd+Shift+G, type /bin/bash, hit Open, toggle on.

TEST IT:
  Plug in the IC RECORDER, wait a few seconds, then:
    tail -20 ~/Library/Logs/sync-ic-recorder.log
    tail -20 ~/Library/Logs/transcribe-memos.log
EOF
