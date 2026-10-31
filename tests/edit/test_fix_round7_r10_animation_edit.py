"""第七轮审计 0B 回归：普通属性动画编辑闭环（R10）。

R10：六个普通属性（translate x/y、scale x/y、rotation、opacity）的 ◆
入口在第六轮已存在，但真实编辑工作流失败——
- 已动画参数在数值框输入新值只写静态 base，关键帧求值覆盖静态值，
  渲染/采样仍是旧值（0→100 动画中点输入 100 无效）；
- 再点 ◆ 写入的是模型求值（0）而不是刚输入的 100；
- 播放头移动后 Inspector 不按帧求值刷新。

修复语义（对齐专业剪辑）：参数已动画时数值编辑 = 在播放头帧更新/插入
关键帧（同帧原位替换）；◆ 取值优先行控件当前值；播放头移动按帧求值
回显（blockSignals 不反写）；未动画参数保持静态 base 路径。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import AddKeyframeCommand
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _clip(clip_id="c1", track_id="v1") -> ClipInstance:
    return ClipInstance(clip_id, "asset", track_id, 0, 100, 0, 100)


def _seq(clip) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-r10", name="R10", width=1920, height=1080,
        fps=Rational(25, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
    )


def _animated_clip() -> ClipInstance:
    """translate_x 0@0 → 100@50 的已动画片段。"""
    clip = _clip()
    cmd = AddKeyframeCommand(
        command_id="k0", label="kf0", track_id="v1", clip_id="c1",
        param_name="transform.translate_x", frame=0, value=0.0)
    clip = cmd.apply(_seq(clip)).get_track_by_id("v1").get_clip_by_id("c1")
    cmd = AddKeyframeCommand(
        command_id="k1", label="kf1", track_id="v1", clip_id="c1",
        param_name="transform.translate_x", frame=50, value=100.0)
    return cmd.apply(_seq(clip)).get_track_by_id("v1").get_clip_by_id("c1")


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv])
    yield app


@pytest.fixture
def panel(qapp):
    from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel

    panel = EditPropertiesPanel()
    yield panel
    panel.close()


class TestR10AnimatedEditRouting:
    def test_animated_param_edit_emits_keyframe_path(self, panel, qapp):
        """已动画 translate_x 输入 100 → animated_param_edited(25, 100)，
        不再走静态 clip_param_changed（输入值渲染不可见的旧路径）。"""
        clip = _animated_clip()
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: 25)

        animated_calls, static_calls = [], []
        panel.animated_params_edited.connect(
            lambda cid, edits, f: animated_calls.append((cid, edits, f)))
        panel.clip_param_changed.connect(
            lambda *a: static_calls.append(a))

        panel._route_clip_param("c1", "transform", "translate_x", 100.0)

        assert animated_calls == [
            ("c1", [("transform.translate_x", 100.0)], 25)]
        assert not static_calls, "已动画参数不得再写静态 base"

    def test_non_animated_param_keeps_static_path(self, panel, qapp):
        """未动画参数保持静态 base 路径（第六轮前行为不回归）。"""
        clip = _clip()
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: 25)

        animated_calls, static_calls = [], []
        panel.animated_params_edited.connect(
            lambda *a: animated_calls.append(a))
        panel.clip_param_changed.connect(
            lambda *a: static_calls.append(a))

        panel._route_clip_param("c1", "transform", "translate_x", 55.0)
        assert not animated_calls
        assert static_calls, "未动画参数走静态 UpdateClipParam 路径"

    def test_opacity_animated_edit_normalizes_domain(self, panel, qapp):
        """opacity 已动画：UI 50% → 模型域 0.5。"""
        clip = _clip()
        cmd = AddKeyframeCommand(
            command_id="k", label="kf", track_id="v1", clip_id="c1",
            param_name="opacity", frame=0, value=0.2)
        clip = cmd.apply(_seq(clip)).get_track_by_id("v1").get_clip_by_id("c1")
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: 10)

        calls = []
        panel.animated_params_edited.connect(
            lambda cid, edits, f: calls.append((cid, edits, f)))
        panel._route_clip_param("c1", "opacity", "value", 50.0)
        assert calls == [("c1", [("opacity", pytest.approx(0.5))], 10)]


class TestR10ScaleLockAnimatedPair:
    def _scale_animated_clip(self):
        clip = _clip()
        for param, frame, value in (
            ("transform.scale_x", 0, 1.0), ("transform.scale_x", 50, 2.0),
            ("transform.scale_y", 0, 1.0), ("transform.scale_y", 50, 3.0),
        ):
            cmd = AddKeyframeCommand(
                command_id=f"k{param}{frame}", label="kf",
                track_id="v1", clip_id="c1",
                param_name=param, frame=frame, value=value)
            clip = cmd.apply(_seq(clip)).get_track_by_id("v1").get_clip_by_id("c1")
        return clip

    def test_locked_pair_emits_both_axes(self, panel, qapp):
        """scale 锁住 + 两轴都已动画：编辑 scale_x 同时给 scale_y 同帧
        打点（静态路径 with_scale(x, y) 的动画等价物）。"""
        clip = self._scale_animated_clip()
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: 25)
        panel._scale_locked = True

        calls = []
        panel.animated_params_edited.connect(
            lambda cid, edits, f: calls.append((cid, edits, f)))
        panel._route_clip_param("c1", "transform", "scale_x", 1.5)
        assert calls == [("c1", [
            ("transform.scale_x", 1.5), ("transform.scale_y", 1.5)], 25)]

    def test_unlocked_emits_single_axis(self, panel, qapp):
        clip = self._scale_animated_clip()
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: 25)
        panel._scale_locked = False

        calls = []
        panel.animated_params_edited.connect(
            lambda cid, edits, f: calls.append((cid, edits, f)))
        panel._route_clip_param("c1", "transform", "scale_x", 1.5)
        assert calls == [("c1", [("transform.scale_x", 1.5)], 25)]

    def test_peer_not_animated_skips_peer(self, panel, qapp):
        """peer 轴未动画：不自动创建关键帧轨道（保持其静态值）。"""
        clip = _animated_clip()  # 只有 translate_x 动画
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: 25)
        panel._scale_locked = True

        # scale_x 未动画 → 走静态路径（不进动画分支）
        static_calls = []
        panel.clip_param_changed.connect(lambda *a: static_calls.append(a))
        panel._route_clip_param("c1", "transform", "scale_x", 1.5)
        assert static_calls and not any(
            a[2] == "scale_y" for a in static_calls if len(a) > 2)


class TestR10KeyframeButtonUsesWidgetValue:
    def test_diamond_reads_typed_value_not_model_eval(self, panel, qapp):
        """0→100 动画中点（求值 50）输入 100 后点 ◆：写入 100 不是 50
        （旧逻辑读 clip.get_param_at_frame → 动画求值覆盖用户输入）。"""
        clip = _animated_clip()
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: 25)

        record = panel._animated_value_widgets["transform.translate_x"]
        record["set"](100.0)  # 模拟用户输入（blockSignals 程序路径）

        calls = []
        panel.keyframe_toggled.connect(lambda *a: calls.append(a))
        panel._keyframe_buttons["transform.translate_x"].click()

        assert calls and calls[0][0] == "c1"
        assert calls[0][1] == "transform.translate_x"
        assert calls[0][3] == pytest.approx(100.0), \
            f"◆ 取值应为控件当前值 100，实测 {calls[0][3]}"


class TestR10PerFrameInspectorRefresh:
    def test_playhead_move_refreshes_widget_values(self, panel, qapp):
        """播放头 0→25→50：行控件按帧求值 0→50→100（旧逻辑静态 0）。"""
        clip = _animated_clip()
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: panel._current_frame)

        record = panel._animated_value_widgets["transform.translate_x"]
        for frame, expect in ((0, 0.0), (25, 50.0), (50, 100.0)):
            panel.set_current_frame(frame)
            assert record["get"]() == pytest.approx(expect), \
                f"帧 {frame} 应显示 {expect}，实测 {record['get']()}"

    def test_program_refresh_does_not_write_back(self, panel, qapp):
        """按帧回显不触发命令栈（程序刷新不反写）。"""
        clip = _animated_clip()
        panel.set_clip(clip)
        panel.set_frame_provider(lambda: panel._current_frame)

        calls = []
        panel.clip_param_changed.connect(lambda *a: calls.append(a))
        panel.animated_params_edited.connect(lambda *a: calls.append(a))
        panel.set_current_frame(25)
        assert not calls, "回显（blockSignals）不得触发参数提交"


class TestR10ControllerSlot:
    def _host(self, timeline):
        from src.app.controllers.edit_controller import EditControllerMixin

        host = SimpleNamespace()
        host.edit_page = SimpleNamespace(_current_timeline=timeline)
        host.edit_properties_panel = SimpleNamespace(
            update_current_clip=lambda c: None)
        applied = []

        def _execute(command, tl):
            new_tl = command.apply(tl)
            applied.append(new_tl)
            host.edit_page._current_timeline = new_tl
            return new_tl

        host._execute_timeline_edit_command = _execute
        host._invalidate_clip_render_cache = lambda cid: None
        host._request_program_viewer_update = lambda: None
        host._show_edit_status_message = lambda msg: None
        host._on_edit_animated_params_edited = (
            EditControllerMixin._on_edit_animated_params_edited.__get__(
                host, type(host)))
        return host, applied

    def test_slot_updates_keyframe_at_current_frame(self, qapp):
        """宿主槽：已动画 translate_x @F25=100 → 关键帧写入 25/100，
        中点求值变为 50（0→100@25 的线性段 12 帧 × 4/帧 = 48）。"""
        clip = _animated_clip()
        timeline = _seq(clip)
        host, applied = self._host(timeline)

        host._on_edit_animated_params_edited(
            "c1", [("transform.translate_x", 100.0)], 25)

        assert applied, "应执行一次关键帧命令（单次 Undo 步）"
        new_clip = host.edit_page._current_timeline \
            .get_track_by_id("v1").get_clip_by_id("c1")
        assert new_clip.has_keyframe_at("transform.translate_x", 25)
        assert new_clip.get_param_at_frame(
            "transform.translate_x", 25) == pytest.approx(100.0)
        assert new_clip.get_param_at_frame(
            "transform.translate_x", 12) == pytest.approx(48.0), \
            "0→100@25 线性段（12 帧 × 4/帧）应为 48"
