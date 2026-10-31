"""第三轮复查批A回归：Properties 拒绝/刷新/Reset 事务（N09–N14）。

对应 docs/audit/2026-10-14-edit-third-review/reports/
topos_round3_interaction_report.md（T3-01…T3-06）。

领域模型与命令为真实实现；controller 用轻量宿主（真实 handler +
替身面板），与 test_recheck_c_multiselect 相同的取证方式。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.edit.core.clip_params import (
    CropParams, SpeedParams, TransformParams,
)
from src.features.edit.core.commands import MacroCommand, UpdateClipParamCommand
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100,
          transform: TransformParams | None = None,
          crop: CropParams | None = None,
          speed: Any = None, opacity: float = 0.8) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"a-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=300,
        transform=transform or TransformParams(),
        crop=crop or CropParams(left=0.10),
        speed=speed if speed is not None else SpeedParams(speed=1.0),
        opacity=opacity,
    )


def _timeline(v_locked: bool = False) -> TimelineSequence:
    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        is_locked=v_locked,
    ))
    t = t.with_clip_added("v1", _clip("c1", "v1", 0, 100,
                                      TransformParams(translate_x=10.0),
                                      crop=CropParams(left=0.10, right=0.11),
                                      opacity=0.8))
    t = t.with_clip_added("v1", _clip("c2", "v1", 200, 100,
                                      TransformParams(translate_x=20.0),
                                      crop=CropParams(left=0.20, right=0.21),
                                      opacity=0.3))
    return t


class _PanelRecorder(MagicMock):
    """替身面板：记录 update_clips / set_clips 调用。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.update_clips_calls: list = []
        self.set_clips_calls: list = []

    def update_clips(self, clips):  # noqa: N802（Qt 命名）
        self.update_clips_calls.append(list(clips))

    def set_clips(self, clips, fps=24.0, **kw):  # noqa: N802
        self.set_clips_calls.append(list(clips))


class _ControllerHarness:
    def __init__(self, timeline: TimelineSequence,
                 panel: _PanelRecorder | None = None):
        from src.app.controllers.edit_controller import EditControllerMixin

        class _Host(EditControllerMixin):
            pass

        self.host = _Host()
        self.host.edit_page = MagicMock()
        self.host.edit_page._current_timeline = timeline
        self.host.edit_page._fps = 24.0
        self.panel = panel or _PanelRecorder()
        self.host.edit_properties_panel = self.panel
        self.host.timeline_preview_renderer = None
        executed: list[Any] = []

        def _execute(command, tl):
            result = command.apply(tl)  # 先 apply：领域拒绝不记为已执行
            executed.append(command)
            new_tl = result[0] if isinstance(result, tuple) else result
            self.host.edit_page._current_timeline = new_tl
            return new_tl

        self.host._execute_timeline_edit_command = _execute
        self.executed = executed


# ------------------------------------------------------------------ N09

def test_n09_locked_single_param_rejection_is_caught_and_restores() -> None:
    """锁轨单选 Transform 输入 99：无未捕获异常，模型不变，面板恢复。"""
    timeline = _timeline(v_locked=True)
    harness = _ControllerHarness(timeline)

    # 面板控件已显示用户输入 99（假值），拒绝后应从 canonical 恢复 10
    harness.host._on_edit_clip_param_changed(
        "c1", "transform", "translate_x",
        TransformParams(translate_x=99.0))

    tl = harness.host.edit_page._current_timeline
    assert tl.get_clip_by_id("c1").transform.translate_x == 10.0
    assert harness.executed == [] or all(
        not isinstance(c, UpdateClipParamCommand) for c in harness.executed)
    # 恢复路径：从 canonical timeline 重新拉取 clip 刷回面板
    assert harness.panel.update_clips_calls, "rejection must restore panel"
    restored = harness.panel.update_clips_calls[-1]
    assert restored[0].clip_id == "c1"
    assert restored[0].transform.translate_x == 10.0


def test_n09_batch_execute_rejection_restores_panel() -> None:
    """执行期领域拒绝（非预检命中，如命令内部校验）同样收敛+恢复。"""

    class _BoomCommand:
        def apply(self, tl):  # pragma: no cover - 不应成功
            raise AssertionError("must not apply")

        def undo(self, tl):  # pragma: no cover
            return tl

    timeline = _timeline()
    harness = _ControllerHarness(timeline)
    # 走 UpdateClipParamCommand 到达执行阶段，再让执行抛领域 ValueError
    original = harness.host._execute_timeline_edit_command

    def _raise(command, tl):
        raise ValueError("simulated domain rejection")

    harness.host._execute_timeline_edit_command = _raise
    batch = [("c1", "transform", "translate_x",
              TransformParams(translate_x=55.0))]
    # 不冒异常即通过（此前未捕获 Qt 槽 ValueError）
    harness.host._on_edit_clips_param_batch_changed(batch)
    assert harness.panel.update_clips_calls
    harness.host._execute_timeline_edit_command = original


def test_n11_speed_fallback_undo_restores_old_params() -> None:
    """N11：UpdateClipParamCommand 未携带 old_value 时 apply 捕获真实旧值。

    此前 undo 读"当前 speed"当旧值（apply 后当前值已是新值），历史弹
    掉而内容不恢复。
    """
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(EditTrack(
        track_id="v1", name="V1", track_type="video", index=0))
    base = _clip("c1", "v1", 0, 50, speed=SpeedParams(speed=2.0))
    tl2 = timeline.with_clip_added(
        "v1", base if base.source_duration == 100 else
        ClipInstance(
            clip_id="c1", asset_id="a-c1", track_id="v1",
            timeline_start=0, timeline_duration=50,
            source_start=0, source_duration=100,
            transform=TransformParams(),
            crop=CropParams(),
            speed=SpeedParams(speed=2.0),
        ))

    cmd = UpdateClipParamCommand(
        command_id="cmd-speed",
        label="speed fallback",
        track_id="v1",
        clip_id="c1",
        param_type="speed",
        new_value=SpeedParams(speed=1.0),
        old_value=None,
    )
    applied = cmd.apply(tl2)
    applied_clip = applied.get_clip_by_id("c1")
    assert applied_clip.speed == SpeedParams(speed=1.0)
    assert applied_clip.timeline_duration == 100
    undone = cmd.undo(applied)
    restored = undone.get_clip_by_id("c1")
    assert restored.speed == SpeedParams(speed=2.0)
    assert restored.timeline_duration == 50


# ------------------------------------------------------------------ N10

def test_n10_facade_exports_change_clip_speed_command() -> None:
    from src.features.edit.commands_facade import get_command_classes

    cmds = get_command_classes()
    assert cmds is not None
    assert "ChangeClipSpeedCommand" in cmds
    assert cmds["ChangeClipSpeedCommand"] is not None


def test_n10_single_speed_uses_change_clip_speed_command() -> None:
    """单选 Properties Speed 输入走统一 ChangeClipSpeedCommand。"""
    timeline = _timeline()
    harness = _ControllerHarness(timeline)
    from src.features.edit.core.commands import ChangeClipSpeedCommand

    harness.host._on_edit_clip_param_changed(
        "c1", "speed", "speed", SpeedParams(speed=2.0))

    assert harness.executed, "speed must commit a command"
    assert isinstance(harness.executed[0], ChangeClipSpeedCommand)


# ------------------------------------------------------------------ N12

def test_n12_batch_refreshes_panel_snapshots_after_commit() -> None:
    """批量 X55 后再 Y66：第二次派生基于 canonical，X 不回退。"""
    timeline = _timeline()
    harness = _ControllerHarness(timeline)

    batch_x = [
        ("c1", "transform", "translate_x", TransformParams(translate_x=55.0)),
        ("c2", "transform", "translate_x", TransformParams(translate_x=55.0)),
    ]
    harness.host._on_edit_clips_param_batch_changed(batch_x)
    tl = harness.host.edit_page._current_timeline

    # 面板快照被刷新为 canonical（这是第二次派生的数据源）
    assert harness.panel.update_clips_calls
    refreshed_ids = {c.clip_id for c in harness.panel.update_clips_calls[-1]}
    assert refreshed_ids == {"c1", "c2"}

    # 用刷新后的快照派生 Y66 —— X 必须保持 55
    fresh_c1 = tl.get_clip_by_id("c1")
    fresh_c2 = tl.get_clip_by_id("c2")
    assert fresh_c1.transform.translate_x == 55.0
    assert fresh_c2.transform.translate_x == 55.0

    batch_y = [
        ("c1", "transform", "translate_y",
         fresh_c1.transform.with_translate(55.0, 66.0)),
        ("c2", "transform", "translate_y",
         fresh_c2.transform.with_translate(55.0, 66.0)),
    ]
    harness.host._on_edit_clips_param_batch_changed(batch_y)
    tl2 = harness.host.edit_page._current_timeline
    assert tl2.get_clip_by_id("c1").transform.translate_x == 55.0
    assert tl2.get_clip_by_id("c1").transform.translate_y == 66.0
    assert tl2.get_clip_by_id("c2").transform.translate_x == 55.0


def test_n12_panel_update_clips_swaps_snapshots() -> None:
    from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel

    panel = EditPropertiesPanel.__new__(EditPropertiesPanel)
    c1_old = _clip("c1", "v1", transform=TransformParams(translate_x=10.0))
    c2 = _clip("c2", "v1")
    panel._current_clips = [c1_old, c2]
    panel._sections = {}
    panel._updating = False
    panel._update_keyframe_buttons = lambda: None

    c1_new = _clip("c1", "v1", transform=TransformParams(translate_x=55.0))
    panel.update_clips([c1_new])
    assert panel._current_clips[0] is c1_new
    assert panel._current_clips[1] is c2


# ------------------------------------------------------------------ N13

def test_n13_crop_reset_single_macro_all_fields_default() -> None:
    """Crop Reset：五项一次提交，一片一命令，全部归默认。"""
    from src.features.edit.core.timeline_ops import validate_timeline

    timeline = _timeline()
    harness = _ControllerHarness(timeline)
    # 两片 Crop 均非默认
    assert timeline.get_clip_by_id("c1").crop.left != CropParams().left

    harness.host._on_edit_clips_param_batch_changed([
        ("c1", "crop", "reset", CropParams()),
        ("c2", "crop", "reset", CropParams()),
    ])

    assert len(harness.executed) == 1
    assert isinstance(harness.executed[0], MacroCommand)
    tl = harness.host.edit_page._current_timeline
    assert tl.get_clip_by_id("c1").crop == CropParams()
    assert tl.get_clip_by_id("c2").crop == CropParams()
    assert validate_timeline(tl) == []


def test_n13_default_reset_is_noop_zero_history() -> None:
    """已默认的 Speed Reset：批为空，零命令零历史。"""
    timeline = _timeline()  # speed 默认 1.0
    harness = _ControllerHarness(timeline)
    harness.host._on_edit_clips_param_batch_changed([
        ("c1", "speed", "reset", SpeedParams(speed=1.0)),
    ])
    assert harness.executed == []


def test_n13_same_value_batch_is_noop() -> None:
    """同值 no-op 跳过：不产生命令/历史。"""
    timeline = _timeline()
    harness = _ControllerHarness(timeline)
    harness.host._on_edit_clips_param_batch_changed([
        ("c1", "transform", "translate_x",
         TransformParams(translate_x=10.0)),  # == 当前值
    ])
    assert harness.executed == []


# ------------------------------------------------------------------ N14

def test_n14_reset_transform_keeps_multi_selection_context() -> None:
    """Transform Reset 后面板收到完整多选上下文（不是 first 单选）。"""
    timeline = _timeline()
    harness = _ControllerHarness(timeline)
    harness.host._on_edit_reset_transform(["c1", "c2"])

    assert harness.panel.set_clips_calls, "reset must refresh full context"
    refreshed = harness.panel.set_clips_calls[-1]
    assert [c.clip_id for c in refreshed] == ["c1", "c2"]
    tl = harness.host.edit_page._current_timeline
    assert tl.get_clip_by_id("c1").transform.translate_x == 0.0
    assert tl.get_clip_by_id("c2").transform.translate_x == 0.0


# ------------------------------------------------------------------ 面板 Reset 路径

class _SectionStub:
    def __init__(self, param_defs: dict):
        self.param_defs = param_defs
        self._param_widgets = {}
        self.parameters = {}


def test_n13_panel_reset_emits_single_batch_per_group() -> None:
    """面板 Crop Reset：一次动作发一个 batch（此前逐参数 5 个宏）。"""
    from src.features.edit.ui import edit_properties_panel as epp

    panel = epp.EditPropertiesPanel.__new__(epp.EditPropertiesPanel)
    c1 = _clip("c1", "v1", crop=CropParams(left=0.10, right=0.11,
                                           top=0.12, bottom=0.13,
                                           feather=5.0))
    panel._current_clips = [c1]
    panel._updating = False

    section = _SectionStub(dict(epp.CROP_PARAM_DEFS))
    panel._sections = {"clip:c1:crop": section}
    emitted: list = []
    panel.clips_param_batch_changed = MagicMock()
    panel.clips_param_batch_changed.emit.side_effect = emitted.append
    panel._reset_section_widgets = lambda s: None

    epp.EditPropertiesPanel._on_section_reset(panel, "clip:c1:crop")

    assert len(emitted) == 1, "reset must be one atomic batch"
    batch = emitted[0]
    assert len(batch) == 1
    clip_id, group, path, value = batch[0]
    assert (clip_id, group) == ("c1", "crop")
    assert value == CropParams(), "all crop fields must reset to defaults"


def test_n13_panel_reset_default_crop_is_noop_batch() -> None:
    from src.features.edit.ui import edit_properties_panel as epp

    panel = epp.EditPropertiesPanel.__new__(epp.EditPropertiesPanel)
    c1 = _clip("c1", "v1", crop=CropParams())
    panel._current_clips = [c1]
    panel._updating = False

    section = _SectionStub(dict(epp.CROP_PARAM_DEFS))
    panel._sections = {"clip:c1:crop": section}
    panel.clips_param_batch_changed = MagicMock()
    panel._reset_section_widgets = lambda s: None

    epp.EditPropertiesPanel._on_section_reset(panel, "clip:c1:crop")

    panel.clips_param_batch_changed.emit.assert_not_called()
