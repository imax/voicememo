# voicememo

Auto-sync + transcription pipeline for a Sony IC Recorder on macOS.

## What it does

1. You plug in the recorder (mounts as `/Volumes/IC RECORDER`).
2. macOS launchd fires `sync-ic-recorder.sh` on the mount event.
3. The script `rsync`s new `.mp3`s from every `REC_FILE/FOLDER*/` (the Sony
   rolls over from `FOLDER01` to `FOLDER01_02` etc. as folders fill up) into
   the flat `~/Sony/Files/` dir.
4. It then kicks off `transcribe.py` via its own LaunchAgent (so the
   transcribe process survives the sync script exiting — a plain
   backgrounded child gets reaped with the sync job's process group).
5. Each new mp3 is transcribed once (via OpenAI's `gpt-4o-transcribe`, biased
   with your personal vocabulary) and cached under
   `~/Library/Caches/voicememo/transcripts/<basename>.txt`.
6. Long transcripts get a paragraph-split pass via `gpt-4o-mini` and land in
   `~/Library/Caches/voicememo/processed/<basename>.txt` (short ones are copied
   through).
7. New transcripts are spliced into `~/Sony/Memos/<Month YYYY>.md` by day
   (reverse-chronological), append-only: existing entries — including your
   manual edits — are left untouched (see
   [Editing notes](#editing-notes-append-only)). Paragraphs that open with a
   voice-command trigger (`хайлайт`, `туду`, …) are rendered specially — see
   [Voice commands](#voice-commands).

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
- `VOICEMEMO_REBUILD=1` — regenerate all month files + `Todos.md` from cache
  instead of appending. Discards manual edits; see
  [Editing notes](#editing-notes-append-only).

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

After editing, future recordings pick up the new vocab automatically.
Re-applying vocab to *existing* memos is more involved now that month files are
append-only: re-transcribing refreshes the cache but won't touch the already-
merged `.md`. To force a single memo through again, drop its raw transcript and
its manifest entry (and delete the stale copy from the month file so you don't
get a duplicate), then re-run:
```bash
base=<basename>   # e.g. 260507_0952
rm ~/Library/Caches/voicememo/transcripts/$base.txt ~/Library/Caches/voicememo/processed/$base.txt
python3 -c "import json,pathlib; p=pathlib.Path.home()/'Library/Caches/voicememo/merged.json'; d=json.load(p.open()); d['merged']=[x for x in d['merged'] if x!='$base']; json.dump(d,p.open('w'))"
# then remove that entry from ~/Sony/Memos/<Month>.md by hand, and:
~/bin/transcribe-memos.py
```
Or just rebuild everything with `VOICEMEMO_REBUILD=1` (discards manual edits).

## Paragraph splitting

Single-blob transcripts of long memos are hard to skim. After transcription,
texts longer than `VOICEMEMO_PARAGRAPH_MIN_CHARS` (default 400) get a second
pass through `gpt-4o-mini` with a strict prompt: insert paragraph breaks at
topic shifts, change zero words. The result lands in
`~/Library/Caches/voicememo/processed/`, separate from the raw cache so you
can blow away `processed/` and re-paragraph everything (free, no transcribe
API calls) after tweaking the prompt.

## Voice commands

You can shape how a paragraph renders by opening it with a trigger word — say
it out loud at the start of a thought while recording. The trigger is detected
per **paragraph** (so one memo can mix normal text, highlights, and todos), and
the trigger word itself is stripped from the output.

| Spoken opener | Effect |
|---|---|
| `хайлайт …` | Wraps the paragraph in `==…==` (Obsidian highlight) in the month file. |
| `туду …` / `задача …` / `марк …` / `запиши …` | Keeps the paragraph as normal text in the month file **and** collects it into `~/Sony/Memos/Todos.md`, reverse-chronological with date + time. |

The opener is matched case-insensitively, followed by a separator (space,
colon, dash, …). A word boundary guards against false hits — "Маркетинг" is not
treated as a `марк` todo. A bare trigger with no body after it (e.g. just
"Хайлайт") falls through as a normal paragraph.

Detection runs at **merge time** (when an entry is first appended to its month
file), not transcription time. Because month files are append-only (see
[Editing notes](#editing-notes-append-only)), changing the trigger list only
affects *future* recordings — already-merged entries keep their original
rendering. To re-apply new trigger rules to old memos, do a full rebuild
(`VOICEMEMO_REBUILD=1`), which discards manual edits in the regenerated files.
To change or extend the trigger words, edit `TRIGGER_RE` in `transcribe.py`.

## Editing notes (append-only)

The month files are **yours to edit** — fix a transcription, add a note,
reorganize. Normal runs never rewrite what's already there: each new recording
is spliced into the right day (newest-first) and existing entry bodies are left
verbatim. Only the whitespace between blocks is normalized.

How it knows what's already merged: a manifest at `$CACHE_BASE/merged.json`
tracks which transcripts have been written into the `.md` files. It's the
source of truth for "what's new", *not* the file contents — so:

- **Delete an entry** from a month file and it stays gone (it won't reappear).
- **Edit an entry's text** and your version survives every future run; the cache
  still holds the original, but it's never re-injected.
- The same applies to `Todos.md` — check items off or rewrite lines freely;
  only genuinely-new todos are appended.

The trade-off: the script can no longer retroactively re-render old entries
(e.g. after changing trigger rules or the paragraph-split prompt). When you
*do* want a clean regeneration from the cache, run with **`VOICEMEMO_REBUILD=1`**
— it rewrites every month file + `Todos.md` from scratch and resets the
manifest. **This discards manual edits in the regenerated files**, so it's an
explicit, opt-in escape hatch, not the default.

## File layout

```
$SONY_BASE/Files/                  copied mp3s          (default: ~/Documents/Sony/Files/)
$SONY_BASE/Memos/<Month YYYY>.md   monthly transcripts   (default: ~/Documents/Sony/Memos/)
$SONY_BASE/Memos/Todos.md          todos collected from voice commands (append-only)
$CACHE_BASE/transcripts/           per-mp3 raw transcripts (idempotency cache;
                                     default: ~/Library/Caches/voicememo/transcripts/)
$CACHE_BASE/processed/             paragraph-split versions; what merge reads
                                     (default: ~/Library/Caches/voicememo/processed/)
$CACHE_BASE/merged.json            which transcripts are already in the .md files
                                     (so manual edits aren't clobbered)
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

`CACHE_BASE` is split from `SONY_BASE` on purpose — see the iCloud lock note
under [macOS gotchas](#macos-gotchas).

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

# Re-transcribe everything from scratch (will re-spend on cloud).
# REBUILD regenerates the .md files and resets merged.json — without it the
# manifest would think everything's already merged and skip the deleted files.
rm -rf ~/Library/Caches/voicememo/transcripts/* ~/Library/Caches/voicememo/processed/* ~/Documents/Sony/Memos/*.md
VOICEMEMO_REBUILD=1 ~/bin/transcribe-memos.py

# Re-paragraph everything (cheap — only the post-process LLM, no transcribe).
# REBUILD is required to push the re-paragraphed text into the month files.
# WARNING: a rebuild discards manual edits in the regenerated month files.
rm -rf ~/Library/Caches/voicememo/processed/*
VOICEMEMO_REBUILD=1 ~/bin/transcribe-memos.py

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

## Tests

`test_transcribe.py` covers the append/merge logic — day insertion ordering,
manual-edit preservation, the bootstrap/incremental/rebuild paths, and the
Todos.md check-off round-trip. Zero dependencies, runs on stock `python3`;
each test isolates its output into a temp dir, so it never touches the vault.

```bash
./test_transcribe.py        # or: python3 test_transcribe.py
pytest test_transcribe.py   # also works if pytest is installed
```

**Keep them current.** When you change the merge/append/manifest logic in
`transcribe.py`, add or update a test in the same change and run the suite
before committing — it's fast and needs no setup. The month files are
user-edited data, so a regression here silently corrupts notes.

## macOS gotchas

- `~/Documents` is TCC-protected AND iCloud-synced when "Desktop & Documents
  Folders" sync is on. That's fine for the canonical `~/Documents/Sony/`
  layout (you get free off-machine backup of recordings + monthly notes),
  but two things follow from it:
  - **Full Disk Access on `/bin/bash`** is required so launchd-spawned bash
    can `rsync` into the TCC-protected folder. The transcribe LaunchAgent
    is routed through `/bin/bash` for the same reason. See [install](#install).
  - **The cache must live outside iCloud.** `~/Documents/Sony/.cache/` was
    the original location, but iCloud's `bird` daemon holds an exclusive
    advisory lock while inspecting each file's sync state. Mid-batch reads
    of the per-mp3 txt cache would fail with `OSError: [Errno 11] Resource
    deadlock avoided`, crashing the month-rebuild. Since 2026-05, the cache
    defaults to `~/Library/Caches/voicememo/` (overridable via `CACHE_BASE`
    in `config.sh`). The mp3s and `.md` monthly notes still live under
    `SONY_BASE` and still sync to iCloud — only the derived txt cache moved.
    `transcribe.py` also retries on EDEADLK as a belt-and-braces guard for
    anyone who points `CACHE_BASE` back into an iCloud-managed path.
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
