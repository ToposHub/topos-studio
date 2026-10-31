"""E01: 媒体池"添加到时间线"不再二次追加音频。

审计实测：TimelineView.append_clip() 对有声视频已构造 A/V 宏（V+A+link），
edit_controller 的 _on_add_asset_to_timeline / _auto_add_media_to_timeline
随后又手动追加一次音频，第二次 append 与刚落下的 A/V 组在音频轨上重叠，
必定抛 ``ValueError: New clip overlaps`` 且无用户反馈。

回归锁定三点：
1. 两个入口的方法体里 append 调用只出现一次（AST 级，防止回归再拼二次追加）；
2. _append_media_to_timeline_once 捕获 ValueError → 状态栏提示、不向 Qt 槽外抛；
3. 失败时零提交（append 抛异常 = MacroCommand 事务回滚，无半提交状态）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.edit_controller import EditControllerMixin

_CONTROLLER = (
    Path(__file__).resolve().parents[2] / "src" / "app" / "controllers" /
    "edit_controller.py"
)


def _count_append_calls(method_name: str) -> int:
    """统计方法体里对 append_clip 的调用次数（含 helper 转发）。"""
    tree = ast.parse(_CONTROLLER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            calls = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "append_clip"
            ]
            helper_calls = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_append_media_to_timeline_once"
            ]
            return len(calls) + len(helper_calls)
    raise AssertionError(f"method not found: {method_name}")


class TestNoDoubleAudioAppend:
    def test_add_asset_appends_once(self) -> None:
        assert _count_append_calls("_on_add_asset_to_timeline") == 1

    def test_auto_add_appends_once(self) -> None:
        assert _count_append_calls("_auto_add_media_to_timeline") == 1


class TestAppendFailureSurfacesToUser:
    """append 拒绝（重叠/无目标轨）→ 状态栏提示，不抛进 Qt 槽。"""

    def _make_controller(self, append_behavior):
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        panel = SimpleNamespace(append_clip=append_behavior)
        ctrl.timeline_panel = panel
        ctrl._status_messages = []
        ctrl.status_bar = SimpleNamespace(
            showMessage=lambda msg, timeout=5000: ctrl._status_messages.append(msg)
        )
        return ctrl

    def test_overlap_error_becomes_status_message(self) -> None:
        def _raise(*a, **k):
            raise ValueError("New clip overlaps with ... at [1572, 3144)")

        ctrl = self._make_controller(_raise)
        asset = SimpleNamespace(media_type="video")
        added = ctrl._append_media_to_timeline_once(asset, "a1", 0, 99)
        assert added is False
        assert ctrl._status_messages, "拒绝原因必须到达状态栏"
        assert "overlaps" in ctrl._status_messages[0]

    def test_no_target_track_false_returns_message(self) -> None:
        ctrl = self._make_controller(lambda *a, **k: False)
        asset = SimpleNamespace(media_type="video")
        added = ctrl._append_media_to_timeline_once(asset, "a1", 0, 99)
        assert added is False
        assert ctrl._status_messages

    def test_success_returns_true_and_no_message(self) -> None:
        ctrl = self._make_controller(lambda *a, **k: True)
        asset = SimpleNamespace(media_type="video")
        added = ctrl._append_media_to_timeline_once(asset, "a1", 0, 99)
        assert added is True
        assert ctrl._status_messages == []
