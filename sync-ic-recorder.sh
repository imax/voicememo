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
REC_DIR="$VOLUME/REC_FILE"
DEST="$SONY_BASE/Files/"
LOG="$HOME/Library/Logs/sync-ic-recorder.log"
TRANSCRIBE="$HOME/bin/transcribe-memos.py"
TRANSCRIBE_LABEL="com.maxua.transcribe-memos"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

[ -d "$VOLUME" ] || exit 0
[ -d "$REC_DIR" ] || { log "IC RECORDER mounted but no REC_FILE dir"; exit 0; }

# Sony's recorder rolls FOLDER01 -> FOLDER01_02 -> FOLDER01_03 once a folder
# fills up. Sync every FOLDER* dir so new rollovers are picked up automatically.
SRCS=( "$REC_DIR"/FOLDER*/ )
# Glob with no matches expands to the literal pattern — guard against that.
if [ ! -d "${SRCS[0]}" ]; then
  log "IC RECORDER mounted but no REC_FILE/FOLDER* dirs"
  exit 0
fi

mkdir -p "$DEST"

# Wait briefly for the mount to settle so rsync sees a stable view.
sleep 2

log "sync start: ${#SRCS[@]} folder(s) -> $DEST"
BEFORE=$(find "$DEST" -type f | wc -l | tr -d ' ')

# --ignore-existing: never overwrite files we already pulled.
# --exclude='.*': skip macOS metadata that the recorder/macOS leaves behind.
# Filenames are timestamped (YYMMDD_HHMM.mp3) and unique across folders, so
# flattening into one DEST is safe.
RC=0
for SRC in "${SRCS[@]}"; do
  log "  rsync $SRC"
  rsync -a --ignore-existing --exclude='.*' "$SRC" "$DEST" >> "$LOG" 2>&1
  SUB_RC=$?
  [ "$SUB_RC" -ne 0 ] && RC=$SUB_RC
done

AFTER=$(find "$DEST" -type f | wc -l | tr -d ' ')
NEW=$((AFTER - BEFORE))
log "sync done rc=$RC new_files=$NEW"

if [ "$RC" -eq 0 ] && [ "$NEW" -gt 0 ]; then
  /usr/bin/osascript -e "display notification \"$NEW new file(s) copied\" with title \"IC Recorder synced\""
elif [ "$RC" -ne 0 ]; then
  /usr/bin/osascript -e "display notification \"rsync exit $RC — see log\" with title \"IC Recorder sync failed\""
fi

# Kick off transcription via its own LaunchAgent. Backgrounded `nohup &`
# children of a launchd-spawned script get reaped when the job's process
# group exits — silent death, no log line. Owning a separate LaunchAgent
# means launchd tracks the transcribe process directly. `-k` kills any
# stuck instance and starts fresh (safe: cache makes restart idempotent).
# Falls back to direct exec if the agent isn't loaded yet (pre-update install).
log "kicking off transcription"
if launchctl kickstart -k "gui/$(id -u)/$TRANSCRIBE_LABEL" >> "$LOG" 2>&1; then
  log "kickstart ok ($TRANSCRIBE_LABEL)"
elif [ -x "$TRANSCRIBE" ]; then
  log "kickstart failed; running directly (rerun install.sh to fix)"
  nohup "$TRANSCRIBE" >> "$LOG" 2>&1 &
  log "transcription pid=$!"
else
  log "transcribe-memos.py not found at $TRANSCRIBE — skipping"
fi
