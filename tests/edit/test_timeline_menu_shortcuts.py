"""E03: 菜单命令与 Split 快捷键。

审计实测：Edit/Timeline 的 Cut/Copy/Paste/Split/Insert/Overwrite/Append
七项 MenuActionDescriptor 因宿主回调不存在被 MenuBuilder 静默跳过；
``Ctrl+Shift+X``（Split 默认键）被 Ctrl+X 的 Cut 分支吞掉（旧条件只查
Control 不排 Shift）。

回归锁定：
- 七个宿主回调在 EditControllerMixin 上存在且可解析（MenuBuilder 的
  callback_resolver 语义 = getattr）；
- module.py 注册的 Timeline 菜单 descriptor 全部能在宿主上解析；
- timeline_view 的 Ctrl+X / Ctrl+D 分支排除 Shift。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.edit_controller import EditControllerMixin

_SRC = Path(__file__).resolve().parents[2] / "src"
_MODULE = _SRC / "features" / "edit" / "module.py"
_TIMELINE_VIEW = _SRC / "features" / "edit" / "ui" / "timeline_view.py"

EXPECTED_CALLBACKS = [
    "on_timeline_cut",
    "on_timeline_copy",
    "on_timeline_paste",
    "on_timeline_split",
    "on_timeline_insert",
    "on_timeline_overwrite",
    "on_timeline_append",
]


class TestHostCallbacksResolve:
    def test_seven_callbacks_exist_on_host(self) -> None:
        for name in EXPECTED_CALLBACKS:
            assert callable(getattr(EditControllerMixin, name, None)), name

    def test_all_edit_timeline_descriptors_resolve(self) -> None:
        """module.py 注册的 Edit/Timeline 菜单项不得再有无法解析的回调。"""
        text = _MODULE.read_text(encoding="utf-8")
        block = text[text.index("Timeline operations in the Edit menu"):]
        block = block[:block.index("Timeline navigation")]
        callback_names = set(re.findall(r'callback_name="(\w+)"', block))
        assert callback_names, "descriptor 解析失败"
        missing = [
            name for name in callback_names
            if not callable(getattr(EditControllerMixin, name, None))
        ]
        assert not missing, f"菜单回调缺失（会被 MenuBuilder 静默跳过）: {missing}"


class TestCutShortcutExcludesShift:
    def test_ctrl_x_branch_excludes_shift(self) -> None:
        lines = _TIMELINE_VIEW.read_text(encoding="utf-8").splitlines()
        branch = [line for line in lines
                  if "Key_X" in line and "ControlModifier" in line]
        assert branch, "未找到 Ctrl+X Cut 分支"
        assert "not modifiers & Qt.KeyboardModifier.ShiftModifier" in branch[0], (
            "Ctrl+X 必须排除 Shift（否则 Ctrl+Shift+X Split 键误触 Cut）"
        )

    def test_ctrl_d_branch_excludes_shift(self) -> None:
        lines = _TIMELINE_VIEW.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "Key_D" in line and "ControlModifier" in line:
                window = "\n".join(lines[i:i + 2])
                assert "not modifiers & Qt.KeyboardModifier.ShiftModifier" in window
                break
        else:
            raise AssertionError("未找到 Ctrl+D Duplicate 分支")


class TestMenuDelegationTargets:
    """Cut/Copy/Paste/Split 委托 TimelineView；Insert/Overwrite/Append
    委托 EditPage 源监视器桥——委托目标必须真实存在。"""

    def test_timeline_view_targets_exist(self) -> None:
        text = _TIMELINE_VIEW.read_text(encoding="utf-8")
        for method in ("_cut_selected_clips", "_copy_selected_clips",
                       "_paste_clips_at_playhead", "_split_all_at_playhead"):
            assert f"def {method}" in text, method

    def test_edit_page_bridge_targets_exist(self) -> None:
        page = _SRC / "features" / "edit" / "ui" / "edit_page.py"
        text = page.read_text(encoding="utf-8")
        for method in ("_on_timeline_source_insert", "_on_timeline_source_overwrite",
                       "_on_timeline_source_append"):
            assert f"def {method}" in text, method
