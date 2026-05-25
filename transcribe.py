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
VOCAB_FILE = Path(os.environ.get("VOICEMEMO_VOCAB_FILE",
                                 HOME / ".config" / "voicememo" / "vocabulary.md"))


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
CACHE_DIR = CACHE_BASE / "transcripts"
PROCESSED_DIR = CACHE_BASE / "processed"

API_URL = "https://api.openai.com/v1/audio/transcriptions"
CHAT_URL = "https://api.openai.com/v1/chat/completions"
API_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
POSTPROCESS_MODEL = os.environ.get("OPENAI_POSTPROCESS_MODEL", "gpt-4o-mini")
LANG = os.environ.get("VOICEMEMO_LANG", "uk")

# Texts shorter than this (chars) skip the paragraph-split post-process —
# they're already one thought, no point spending an API call.
PARAGRAPH_MIN_CHARS = int(os.environ.get("VOICEMEMO_PARAGRAPH_MIN_CHARS", "400"))
SKIP_POSTPROCESS = bool(os.environ.get("VOICEMEMO_SKIP_POSTPROCESS"))

NAME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(?:_(\d+))?$")


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


def format_uk_date(iso_date: str) -> str:
    _, mm, dd = iso_date.split("-")
    return f"{int(dd)} {UK_MONTHS_GEN[int(mm)]}"


def merge_month(year: int, month: int, entries: list) -> Path:
    # Filename "May 2026.md". No H1 — filename is the title.
    # Reverse-chronological: newest day on top, newest recording within a day on top.
    # Day header: ## 9 травня. Recording header: **HH:MM**.
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
        lines.append(e["text"].strip())
        lines.append("")
    out.write_text("\n".join(lines).lstrip("\n"))
    return out


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

    by_month: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for txt in PROCESSED_DIR.glob("*.txt"):
        info = parse_name(txt.stem)
        if not info:
            continue
        text = read_text_robust(txt).strip()
        if not text:
            continue
        year, month = info["sort_key"][0], info["sort_key"][1]
        by_month[(year, month)].append({
            "date": info["date"],
            "time": info["time"],
            "segment": info["segment"],
            "sort_key": info["sort_key"],
            "text": text,
        })

    months_written = []
    for (year, month), entries in by_month.items():
        entries.sort(key=lambda e: e["sort_key"])
        merge_month(year, month, entries)
        months_written.append(f"{MONTH_NAMES[month]} {year}")

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
