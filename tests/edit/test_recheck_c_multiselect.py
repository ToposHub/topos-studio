"""复查批次 C 回归：R12 no-op 历史 / R16 多选原子写入。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.1
（K12）与 §3.2（RI-05）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.edit.core.clip_params import TransformParams
from src.features.edit.core.commands import (
    MacroCommand,
    MoveClipCommand,
    UpdateClipParamCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100,
          transform: TransformParams | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"a-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=300,
        transform=transform or TransformParams(),
    )


def _timeline(v_locked: bool = False) -> TimelineSequence:
    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        is_locked=v_locked,
    ))
    t = t.with_clip_added("v1", _clip("c1", "v1", 0, 100,
                                      TransformParams(translate_x=10.0)))
    t = t.with_clip_added("v1", _clip("c2", "v1", 200, 100,
                                      TransformParams(translate_x=20.0)))
    return t


# ------------------------------------------------------------------ R16

class _ControllerHarness:
    """轻量宿主：真实 handler + 替身面板链路。"""

    def __init__(self, timeline: TimelineSequence):
        from src.app.controllers.edit_controller import EditControllerMixin

        class _Host(EditControllerMixin):
            pass

        self.host = _Host()
        self.host.edit_page = MagicMock()
        self.host.edit_page._current_timeline = timeline
        self.host.edit_properties_panel = MagicMock()
        self.host.timeline_preview_renderer = None
        executed: list[Any] = []

        def _execute(command, tl):
            executed.append(command)
            result = command.apply(tl)
            new_tl = result[0] if isinstance(result, tuple) else result
            self.host.edit_page._current_timeline = new_tl
            return new_tl

        self.host._execute_timeline_edit_command = _execute
        self.executed = executed


def test_batch_param_writes_all_clips_one_macro() -> None:
    """RI-05：多选 X 输入写全部片（各自原值派生），单宏一次提交。"""
    timeline = _timeline()
    harness = _ControllerHarness(timeline)

    batch = [
        ("c1", "transform", "translate_x",
         TransformParams(translate_x=55.0)),
        ("c2", "transform", "translate_x",
         TransformParams(translate_x=55.0)),
    ]
    harness.host._on_edit_clips_param_batch_changed(batch)

    assert len(harness.executed) == 1
    assert isinstance(harness.executed[0], MacroCommand)
    new_timeline = harness.host.edit_page._current_timeline
    assert new_timeline.get_clip_by_id("c1").transform.translate_x == 55.0
    assert new_timeline.get_clip_by_id("c2").transform.translate_x == 55.0


def test_batch_param_locked_track_rejects_whole_group() -> None:
    """RI-05：选区含锁轨 target 整组拒绝，未锁 target 不半提交。"""
    timeline = _timeline(v_locked=True)
    harness = _ControllerHarness(timeline)

    batch = [
        ("c1", "transform", "translate_x",
         TransformParams(translate_x=55.0)),
        ("c2", "transform", "translate_x",
         TransformParams(translate_x=55.0)),
    ]
    harness.host._on_edit_clips_param_batch_changed(batch)
    assert harness.executed == []
    tl = harness.host.edit_page._current_timeline
    assert tl.get_clip_by_id("c1").transform.translate_x == 10.0
    assert tl.get_clip_by_id("c2").transform.translate_x == 20.0


def test_panel_derives_per_clip_values() -> None:
    """RI-05：面板派生基于各片自身原值（scale lock 批量同步两轴）。"""
    from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel

    c1 = _clip("c1", "v1", transform=TransformParams(
        translate_x=10.0, scale_x=1.0, scale_y=1.0))
    c2 = _clip("c2", "v1", transform=TransformParams(
        translate_x=20.0, scale_x=2.0, scale_y=3.0))
    batch: list[tuple] = []
    panel = EditPropertiesPanel.__new__(EditPropertiesPanel)
    panel._current_clips = [c1, c2]
    panel._scale_locked = True
    panel.clips_param_batch_changed = MagicMock()
    panel.clips_param_batch_changed.emit.side_effect = batch.append

    EditPropertiesPanel._route_clip_param(
        panel, "c1", "transform", "scale_x", 5.0
    )
    assert len(batch) == 1
    payload = batch[0]
    assert [item[0] for item in payload] == ["c1", "c2"]
    # c1 从自身 1.0/1.0 派生 → 5/5；c2 从自身 2.0/3.0 派生 → 5/5（lock）
    assert payload[0][3].scale_x == 5.0 and payload[0][3].scale_y == 5.0
    assert payload[1][3].scale_x == 5.0 and payload[1][3].scale_y == 5.0
    # 未改字段保留各自原值
    assert payload[0][3].translate_x == 10.0
    assert payload[1][3].translate_x == 20.0


def test_reset_transform_atomic_one_undo() -> None:
    """RI-05：两片 Reset 单宏提交（此前两条历史，一次 Undo 只还一片）。"""
    timeline = _timeline()
    harness = _ControllerHarness(timeline)
    harness.host._on_edit_reset_transform(["c1", "c2"])

    assert len(harness.executed) == 1
    assert isinstance(harness.executed[0], MacroCommand)
    new_timeline = harness.host.edit_page._current_timeline
    assert new_timeline.get_clip_by_id("c1").transform.translate_x == 0.0
    assert new_timeline.get_clip_by_id("c2").transform.translate_x == 0.0
