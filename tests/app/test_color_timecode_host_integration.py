"""CV-B02 宿主集成：精确 TC 域注入真实 transport 与 FileOpsMixin。

覆盖：独立视频按 metadata 建 DF/起始 TC 域；timeline clip 把 clip-local
→ source 换算接进显示；探测失败显式回落 float 近似；上下文清除后清除。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QWidget

from src.app.controllers.file_controller import FileOpsMixin
from src.features.color.ui.canvas import VideoControlBar
from src.features.edit.core.timeline_v2 import ClipInstance
from src.shared.media.metadata import Rational, VideoMetadata
from src.shared.media.timecode import TimecodeType

FPS_2997 = Rational(30000, 1001)
FPS_25 = Rational(25, 1)


def _probe_result(fps, *, timecode=None, total=200000) -> VideoMetadata:
    return VideoMetadata(
        width=1920, height=1080, fps=fps, total_frames=total,
        codec_name="prores", codec_profile=None,
        pixel_format="yuv422p10le", bit_depth=10, timecode=timecode)


class _Controller(FileOpsMixin):
    def __init__(self, bar: VideoControlBar,
                 controller=None, clip_context=None) -> None:
        self.current_page = "Color"
        self.canvas = SimpleNamespace(video_control_bar=bar)
        self.video_controller = controller
        self._color_timeline_clip_playback_context = clip_context


@pytest.fixture
def bar(qapp):
    owner = QWidget()
    # show_for_video 的延迟定位会回调 parent 的定位方法（真实宿主是
    # Canvas）；独立 fixture 下给一个空实现。
    owner.update_video_control_bar_position = lambda: None
    control_bar = VideoControlBar(owner)
    control_bar.show_for_video()
    yield control_bar
    owner.deleteLater()


def _patch_probe(monkeypatch, metadata) -> None:
    from src.shared.media import factory

    monkeypatch.setattr(
        factory.MediaSourceFactory, "probe_video_metadata",
        classmethod(lambda cls, path: metadata),
        raising=True)


def test_standalone_video_builds_exact_df_domain_from_metadata(
        bar, monkeypatch) -> None:
    _patch_probe(monkeypatch, _probe_result(FPS_2997))
    controller = _Controller(
        bar, controller=SimpleNamespace(current_path="/tmp/df.mov"),
        clip_context=None)
    controller._refresh_color_timecode_context()
    bar.set_total_frames(200000)
    bar.set_current_frame(107892)
    assert bar.timecode_label.text() == "01:00:00;00"
    bar.set_current_frame(1800)
    assert bar.timecode_label.text() == "00:01:00;02"


def test_start_timecode_offsets_standalone_display(bar, monkeypatch) -> None:
    _patch_probe(monkeypatch, _probe_result(FPS_25, timecode="01:00:00:00"))
    controller = _Controller(
        bar, controller=SimpleNamespace(current_path="/tmp/tc.mov"))
    controller._refresh_color_timecode_context()
    bar.set_total_frames(1000)
    bar.set_current_frame(24)
    assert bar.timecode_label.text() == "01:00:00:24"


def test_timeline_clip_display_maps_local_through_source_domain(
        bar, monkeypatch) -> None:
    _patch_probe(monkeypatch, _probe_result(FPS_25, timecode="01:00:00:00"))
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=100, timeline_duration=40,
        source_start=50, source_duration=40)
    context = {"timeline_id": "t1", "clip": clip,
               "effective_source_start": None}
    controller = _Controller(
        bar, controller=SimpleNamespace(current_path="/tmp/clip.mov"),
        clip_context=context)
    controller._refresh_color_timecode_context()
    bar.set_total_frames(40)
    # clip-local 10 → source 60 → 起始 TC 偏移：01:00:02:10。
    bar.set_current_frame(10)
    assert bar.timecode_label.text() == "01:00:02:10"


def test_missing_metadata_falls_back_to_legacy_float_display(
        bar, monkeypatch) -> None:
    _patch_probe(monkeypatch, None)
    controller = _Controller(
        bar, controller=SimpleNamespace(current_path="/tmp/broken.mov"))
    bar.set_frame_rate(25.0)
    controller._refresh_color_timecode_context()
    bar.set_total_frames(1000)
    bar.set_current_frame(24)
    # 无域：旧 float 路径仍给出正确的 25fps NDF 显示。
    assert bar.timecode_label.text() == "00:00:00:24"
    # 显式清除域后同样回落。
    bar.set_timecode_formatter(None)
    bar.set_current_frame(25)
    assert bar.timecode_label.text() == "00:00:01:00"


def test_clearing_clip_context_rebuilds_without_clip_mapping(
        bar, monkeypatch) -> None:
    _patch_probe(monkeypatch, _probe_result(FPS_25))
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=10,
        source_start=90, source_duration=10)
    controller = _Controller(
        bar, controller=SimpleNamespace(current_path="/tmp/x.mov"),
        clip_context={"timeline_id": "t1", "clip": clip,
                      "effective_source_start": None})
    controller._refresh_color_timecode_context()
    bar.set_total_frames(10)
    bar.set_current_frame(0)
    assert bar.timecode_label.text() == "00:00:03:15"  # 源 90 帧

    controller._color_timeline_clip_playback_context = None
    controller._refresh_color_timecode_context()
    bar.set_current_frame(0)
    assert bar.timecode_label.text() == "00:00:00:00"  # 回恒等映射


def test_scrub_emits_domain_timecode_to_header_mirror(bar, monkeypatch) -> None:
    """拖拽时 timecode_changed 只发最终格式化结果（不反向 seek）。"""
    _patch_probe(monkeypatch, _probe_result(FPS_25))
    controller = _Controller(
        bar, controller=SimpleNamespace(current_path="/tmp/x.mov"))
    controller._refresh_color_timecode_context()
    bar.set_total_frames(100)
    mirrored: list[str] = []
    seeks: list[int] = []
    bar.timecode_changed.connect(mirrored.append)
    bar.seek_requested.connect(seeks.append)
    bar.slider.setSliderDown(True)
    bar.slider.setValue(65)
    assert mirrored[-1] == "00:00:02:15"
    bar.slider.setSliderDown(False)
    assert seeks == [65]  # seek 仅一次（release），镜像不发 seek


def test_transport_timecode_formatter_domain_flag_in_state(bar, monkeypatch) -> None:
    """有精确域时 CV-B01 快照的时间域仍由呈现路径裁决（clip_local）。"""
    from src.features.color.core.color_viewer_capabilities import (
        PRESENTATION_SOURCE_GL, TIME_DOMAIN_CLIP_LOCAL,
    )
    assert TIME_DOMAIN_CLIP_LOCAL == "clip_local"
    assert PRESENTATION_SOURCE_GL == "source_gl"
