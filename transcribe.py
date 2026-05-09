#!/usr/bin/env python3
"""
Transcribe Sony IC Recorder voice memos with OpenAI's audio API.

- Reads mp3s from ~/Sony/Files/.
- Caches per-file transcripts under ~/Sony/.cache/transcripts/<basename>.txt
  so re-runs only transcribe new files.
- Merges per-day transcripts into ~/Sony/Memos/YYYY-MM-DD.md, sorted by time.

Sony filename convention: YYMMDD_HHMM[_NN].mp3  (NN = split-segment index).

Backend: OpenAI /v1/audio/transcriptions with `gpt-4o-transcribe` model
(highest quality for non-English as of 2026). Falls through to local
openai-whisper CLI if no API key is configured.

API key resolution (in order):
  1. $OPENAI_API_KEY environment variable
  2. ~/.config/openai/api_key file (chmod 600)

Idempotent. Safe to run on a schedule or after every sync.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HOME = Path.home()
SRC_DIR = HOME / "Sony" / "Files"
MEMOS_DIR = HOME / "Sony" / "Memos"
CACHE_DIR = HOME / "Sony" / ".cache" / "transcripts"
LOG_FILE = HOME / "Library" / "Logs" / "transcribe-memos.log"
KEY_FILE = HOME / ".config" / "openai" / "api_key"

API_URL = "https://api.openai.com/v1/audio/transcriptions"
API_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
LANG = os.environ.get("WHISPER_LANG", "uk")
LOCAL_WHISPER_BIN = shutil.which("whisper")
LOCAL_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")

NAME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(?:_(\d+))?$")


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def get_api_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip() or None
    return None


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


def transcribe_via_openai(mp3: Path, out_txt: Path, api_key: str) -> bool:
    # Use a curl config file to keep the auth header out of `ps` output.
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".curlrc") as cfg:
        os.chmod(cfg.name, 0o600)
        cfg.write(f'header = "Authorization: Bearer {api_key}"\n')
        cfg.write("silent\n")
        cfg.write("show-error\n")
        cfg.write("fail\n")
        cfg_path = cfg.name
    try:
        cmd = [
            "curl", "-K", cfg_path,
            "-X", "POST", API_URL,
            "-F", f"file=@{mp3}",
            "-F", f"model={API_MODEL}",
            "-F", f"language={LANG}",
            "-F", "response_format=text",
        ]
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


def transcribe_via_local(mp3: Path, out_txt: Path) -> bool:
    if not LOCAL_WHISPER_BIN:
        log("no local whisper binary on PATH (brew install openai-whisper)")
        return False
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            LOCAL_WHISPER_BIN, str(mp3),
            "--language", LANG,
            "--model", LOCAL_MODEL,
            "--output_dir", tmp,
            "--output_format", "txt",
            "--verbose", "False",
        ]
        log(f"local transcribe: {mp3.name}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            log(f"timeout: {mp3.name}")
            return False
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip()[-500:]
            log(f"local whisper failed rc={result.returncode}: {tail}")
            return False
        produced = Path(tmp) / (mp3.stem + ".txt")
        if not produced.exists():
            log(f"local whisper produced no .txt for {mp3.name}")
            return False
        shutil.move(str(produced), str(out_txt))
    return True


def transcribe_one(mp3: Path, out_txt: Path, api_key: str | None) -> bool:
    if api_key:
        return transcribe_via_openai(mp3, out_txt, api_key)
    return transcribe_via_local(mp3, out_txt)


def merge_day(date: str, entries: list) -> Path:
    out = MEMOS_DIR / f"{date}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {date}", ""]
    for e in entries:
        suffix = f" (cont. {e['segment']})" if e["segment"] else ""
        lines.append(f"## {e['time']}{suffix}")
        lines.append("")
        lines.append(e["text"].strip())
        lines.append("")
    out.write_text("\n".join(lines) + "\n")
    return out


def main() -> int:
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    MEMOS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    api_key = get_api_key()
    log(f"backend: {'openai-' + API_MODEL if api_key else 'local-' + LOCAL_MODEL}")

    mp3s = sorted(SRC_DIR.rglob("*.mp3"))
    new_count = 0
    for mp3 in mp3s:
        cached = CACHE_DIR / (mp3.stem + ".txt")
        if cached.exists() and cached.stat().st_size > 0:
            continue
        if transcribe_one(mp3, cached, api_key):
            new_count += 1

    by_day: dict[str, list[dict]] = defaultdict(list)
    for txt in CACHE_DIR.glob("*.txt"):
        info = parse_name(txt.stem)
        if not info:
            continue
        text = txt.read_text().strip()
        if not text:
            continue
        by_day[info["date"]].append({
            "time": info["time"],
            "segment": info["segment"],
            "sort_key": info["sort_key"],
            "text": text,
        })

    days_written = []
    for date, entries in by_day.items():
        entries.sort(key=lambda e: e["sort_key"])
        merge_day(date, entries)
        days_written.append(date)

    log(f"done: {new_count} new transcript(s), {len(days_written)} day file(s)")

    if new_count > 0:
        latest = sorted(days_written)[-3:]
        msg = f"{new_count} new memo(s) → {', '.join(latest)}"
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "Memos transcribed"'],
            check=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
