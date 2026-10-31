#!/usr/bin/env python3
"""i18n 硬编码防回归检查（P3 防回归，2026-10-28）。

AST 扫描 src/ 的 UI 字面量 setter/构造器调用（与首轮清单
docs/i18n_leak_inventory_2026-10-28.md 同口径：5 setter + 11 构造器 +
addAction 首参；豁免空串/纯数字符号/时间格式/占位符），对存量残留走
基线棘轮（docs/i18n_literal_baseline.json，含理由）：
- 命中基线的残留不报警；
- **新增**字面量 → 非零退出（CI 红）。

用法：python tools/check_i18n_literals.py [--update-baseline]
（--update-baseline 仅在人工核验新残留可豁免后使用，随提交说明登记理由。）
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "docs" / "i18n_literal_baseline.json"

SETTERS = {
    "setText", "setWindowTitle", "setToolTip", "setPlaceholderText", "setTitle",
    "setTabText", "setStatusTip", "setWhatsThis", "setLabelText", "setSuffix",
}
CTOR_WIDGETS = {
    "QPushButton", "QLabel", "QCheckBox", "QRadioButton", "QGroupBox",
    "QMenu", "QAction", "QToolButton", "QCommandLinkButton", "QListWidgetItem",
    "QTableWidgetItem", "QTreeWidgetItem",
}

EXEMPT_RE = re.compile(r"[\d\s%:./\-+×xX°…—–]*")
PLACEHOLDER_RE = re.compile(r"(\{\}|%\w?|<[absp]>)+")


def is_exempt(s: str) -> bool:
    if not s.strip():
        return True
    if EXEMPT_RE.fullmatch(s):
        return True
    if PLACEHOLDER_RE.fullmatch(s):
        return True
    if len(s.strip()) <= 1 and not re.search(r"[A-Za-z\u4e00-\u9fff]", s):
        return True
    return False


def scan(root: Path) -> list[str]:
    hits: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if "/tests/" in str(py) or "/examples/" in str(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            label = arg = None
            if isinstance(fn, ast.Attribute) and fn.attr in SETTERS:
                label, arg = fn.attr, (node.args[0] if node.args else None)
            elif isinstance(fn, ast.Attribute) and fn.attr == "addAction" and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    label, arg = "addAction", a0
                else:
                    continue
            elif isinstance(fn, ast.Name) and fn.id in CTOR_WIDGETS:
                label, arg = fn.id + "()", (node.args[0] if node.args else None)
            elif (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                  and fn.value.id in CTOR_WIDGETS):
                label, arg = fn.value.id + "()", (node.args[0] if node.args else None)
            if label and arg is not None and isinstance(arg, ast.Constant) \
                    and isinstance(arg.value, str) and not is_exempt(arg.value):
                hits.append(f"{py.relative_to(REPO)}:{node.lineno}:{arg.value}")
    return hits


def main() -> int:
    update = "--update-baseline" in sys.argv
    hits = scan(REPO / "src")
    baseline = set(json.loads(BASELINE.read_text(encoding="utf-8"))) \
        if BASELINE.exists() else set()
    new = [h for h in hits if h not in baseline]
    gone = baseline - set(hits)

    if update:
        BASELINE.write_text(json.dumps(sorted(hits), indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        print(f"baseline updated: {len(hits)} entries (was {len(baseline)})")
        return 0

    for h in new:
        print(f"NEW i18n literal: {h}")
    if gone:
        print(f"info: {len(gone)} baseline entries no longer hit (可 --update-baseline 收紧)")
    print(f"scan={len(hits)} baseline={len(baseline)} new={len(new)}")
    if new:
        print("FAIL: 存在新增 UI 硬编码字面量——请改用 I18N.tr(key, 原文) 并补 10 语言词条")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
