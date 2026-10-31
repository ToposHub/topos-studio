"""B5: 时间线内嵌快捷键迁移测试（步骤 2 纯注册 + 步骤 3 匹配层）。

覆盖：
- action 清单完整性（TIMELINE_KEY_ACTIONS vs ShortcutManager 注册表 diff 为空）
- matches_key_event：默认键匹配、修饰键区分（A vs Shift+R vs Ctrl+A）、
  用户改键后新键生效旧键失效
- 迁移后 TimelineView 工具键仍可用（冒烟）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.ui.timeline_shortcuts import (  # noqa: E402
    TIMELINE_KEY_ACTIONS,
    matches_key_event,
    register_timeline_shortcuts,
)

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import QEvent

    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")


@pytest.fixture
def manager():
    sm = __import__(
        "src.shared.ui.shortcut_manager", fromlist=["ShortcutManager"]
    ).ShortcutManager()
    # 独立实例，避免污染全局单例
    register_timeline_shortcuts(sm)
    return sm


def _key_event(key: Qt.Key, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers)


class TestRegistration:
    def test_all_actions_registered(self, manager) -> None:
        """清单 vs 注册表 diff 为空（计划验收）。"""
        for action in TIMELINE_KEY_ACTIONS:
            assert manager.get_action(action.action_id) is not None, action.action_id
            assert manager.get_shortcut(action.action_id) == action.default_key

    def test_no_conflicts_with_defaults(self, manager) -> None:
        """默认键值互不冲突（同为无修饰单键的 action 各不相同）。"""
        seen: dict = {}
        for action in TIMELINE_KEY_ACTIONS:
            key = action.default_key
            assert key not in seen or "+" in key, (key, action.action_id)
            seen[key] = action.action_id


class TestMatchesKeyEvent:
    def test_plain_key_matches(self, manager, monkeypatch) -> None:
        monkeypatch.setattr(
            type(manager), "instance", classmethod(lambda cls: manager))
        event = _key_event(Qt.Key.Key_A)
        assert matches_key_event(event, "edit.timeline.tool.select") is True
        assert matches_key_event(event, "edit.timeline.tool.trim") is False

    def test_modifier_distinguished(self, manager, monkeypatch) -> None:
        monkeypatch.setattr(
            type(manager), "instance", classmethod(lambda cls: manager))
        # R 无修饰 = ripple；Shift+R = rate_stretch
        assert matches_key_event(
            _key_event(Qt.Key.Key_R), "edit.timeline.tool.ripple") is True
        assert matches_key_event(
            _key_event(Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier),
            "edit.timeline.tool.rate_stretch") is True
        # Ctrl+A（全选）不误匹配 select 工具（A 无修饰）
        assert matches_key_event(
            _key_event(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier),
            "edit.timeline.tool.select") is False

    def test_rebind_takes_effect(self, manager, monkeypatch) -> None:
        """改键后：新键生效、旧键失效（计划回归清单核心项）。"""
        monkeypatch.setattr(
            type(manager), "instance", classmethod(lambda cls: manager))
        manager.set_shortcut("edit.timeline.tool.blade", "X")
        assert manager.get_shortcut("edit.timeline.tool.blade") == "X"
        assert matches_key_event(
            _key_event(Qt.Key.Key_X), "edit.timeline.tool.blade") is True
        assert matches_key_event(
            _key_event(Qt.Key.Key_B), "edit.timeline.tool.blade") is False


class TestViewToolKeysStillWork:
    def _view(self):
        from src.features.edit.ui.timeline_view import TimelineView
        from src.features.edit.core.timeline_v2 import Rational, TimelineSequence

        view = TimelineView()
        view.load_timeline(TimelineSequence(fps=Rational(24, 1)))
        return view

    def test_default_blade_key_switches_tool(self, qapp, manager, monkeypatch) -> None:
        from src.features.edit.ui.timeline_items import EditTool

        monkeypatch.setattr(
            type(manager), "instance", classmethod(lambda cls: manager))
        view = self._view()
        event = _key_event(Qt.Key.Key_B)
        view.keyPressEvent(event)
        assert view._active_tool == EditTool.BLADE

    def test_rebind_changes_tool_key(self, qapp, manager, monkeypatch) -> None:
        """改键 B→X：旧键无效、新键触发剃刀（计划回归清单）。"""
        from src.features.edit.ui.timeline_items import EditTool

        monkeypatch.setattr(
            type(manager), "instance", classmethod(lambda cls: manager))
        manager.set_shortcut("edit.timeline.tool.blade", "X")
        view = self._view()
        view.set_tool(EditTool.SELECT)
        view.keyPressEvent(_key_event(Qt.Key.Key_B))
        assert view._active_tool == EditTool.SELECT  # 旧键失效
        view.keyPressEvent(_key_event(Qt.Key.Key_X))
        assert view._active_tool == EditTool.BLADE  # 新键生效
