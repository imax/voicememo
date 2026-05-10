#!/bin/bash
# Sync new recordings from Sony IC Recorder to $SONY_BASE/Files/, then kick
# off transcription. Triggered by launchd (StartOnMount) on every volume
# mount; exits silently if the IC RECORDER isn't the volume that just
# appeared.
#
# Canonical source lives in <repo>/voicememo/. install.sh symlinks
# ~/bin/sync-ic-recorder.sh to this file.

set -u

# Shared config — see voicememo/config.sh.example. Both this script and
# transcribe.py read SONY_BASE from here so the paths can't drift.
CONFIG_FILE="$HOME/.config/voicememo/config.sh"
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"
SONY_BASE="${SONY_BASE:-$HOME/Documents/Sony}"

VOLUME="/Volumes/IC RECORDER"
SRC="$VOLUME/REC_FILE/FOLDER01/"
DEST="$SONY_BASE/Files/"
LOG="$HOME/Library/Logs/sync-ic-recorder.log"
TRANSCRIBE="$HOME/bin/transcribe-memos.py"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

[ -d "$VOLUME" ] || exit 0
[ -d "$SRC" ] || { log "IC RECORDER mounted but no REC_FILE/FOLDER01 dir"; exit 0; }

mkdir -p "$DEST"

# Wait briefly for the mount to settle so rsync sees a stable view.
sleep 2

log "sync start: $SRC -> $DEST"
BEFORE=$(find "$DEST" -type f | wc -l | tr -d ' ')

# --ignore-existing: never overwrite files we already pulled.
# --exclude='.*': skip macOS metadata that the recorder/macOS leaves behind.
rsync -a --ignore-existing --exclude='.*' "$SRC" "$DEST" >> "$LOG" 2>&1
RC=$?

AFTER=$(find "$DEST" -type f | wc -l | tr -d ' ')
NEW=$((AFTER - BEFORE))
log "sync done rc=$RC new_files=$NEW"

if [ "$RC" -eq 0 ] && [ "$NEW" -gt 0 ]; then
  /usr/bin/osascript -e "display notification \"$NEW new file(s) copied\" with title \"IC Recorder synced\""
elif [ "$RC" -ne 0 ]; then
  /usr/bin/osascript -e "display notification \"rsync exit $RC — see log\" with title \"IC Recorder sync failed\""
fi

# Kick off transcription in the background. It's idempotent (skips already-
# transcribed files), so we run it even when NEW=0 — cheap, and recovers if
# a previous run was interrupted.
if [ -x "$TRANSCRIBE" ]; then
  log "kicking off transcription"
  nohup "$TRANSCRIBE" >> "$LOG" 2>&1 &
  log "transcription pid=$!"
else
  log "transcribe-memos.py not found at $TRANSCRIBE — skipping"
fi
