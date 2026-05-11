# voicememo

Auto-sync + transcription pipeline for a Sony IC Recorder on macOS.

## What it does

1. You plug in the recorder (mounts as `/Volumes/IC RECORDER`).
2. macOS launchd fires `sync-ic-recorder.sh` on the mount event.
3. The script `rsync`s new `.mp3`s from `REC_FILE/FOLDER01/` to `~/Sony/Files/`.
4. It then kicks off `transcribe.py` via its own LaunchAgent (so the
   transcribe process survives the sync script exiting — a plain
   backgrounded child gets reaped with the sync job's process group).
5. Each new mp3 is transcribed once (via OpenAI's `gpt-4o-transcribe`, biased
   with your personal vocabulary) and cached under
   `~/Sony/.cache/transcripts/<basename>.txt`.
6. Long transcripts get a paragraph-split pass via `gpt-4o-mini` and land in
   `~/Sony/.cache/processed/<basename>.txt` (short ones are copied through).
7. All processed transcripts for a given month get merged into
   `~/Sony/Memos/<Month YYYY>.md`, reverse-chronological.

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

**OpenAI `gpt-4o-transcribe`** via `/v1/audio/transcriptions`. Best quality
for non-English (Ukrainian works very well). ~$0.006/min audio.

No local/offline fallback. If the API is unreachable or the key is missing,
the run logs an error and exits — the next kick picks up everything that
wasn't cached, so a transient outage self-heals on the next sync.

Tweak via env vars:
- `OPENAI_TRANSCRIBE_MODEL` — defaults to `gpt-4o-transcribe`. Try
  `gpt-4o-mini-transcribe` for cheaper.
- `OPENAI_POSTPROCESS_MODEL` — chat model used for paragraph splitting.
  Defaults to `gpt-4o-mini` (~$0.0001 per long memo).
- `VOICEMEMO_LANG` — input language hint, defaults to `uk`.
- `VOICEMEMO_VOCAB_FILE` — override path to vocabulary.md.
- `VOICEMEMO_PARAGRAPH_MIN_CHARS` — texts shorter than this skip the
  paragraph-split step. Default `400`.
- `VOICEMEMO_SKIP_POSTPROCESS=1` — disable paragraph-splitting entirely.

## Vocabulary (transcription quality)

`gpt-4o-transcribe` handles Ukrainian prose well but has no idea who your
kids are, what your projects are called, or what niche local platforms you
mention. Hence "Маршалокриптоавтосінка" instead of "Marshall crypto auto
sync", or "джині" instead of "Djinni".

The API's `prompt` parameter takes a vocabulary hint that biases decoding.
We keep it in a plain Markdown file at `~/.config/voicememo/vocabulary.md`
(seeded from `vocabulary.md.example`). Group words under any `##` headings;
everything before the first heading is treated as preamble.

**Only user-specific terms belong here** — names of people, niche brands,
project codenames, local places. Don't add global brands, major cities, or
generic English jargon: the model already knows them, and every general
term displaces a useful one (the prompt has a ~200-word effective ceiling).

After editing, future recordings pick up the new vocab automatically. To
re-apply vocab to existing memos, delete their raw transcript:
```bash
rm ~/Documents/Sony/.cache/transcripts/<basename>.txt
~/bin/transcribe-memos.py
```

## Paragraph splitting

Single-blob transcripts of long memos are hard to skim. After transcription,
texts longer than `VOICEMEMO_PARAGRAPH_MIN_CHARS` (default 400) get a second
pass through `gpt-4o-mini` with a strict prompt: insert paragraph breaks at
topic shifts, change zero words. The result lands in
`~/Sony/.cache/processed/`, separate from the raw cache so you can blow away
processed/ and re-paragraph everything (free, no transcribe API calls) after
tweaking the prompt.

## File layout

```
$SONY_BASE/Files/                  copied mp3s          (default: ~/Documents/Sony/Files/)
$SONY_BASE/Memos/<Month YYYY>.md   monthly transcripts   (default: ~/Documents/Sony/Memos/)
$SONY_BASE/.cache/transcripts/     per-mp3 raw transcripts (idempotency cache)
$SONY_BASE/.cache/processed/       paragraph-split versions; what merge reads
~/.config/voicememo/config.sh      shared config (sourced by sync.sh + transcribe.py)
~/.config/voicememo/vocabulary.md  vocab hint passed to the transcription API
~/.config/openai/api_key           OpenAI key, chmod 600 (gitignored, never in repo)
~/bin/sync-ic-recorder.sh          symlink → repo
~/bin/transcribe-memos.py          symlink → repo
~/Library/LaunchAgents/com.maxua.sync-ic-recorder.plist
~/Library/LaunchAgents/com.maxua.transcribe-memos.plist
~/Library/Logs/sync-ic-recorder.{log,out.log,err.log}
~/Library/Logs/transcribe-memos.{log,out.log,err.log}
```

## Config file

Both scripts read `~/.config/voicememo/config.sh` for the shared `SONY_BASE`
path. Seeded by `install.sh` from `config.sh.example`. To relocate
recordings, edit one line — both scripts pick it up on the next run.

```bash
SONY_BASE="$HOME/Documents/Sony"   # or ~/Sony, or wherever
```

`~/Documents` puts recordings + transcripts under iCloud (free off-machine
backup) but requires Full Disk Access for `/bin/bash` so launchd-spawned
rsync can write into the TCC-protected folder. Plain `~/Sony` avoids both.

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
rm -rf ~/Sony/.cache/transcripts/* ~/Sony/.cache/processed/* ~/Sony/Memos/*.md
~/bin/transcribe-memos.py

# Re-paragraph everything (cheap — only the post-process LLM, no transcribe)
rm -rf ~/Sony/.cache/processed/*
~/bin/transcribe-memos.py

# Reload both LaunchAgents after editing a plist (easiest: just rerun install)
./install.sh

# Tail logs while testing
tail -f ~/Library/Logs/sync-ic-recorder.log ~/Library/Logs/transcribe-memos.log

# If transcription seems stuck, check launchd's view + crash logs
launchctl print "gui/$(id -u)/com.maxua.transcribe-memos" | head -40
tail -20 ~/Library/Logs/transcribe-memos.err.log

# Force-kick transcription (kills any running instance and restarts)
launchctl kickstart -k "gui/$(id -u)/com.maxua.transcribe-memos"
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
