"""R07：Trim 边界帧的显示契约——请求身份 + 权威源时钟 + 模式扩展。

复审缺口：

- **身份**：同次拖动共用 generation，慢请求 A 的迟到结果仍能通过
  generation 校验覆盖新位置 B（请求无独立身份）；
- **权威时钟**：``_display_boundary`` 比例取整，对 reverse/曲线片段
  诚实退 None——边界帧显示能力缺口（导出/预览路径的
  ``timeline_to_source_frame`` 本就权威覆盖这些形态）；
- **模式**：只有 roll 调双帧；单边 trim/slip 无同等监视器体验
  （widget 的 ``update_trim`` 无人调用）。

契约：请求携带 ``request_id``（view 分配单调递增，worker 投递前双重
校验）；边界帧经权威映射（reverse/曲线/冻结/混fps 与导出采样一致）；
单边 trim 显示被修边帧 + 邻片/空位；slip 显示新源窗首末帧；切序列/
隐藏/销毁迟到帧不污染。
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import numpy as np
import pytest
from PyQt6.QtGui import QImage

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_ui


def _timeline(*clips: ClipInstance, fps: Rational = Rational(24, 1),
              ) -> TimelineSequence:
    return TimelineSequence(
        "trim-r07", "Trim", width=64, height=48, fps=fps,
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=clips),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


def _view(qtbot: Any, timeline: TimelineSequence) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    return view


def _clip(**kwargs: Any) -> ClipInstance:
    base = dict(
        clip_id="c1", asset_id="asset", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )
    base.update(kwargs)
    return ClipInstance(**base)


class _MonitorStub:
    """TrimMonitorWidget 替身：记录调用。"""

    def __init__(self) -> None:
        self.visible = False
        self.frames: dict[str, Any] = {}
        self.unavailable: list[str] = []
        self.pending = 0
        self.roll_calls: list[tuple] = []
        self.trim_calls: list[tuple] = []

    def isVisible(self) -> bool:
        return self.visible

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def set_frames_pending(self) -> None:
        self.pending += 1

    def set_frame_unavailable(self, side: str) -> None:
        self.unavailable.append(side)

    def update_frame(self, side: str, image: Any) -> None:
        self.frames[side] = image

    def update_roll(self, *args: Any, **kwargs: Any) -> None:
        self.roll_calls.append(args)

    def update_trim(self, *args: Any, **kwargs: Any) -> None:
        self.trim_calls.append(args)


class _SignalStub:
    def connect(self, *args: Any, **kwargs: Any) -> None:
        pass


class _ProviderStub:
    """TrimDualFrameProvider 替身：记录请求。"""

    def __init__(self) -> None:
        self.requests: list[tuple] = []
        self.signals = type(
            "Signals", (),
            {"frame_ready": _SignalStub(),
             "decode_failed": _SignalStub()})()

    def request(self, left_path, left_frame, right_path, right_frame,
                generation, request_id=None) -> None:
        self.requests.append((
            left_path, left_frame, right_path, right_frame,
            generation, request_id))

    def invalidate(self) -> None:
        self.requests.append(("invalidate",))

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _install(view: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[
        _MonitorStub, _ProviderStub]:
    import src.features.edit.ui.trim_frame_provider as provider_mod

    stub_provider = _ProviderStub()
    monkeypatch.setattr(
        provider_mod, "TrimDualFrameProvider", lambda *a, **k: stub_provider)
    monitor = _MonitorStub()
    view._trim_monitor_dialog = monitor
    return monitor, stub_provider


# ── 权威边界映射（②）：reverse/曲线/冻结/混fps 不再退 None ───────────


def test_r07_authoritative_boundary_matches_export_mapping(qtbot):
    """恒速：offset k 与导出映射逐点一致（回归保护）。"""
    view = _view(qtbot, _timeline(_clip()))
    clip = view.get_timeline().get_clip_by_id("c1")
    for offset in (0, 1, 50, 99):
        assert view._authoritative_boundary(clip, offset) == \
            clip.timeline_to_source_frame(clip.timeline_start + offset)


def test_r07_authoritative_boundary_reverse_and_curve(qtbot):
    """reverse/速度点曲线：权威映射给真实显示帧（旧比例法退 None）。"""
    view = _view(qtbot, _timeline(
        _clip(speed=SpeedParams(speed=1.0, reverse=True))))
    reversed_clip = view.get_timeline().get_clip_by_id("c1")
    frame = view._authoritative_boundary(reversed_clip, 10)
    assert frame is not None
    assert frame == reversed_clip.timeline_to_source_frame(
        reversed_clip.timeline_start + 10)

    curved = _clip(speed=SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=30, speed=2.0),)))
    view.load_timeline(_timeline(curved))
    curved_clip = view.get_timeline().get_clip_by_id("c1")
    frame = view._authoritative_boundary(curved_clip, 40)
    assert frame is not None
    assert frame == curved_clip.timeline_to_source_frame(
        curved_clip.timeline_start + 40)


def test_r07_authoritative_boundary_freeze_and_mixed_fps(qtbot):
    frozen = _clip().with_freeze_frame(7)
    view = _view(qtbot, _timeline(frozen))
    frozen_clip = view.get_timeline().get_clip_by_id("c1")
    assert view._authoritative_boundary(frozen_clip, 42) == 7

    mixed = _clip(
        _source_fps_num=48, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1)
    view.load_timeline(_timeline(mixed))
    mixed_clip = view.get_timeline().get_clip_by_id("c1")
    # 24fps 时间线第 1 tick = 源 2 帧（48fps 源）
    assert view._authoritative_boundary(mixed_clip, 1) == 2


def test_r07_authoritative_boundary_beyond_committed_geometry(qtbot):
    """roll 拖拽查询越过提交几何（offset > duration）仍可映射。"""
    view = _view(qtbot, _timeline(_clip(source_duration=300)))
    clip = view.get_timeline().get_clip_by_id("c1")
    # duration=100、offset 130（roll 右移 31）：仍应给出权威帧
    frame = view._authoritative_boundary(clip, 130)
    assert frame == 130


# ── 请求身份（③）：慢 A 迟到不得覆盖快 B ────────────────────────────


class _DelayedDecoder:
    def __init__(self, delay: float) -> None:
        self._delay = delay

    def read_frame(self, frame: int) -> object:
        time.sleep(self._delay)
        return frame  # frame_converter 负责转 RGB

    def close(self) -> None:
        return None


def test_r07_provider_drops_stale_request_results(qtbot):
    """慢 A（rid=1）解码期间快 B（rid=2）到达：A 不投递、不覆盖 B。

    跨线程信号是排队投递——等待期间泵送事件循环。
    """
    from PyQt6.QtWidgets import QApplication

    from src.features.edit.ui.trim_frame_provider import TrimDualFrameProvider

    def _pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            QApplication.processEvents()
            time.sleep(0.01)

    delivered: list[tuple] = []
    provider = TrimDualFrameProvider(
        decoder_factory=lambda path: _DelayedDecoder(
            0.25 if "slow" in path else 0.0),
        frame_converter=lambda decoded: np.zeros((2, 3, 3), np.uint8),
    )
    provider.signals.frame_ready.connect(
        lambda side, frame, image, gen, rid: delivered.append(
            (side, frame, gen, rid)))
    provider.start()
    try:
        provider.request("slow.mp4", 10, None, None, 7, request_id=1)
        _pump(0.05)  # worker 已拾取慢请求
        provider.request(None, None, "fast.mp4", 20, 7, request_id=2)
        deadline = time.time() + 2.0
        while time.time() < deadline and len(delivered) < 1:
            QApplication.processEvents()
            time.sleep(0.02)
        _pump(0.35)  # 让慢请求的丢弃路径走完（若未丢弃，此刻必已投递）
        assert delivered == [("right", 20, 7, 2)]
    finally:
        provider.stop()


def test_r07_view_rejects_stale_request_id(qtbot, monkeypatch):
    view = _view(qtbot, _timeline(_clip()))
    monitor, provider = _install(view, monkeypatch)
    monitor.visible = True

    # 视图提交 rid=5 后，rid=4 的迟到帧不得更新监视器
    view._trim_frame_request_seq = 5
    view._trim_frame_generation = 3
    img = QImage(2, 2, QImage.Format.Format_RGB888)
    view._on_trim_frame_ready("left", 5, img, 3, 5)
    assert monitor.frames.get("left") is img
    stale = QImage(2, 2, QImage.Format.Format_RGB888)
    view._on_trim_frame_ready("left", 4, stale, 3, 4)
    assert monitor.frames["left"] is img  # 未被覆盖
    stale = QImage(2, 2, QImage.Format.Format_RGB888)
    view._on_trim_frame_ready("left", 4, stale, 3, 4)
    assert monitor.frames["left"] is img  # 未被覆盖


def test_r07_timeline_switch_invalidates(qtbot, monkeypatch):
    """切序列：generation bump + 旧 generation 帧不投递。"""
    view = _view(qtbot, _timeline(_clip()))
    monitor, provider = _install(view, monkeypatch)
    view._trim_frame_generation = 1
    view.load_timeline(_timeline(_clip(clip_id="other")))

    assert "invalidate" in [r[0] for r in provider.requests] or \
        view._trim_frame_generation > 1
    img = QImage(2, 2, QImage.Format.Format_RGB888)
    monitor.visible = True
    view._on_trim_frame_ready("left", 1, img, 1, 1)  # 旧 generation
    assert "left" not in monitor.frames


# ── 模式扩展（①）：单边 trim + slip 的监视器 ────────────────────────


def test_r07_single_trim_left_monitor_with_neighbor(qtbot, monkeypatch):
    """单边左修：A 面板 = 被修片新入点首帧；B 面板 = 邻片在切点前帧。

    A(0..100)、B(100..200，源起点 50 有左余量)；把 B 左修到 90：
    cut=90，邻片 A 覆盖 89。
    """
    a = _clip(clip_id="A", timeline_start=0, timeline_duration=100)
    b = _clip(clip_id="B", timeline_start=100, timeline_duration=100,
              source_start=50, source_duration=150)
    view = _view(qtbot, _timeline(a, b))
    monitor, provider = _install(view, monkeypatch)
    monitor.visible = True

    view._on_clip_trim_left("B", "v1", 90)

    assert monitor.trim_calls, "single-edge trim must drive the monitor"
    (_lp, left_frame, _rp, right_frame, _gen, _rid) = provider.requests[-1]
    # A 面：B 的新首帧（新入点 = B 窗口 offset 0，源起点 50−10=40）
    assert left_frame == 40
    # B 面：邻片 A 在 cut-1=89 帧处 → A 的 offset 89
    assert right_frame == 89


def test_r07_single_trim_right_monitor_gap_placeholder(qtbot, monkeypatch):
    """单边右修到空位：无邻片覆盖新切点 → 该侧显式占位。"""
    a = _clip(clip_id="A", timeline_start=0, timeline_duration=100)
    view = _view(qtbot, _timeline(a))
    monitor, provider = _install(view, monkeypatch)
    monitor.visible = True

    view._on_clip_trim_right("A", "v1", 60)  # 新出点 60，右侧无邻片

    assert monitor.trim_calls
    assert "right" in monitor.unavailable
    (_lp, left_frame, _rp, _right_frame, _gen, _rid) = provider.requests[-1]
    assert left_frame is not None  # 被修侧照常给真实帧


def test_r07_slip_monitor_shows_new_window_ends(qtbot, monkeypatch):
    """slip：A = 新源窗首帧、B = 新源窗末帧（同片双画面）。"""
    clip = _clip(source_duration=300)
    view = _view(qtbot, _timeline(clip))
    monitor, provider = _install(view, monkeypatch)
    monitor.visible = True

    view._show_slip_monitor(
        view.get_timeline().get_clip_by_id("c1"), 25)

    assert monitor.roll_calls, "slip must drive the dual monitor"
    (_lp, left_frame, _rp, right_frame, _gen, _rid) = provider.requests[-1]
    # 新源窗 [25, 125)：首帧 25、末帧 124
    assert left_frame == 25
    assert right_frame == 124


def test_r07_noop_trim_does_not_drive_monitor(qtbot, monkeypatch):
    """零位移不进监视器（与零 history 契约一致）。"""
    a = _clip(clip_id="A", timeline_start=0, timeline_duration=100)
    view = _view(qtbot, _timeline(a))
    monitor, provider = _install(view, monkeypatch)
    monitor.visible = True

    view._on_clip_trim_left("A", "v1", 0)  # delta 0
    assert not monitor.trim_calls
    assert provider.requests == []
