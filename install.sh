#!/bin/bash
# Bootstrap voice-memo sync + transcription on a fresh Mac.
#
# Idempotent — safe to re-run. After running, plug in the IC Recorder and
# the workflow runs automatically: sync new mp3s -> transcribe with the
# OpenAI audio API -> append to a monthly ~/Sony/Memos/<Month YYYY>.md file.
#
# Manual prerequisites this script does NOT do for you:
#   1. Save your OpenAI key to ~/.config/openai/api_key (chmod 600). The
#      script requires it — there's no local fallback.
#   2. Grant Full Disk Access to /bin/bash so launchd can read /Volumes
#      and ~/Documents:
#      open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Repo: $REPO_DIR"

# 1. Tools — we only need curl (system) and python3 (system).
if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found (huh, it's normally on macOS)" >&2
  exit 1
fi
if ! command -v /usr/bin/python3 >/dev/null 2>&1; then
  echo "/usr/bin/python3 not found" >&2
  exit 1
fi

# 2. Shared config — seeded from the example template on first install.
mkdir -p "$HOME/.config/voicememo" "$HOME/.config/openai"
if [ ! -f "$HOME/.config/voicememo/config.sh" ]; then
  cp "$REPO_DIR/config.sh.example" "$HOME/.config/voicememo/config.sh"
  echo "==> seeded $HOME/.config/voicememo/config.sh"
fi
if [ ! -f "$HOME/.config/voicememo/vocabulary.md" ]; then
  cp "$REPO_DIR/vocabulary.md.example" "$HOME/.config/voicememo/vocabulary.md"
  echo "==> seeded $HOME/.config/voicememo/vocabulary.md (edit to add your names/brands)"
fi
. "$HOME/.config/voicememo/config.sh"
SONY_BASE="${SONY_BASE:-$HOME/Documents/Sony}"
echo "==> SONY_BASE=$SONY_BASE"

# 3. Working directories (derived from SONY_BASE)
mkdir -p \
  "$HOME/bin" \
  "$SONY_BASE/Files" \
  "$SONY_BASE/Memos" \
  "$SONY_BASE/.cache/transcripts" \
  "$SONY_BASE/.cache/processed" \
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

# 4. Render and (re)load the LaunchAgents
#    - sync-ic-recorder: triggered on IC RECORDER mount
#    - transcribe-memos: on-demand, kicked by the sync script via launchctl
for LABEL in com.maxua.sync-ic-recorder com.maxua.transcribe-memos; do
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  sed "s#__HOME__#$HOME#g" "$REPO_DIR/$LABEL.plist.template" > "$PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "==> LaunchAgent loaded: $LABEL"
done

cat <<EOF

Setup complete.

REMAINING MANUAL STEPS:

  1) Paste your OpenAI API key into ~/.config/openai/api_key (single line, no
     quotes). The transcribe script requires it and will exit cleanly with an
     error in the log if missing — re-kick after fixing.

  2) Grant Full Disk Access to /bin/bash so launchd can read /Volumes/<removable>:
       open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
     Click '+', press Cmd+Shift+G, type /bin/bash, hit Open, toggle on.

TEST IT:
  Plug in the IC RECORDER, wait a few seconds, then:
    tail -20 ~/Library/Logs/sync-ic-recorder.log
    tail -20 ~/Library/Logs/transcribe-memos.log
EOF
