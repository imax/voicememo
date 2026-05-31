#!/usr/bin/env python3
"""
Transcribe Sony IC Recorder voice memos with OpenAI's audio API.

- Reads mp3s from $SONY_BASE/Files/ (default ~/Documents/Sony/Files/).
- Caches per-file transcripts under
  $CACHE_BASE/transcripts/<basename>.txt (default ~/Library/Caches/voicememo/)
  so re-runs only transcribe new files.
- Merges per-month transcripts into $SONY_BASE/Memos/<Month YYYY>.md.

Sony filename convention: YYMMDD_HHMM[_NN].mp3  (NN = split-segment index).

Backend: OpenAI /v1/audio/transcriptions with `gpt-4o-transcribe`. Requires
an API key. Idempotent: a run with no internet/key just exits; the next run
picks up everything that wasn't cached.

API key resolution (in order):
  1. $OPENAI_API_KEY environment variable
  2. ~/.config/openai/api_key file (chmod 600)
"""

# launchd-spawned scripts use /usr/bin/python3 (Apple's, currently 3.9), which
# doesn't support PEP 604 `X | Y` annotations. This makes annotations lazy.
from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HOME = Path.home()
LOG_FILE = HOME / "Library" / "Logs" / "transcribe-memos.log"
KEY_FILE = HOME / ".config" / "openai" / "api_key"
CONFIG_FILE = HOME / ".config" / "voicememo" / "config.sh"
_DEFAULT_VOCAB = HOME / ".config" / "voicememo" / "vocabulary.md"


def _load_shared_config() -> dict:
    """Parse the shared sh-style config (KEY="value" lines, $HOME / ~ expansion)."""
    config = {}
    if not CONFIG_FILE.exists():
        return config
    for raw in CONFIG_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        config[k.strip()] = os.path.expandvars(os.path.expanduser(v))
    return config


_cfg = _load_shared_config()
# SONY_BASE is the only path shared with sync-ic-recorder.sh. Subdirs derive.
BASE_DIR = Path(_cfg.get("SONY_BASE", str(HOME / "Documents" / "Sony")))
SRC_DIR = BASE_DIR / "Files"
MEMOS_DIR = BASE_DIR / "Memos"
# Cache lives outside BASE_DIR by default so iCloud (which manages ~/Documents)
# doesn't lock these derived files mid-run. The .txt files here are
# regeneratable from the mp3s, so no reason to sync them anywhere. Override
# with CACHE_BASE in ~/.config/voicememo/config.sh if you want.
CACHE_BASE = Path(_cfg.get("CACHE_BASE", str(HOME / "Library" / "Caches" / "voicememo")))
VOCAB_FILE = Path(os.environ.get("VOICEMEMO_VOCAB_FILE",
                                 _cfg.get("VOCAB_FILE", str(_DEFAULT_VOCAB))))
CACHE_DIR = CACHE_BASE / "transcripts"
PROCESSED_DIR = CACHE_BASE / "processed"
# Which transcripts are already spliced into the month files. Decouples the .md
# (yours to edit) from the cache (transcription source of truth): a deleted
# entry stays deleted, a manually-fixed body is never overwritten.
MERGED_MANIFEST = CACHE_BASE / "merged.json"

API_URL = "https://api.openai.com/v1/audio/transcriptions"
CHAT_URL = "https://api.openai.com/v1/chat/completions"
API_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
POSTPROCESS_MODEL = os.environ.get("OPENAI_POSTPROCESS_MODEL", "gpt-4o-mini")
LANG = os.environ.get("VOICEMEMO_LANG", "uk")

# Texts shorter than this (chars) skip the paragraph-split post-process —
# they're already one thought, no point spending an API call.
PARAGRAPH_MIN_CHARS = int(os.environ.get("VOICEMEMO_PARAGRAPH_MIN_CHARS", "400"))
SKIP_POSTPROCESS = bool(os.environ.get("VOICEMEMO_SKIP_POSTPROCESS"))

# Append-only month files: normal runs splice new recordings into the existing
# <Month>.md without rewriting what's there, so your manual edits survive. Set
# VOICEMEMO_REBUILD=1 to force a full regeneration from cache (the old behavior)
# — useful to reset a file or after bulk cache surgery.
REBUILD = bool(os.environ.get("VOICEMEMO_REBUILD"))

NAME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(?:_(\d+))?$")

# Voice-command triggers at the start of a paragraph.
# "хайлайт ідея X" -> ==Ідея X== in the month file.
# "туду/задача/марк/запиши X" -> regular paragraph in month, plus an entry in Todos.md.
# \b stops "марк" from eating "Маркетинг"; case-insensitive matches "Хайлайт", "ХАЙЛАЙТ", etc.
# Separator class is "+" so "Хайлайт" alone (no body) falls through as a normal paragraph.
TRIGGER_RE = re.compile(
    r"^\s*(?P<word>хайлайт|туду|задача|марк|запиши)\b[\s:.\-,–—]+",
    re.IGNORECASE,
)
HIGHLIGHT_WORDS = {"хайлайт"}
TODO_WORDS = {"туду", "задача", "марк", "запиши"}

# Structural anchors for parsing month files / Todos.md back into entries.
# Only these (headers + time markers) are interpreted; everything else is body
# text preserved verbatim, so manual edits survive an append.
DAY_HEADER_RE = re.compile(r"^##\s+(\d{1,2})\s+(\S+)")
ENTRY_MARKER_RE = re.compile(r"^\*\*(\d{2}):(\d{2})(?: \(cont\. (\d+)\))?\*\*\s*$")
TODO_DAY_RE = re.compile(r"^##\s+.*\((\d{4})-(\d{2})-(\d{2})\)")
TODO_TIME_RE = re.compile(r"^\s*-\s+\*\*(\d{2}):(\d{2})\*\*")


def classify_paragraph(para: str) -> tuple[str, str]:
    """Return (kind, body). kind ∈ {'highlight', 'todo', 'normal'}; body has the trigger stripped."""
    m = TRIGGER_RE.match(para)
    if not m:
        return "normal", para
    body = para[m.end():].lstrip()
    if not body:
        return "normal", para
    body = body[0].upper() + body[1:]
    word = m.group("word").lower()
    if word in HIGHLIGHT_WORDS:
        return "highlight", body
    return "todo", body


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def read_text_robust(path: Path, attempts: int = 12, delay: float = 1.0) -> str:
    """Read a text file, retrying on transient OS-level lock contention.

    ~/Documents lives on iCloud Drive, and `bird` (the iCloud daemon)
    holds an exclusive advisory lock while it inspects a file's sync
    state. Reads in that window fail with OSError(EDEADLK) — "Resource
    deadlock avoided". Observed in practice: the lock is mostly held for
    <1s, but a re-scan of the .cache subdir during our glob loop can
    extend it past 5s. A generous linear backoff (~78s total) rides it
    out instead of killing the whole month-rebuild.
    """
    for i in range(attempts):
        try:
            return path.read_text()
        except OSError as e:
            if e.errno != errno.EDEADLK or i == attempts - 1:
                raise
            log(f"read retry {i+1}/{attempts} on {path.name}: {e}")
            time.sleep(delay * (i + 1))
    # unreachable: last iteration either returns or re-raises
    return ""


def get_api_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip() or None
    return None


def load_vocabulary() -> str:
    """Read vocabulary.md and flatten into a comma-separated prompt hint.

    The model uses the prompt as a vocabulary bias for decoding — proper nouns,
    English brand names, and domain terms become much more likely to surface
    with their canonical spelling instead of phonetic Cyrillic mangling.
    """
    if not VOCAB_FILE.exists():
        return ""
    terms: list[str] = []
    in_vocab = False  # skip the preamble; vocab starts at the first `##`
    for raw in VOCAB_FILE.read_text().splitlines():
        line = raw.strip()
        if line.startswith("##"):
            in_vocab = True
            continue
        if not in_vocab:
            continue
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        for token in line.split(","):
            t = token.strip().rstrip(".")
            if t:
                terms.append(t)
    if not terms:
        return ""
    # Seed sentence helps the model treat the list as vocabulary rather than
    # content to transcribe. Ukrainian framing matches the audio language.
    return "Можливі імена, бренди, місця та терміни: " + ", ".join(terms) + "."


def _write_curl_config(api_key: str, json_body: bool = False) -> str:
    """Write a chmod-600 curl config with the auth header. Caller unlinks it."""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".curlrc") as cfg:
        os.chmod(cfg.name, 0o600)
        cfg.write(f'header = "Authorization: Bearer {api_key}"\n')
        if json_body:
            cfg.write('header = "Content-Type: application/json"\n')
        cfg.write("silent\nshow-error\nfail\n")
        return cfg.name


def parse_name(stem: str):
    m = NAME_RE.match(stem)
    if not m:
        return None
    yy, mm, dd, hh, mn, seg = m.groups()
    year = 2000 + int(yy)
    return {
        "date": f"{year:04d}-{int(mm):02d}-{int(dd):02d}",
        "time": f"{int(hh):02d}:{int(mn):02d}",
        "segment": int(seg) if seg else 0,
        "sort_key": (year, int(mm), int(dd), int(hh), int(mn), int(seg or 0)),
    }


def transcribe_via_openai(mp3: Path, out_txt: Path, api_key: str, vocab_prompt: str) -> bool:
    cfg_path = _write_curl_config(api_key)
    try:
        cmd = [
            "curl", "-K", cfg_path,
            "-X", "POST", API_URL,
            "-F", f"file=@{mp3}",
            "-F", f"model={API_MODEL}",
            "-F", f"language={LANG}",
            "-F", "response_format=text",
        ]
        if vocab_prompt:
            cmd += ["-F", f"prompt={vocab_prompt}"]
        log(f"openai transcribe: {mp3.name}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            log(f"timeout: {mp3.name}")
            return False
    finally:
        os.unlink(cfg_path)

    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-500:]
        log(f"openai failed rc={result.returncode}: {tail}")
        return False
    text = result.stdout.strip()
    if not text:
        log(f"openai returned empty body for {mp3.name}")
        return False
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text(text + "\n")
    return True




def split_paragraphs(text: str, api_key: str) -> str:
    """Insert paragraph breaks into a single-blob transcript via a chat model.

    Strict instruction: do not change a single word. The model can be greedy
    about "fixing" punctuation if you let it, so we always keep the raw cache
    as ground truth and treat this output as a presentation layer.
    """
    system = (
        "You receive a transcript of a Ukrainian voice memo, returned as one "
        "long paragraph. Split it into paragraphs by inserting a blank line "
        "between paragraphs wherever the topic, scene, or train of thought "
        "shifts. Do NOT change any words. Do NOT fix spelling, punctuation, "
        "or grammar. Do NOT add or remove anything. Return ONLY the original "
        "text with paragraph breaks inserted — no preamble, no commentary, "
        "no quotes around the output."
    )
    payload = {
        "model": POSTPROCESS_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    }
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as jf:
        os.chmod(jf.name, 0o600)
        json.dump(payload, jf, ensure_ascii=False)
        json_path = jf.name
    cfg_path = _write_curl_config(api_key, json_body=True)
    try:
        cmd = [
            "curl", "-K", cfg_path,
            "-X", "POST", CHAT_URL,
            "-d", f"@{json_path}",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            log("paragraph split: timeout")
            return text
    finally:
        os.unlink(json_path)
        os.unlink(cfg_path)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-500:]
        log(f"paragraph split failed rc={result.returncode}: {tail}")
        return text
    try:
        resp = json.loads(result.stdout)
        content = resp["choices"][0]["message"]["content"].strip()
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        log(f"paragraph split parse failed: {exc}")
        return text
    return content or text


def materialize_processed(raw_txt: Path, processed_txt: Path, api_key: str | None) -> None:
    """Produce the final, paragraph-broken text from a raw transcript.

    Short memos (single thought) are copied through verbatim. Longer ones go
    through the chat model for paragraph breaks. The result is cached so
    later runs are free.
    """
    text = raw_txt.read_text().strip()
    if (
        api_key
        and not SKIP_POSTPROCESS
        and len(text) >= PARAGRAPH_MIN_CHARS
        and "\n\n" not in text  # already paragraphed (re-runs, manual edits)
    ):
        text = split_paragraphs(text, api_key)
        log(f"paragraph split: {raw_txt.name} ({len(text)} chars)")
    processed_txt.parent.mkdir(parents=True, exist_ok=True)
    processed_txt.write_text(text.strip() + "\n")


MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Ukrainian month names in genitive case ("9 травня", "1 січня").
UK_MONTHS_GEN = [
    "", "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]


UK_MONTH_TO_NUM = {name: i for i, name in enumerate(UK_MONTHS_GEN) if name}


def format_uk_date(iso_date: str) -> str:
    _, mm, dd = iso_date.split("-")
    return f"{int(dd)} {UK_MONTHS_GEN[int(mm)]}"


def parse_uk_day_header(line: str, year: int):
    """'## 9 травня' -> (year, 5, 9). None if the header isn't our day format."""
    m = DAY_HEADER_RE.match(line)
    if not m:
        return None
    mon = UK_MONTH_TO_NUM.get(m.group(2))
    if not mon:
        return None
    return (year, mon, int(m.group(1)))


def render_entry_text(text: str, date: str, time: str, todos: list) -> str:
    """Apply paragraph-level triggers. Highlight paras get wrapped in ==..==; todo
    paras stay as normal text but also append a record to `todos` for Todos.md."""
    paragraphs = text.split("\n\n")
    rendered = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        kind, body = classify_paragraph(para)
        if kind == "highlight":
            rendered.append(f"=={body}==")
        elif kind == "todo":
            rendered.append(body)
            todos.append({"date": date, "time": time, "text": body})
        else:
            rendered.append(para)
    return "\n\n".join(rendered)


def rebuild_month(year: int, month: int, entries: list, todos: list) -> Path:
    """Regenerate a whole <Month YYYY>.md from cache (the --rebuild path only).

    Reverse-chronological: newest day on top, newest recording within a day on
    top. Day header: ## 9 травня. Recording header: **HH:MM**. This clobbers any
    manual edits, so normal runs use append_to_month instead.
    """
    out = MEMOS_DIR / f"{MONTH_NAMES[month]} {year}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    entries = list(reversed(entries))

    lines = []
    current_date = None
    for e in entries:
        if e["date"] != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(f"## {format_uk_date(e['date'])}")
            lines.append("")
            current_date = e["date"]
        suffix = f" (cont. {e['segment']})" if e["segment"] else ""
        lines.append(f"**{e['time']}{suffix}**")
        lines.append("")
        lines.append(render_entry_text(e["text"], e["date"], e["time"], todos))
        lines.append("")
    out.write_text("\n".join(lines).lstrip("\n"))
    return out


# --- Append-only month files -------------------------------------------------
#
# Normal runs never rewrite an existing <Month>.md. We parse it into day
# sections (anchored on the stable `## DD month` headers) and recording blocks
# (anchored on `**HH:MM**` markers), splice in only the new recordings, then
# re-serialize. Recording bodies are kept verbatim — only inter-block
# whitespace is normalized and blocks are re-sorted into reverse-chronological
# order — so your manual edits to the text survive untouched.

def parse_month_file(path: Path, year: int):
    """Parse a month file into (preamble, [day]). Each day is
    {key:(y,m,d)|None, header:str, prelude:[str], entries:[{tkey, lines:[str]}]}.
    Unrecognized lines become body/prelude text and are preserved on re-write.
    """
    preamble: list = []
    days: list = []
    if not path.exists():
        return preamble, days
    cur_day = None
    cur_entry = None
    for raw in read_text_robust(path).splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            cur_day = {"key": parse_uk_day_header(line, year), "header": line,
                       "prelude": [], "entries": []}
            days.append(cur_day)
            cur_entry = None
            continue
        if cur_day is None:
            preamble.append(line)
            continue
        m = ENTRY_MARKER_RE.match(line)
        if m:
            cur_entry = {"tkey": (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)),
                         "lines": [line]}
            cur_day["entries"].append(cur_entry)
            continue
        if cur_entry is not None:
            cur_entry["lines"].append(line)
        else:
            cur_day["prelude"].append(line)
    return preamble, days


def serialize_month(preamble: list, days: list) -> str:
    days = sorted(days, key=lambda d: d["key"] or (0, 0, 0), reverse=True)
    out: list = [l for l in preamble if l.strip()]
    for d in days:
        if out:
            out.append("")
        out.append(d["header"])
        out.append("")
        prelude = "\n".join(d["prelude"]).strip()
        if prelude:
            out.append(prelude)
            out.append("")
        for e in sorted(d["entries"], key=lambda e: e["tkey"], reverse=True):
            out.append(e["lines"][0])
            out.append("")
            body = "\n".join(e["lines"][1:]).strip()
            if body:
                out.append(body)
                out.append("")
    return "\n".join(out).strip("\n") + "\n"


def append_to_month(year: int, month: int, entries: list) -> Path:
    """Splice already-rendered `entries` into <Month>.md by day, leaving every
    other day and body untouched. Entries carry pre-rendered `text`."""
    out = MEMOS_DIR / f"{MONTH_NAMES[month]} {year}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    preamble, days = parse_month_file(out, year)
    by_key = {d["key"]: d for d in days if d["key"]}
    for e in entries:
        key = tuple(e["sort_key"][:3])
        day = by_key.get(key)
        if day is None:
            day = {"key": key, "header": f"## {format_uk_date(e['date'])}",
                   "prelude": [], "entries": []}
            days.append(day)
            by_key[key] = day
        suffix = f" (cont. {e['segment']})" if e["segment"] else ""
        h, mi = e["time"].split(":")
        day["entries"].append({
            "tkey": (int(h), int(mi), e["segment"]),
            "lines": [f"**{e['time']}{suffix}**"] + e["text"].strip().split("\n"),
        })
    out.write_text(serialize_month(preamble, days))
    return out


# --- Todos.md (same day-grouped, append-only model) --------------------------

def parse_todos_file(path: Path):
    """Parse Todos.md into (title, [day]). day: {key, header, items:[{tkey, line}]}.
    Item lines are kept verbatim so check-offs / manual edits survive."""
    title = "# Todos"
    days: list = []
    if not path.exists():
        return title, days
    cur = None
    for raw in read_text_robust(path).splitlines():
        line = raw.rstrip()
        m = TODO_DAY_RE.match(line)
        if m:
            cur = {"key": (int(m.group(1)), int(m.group(2)), int(m.group(3))),
                   "header": line, "items": []}
            days.append(cur)
            continue
        if line.startswith("# ") and not line.startswith("##"):
            title = line
            continue
        if cur is not None and line.lstrip().startswith("- "):
            tm = TODO_TIME_RE.match(line)
            tkey = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
            cur["items"].append({"tkey": tkey, "line": line})
    return title, days


def serialize_todos(title: str, days: list) -> str:
    days = sorted(days, key=lambda d: d["key"], reverse=True)
    out = [title, ""]
    for d in days:
        out.append(d["header"])
        out.append("")
        for it in sorted(d["items"], key=lambda i: i["tkey"], reverse=True):
            out.append(it["line"])
        out.append("")
    return "\n".join(out).strip("\n") + "\n"


def append_to_todos(new_todos: list) -> None:
    if not new_todos:
        return
    out = MEMOS_DIR / "Todos.md"
    title, days = parse_todos_file(out)
    by_key = {d["key"]: d for d in days}
    for t in new_todos:
        key = (int(t["date"][0:4]), int(t["date"][5:7]), int(t["date"][8:10]))
        day = by_key.get(key)
        if day is None:
            day = {"key": key, "header": f"## {format_uk_date(t['date'])} ({t['date']})",
                   "items": []}
            days.append(day)
            by_key[key] = day
        h, mi = t["time"].split(":")
        day["items"].append({"tkey": (int(h), int(mi)),
                             "line": f"- **{t['time']}** {t['text']}"})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialize_todos(title, days))


def write_todos_full(todos: list) -> None:
    """Full regeneration of Todos.md (the --rebuild path only)."""
    out = MEMOS_DIR / "Todos.md"
    if not todos:
        if out.exists():
            out.write_text("# Todos\n\n_(порожньо)_\n")
        return
    todos = sorted(todos, key=lambda t: (t["date"], t["time"]), reverse=True)
    lines = ["# Todos", ""]
    current_date = None
    for t in todos:
        if t["date"] != current_date:
            if current_date is not None:
                lines.append("")
            lines.append(f"## {format_uk_date(t['date'])} ({t['date']})")
            lines.append("")
            current_date = t["date"]
        lines.append(f"- **{t['time']}** {t['text']}")
    out.write_text("\n".join(lines) + "\n")


# --- Merge-state manifest ----------------------------------------------------

def load_manifest():
    """Set of basenames already spliced into month files, or None if there is
    no manifest yet (first run under the append-only model)."""
    if not MERGED_MANIFEST.exists():
        return None
    try:
        data = json.loads(read_text_robust(MERGED_MANIFEST))
        return set(data.get("merged", []))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log(f"manifest read failed ({exc}); treating as empty")
        return set()


def save_manifest(merged) -> None:
    MERGED_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MERGED_MANIFEST.with_name(MERGED_MANIFEST.name + ".tmp")
    tmp.write_text(json.dumps({"merged": sorted(merged)}, ensure_ascii=False))
    tmp.replace(MERGED_MANIFEST)


def reconcile(all_entries: dict, manifest, preexisting: set) -> list:
    """Turn the processed-transcript cache into month files + Todos.md.

    `all_entries`: {basename: entry} for every non-empty processed transcript.
    `manifest`: set of basenames already in the .md files, or None on the first
    append-only run. `preexisting`: basenames present before this run started.

    Returns the list of month labels that were written/touched. Updates the
    manifest. Under VOICEMEMO_REBUILD everything is regenerated from scratch;
    otherwise only genuinely-new transcripts are spliced in (append-only).
    """
    if REBUILD:
        by_month: dict = defaultdict(list)
        for e in all_entries.values():
            by_month[(e["sort_key"][0], e["sort_key"][1])].append(e)
        todos: list = []
        months_written = []
        for (year, month), entries in by_month.items():
            entries.sort(key=lambda e: e["sort_key"])
            rebuild_month(year, month, entries, todos)
            months_written.append(f"{MONTH_NAMES[month]} {year}")
        write_todos_full(todos)
        save_manifest(set(all_entries))
        log(f"rebuild: regenerated {len(months_written)} month file(s), "
            f"{len(todos)} todo(s); manifest reset to {len(all_entries)} entries")
        return months_written

    # Append-only. `merged` = what's assumed already in the .md files; on the
    # first run (no manifest) that's whatever existed on disk before this run.
    merged = manifest if manifest is not None else preexisting
    new = [e for bn, e in all_entries.items() if bn not in merged]
    by_month: dict = defaultdict(list)
    for e in new:
        by_month[(e["sort_key"][0], e["sort_key"][1])].append(e)
    todos: list = []
    months_written = []
    for (year, month), entries in by_month.items():
        entries.sort(key=lambda e: e["sort_key"])
        for e in entries:
            e["text"] = render_entry_text(e["text"], e["date"], e["time"], todos)
        append_to_month(year, month, entries)
        months_written.append(f"{MONTH_NAMES[month]} {year}")
    append_to_todos(todos)
    save_manifest(set(merged) | set(all_entries))
    if manifest is None:
        log(f"bootstrap: assumed {len(merged)} entr(ies) already in files; "
            f"appended {len(new)} new, {len(todos)} todo(s)")
    else:
        log(f"incremental: appended {len(new)} new entr(ies) to "
            f"{len(months_written)} month file(s), {len(todos)} new todo(s)")
    return months_written


def main() -> int:
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    MEMOS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now()
    api_key = get_api_key()
    if not api_key:
        log("ERROR: no OpenAI key — paste one into ~/.config/openai/api_key "
            "or export OPENAI_API_KEY. Idempotent — re-run after fixing.")
        return 1
    vocab_prompt = load_vocabulary()
    vocab_tag = f"vocab={len(vocab_prompt)}c" if vocab_prompt else "vocab=none"
    log(f"start pid={os.getpid()} backend=openai-{API_MODEL} {vocab_tag}")

    mp3s = sorted(SRC_DIR.rglob("*.mp3"))
    pending = [m for m in mp3s
               if not ((CACHE_DIR / (m.stem + ".txt")).exists()
                       and (CACHE_DIR / (m.stem + ".txt")).stat().st_size > 0)]
    log(f"queue: {len(pending)} new of {len(mp3s)} mp3s")
    # Snapshot which transcripts already exist before we materialize new ones.
    # On the first append-only run (no manifest) we assume these are already in
    # the month files and seed the manifest from them, leaving the files alone.
    preexisting = {p.stem for p in PROCESSED_DIR.glob("*.txt") if p.stat().st_size > 0}
    new_count = 0
    for mp3 in mp3s:
        raw = CACHE_DIR / (mp3.stem + ".txt")
        processed = PROCESSED_DIR / (mp3.stem + ".txt")
        if not (raw.exists() and raw.stat().st_size > 0):
            if not transcribe_via_openai(mp3, raw, api_key, vocab_prompt):
                continue
            new_count += 1
        if not (processed.exists() and processed.stat().st_size > 0):
            materialize_processed(raw, processed, api_key)

    all_entries: dict[str, dict] = {}
    for txt in PROCESSED_DIR.glob("*.txt"):
        info = parse_name(txt.stem)
        if not info:
            continue
        text = read_text_robust(txt).strip()
        if not text:
            continue
        all_entries[txt.stem] = {
            "date": info["date"],
            "time": info["time"],
            "segment": info["segment"],
            "sort_key": info["sort_key"],
            "text": text,
        }

    months_written = reconcile(all_entries, load_manifest(), preexisting)

    elapsed = (datetime.now() - started_at).total_seconds()
    log(f"done in {elapsed:.0f}s: {new_count} new transcript(s), {len(months_written)} month file(s)")

    if new_count > 0:
        latest = sorted(months_written)[-2:]
        msg = f"{new_count} new memo(s) → {', '.join(latest)}"
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "Memos transcribed"'],
            check=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
