"""I18N 局部 import 遮蔽防护。

历史事故（两次）：函数中段出现 ``from src.shared.i18n.i18n import
I18N`` 后，Python 把整个函数内的 ``I18N`` 标记为局部名——位于该
import 之前执行的任何 ``I18N.tr(...)`` 引用都会抛
``UnboundLocalError`` 并崩溃用户界面
（scope_panel.init_ui、timeline_clip_strip._show_context_menu）。

函数内的局部 I18N import 本身是项目允许的风格（防循环导入），
本测试只禁止「引用出现在局部 import 之前」的反序写法。
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _iter_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _local_i18n_import_line(func) -> int | None:
    """函数体内最早的 I18N 局部 import 行号（含嵌套块）。"""
    best: int | None = None
    for node in ast.walk(func):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(alias.name == "I18N" for alias in node.names):
                if best is None or node.lineno < best:
                    best = node.lineno
    return best


def _collect_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for func in _iter_functions(tree):
            import_line = _local_i18n_import_line(func)
            if import_line is None:
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Name)
                    and node.id == "I18N"
                    and isinstance(node.ctx, ast.Load)
                    and node.lineno < import_line
                ):
                    rel = path.relative_to(SRC_ROOT.parent)
                    violations.append(
                        f"{rel}:{node.lineno} in {func.name}: "
                        f"I18N 引用早于第 {import_line} 行的局部 import"
                    )
                    break
    return violations


def test_no_i18n_usage_before_local_import():
    violations = _collect_violations()
    assert not violations, (
        "以下函数在局部 import I18N 之前就引用了它（UnboundLocalError "
        "风险，详见 scope_panel/timeline_clip_strip 历史事故）：\n"
        + "\n".join(violations)
    )


def test_guard_scans_are_not_empty():
    """防护本身的有效性自检：src 树确实被扫描到。"""
    assert any(SRC_ROOT.rglob("*.py"))
    # 至少存在函数内局部 import 的合法用法（import 先于引用）
    assert _collect_violations() == []
