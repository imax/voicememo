# voicememo

Auto-sync + transcription pipeline for a Sony IC Recorder on macOS.

## What it does

1. You plug in the recorder (mounts as `/Volumes/IC RECORDER`).
2. macOS launchd fires `sync-ic-recorder.sh` on the mount event.
3. The script `rsync`s new `.mp3`s from `REC_FILE/FOLDER01/` to `~/Sony/Files/`.
4. It then kicks off `transcribe.py` in the background.
5. Each new mp3 is transcribed once (via OpenAI's `gpt-4o-transcribe`) and
   cached under `~/Sony/.cache/transcripts/<basename>.txt`.
6. All transcripts for a given day get merged into `~/Sony/Memos/YYYY-MM-DD.md`,
   sorted by recording time.

You get two macOS notifications: "IC Recorder synced" right after copy,
"Memos transcribed" once the API responses come back.

## Install

On a fresh Mac:

```bash
cd <repo>/voicememo
./install.sh
```

Then two manual steps the installer can't do:

1. **Save your OpenAI key** to `~/.config/openai/api_key` (single line, no
   quotes). The file is created with `chmod 600`.
2. **Grant Full Disk Access to `/bin/bash`** so launchd-spawned bash can read
   `/Volumes/<removable>`. Deep-link:
   ```bash
   open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
   ```
   Add `/bin/bash` (Cmd+Shift+G to type the path).

## Backend

Default: **OpenAI `gpt-4o-transcribe`** via `/v1/audio/transcriptions`. Best
quality for non-English (Ukrainian works very well). ~$0.006/min audio.

Fallback (no API key): **local `openai-whisper`** with `large-v3`. Same model
family as cloud whisper-1, slightly older than gpt-4o-transcribe but still
strong on Ukrainian. Free, offline, slower.

The script picks based on whether a key is present; switch by emptying or
filling `~/.config/openai/api_key`.

Tweak via env vars:
- `OPENAI_TRANSCRIBE_MODEL` — defaults to `gpt-4o-transcribe`. Try `whisper-1`
  for cheaper, `gpt-4o-mini-transcribe` for cheaper still.
- `WHISPER_LANG` — defaults to `uk`.
- `WHISPER_MODEL` — local-fallback model, defaults to `large-v3`.

## File layout

```
~/Sony/Files/                  copied mp3s
~/Sony/Memos/YYYY-MM-DD.md     daily merged transcripts
~/Sony/.cache/transcripts/     per-mp3 raw transcripts (idempotency cache)
~/.config/openai/api_key       OpenAI key, chmod 600 (gitignored, never in repo)
~/bin/sync-ic-recorder.sh      symlink → repo
~/bin/transcribe-memos.py      symlink → repo
~/Library/LaunchAgents/com.maxua.sync-ic-recorder.plist
~/Library/Logs/sync-ic-recorder.{log,out.log,err.log}
~/Library/Logs/transcribe-memos.log
```

## Sony filename convention

Recordings come off the device as `YYMMDD_HHMM[_NN].mp3`:
- `260507_0952.mp3` → 2026-05-07 at 09:52
- `260507_0952_01.mp3` → continuation segment of the same recording

`transcribe.py` parses this to date-bucket and time-sort entries.

## Obsidian integration (optional)

Symlink the memos directory into your vault so daily transcripts show up
alongside other notes:

```bash
ln -s "$HOME/Sony/Memos" "$HOME/vaults/main/Memos"
```

`~/Sony/Memos/` stays the canonical location; the vault just sees through the
symlink. Obsidian indexes and searches the contents normally. If your vault
is git-tracked, decide whether to commit the symlink (portable across Macs
with the same paths) or add `Memos` to the vault's `.gitignore`.

## Manual operations

```bash
# Force-run the sync (e.g. for testing)
~/bin/sync-ic-recorder.sh

# Force-run transcription only
~/bin/transcribe-memos.py

# Re-transcribe everything from scratch (will re-spend on cloud)
rm -rf ~/Sony/.cache/transcripts/* ~/Sony/Memos/*.md
~/bin/transcribe-memos.py

# Reload the LaunchAgent after editing the plist
PLIST=~/Library/LaunchAgents/com.maxua.sync-ic-recorder.plist
launchctl unload "$PLIST" && launchctl load "$PLIST"

# Tail logs while testing
tail -f ~/Library/Logs/sync-ic-recorder.log ~/Library/Logs/transcribe-memos.log
```

## macOS gotchas

- `~/Documents` and `~/Desktop` are TCC-protected AND iCloud-synced when
  "Desktop & Documents Folders" is on. Don't put `Sony/` there — recordings
  would silently upload to iCloud and bash would hit "Operation not
  permitted" without Full Disk Access. We use `~/Sony/` precisely to dodge
  both problems.
- `/Volumes/<removable>` is TCC-protected. Without FDA on `/bin/bash`,
  launchd-spawned scripts will see "Operation not permitted" trying to read
  the recorder.

## Security notes

- The OpenAI key lives only in `~/.config/openai/api_key` (mode 600). The
  repo `.gitignore` excludes anything that could leak it.
- The transcribe script passes the key to `curl` via a `-K` config file
  (also chmod 600, in `$TMPDIR`, deleted on exit) so the key never appears
  in `ps`.
- If the key was ever pasted into a shell or AI chat history, rotate it at
  https://platform.openai.com/api-keys.
