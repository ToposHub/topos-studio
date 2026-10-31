#!/usr/bin/env python3
"""修复战役驱动：统计 REMEDIATION_PLAN.md 勾选进度；按 ID 翻转勾选并附 commit。

用法：
  python check_fixes.py                     # 打印分桶进度
  python check_fixes.py --mark AUD-0200,AUD-0501 --commit abc1234 [--note "..."]
  python check_fixes.py --mark-line "src/features/edit/ai_editing/" --commit abc1234   # E-rollup 行
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PLAN = BASE.parent / "REMEDIATION_PLAN.md"

SECTIONS = [
    ("A", "## 2.A", "## 2.B"),
    ("B", "## 2.B", "## 2.C"),
    ("C", "## 2.C", "## 2.D"),
    ("D", "## 2.D", "## 2.E"),
    ("E", "## 2.E", "## 2.F"),   # 含 E-rollup
    ("F", "## 2.F", "## 7"),
    ("S7", "## 7", "## 8"),
]


def stats() -> int:
    text = PLAN.read_text(encoding="utf-8")
    lines = text.splitlines()
    bounds = {}
    for name, start, _end in SECTIONS:
        for i, ln in enumerate(lines):
            if ln.startswith(start):
                bounds[name] = i
                break
    order = [n for n, _s, _e in SECTIONS if n in bounds]
    total_done = total_todo = 0
    for idx, name in enumerate(order):
        start = bounds[name] + 1
        end = bounds[order[idx + 1]] if idx + 1 < len(order) else len(lines)
        done = todo = 0
        for ln in lines[start:end]:
            if ln.startswith("- [x]"):
                done += 1
            elif ln.startswith("- [ ]"):
                todo += 1
        total_done += done
        total_todo += todo
        print(f"bucket {name}: done={done} pending={todo}")
    print(f"TOTAL: done={total_done} pending={total_todo}")
    return 0


def flip(ids: list[str], commit: str, note: str) -> int:
    text = PLAN.read_text(encoding="utf-8")
    missing = []
    for fid in ids:
        pat = re.compile(r"^- \[ \] \*\*" + re.escape(fid) + r"\*\*.*$", re.M)
        m = pat.search(text)
        if not m:
            missing.append(fid)
            continue
        line = m.group(0)
        suffix = f" (fixed: {commit})"
        if note:
            suffix = f" (fixed: {commit} {note})"
        text = text[: m.start()] + line.replace("- [ ]", "- [x]", 1) + suffix + text[m.end():]
    PLAN.write_text(text, encoding="utf-8")
    if missing:
        print(f"NOT FOUND (already fixed or absent): {', '.join(missing)}")
    print(f"marked {len(ids) - len(missing)} item(s) fixed @ {commit}")
    return 1 if missing and not ids else 0


def flip_line(sub: str, commit: str, note: str) -> int:
    text = PLAN.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.startswith("- [ ]") and sub in ln:
            suffix = f" (fixed: {commit})\n" if ln.endswith("\n") else f" (fixed: {commit})"
            if note:
                suffix = f" (fixed: {commit} {note})" + ("\n" if ln.endswith("\n") else "")
            lines[i] = ln.replace("- [ ]", "- [x]", 1).rstrip("\n") + suffix
            PLAN.write_text("".join(lines), encoding="utf-8")
            print(f"marked line containing {sub!r} @ {commit}")
            return 0
    print(f"no unchecked line contains {sub!r}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mark", help="comma-separated finding IDs")
    ap.add_argument("--mark-line", dest="mark_line", help="substring of a rollup line")
    ap.add_argument("--commit", default="")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    if not args.mark and not args.mark_line:
        return stats()
    if not args.commit:
        ap.error("--commit is required when marking")
    if args.mark:
        return flip([x.strip() for x in args.mark.split(",") if x.strip()], args.commit, args.note)
    return flip_line(args.mark_line, args.commit, args.note)


if __name__ == "__main__":
    sys.exit(main())
