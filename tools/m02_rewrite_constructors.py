#!/usr/bin/env python
"""M02 · 把 project.py 中显式 `EditProject(...)` 构造机械转换为
`replace(<receiver>, <仅变化字段>)`，使较新表（color_groups /
color_stack_versions / audio_sync_bindings / source_patch_overrides /
interpretation_overrides / project_snapshots）与 revision 不再被抹掉。

变换规则（AST 驱动，逐行文本替换）：
1. 找到 Call(func=Name('EditProject'))。
2. 删除形如 `field=<recv>.field` 的恒等关键字参数（recv ∈ {self, project,
   edit_project, proj}——与所在函数的首个工程参数一致）。
3. `EditProject(` 改写为 `replace(<recv>, `（模块级函数用 project 参数，
   方法用 self）。
4. 其余参数原样保留（变化字段、revision=新值、modified_at=now 等）。

用法：python tools/m02_rewrite_constructors.py <file.py> [--apply]
默认 dry-run 打印 diff；--apply 写回。
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path

RECEIVERS = {"self", "project", "edit_project", "proj"}


def receiver_for(tree: ast.Module, call_lineno: int) -> str | None:
    """定位包含该行的作用域，返回工程接收者名（self=方法 / project=模块函数）。"""
    best: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.lineno <= call_lineno <= (node.end_lineno or node.lineno)):
            if best is None or node.lineno >= getattr(best, "lineno", 0):
                best = node
    if not isinstance(best, ast.FunctionDef):
        return None
    args = best.args
    if args.args:
        first = args.args[0].arg
        if first == "self":
            return "self"
        for a in args.args:
            if a.arg in RECEIVERS:
                return a.arg
    return None


def rewrite(path: Path, apply: bool) -> bool:
    src = path.read_text()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)

    # 收集每个 EditProject( 调用的：起始行、恒等参数行集合
    edits: list[tuple[int, set[int], str]] = []  # (call_line, drop_lines, receiver)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "EditProject"):
            continue
        recv = receiver_for(tree, node.lineno)
        if recv is None:
            print(f"  line {node.lineno}: cannot determine receiver, skip", file=sys.stderr)
            continue
        drop: set[int] = set()
        for kw in node.keywords:
            if kw.arg is None or kw.value is None:
                continue
            v = kw.value
            if (isinstance(v, ast.Attribute) and v.attr == kw.arg
                    and isinstance(v.value, ast.Name) and v.value.id == recv):
                for ln in range(kw.lineno, (kw.end_lineno or kw.lineno) + 1):
                    drop.add(ln)
        edits.append((node.lineno, drop, recv))

    if not edits:
        print(f"  {path}: no EditProject() calls found")
        return False

    changed = False
    for call_line, drop, recv in edits:
        if lines[call_line - 1].count("EditProject(") != 1:
            print(f"  line {call_line}: ambiguous EditProject( line, skip", file=sys.stderr)
            continue
        lines[call_line - 1] = lines[call_line - 1].replace(
            "EditProject(", f"replace({recv}, ", 1)
        for ln in sorted(drop):
            # 只删除纯参数行（以防同一物理行有多个 token）
            if lines[ln - 1].strip().endswith((",", ")")):
                lines[ln - 1] = ""
                changed = True
        changed = True

    new_src = "".join(lines)
    if not changed:
        return False
    # 语法自检：产物必须可解析
    try:
        ast.parse(new_src)
    except SyntaxError as exc:
        print(f"  {path}: transformation produced syntax error: {exc}", file=sys.stderr)
        return False

    diff = list(difflib.unified_diff(
        src.splitlines(), new_src.splitlines(), lineterm="", n=1))
    print(f"--- {path} (first 80 diff lines of {len(diff)}) ---")
    for ln in diff[:80]:
        print(ln)
    if apply:
        path.write_text(new_src)
        print(f"  APPLIED -> {path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    any_change = False
    for f in args.files:
        any_change |= rewrite(Path(f), args.apply)
    return 0 if not apply else (0 if any_change else 1)


if __name__ == "__main__":
    raise SystemExit(main())
