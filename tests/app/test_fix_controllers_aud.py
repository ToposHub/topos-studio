"""AUD 批 A（Critical×4）修复回归测试。

覆盖：

* AUD-2209 —— ``ai_workflow_composite_render._write_png`` 写 PNG 前必须做
  linear→sRGB OETF（复用主导出路径的 ``_encode_linear_to_srgb``），不再把
  scene-linear 直接乘 255 量化。
* AUD-2301 —— ``color_controller._iter_brush_shapes`` 递归穿透 MaskGroup
  且防御式读取 ``type``（根级群组不再让 brush 流程 AttributeError 崩溃）。
* AUD-2700 —— ``edit_controller._on_bypass_composite_requested`` 用既有
  不可变更新模式发布 ``composite_bypassed`` 切换（旧代码引用全库不存在的
  ``self._current_edit_page`` / ``EditPage.current_timeline()`` /
  ``TimelineSequence.replace_clip`` / ``EditProject.with_timeline``）。
* AUD-2703 —— ``edit_controller.on_clip_transform_drag_ended`` 的提交命令
  走 ``TimelineView.execute_command``（无视图时直接 apply + 同步视图），
  不再因 ``EditPage`` 没有 ``_command_stack`` 被静默丢弃。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from unittest.mock import MagicMock

from src.app.controllers import edit_controller as ec
from src.app.controllers.color_controller import ColorControllerMixin
from src.app.controllers.edit_controller import EditControllerMixin
from src.features.color.core.layer import Layer, MaskGroup, MaskShape


# ═══════════════════════════════════════════════════════════════════════
# AUD-2209: _write_png applies linear→sRGB before 8bit quantization
# ═══════════════════════════════════════════════════════════════════════


def _builtin_srgb(v: float) -> float:
    """仓库内置的 sRGB OETF 数学（composite_render_output 回退路径）。"""
    if v <= 0.0031308:
        return 12.92 * v
    return 1.055 * v ** (1.0 / 2.4) - 0.055


@pytest.fixture
def force_builtin_srgb(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 ``_encode_linear_to_srgb`` 走内置数学回退（确定性断言）。

    真实 provider（OCIO 等）与回退公式同源标准 sRGB；测试里禁用 provider
    只为拿到精确期望值。
    """
    import src.shared.color.color_transform_provider as provider_mod

    def _raise(config_manager: Any = None) -> Any:
        raise RuntimeError("provider disabled for deterministic test")

    monkeypatch.setattr(provider_mod, "get_color_provider", _raise)


def test_write_png_encodes_srgb_not_linear(
        tmp_path: Any, force_builtin_srgb: None) -> None:
    """scene-linear 0.5 灰 → sRGB 后 ≈188/255，而非 linear 直乘的 128。"""
    import cv2

    from src.app.controllers.ai_workflow_composite_render import _write_png

    frame = np.full((4, 4, 3), 0.5, dtype=np.float32)
    path = str(tmp_path / "out.png")
    assert _write_png(frame, path) is True

    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    assert img is not None and img.shape == (4, 4, 3)
    assert img.dtype == np.uint8
    expected = int(np.clip(_builtin_srgb(0.5), 0.0, 1.0) * 255.0 + 0.5)
    assert abs(int(img[0, 0, 0]) - expected) <= 1
    # 旧的错误行为：linear 直接乘 255 → 128。必须不再出现。
    assert abs(int(img[0, 0, 0]) - 128) > 20


def test_write_png_dark_range_uses_linear_knee(
        tmp_path: Any, force_builtin_srgb: None) -> None:
    """低于阈值（<=0.0031308）走 12.92 线性段：0.001 → 3，而非 0。"""
    import cv2

    from src.app.controllers.ai_workflow_composite_render import _write_png

    frame = np.full((2, 2, 3), 0.001, dtype=np.float32)
    path = str(tmp_path / "dark.png")
    assert _write_png(frame, path) is True

    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    expected = int(np.clip(_builtin_srgb(0.001), 0.0, 1.0) * 255.0 + 0.5)
    assert abs(int(img[0, 0, 0]) - expected) <= 1


def test_write_png_hdr_highlights_clip_to_display_white(
        tmp_path: Any, force_builtin_srgb: None) -> None:
    """>1.0 的 scene-linear 高光经 OETF 后 clip 到显示白（255）。"""
    import cv2

    from src.app.controllers.ai_workflow_composite_render import _write_png

    frame = np.full((2, 2, 3), 4.0, dtype=np.float32)
    path = str(tmp_path / "hdr.png")
    assert _write_png(frame, path) is True

    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    assert int(img[0, 0, 0]) == 255
    assert int(img[0, 0, 1]) == 255
    assert int(img[0, 0, 2]) == 255


# ═══════════════════════════════════════════════════════════════════════
# AUD-2301: _iter_brush_shapes is group-safe and recursive
# ═══════════════════════════════════════════════════════════════════════


def test_iter_brush_shapes_flattens_nested_groups() -> None:
    """根级群组 + 嵌套群组中的画笔都能被递归找到，不触发 AttributeError。"""
    layer = Layer("Main")
    root_brush = MaskShape(MaskShape.BRUSH)
    circle = MaskShape(MaskShape.CIRCLE)
    grouped_brush = MaskShape(MaskShape.BRUSH)
    nested_brush = MaskShape(MaskShape.BRUSH)

    inner = MaskGroup("Inner")
    inner.add_child(nested_brush)
    outer = MaskGroup("Outer")
    outer.add_child(grouped_brush)
    outer.add_child(inner)

    layer.mask_shapes.extend([root_brush, circle, outer])

    host = SimpleNamespace()
    shapes = ColorControllerMixin._iter_brush_shapes(host, layer)  # type: ignore[arg-type]

    assert [s.id for s in shapes] == [
        root_brush.id, grouped_brush.id, nested_brush.id,
    ]
    # MaskGroup 本身没有实例 ``type``（只有类常量 ``TYPE``）——能走到这里
    # 即证明遍历是防御式的（旧代码 ``s.type`` 会直接 AttributeError）。
    assert not any(isinstance(s, MaskGroup) for s in shapes)


def test_iter_brush_shapes_empty_group_only_layer() -> None:
    """只有空群组时安全返回空列表（旧代码在首元素即崩溃）。"""
    layer = Layer("Main")
    layer.mask_shapes.append(MaskGroup("Empty"))
    host = SimpleNamespace()
    assert ColorControllerMixin._iter_brush_shapes(host, layer) == []  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# AUD-2700: Bypass Composite publishes via immutable timeline update
# ═══════════════════════════════════════════════════════════════════════


class _FakeClip:
    def __init__(self, clip_id: str, track_id: str,
                 composite_id: Optional[str], bypassed: bool = False) -> None:
        self.clip_id = clip_id
        self.track_id = track_id
        self.composite_id = composite_id
        self.composite_bypassed = bypassed

    def with_composite_bypassed(self, bypassed: bool) -> "_FakeClip":
        return _FakeClip(self.clip_id, self.track_id,
                         self.composite_id, bypassed)


class _FakeTrack:
    def __init__(self, track_id: str, clips: List[_FakeClip]) -> None:
        self.track_id = track_id
        self.clips = list(clips)

    def get_clip_by_id(self, clip_id: str) -> Optional[_FakeClip]:
        return next((c for c in self.clips if c.clip_id == clip_id), None)

    def with_clip_updated(self, new_clip: _FakeClip) -> "_FakeTrack":
        return _FakeTrack(self.track_id, [
            new_clip if c.clip_id == new_clip.clip_id else c
            for c in self.clips
        ])


class _FakeTimeline:
    def __init__(self, timeline_id: str, tracks: List[_FakeTrack]) -> None:
        self.timeline_id = timeline_id
        self.tracks = list(tracks)

    @property
    def all_tracks(self) -> List[_FakeTrack]:
        return list(self.tracks)

    def get_clip_by_id(self, clip_id: str) -> Optional[_FakeClip]:
        for track in self.tracks:
            clip = track.get_clip_by_id(clip_id)
            if clip is not None:
                return clip
        return None

    def with_track_updated(self, new_track: _FakeTrack) -> "_FakeTimeline":
        return _FakeTimeline(self.timeline_id, [
            new_track if t.track_id == new_track.track_id else t
            for t in self.tracks
        ])


class _BypassHost(EditControllerMixin):
    """最小宿主：只提供 _on_bypass_composite_requested 触达的属性。"""

    def __init__(self, timeline: _FakeTimeline,
                 clip: Optional[_FakeClip]) -> None:
        self._current_edit_project = SimpleNamespace(timelines={})
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        self._current_timeline_sequences: Dict[str, _FakeTimeline] = {}
        self.status_bar = MagicMock()
        self._refresh_calls: List[bool] = []
        self._found_clip = clip

    def _find_timeline_clip(self, clip_id: str) -> Optional[_FakeClip]:
        return self._found_clip

    def _refresh_edit_preview_after_composite_change(self) -> None:
        self._refresh_calls.append(True)


def test_bypass_composite_toggles_and_publishes_timeline() -> None:
    """切换 bypass：不可变更新 track/clip → 页面时间线 + 序列缓存 + 预览刷新。"""
    clip = _FakeClip("c1", "v1", composite_id="graph-1", bypassed=False)
    track = _FakeTrack("v1", [clip])
    timeline = _FakeTimeline("t1", [track])
    host = _BypassHost(timeline, clip)

    host._on_bypass_composite_requested("c1", "v1")

    # 命令已生效：新时间线发布到 edit_page 与序列缓存（保存取值源）。
    new_timeline = host.edit_page._current_timeline
    assert new_timeline is not timeline
    assert host._current_timeline_sequences["t1"] is new_timeline
    toggled = new_timeline.get_clip_by_id("c1")
    assert toggled is not None and toggled.composite_bypassed is True
    # 旧时间线保持不可变（原 clip 未被就地修改）。
    assert timeline.get_clip_by_id("c1").composite_bypassed is False
    # 预览刷新被触发（bypass 改变 Edit 预览取材）。
    assert host._refresh_calls == [True]
    assert "bypassed" in str(host.status_bar.showMessage.call_args)

    # 再切一次 → enabled，且发布的是更新后的时间线。
    host._found_clip = toggled
    host.edit_page = SimpleNamespace(_current_timeline=new_timeline)
    host._on_bypass_composite_requested("c1", "v1")
    again = host.edit_page._current_timeline.get_clip_by_id("c1")
    assert again is not None and again.composite_bypassed is False
    assert "enabled" in str(host.status_bar.showMessage.call_args)


def test_bypass_composite_without_composite_id_is_safe() -> None:
    """无 composite 的片段只提示，不触碰时间线。"""
    clip = _FakeClip("c2", "v1", composite_id=None)
    timeline = _FakeTimeline("t1", [_FakeTrack("v1", [clip])])
    host = _BypassHost(timeline, clip)

    host._on_bypass_composite_requested("c2", "v1")

    assert host.edit_page._current_timeline is timeline
    assert host._current_timeline_sequences == {}
    host.status_bar.showMessage.assert_called_once_with(
        "No composite to bypass", 2000)


def test_bypass_composite_without_timeline_warns_and_returns() -> None:
    """无当前时间线时告警返回，不再 AttributeError。"""
    clip = _FakeClip("c3", "v1", composite_id="graph-3")
    host = _BypassHost(timeline=None, clip=clip)  # type: ignore[arg-type]

    host._on_bypass_composite_requested("c3", "v1")

    host.status_bar.showMessage.assert_not_called()
    assert host._refresh_calls == []


# ═══════════════════════════════════════════════════════════════════════
# AUD-2703: transform drag end commits the command
# ═══════════════════════════════════════════════════════════════════════


class _FakeParamCommand:
    """UpdateClipParamCommand 替身（记录 apply 调用）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)
        self.applied_to: List[Any] = []

    def apply(self, timeline: Any) -> Any:
        self.applied_to.append(timeline)
        return getattr(self.new_value, "result_timeline", None)


class _FakeView:
    def __init__(self, timeline: Any) -> None:
        self._timeline = timeline
        self.executed: List[_FakeParamCommand] = []

    def execute_command(self, command: Any) -> None:
        self.executed.append(command)
        self._timeline = command.apply(self._timeline)


class _DragHost(EditControllerMixin):
    """最小宿主：只提供 on_clip_transform_drag_ended 触达的属性。"""

    def __init__(self, timeline: Any, view: Optional[_FakeView]) -> None:
        self.edit_page = SimpleNamespace(_current_timeline=timeline)
        if view is not None:
            self.timeline_panel = SimpleNamespace(
                get_timeline_view=lambda: view)
        else:
            self.timeline_panel = None
        self._sync_mock = MagicMock()
        self._sync_timeline_to_views = self._sync_mock  # type: ignore[method-assign]
        self._show_clip_mock = MagicMock()
        self._show_clip_in_properties = self._show_clip_mock  # type: ignore[method-assign]


@pytest.fixture
def fake_command_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ec, "get_command_classes",
        lambda: {"UpdateClipParamCommand": _FakeParamCommand},
    )


def test_transform_drag_end_commits_via_timeline_view(
        fake_command_factory: None) -> None:
    """有 TimelineView 时命令经 execute_command 提交（undo 栈/自动保存入口）。"""
    committed_timeline = SimpleNamespace(
        get_clip_by_id=lambda cid: _FakeClip(cid, "v1", None))
    initial_timeline = SimpleNamespace(
        get_clip_by_id=lambda cid: _FakeClip(cid, "v1", None))
    view = _FakeView(initial_timeline)
    host = _DragHost(initial_timeline, view)
    final_transform = SimpleNamespace(
        result_timeline=committed_timeline, x=1)

    host.on_clip_transform_drag_ended("c1", final_transform,
                                      original_transform=SimpleNamespace())

    assert len(view.executed) == 1
    command = view.executed[0]
    assert command.param_type == "transform"
    assert command.new_value is final_transform
    # 属性面板从提交后的时间线刷新。
    host._show_clip_mock.assert_called_once()
    refreshed = host._show_clip_mock.call_args[0][0]
    assert refreshed.clip_id == "c1"


def test_transform_drag_end_fallback_applies_and_syncs(
        fake_command_factory: None) -> None:
    """无 TimelineView（headless/测试）时直接 apply + 同步视图，不丢命令。"""
    committed_timeline = SimpleNamespace(
        get_clip_by_id=lambda cid: _FakeClip(cid, "v1", None))
    initial_timeline = SimpleNamespace(
        get_clip_by_id=lambda cid: _FakeClip(cid, "v1", None))
    host = _DragHost(initial_timeline, view=None)
    final_transform = SimpleNamespace(result_timeline=committed_timeline)

    host.on_clip_transform_drag_ended("c1", final_transform,
                                      original_transform=SimpleNamespace())

    # 命令已应用到旧时间线，产物发布到 edit_page 并同步视图。
    assert host.edit_page._current_timeline is committed_timeline
    host._sync_mock.assert_called_once_with(committed_timeline)
    host._show_clip_mock.assert_called_once()
