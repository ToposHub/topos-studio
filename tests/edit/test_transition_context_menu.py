"""E10: 转场右键菜单。

审计实测：
- "Add Transition Before..."（左边缘）只建菜单、结果处理从不检查 → 点击无效；
- "Add Transition (Dip To Color)" 与 Cross Dissolve 发完全相同的
  ``("right")`` 载荷 → Dip 实际走默认 Cross Dissolve。

回归锁定：
- add_transition_requested 载荷含显式 transition_type（4 参）；
- 左边缘菜单项有结果处理分支（源码断言）；
- Dip To Color 发 dip_to_color；_on_add_transition_requested 透传类型；
- 左边缘请求路由到左邻居（cut point 的 left_clip_id = prev）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import re

from src.features.edit.ui.timeline_items import ClipItem

_ITEMS = (
    Path(__file__).resolve().parents[2] / "src" / "features" / "edit" /
    "ui" / "timeline_items.py"
)
_TIMELINE_VIEW = _ITEMS.parent / "timeline_view.py"


class TestSignalPayload:
    def test_signal_declares_transition_type_param(self) -> None:
        text = _ITEMS.read_text(encoding="utf-8")
        assert re.search(
            r"add_transition_requested\s*=\s*pyqtSignal\(str, str, str, str\)",
            text,
        ), "载荷必须为 (clip_id, track_id, edge, transition_type)"


class TestLeftEdgeHandled:
    def test_left_edge_result_branch_exists(self) -> None:
        text = _ITEMS.read_text(encoding="utf-8")
        assert "is_near_left_edge and not self._is_track_locked" in text
        assert '"left", "cross_dissolve"' in text

    def test_dip_to_color_payload_distinct(self) -> None:
        text = _ITEMS.read_text(encoding="utf-8")
        assert '"right", "dip_to_color"' in text


class TestHandlerPassesTransitionType:
    def test_handler_signature_has_transition_type(self) -> None:
        import inspect
        from src.features.edit.ui.timeline_view import TimelineView
        sig = inspect.signature(TimelineView._on_add_transition_requested)
        assert "transition_type" in sig.parameters
        assert sig.parameters["transition_type"].default == "cross_dissolve"

    def test_left_edge_routes_to_left_neighbor(self) -> None:
        """edge="left"：cut point 的左片 = 前邻居，右片 = 自身。"""
        from src.features.edit.ui.timeline_view import TimelineView
        view = TimelineView.__new__(TimelineView)
        view._timeline = SimpleNamespace()  # 只验证路由，不触发创建

        left_clip = SimpleNamespace(clip_id="prev")
        clip = SimpleNamespace(clip_id="cur")
        track = SimpleNamespace(
            get_clip_by_id=lambda cid: clip if cid == "cur" else None,
            get_adjacent_clips=lambda cid: (left_clip, None),
        )
        view._timeline.get_track_by_id = lambda tid: track
        calls: list[dict] = []
        view.add_transition_at_cut_point = lambda **kw: calls.append(kw)

        view._on_add_transition_requested("cur", "v1", "left", "dip_to_color")
        assert calls == [{
            "track_id": "v1",
            "left_clip_id": "prev",
            "right_clip_id": "cur",
            "transition_type": "dip_to_color",
        }]
