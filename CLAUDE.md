# voicememo — notes for Claude

Personal pipeline: syncs a Sony IC recorder and transcribes voice memos into
monthly Markdown notes. Read `README.md` for the full design.

## Tests — run and maintain them

- `test_transcribe.py` is the suite for the month-file append/merge logic (day
  insertion ordering, manual-edit preservation, bootstrap/incremental/rebuild
  paths, Todos.md check-off round-trip). Zero dependencies; runs on stock
  `/usr/bin/python3` (3.9) and is pytest-collectable.
- **Run it before committing any change to `transcribe.py`:** `./test_transcribe.py`
- **When you change merge / append / manifest behavior, add or update a test in
  the same commit.** This logic edits user-owned notes, so a regression
  silently corrupts data.
- **Don't write "tests pass" in a commit message unless the tests are committed
  and you actually ran them.** (Throwaway scripts in `/tmp` don't count.)

## Invariants — don't regress these

- **Month files (`$SONY_BASE/Memos/*.md`) and `Todos.md` are append-only and
  user-edited.** Normal runs splice in only new recordings (tracked by
  `$CACHE_BASE/merged.json`) and must never rewrite or reorder existing entry
  bodies. The only path allowed to regenerate a file from cache is
  `VOICEMEMO_REBUILD=1`, and it's explicitly opt-in because it discards manual
  edits.
- **The cache is the transcription source of truth; the `.md` is the user's.**
  Never re-inject cache text over an entry that's already in a month file.

## Runtime

- launchd spawns this under Apple's `/usr/bin/python3` (currently 3.9). Keep
  runtime code 3.9-compatible (the `from __future__ import annotations` at the
  top makes `X | Y` annotations lazy — don't rely on PEP 604 at runtime).
