"""第六轮审计 第二阶段回归：C01/G16/U05。

C01：Transform 动画关键帧按权威值域求值——负平移、大位移、角度在
add/sample/remove/undo/save-load 全链路保值（旧代码 translate/rotation
落默认 [0,100]：X=-250 求值为 0、rotation=180 变 100）。
G16：普通 Transform/Opacity 行有 ◆ 动画入口（全名注册、状态刷新走
clip.get_param_at_frame）。
U05：字幕 Find 命中 → find_hit_selected → 宿主 seek 到 cue 开始。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import (
    AddKeyframeCommand,
    RemoveKeyframeCommand,
)
from src.features.edit.core.clip_params import clip_param_range
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _clip(clip_id="c1", track_id="v1") -> ClipInstance:
    return ClipInstance(clip_id, "asset", track_id, 0, 100, 0, 100)


def _seq(clip) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-p2", name="P2", width=1920, height=1080,
        fps=Rational(25, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
    )


# ── C01：权威值域与关键帧保值 ──


class TestC01TransformRanges:
    @pytest.mark.parametrize("param,value,frame", [
        ("transform.translate_x", -250.0, 10),
        ("transform.translate_y", 500.0, 20),
        ("transform.rotation", -45.0, 30),
        ("transform.rotation", 180.0, 40),
        ("transform.scale_x", 0.0, 50),
        ("transform.scale_x", 4.5, 60),
        ("opacity", 0.3, 70),
    ])
    def test_add_sample_keeps_value(self, param, value, frame):
        cmd = AddKeyframeCommand(
            command_id="k", label="kf", track_id="v1", clip_id="c1",
            param_name=param, frame=frame, value=value)
        seq = cmd.apply(_seq(_clip()))
        clip = seq.get_track_by_id("v1").get_clip_by_id("c1")
        got = clip.get_param_at_frame(param, frame)
        assert got == pytest.approx(value), f"{param}={value} 求值为 {got}"

    def test_interpolation_between_extremes(self):
        clip = _clip()
        for frame, value in ((0, -250.0), (50, 250.0)):
            cmd = AddKeyframeCommand(
                command_id=f"k{frame}", label="kf", track_id="v1",
                clip_id="c1", param_name="transform.translate_x",
                frame=frame, value=value)
            clip_seq = cmd.apply(_seq(clip))
            clip = clip_seq.get_track_by_id("v1").get_clip_by_id("c1")
        # 线性插值跨 0：frame 25 应在两关键帧正中（≈0），证明负值端点未被钳掉
        mid = clip.get_param_at_frame("transform.translate_x", 25)
        assert abs(mid) <= 5.0, f"frame25 应 ≈0（-250→250 中点），实测 {mid}"
        quarter = clip.get_param_at_frame("transform.translate_x", 12)
        assert -150.0 <= quarter <= -100.0, \
            f"frame12 应 ≈-130（负值区间插值），实测 {quarter}"

    def test_remove_undo_restores_value(self):
        clip = _clip()
        add = AddKeyframeCommand(
            command_id="k", label="kf", track_id="v1", clip_id="c1",
            param_name="transform.rotation", frame=10, value=-45.0)
        seq = add.apply(_seq(clip))
        clip = seq.get_track_by_id("v1").get_clip_by_id("c1")

        remove = RemoveKeyframeCommand(
            command_id="r", label="rm", track_id="v1", clip_id="c1",
            param_name="transform.rotation", frame=10)
        # Remove 需要先记录旧值（apply 内部完成）
        seq_after = remove.apply(seq)
        clip_after = seq_after.get_track_by_id("v1").get_clip_by_id("c1")
        assert not clip_after.is_param_animated("transform.rotation")

        seq_undo = remove.undo(seq_after)
        clip_undo = seq_undo.get_track_by_id("v1").get_clip_by_id("c1")
        assert clip_undo.get_param_at_frame("transform.rotation", 10) == \
            pytest.approx(-45.0), "undo 恢复的关键帧必须保值"

    def test_save_load_roundtrip_preserves_keyframes(self, tmp_path):
        cmd = AddKeyframeCommand(
            command_id="k", label="kf", track_id="v1", clip_id="c1",
            param_name="transform.translate_x", frame=10, value=-250.0)
        seq = cmd.apply(_seq(_clip()))

        data = seq.to_dict()
        seq2 = TimelineSequence.from_dict(data)
        clip2 = seq2.get_track_by_id("v1").get_clip_by_id("c1")
        assert clip2.get_param_at_frame("transform.translate_x", 10) == \
            pytest.approx(-250.0), "保存/重开后关键帧保值"

    def test_authoritative_ranges(self):
        assert clip_param_range("transform.translate_x") == (-8192.0, 8192.0)
        assert clip_param_range("transform.rotation") == (-3600.0, 3600.0)
        assert clip_param_range("transform.scale_x") == (0.0, 5.0)
        assert clip_param_range("opacity") == (0.0, 1.0)
        assert clip_param_range("unknown_param") == (0.0, 100.0)


def _seq_clip(clip):
    return clip


# ── G16：普通参数 ◆ 入口 ──


class TestG16KeyframeEntry:
    @pytest.fixture
    def panel(self, qapp):
        from src.features.edit.ui.edit_properties_panel import (
            EditPropertiesPanel,
        )

        panel = EditPropertiesPanel()
        yield panel
        panel.close()

    @pytest.fixture
    def qapp(self):
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([sys.argv])
        return app

    def test_transform_rows_register_keyframe_buttons(self, panel, qapp):
        """Transform 各轴与 rotation 的 ◆ 以全名注册（可查询/可触发）。"""
        clip = _clip()
        panel.set_clip(clip) if hasattr(panel, "set_clip") else None
        buttons = getattr(panel, "_keyframe_buttons", {})
        for name in (
            "transform.translate_x", "transform.translate_y",
            "transform.scale_x", "transform.scale_y",
            "transform.rotation",
            "opacity",
        ):
            assert name in buttons, f"{name} 缺少 ◆ 入口"

    def test_button_state_queries_clip_params(self, panel, qapp):
        """◆ 状态刷新走 clip.get_param_at_frame（带关键帧后按钮变实心）。"""
        clip = _clip()
        cmd = AddKeyframeCommand(
            command_id="k", label="kf", track_id="v1", clip_id="c1",
            param_name="transform.translate_x", frame=0, value=-250.0)
        clip = cmd.apply(_seq(clip)).get_track_by_id("v1").get_clip_by_id("c1")
        panel.set_clip(clip)
        if hasattr(panel, "update_current_clip"):
            panel.update_current_clip(clip)
        button = panel._keyframe_buttons.get("transform.translate_x")
        assert button is not None
        assert button._has_keyframe_at_frame or button._is_animated, \
            "已动画化参数的 ◆ 应反映动画状态"


# ── U05：字幕 Find 定位 ──


class TestU05CaptionFindSeek:
    def test_find_emits_hit_signal(self, qapp):
        from src.features.edit.ui.caption_workspace import CaptionWorkspacePanel
        from src.features.edit.core.timeline_v2 import SubtitleClip

        ws = CaptionWorkspacePanel()
        try:
            clip = SubtitleClip(
                clip_id="cue-1", track_id="s0",
                timeline_start=10, timeline_duration=5,
                text="Hello, world")
            seq = TimelineSequence(
                timeline_id="seq-u5", name="U5", width=64, height=48,
                fps=Rational(25, 1),
                subtitle_tracks=(EditTrack(
                    "s0", "SUB", "subtitle", 0, subtitle_clips=(clip,)),),
            )
            ws.set_sequence(seq)
            ws._find_edit.setText("Hello")
            hits = []
            ws.find_hit_selected.connect(
                lambda cid, tid: hits.append((cid, tid)))
            ws._on_find()
            assert hits == [("cue-1", "s0")], "Find 命中必须发 find_hit_selected"
        finally:
            ws.close()

    def test_controller_handler_seeks_to_cue_start(self, qapp):
        """宿主 on_subtitle_find_hit：播放头在 cue 外 → seek 到 cue 开始。"""
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([sys.argv])
        from src.features.edit.ui.timeline_panel import TimelinePanel
        from src.features.edit.core.timeline_v2 import SubtitleClip

        clip = SubtitleClip(
            clip_id="cue-1", track_id="s0",
            timeline_start=10, timeline_duration=5,
            text="Hello, world")
        seq = TimelineSequence(
            timeline_id="seq-u5", name="U5", width=64, height=48,
            fps=Rational(25, 1),
            subtitle_tracks=(EditTrack(
                "s0", "SUB", "subtitle", 0, subtitle_clips=(clip,)),),
        )

        panel = TimelinePanel()
        panel.show()
        panel.set_timeline_sequence(seq)
        panel.set_total_frames(300)
        controller = panel.get_playback_controller()
        controller.seek(250)
        app.processEvents()
        assert controller.current_frame == 250

        # 极简宿主：edit_page 替身提供 _current_timeline + subtitle 查询
        host = _FakeHost(seq, panel)
        host.on_subtitle_find_hit("cue-1", "s0")
        app.processEvents()
        assert controller.current_frame == 10, \
            "Find 命中后播放头应定位到 cue 开始（10）"
        assert panel.timeline_view.get_playhead_frame() == 10, \
            "可见播放头与控制器同帧（U04 联动）"
        panel.close()

    def test_handler_no_seek_when_inside_cue(self, qapp):
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([sys.argv])
        from src.features.edit.ui.timeline_panel import TimelinePanel
        from src.features.edit.core.timeline_v2 import SubtitleClip

        clip = SubtitleClip(
            clip_id="cue-1", track_id="s0",
            timeline_start=10, timeline_duration=5,
            text="Hello, world")
        seq = TimelineSequence(
            timeline_id="seq-u5b", name="U5b", width=64, height=48,
            fps=Rational(25, 1),
            subtitle_tracks=(EditTrack(
                "s0", "SUB", "subtitle", 0, subtitle_clips=(clip,)),),
        )
        panel = TimelinePanel()
        panel.show()
        panel.set_timeline_sequence(seq)
        panel.set_total_frames(300)
        controller = panel.get_playback_controller()
        controller.seek(12)
        app.processEvents()

        host = _FakeHost(seq, panel)
        host.on_subtitle_find_hit("cue-1", "s0")
        app.processEvents()
        assert controller.current_frame == 12, "已在 cue 内不重复 seek"
        panel.close()


class _FakeHost:
    """复用生产 handler 逻辑的极简宿主（EditControllerMixin 方法绑定）。"""

    def __init__(self, timeline, panel):
        from src.app.controllers.edit_controller import EditControllerMixin

        self.edit_page = _FakeEditPage(timeline)
        self.timeline_panel = panel
        # 绑定生产的 on_subtitle_find_hit（未绑定的 mixin 函数）
        self.on_subtitle_find_hit = EditControllerMixin.on_subtitle_find_hit.__get__(
            self, type(self))


class _FakeEditPage:
    def __init__(self, timeline):
        self._current_timeline = timeline
