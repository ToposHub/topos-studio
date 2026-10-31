"""E08/E09: 图片调色入口与片段双击打开 Source。

审计实测：
- ``open_asset_grade_requested``（图片右键 Open Asset Grade in Color）
  全仓 0 connect；
- ``ClipItem.clip_double_clicked`` 0 接收者，"在 Source 中打开"不可用。

回归锁定：
- 宿主 connect 存在（dock_layout_controller / edit_page 源码）；
- E08 处理器：非图片素材拒绝并有提示；图片素材复用既有 asset 栈；
- E09 处理器：按 clip 找 asset、打开并 seek；Motion Graphic 跳过；
- TimelineView 计算的源帧夹在片段源窗口内（_on_clip_double_clicked 语义）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.ui.edit_page import EditPage
from src.features.edit.ui.timeline_view import TimelineView

_DOCK = (
    Path(__file__).resolve().parents[2] / "src" / "app" / "controllers" /
    "dock_layout_controller.py"
)
_EDIT_PAGE = (
    Path(__file__).resolve().parents[2] / "src" / "features" / "edit" /
    "ui" / "edit_page.py"
)
_TIMELINE_VIEW = _EDIT_PAGE.parent / "timeline_view.py"


def _timeline_with_clip(clip_id: str, asset_id: str) -> TimelineSequence:
    clip = ClipInstance(
        clip_id=clip_id, track_id="v1", asset_id=asset_id,
        timeline_start=0, timeline_duration=10,
        source_start=0, source_duration=10,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    return TimelineSequence(
        timeline_id="tl", name="T", fps=25.0,
    ).with_track_added(track).with_clip_added("v1", clip)


class TestGradeEntryWiring:
    def test_dock_controller_connects_grade_signal(self) -> None:
        text = _DOCK.read_text(encoding="utf-8")
        assert "open_asset_grade_requested.connect" in text
        assert "_on_open_asset_grade_in_color" in text

    def test_non_image_asset_rejected(self) -> None:
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        project = SimpleNamespace(
            get_asset_by_id=lambda aid: SimpleNamespace(
                asset_id=aid, media_type="video") if aid == "v1" else None,
            get_color_stack_for_asset=lambda aid: None,
        )
        ctrl._current_edit_project = project
        ctrl._status_messages: list[str] = []
        ctrl.status_bar = SimpleNamespace(
            showMessage=lambda m, t=5000: ctrl._status_messages.append(m))
        ctrl._on_open_asset_grade_in_color("v1")
        assert ctrl._status_messages, "视频素材必须提示走时间线路径"

    def test_existing_stack_reused_and_page_switched(self) -> None:
        ctrl = EditControllerMixin.__new__(EditControllerMixin)
        stack = SimpleNamespace(stack_id="stack-1")
        project = SimpleNamespace(
            get_asset_by_id=lambda aid: SimpleNamespace(
                asset_id=aid, media_type="image") if aid == "img1" else None,
            get_color_stack_for_asset=lambda aid: stack,
        )
        ctrl._current_edit_project = project
        ctrl._status_messages: list[str] = []
        ctrl.status_bar = SimpleNamespace(
            showMessage=lambda m, t=5000: ctrl._status_messages.append(m))
        visited: list[str] = []
        ctrl.switch_to_page = lambda page: visited.append(page)
        loaded: list[str] = []
        ctrl._load_color_stack_to_grading_panel = loaded.append
        ctrl._on_open_asset_grade_in_color("img1")
        assert visited == ["Color"]
        assert loaded == ["stack-1"]


class TestClipDoubleClickWiring:
    def test_edit_page_connects_double_click_relay(self) -> None:
        text = _EDIT_PAGE.read_text(encoding="utf-8")
        assert "clip_open_in_source_requested.connect" in text
        assert "_on_clip_open_in_source" in text

    def test_view_emits_source_frame_clamped_to_clip_window(self) -> None:
        """播放头远超片段末端时，源帧夹在 [source_start, source_start+duration)。"""
        view = TimelineView.__new__(TimelineView)
        clip = ClipInstance(
            clip_id="c1", track_id="v1", asset_id="a1",
            timeline_start=100, timeline_duration=50,
            source_start=10, source_duration=50,
        )
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        view._timeline = TimelineSequence(
            timeline_id="tl", name="T", fps=25.0,
        ).with_track_added(track).with_clip_added("v1", clip)
        view._playhead_frame = 500  # 远超片段末端
        emitted: list[tuple[str, int]] = []

        class _Sig:  # 实例属性遮蔽类信号（未初始化 QObject 不能 connect）
            @staticmethod
            def emit(clip_id: str, frame: int) -> None:
                emitted.append((clip_id, frame))

        view.clip_open_in_source_requested = _Sig()
        view._on_clip_double_clicked("c1", "v1")
        assert emitted == [("c1", 10 + 50 - 1)]  # 夹到源窗口末帧

    def test_handler_opens_asset_and_seeks(self) -> None:
        page = EditPage.__new__(EditPage)
        opened: list = []
        sought: list[int] = []
        def _open(asset, **_kwargs):
            opened.append(asset)  # F2.1/F2.10/F4.1 关键字参数不阻断记录

        page._source_viewer = SimpleNamespace(
            open_asset=_open, seek=sought.append)
        page._current_timeline = _timeline_with_clip("c1", "a1")
        asset = SimpleNamespace(asset_id="a1", is_motion_graphic=False)
        page._main_window = SimpleNamespace(
            _current_edit_project=SimpleNamespace(
                get_asset_by_id=lambda aid: asset))
        page._media_pool_panel = None

        page._on_clip_open_in_source("c1", 5)
        assert opened == [asset]
        assert sought == [5]

    def test_motion_graphic_skipped(self) -> None:
        page = EditPage.__new__(EditPage)
        opened: list = []
        def _open(asset, **_kwargs):
            opened.append(asset)

        page._source_viewer = SimpleNamespace(
            open_asset=_open, seek=lambda f: None)
        page._current_timeline = _timeline_with_clip("c1", "mg1")
        asset = SimpleNamespace(asset_id="mg1", is_motion_graphic=True)
        page._main_window = SimpleNamespace(
            _current_edit_project=SimpleNamespace(
                get_asset_by_id=lambda aid: asset))
        page._media_pool_panel = None

        page._on_clip_open_in_source("c1", 3)
        assert opened == []  # Motion Graphic 不进 Source Viewer
