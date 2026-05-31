#!/usr/bin/env python3
"""Tests for transcribe.py — month-file append/merge logic and the manifest.

Zero dependencies: runs on stock /usr/bin/python3 (3.9) with no pytest needed.

    ./test_transcribe.py          # standalone, exits non-zero on failure
    python3 test_transcribe.py
    pytest test_transcribe.py     # also collectable if pytest is installed

Each test gets an isolated temp dir; transcribe's MEMOS_DIR / MERGED_MANIFEST
module globals are repointed there, so nothing touches the real vault.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcribe as t


def _fresh():
    """Isolate transcribe's output paths into a fresh temp dir; reset REBUILD."""
    d = Path(tempfile.mkdtemp(prefix="voicememo-test-"))
    t.MEMOS_DIR = d
    t.MERGED_MANIFEST = d / "merged.json"
    t.REBUILD = False
    return d


def _entry(date, time, text, segment=0):
    y, m, d = (int(x) for x in date.split("-"))
    h, mi = (int(x) for x in time.split(":"))
    return {"date": date, "time": time, "segment": segment,
            "sort_key": (y, m, d, h, mi, segment), "text": text}


# --- append_to_month -------------------------------------------------------

def test_create_new_month_newest_first():
    d = _fresh()
    t.append_to_month(2026, 5, [_entry("2026-05-09", "08:00", "Ранкова."),
                                _entry("2026-05-09", "19:30", "Вечірня.")])
    txt = (d / "May 2026.md").read_text()
    assert "## 9 травня" in txt
    assert txt.index("19:30") < txt.index("08:00"), "newest time on top"
    assert "Ранкова." in txt and "Вечірня." in txt


def test_append_same_day_inserts_in_order():
    d = _fresh()
    t.append_to_month(2026, 5, [_entry("2026-05-09", "08:00", "A"),
                                _entry("2026-05-09", "19:30", "B")])
    t.append_to_month(2026, 5, [_entry("2026-05-09", "12:00", "C")])
    txt = (d / "May 2026.md").read_text()
    assert txt.index("19:30") < txt.index("12:00") < txt.index("08:00")
    assert txt.count("## 9 травня") == 1, "no duplicate day header"


def test_append_newer_day_on_top():
    d = _fresh()
    t.append_to_month(2026, 5, [_entry("2026-05-09", "08:00", "A")])
    t.append_to_month(2026, 5, [_entry("2026-05-11", "09:00", "B")])
    txt = (d / "May 2026.md").read_text()
    assert txt.index("## 11 травня") < txt.index("## 9 травня")
    assert txt.count("## 11 травня") == 1 and txt.count("## 9 травня") == 1


def test_late_older_transcript_sorts_to_bottom_of_day():
    d = _fresh()
    t.append_to_month(2026, 5, [_entry("2026-05-09", "08:00", "A")])
    t.append_to_month(2026, 5, [_entry("2026-05-09", "06:30", "earlier")])
    seg = (d / "May 2026.md").read_text()
    seg = seg[seg.index("## 9 травня"):]
    assert seg.index("08:00") < seg.index("06:30")


def test_manual_body_edit_survives_append():
    d = _fresh()
    p = d / "May 2026.md"
    t.append_to_month(2026, 5, [_entry("2026-05-09", "12:00", "Обідня думка.")])
    p.write_text(p.read_text().replace("Обідня думка.", "Обідня (ВИПРАВЛЕНО)."))
    t.append_to_month(2026, 5, [_entry("2026-05-09", "21:00", "Пізній.")])
    txt = p.read_text()
    assert "Обідня (ВИПРАВЛЕНО)." in txt, "manual edit must be preserved verbatim"
    assert "Пізній." in txt


def test_serialize_round_trip_is_idempotent():
    d = _fresh()
    t.append_to_month(2026, 5, [_entry("2026-05-09", "08:00", "A"),
                                _entry("2026-05-11", "09:00", "B\n\nдругий абзац")])
    p = d / "May 2026.md"
    once = p.read_text()
    pre, days = t.parse_month_file(p, 2026)
    assert once == t.serialize_month(pre, days), "parse->serialize must be stable"


# --- triggers / rendering --------------------------------------------------

def test_render_highlight_and_todo():
    _fresh()
    todos = []
    rendered = t.render_entry_text(
        "Хайлайт головна ідея.\n\nЗвичайний абзац.\n\nЗадача: подзвонити Олені.",
        "2026-05-12", "10:00", todos)
    assert "==Головна ідея.==" in rendered
    assert "Подзвонити Олені." in rendered and "Задача" not in rendered
    assert len(todos) == 1 and todos[0]["text"] == "Подзвонити Олені."


# --- Todos.md --------------------------------------------------------------

def test_todos_append_and_checkoff_preserved():
    d = _fresh()
    p = d / "Todos.md"
    t.append_to_todos([{"date": "2026-05-12", "time": "10:00", "text": "Подзвонити."}])
    assert "## 12 травня (2026-05-12)" in p.read_text()
    # user checks it off
    p.write_text(p.read_text().replace("- **10:00** Подзвонити.",
                                       "- [x] **10:00** Подзвонити."))
    t.append_to_todos([{"date": "2026-05-12", "time": "14:00", "text": "Квитки."},
                       {"date": "2026-05-15", "time": "08:00", "text": "Звіт."}])
    txt = p.read_text()
    assert "- [x] **10:00** Подзвонити." in txt, "check-off must survive"
    assert "Квитки." in txt and "Звіт." in txt
    assert txt.index("## 15 травня") < txt.index("## 12 травня")


# --- reconcile() end-to-end ------------------------------------------------

def _all(entries):
    return {bn: e for bn, e in entries}


def test_reconcile_bootstrap_is_noop():
    d = _fresh()
    p = d / "May 2026.md"
    original = "## 9 травня\n\n**08:00**\n\nКурований вручну.\n"
    p.write_text(original)
    entries = {"260509_0800": _entry("2026-05-09", "08:00", "Кеш-версія.")}
    mw = t.reconcile(dict(entries), None, {"260509_0800"})
    assert mw == [], "bootstrap touches no months"
    assert t.load_manifest() == {"260509_0800"}, "manifest seeded"
    assert p.read_text() == original, "file left byte-identical"


def test_reconcile_incremental_appends_only_new():
    d = _fresh()
    p = d / "May 2026.md"
    p.write_text("## 9 травня\n\n**08:00**\n\nКурований вручну.\n")
    entries = {"260509_0800": _entry("2026-05-09", "08:00", "Кеш-версія.")}
    t.reconcile(dict(entries), None, {"260509_0800"})  # bootstrap
    entries["260509_2100"] = _entry("2026-05-09", "21:00", "Туду: купити каву.")
    entries["260601_0930"] = _entry("2026-06-01", "09:30", "Новий місяць.")
    mw = t.reconcile(dict(entries), t.load_manifest(), set(entries))
    txt = p.read_text()
    assert "May 2026" in mw and "June 2026" in mw
    assert "Курований вручну." in txt and "Кеш-версія." not in txt
    assert txt.index("21:00") < txt.index("08:00")
    assert "Купити каву." in txt and "Туду" not in txt
    assert (d / "June 2026.md").exists()
    assert len(t.load_manifest()) == 3
    assert "Купити каву." in (d / "Todos.md").read_text()


def test_reconcile_rerun_changes_nothing():
    d = _fresh()
    p = d / "May 2026.md"
    p.write_text("## 9 травня\n\n**08:00**\n\nЗапис.\n")
    entries = {"260509_0800": _entry("2026-05-09", "08:00", "Запис.")}
    t.reconcile(dict(entries), None, {"260509_0800"})
    before = p.read_text()
    mw = t.reconcile(dict(entries), t.load_manifest(), set(entries))
    assert mw == [] and p.read_text() == before


def test_reconcile_rebuild_regenerates_from_cache():
    d = _fresh()
    p = d / "May 2026.md"
    p.write_text("## 9 травня\n\n**08:00**\n\nРучна правка.\n")
    entries = {"260509_0800": _entry("2026-05-09", "08:00", "Кеш-версія.")}
    t.reconcile(dict(entries), None, {"260509_0800"})  # seed manifest
    t.REBUILD = True
    t.reconcile(dict(entries), t.load_manifest(), set(entries))
    txt = p.read_text()
    assert "Кеш-версія." in txt, "rebuild injects cache text"
    assert "Ручна правка" not in txt, "rebuild discards manual edit (documented)"


def _run_standalone():
    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"OK   | {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print(f"FAIL | {name}: {exc!r}")
    print(f"\n{passed} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
