"""wd14-g68 修复批次的回归测试（REMEDIATION_PLAN 批 D，edit UI 尾段）。

覆盖本批 16 个条目的根因修复：
AUD-20903 / 20910 / 21008 / 21101 / 21102 / 21103 / 21104 / 21105 /
21200 / 21201 / 21301 / 21302 / 21303 / 21400 / 21401 / 21402。

可离线功能验证的条目用行为测试；菜单/hover 等无法离线驱动模态交互的
条目用结构断言（inspect.getsource，仓库既有先例 test_fix_wd13g67_aud.py）。
"""

from __future__ import annotations

import inspect
import math
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.features.edit.core.project import MediaAsset
from src.features.edit.core.timeline_v2 import SubtitleClip
from src.features.edit.ui.source_viewer import (
    _CpuFallbackDecodeWorker,
    SourceViewerWidget,
)
from src.features.edit.ui.stem_export_dialog import StemExportDialog
from src.features.edit.ui.subtitle_handle_controller import SubtitleHandleController
from src.features.edit.ui.timeline_items import (
    DragPreviewItem,
    TransitionItem,
)
from src.features.edit.ui.timeline_layout import (
    TimelineLayoutEngine,
    TimelineLayoutState,
    TRACK_AREA_TOP,
    TrackInfo,
)

pytestmark = pytest.mark.edit_core

_SOURCE_ROOT = Path(__file__).resolve().parents[2]


# ============================================================================
# AUD-20903: Source Viewer CPU 回退解码异步化
# ============================================================================

class _RecordingCpuWorker:
    """Fake worker capturing requests (no thread)."""

    def __init__(self) -> None:
        self.requests: list[tuple] = []
        self.closed = False

    def request(self, asset_path, frame, generation, target_size):
        self.requests.append((asset_path, frame, generation, target_size))

    def close_decoder(self) -> None:
        self.closed = True


def _video_asset() -> MediaAsset:
    return MediaAsset(
        asset_id="asset-cpu",
        path="/tmp/cpu-fallback.mov",
        media_type="video",
        display_name="cpu-fallback.mov",
        metadata={"fps": 24.0, "total_frames": 60, "width": 640, "height": 360},
    )


def test_aud_20903_submit_cpu_fallback_dispatches_to_worker(qapp) -> None:
    """CPU 回退路径把解码请求交给后台 worker 并标记在途帧（不再同步解码）。"""
    widget = SourceViewerWidget()
    try:
        recorder = _RecordingCpuWorker()
        widget._cpu_worker = recorder  # type: ignore[assignment]
        widget.open_asset(_video_asset())
        generation = widget._gpu_generation

        widget._submit_cpu_fallback(7)

        assert recorder.requests, "request must be forwarded to the worker"
        path, frame, gen, target_size = recorder.requests[-1]
        assert path == "/tmp/cpu-fallback.mov"
        assert frame == 7
        assert gen == generation
        # 在途标记供播放背压使用
        assert widget._gpu_pending_frame == 7
    finally:
        widget.close_source()


def test_aud_20903_cpu_frame_ready_uploads_and_caches(qapp) -> None:
    """worker 结果经 staleness/generation 守卫后上传、写缓存并清在途标记。"""
    from PyQt6.QtGui import QImage

    widget = SourceViewerWidget()
    try:
        widget.open_asset(_video_asset())
        widget._gpu_expected_frame = 10
        widget._gpu_pending_frame = 10
        widget._error_message = "gpu_timeout"

        uploaded: list[tuple] = []
        widget.viewer.submit_qimage = lambda *a, **k: uploaded.append((a, k))  # type: ignore[method-assign]

        image = QImage(64, 36, QImage.Format.Format_RGB888)
        widget._on_cpu_frame_ready(10, image, 3.5, widget._gpu_generation)

        assert uploaded, "frame must be uploaded to the viewer"
        assert uploaded[0][0][0] is image
        assert widget._frame_cache.get(10) is image
        assert widget._last_good_frame == 10
        assert widget._error_message is None
        assert widget._gpu_pending_frame == -1

        # 旧 generation 的结果被丢弃
        uploaded.clear()
        stale = QImage(64, 36, QImage.Format.Format_RGB888)
        widget._on_cpu_frame_ready(11, stale, 3.5, widget._gpu_generation + 5)
        assert not uploaded
        assert widget._frame_cache.get(11) is None
    finally:
        widget.close_source()


def test_aud_20903_cpu_decode_failed_falls_back_to_last_good(qapp) -> None:
    widget = SourceViewerWidget()
    try:
        widget.open_asset(_video_asset())
        widget._error_message = None

        uploaded: list[tuple] = []
        widget.viewer.submit_qimage = lambda *a, **k: uploaded.append((a, k))  # type: ignore[method-assign]

        widget._on_cpu_decode_failed(4, "boom", widget._gpu_generation)
        # No last good frame yet -> error only
        assert not uploaded
        assert widget._error_message == "decode_error"

        from PyQt6.QtGui import QImage
        good = QImage(64, 36, QImage.Format.Format_RGB888)
        widget._last_good_qimage = good
        widget._on_cpu_decode_failed(5, "boom", widget._gpu_generation)
        assert uploaded and uploaded[0][0][0] is good
        assert widget._error_message == "decode_fallback"
    finally:
        widget.close_source()


def test_aud_20903_worker_decodes_on_background_thread(monkeypatch) -> None:
    """worker 线程完成解码并以 QImage/错误经信号回传（latest-wins 语义）。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage

    closed = threading.Event()
    import numpy as np

    decoded = SimpleNamespace(
        data=np.zeros((12, 16, 3), dtype=np.uint8),
    )

    class _FakeDecoder:
        def read_frame(self, frame: int):
            return decoded

        def close(self) -> None:
            closed.set()

    def _fake_open(path, target_size):
        return _FakeDecoder()

    import src.features.edit.ui.source_viewer as sv_module
    monkeypatch.setattr(sv_module, "_open_cpu_decode_source", _fake_open)

    worker = _CpuFallbackDecodeWorker()
    ready: list[tuple] = []
    failed: list[tuple] = []
    got = threading.Event()
    worker.signals.frame_ready.connect(
        lambda *a: (ready.append(a), got.set()),
        Qt.ConnectionType.DirectConnection,
    )
    worker.signals.decode_failed.connect(
        lambda *a: failed.append(a), Qt.ConnectionType.DirectConnection,
    )
    worker.start()
    try:
        worker.request("/tmp/x.mov", 3, generation=11, target_size=None)
        assert got.wait(timeout=5.0), "worker must emit frame_ready"
        frame, image, decode_ms, generation = ready[0]
        assert frame == 3
        assert generation == 11
        assert isinstance(image, QImage)
        assert image.width() == 16 and image.height() == 12
        assert not failed

        # close_decoder 在 worker 线程内释放解码器句柄
        worker.close_decoder()
        assert closed.wait(timeout=5.0), "decoder must be closed on the worker"
    finally:
        worker.close_decoder()


def test_aud_20903_load_video_frame_cpu_path_is_non_blocking(qapp, monkeypatch) -> None:
    """无 GPU provider 时 _load_video_frame 只提交请求，不同步解码。"""
    widget = SourceViewerWidget()
    try:
        widget.open_asset(_video_asset())
        widget._gpu_frame_provider = None
        recorder = _RecordingCpuWorker()
        widget._cpu_worker = recorder  # type: ignore[assignment]

        widget._load_video_frame(5)

        assert recorder.requests, "CPU fallback must be dispatched to the worker"
        assert recorder.requests[-1][1] == 5
    finally:
        widget.close_source()


# ============================================================================
# AUD-20910: Stem 导出 join 超时后禁止关闭对话框
# ============================================================================

class _StubWorker:
    """可控的 StemExportWorker 替身。"""

    def __init__(self, running: bool, wait_succeeds: bool) -> None:
        self.running = running
        self.wait_succeeds = wait_succeeds
        self.cancelled = False
        self.waited_ms: list[int] = []

    def isRunning(self) -> bool:  # noqa: N802
        return self.running

    def cancel(self) -> None:
        self.cancelled = True

    def wait(self, ms: int) -> bool:
        self.waited_ms.append(ms)
        return self.wait_succeeds


def _stem_result(ok: bool = True):
    return SimpleNamespace(
        cancelled=False,
        all_succeeded=ok,
        stems=[("master", ok)],
        output_dir="/tmp/stems",
    )


def test_aud_20910_reject_blocked_while_worker_alive(qapp) -> None:
    """join 超时后 reject() 不关闭对话框并进入等待后台完成状态。"""
    dialog = StemExportDialog(sequence=None)
    stub = _StubWorker(running=True, wait_succeeds=False)
    dialog._worker = stub  # type: ignore[assignment]

    dialog.reject()

    # worker 已被请求取消并等待过
    assert stub.cancelled and stub.waited_ms
    # 对话框未进入 Rejected 状态（保持存活）
    assert dialog.result() == 0
    # Close 按钮被禁用（等待后台完成）
    from PyQt6.QtWidgets import QDialogButtonBox
    close_btn = dialog._button_box.button(QDialogButtonBox.StandardButton.Close)
    assert close_btn is not None and not close_btn.isEnabled()

    # worker 结束后恢复正常：finished 回调重新启用 Close
    stub.running = False
    dialog._on_finished(_stem_result())
    assert close_btn.isEnabled()


def test_aud_20910_close_event_ignored_while_worker_alive(qapp) -> None:
    dialog = StemExportDialog(sequence=None)
    stub = _StubWorker(running=True, wait_succeeds=False)
    dialog._worker = stub  # type: ignore[assignment]

    event = SimpleNamespace(ignore_calls=0)

    def _ignore() -> None:
        event.ignore_calls += 1

    event.ignore = _ignore
    dialog.closeEvent(event)
    assert event.ignore_calls == 1
    assert dialog.result() == 0


def test_aud_20910_reject_allowed_once_worker_exited(qapp) -> None:
    dialog = StemExportDialog(sequence=None)
    stub = _StubWorker(running=False, wait_succeeds=False)
    dialog._worker = stub  # type: ignore[assignment]
    from PyQt6.QtWidgets import QDialogButtonBox
    close_btn = dialog._button_box.button(QDialogButtonBox.StandardButton.Close)

    assert dialog._cancel_and_join_worker() is True
    dialog.reject()
    # 未进入等待状态：Close 保持可用，状态栏无等待文案
    assert close_btn.isEnabled()
    assert "后台" not in dialog._status_label.text()


# ============================================================================
# AUD-21008: 字幕 end_drag 取值空间统一
# ============================================================================

class _FakeViewer:
    def update(self) -> None:  # noqa: N802 — Qt 命名
        pass


def _subtitle_clip() -> SubtitleClip:
    return SubtitleClip(
        clip_id="cue-1",
        track_id="s1",
        text="hello",
        translate_x=0.0,
        translate_y=0.0,
        scale_x=1.0,
        scale_y=1.0,
        rotation=0.0,
        pivot_mode="center",
        pivot_x=0.0,
        pivot_y=0.0,
    )


def _controller_with_track_style() -> tuple[SubtitleHandleController, list[tuple]]:
    """轨道 default_style 覆盖 translate_x=100，cue 扁平基线为 0。"""
    controller = SubtitleHandleController(_FakeViewer())  # type: ignore[arg-type]
    style = {"translate_x": 100.0, "translate_y": 5.0}
    controller.set_clip(_subtitle_clip(), style)
    calls: list[tuple] = []
    controller.on_drag_ended = lambda clip_id, delta, original: calls.append(
        (clip_id, dict(delta), dict(original))
    )
    return controller, calls


def test_aud_21008_zero_move_press_release_yields_empty_delta() -> None:
    """resolved 覆盖字段 + 零位移 press-release 不再产生伪 delta。"""
    controller, calls = _controller_with_track_style()
    from PyQt6.QtCore import QPointF

    assert controller.start_drag("scale_br", QPointF(10, 10), 0) is True
    controller.end_drag()

    assert len(calls) == 1
    clip_id, delta, original = calls[0]
    assert clip_id == "cue-1"
    # 修复前：final 读 cue 扁平字段(0)，与 original(100) 相差 → 伪 delta
    assert delta == {}, f"zero-move drag must produce empty delta, got {delta}"
    assert original["translate_x"] == 100.0


def test_aud_21008_applied_drag_delta_commits_in_same_space() -> None:
    """实际拖拽只提交真实变化的字段，且值处于 resolved 空间。"""
    controller, calls = _controller_with_track_style()
    from PyQt6.QtCore import QPointF

    controller.start_drag("scale_br", QPointF(10, 10), 0)
    controller._apply_preview_delta({"translate_x": 118.0, "scale_y": 1.25})
    controller.end_drag()

    assert len(calls) == 1
    _clip_id, delta, original = calls[0]
    assert delta == {"translate_x": 118.0, "scale_y": 1.25}
    assert original["translate_x"] == 100.0


def test_aud_21008_drag_back_to_original_is_noop() -> None:
    """拖走又拖回原值 → 无 delta（不压入无操作 undo 项）。"""
    controller, calls = _controller_with_track_style()
    from PyQt6.QtCore import QPointF

    controller.start_drag("scale_br", QPointF(10, 10), 0)
    controller._apply_preview_delta({"translate_x": 140.0})
    controller._apply_preview_delta({"translate_x": 100.0})  # last-write-wins
    controller.end_drag()

    assert calls[0][1] == {}


# ============================================================================
# AUD-21101: Audio Gain 菜单项在 menu.exec() 之前添加
# ============================================================================

def _clip_item_class_source() -> str:
    from src.features.edit.ui.timeline_items import ClipItem
    return inspect.getsource(ClipItem)


def test_aud_21101_audio_gain_action_added_before_menu_exec() -> None:
    src = _clip_item_class_source()
    add_idx = src.index("audio_gain_action = menu.addAction(")
    exec_idx = src.index("action = menu.exec(screen_pos)")
    assert add_idx < exec_idx, (
        "Audio Gain addAction must run before the modal menu.exec()"
    )


# ============================================================================
# AUD-21102: TransitionItem 轨道锁
# ============================================================================

def test_aud_21102_transition_item_track_lock_state() -> None:
    item = TransitionItem(
        "t1", "v1", 100, 12, 5.0, 0,
        is_track_locked=True,
    )
    assert item._is_track_locked is True
    item.set_track_locked(False)
    assert item._is_track_locked is False
    item.set_track_locked(True)
    assert item._is_track_locked is True


def test_aud_21102_locked_transition_press_allows_selection_only(qapp) -> None:
    from PyQt6.QtCore import Qt

    item = TransitionItem(
        "t1", "v1", 100, 12, 5.0, 0, is_track_locked=True,
    )
    clicked: list[tuple[str, str]] = []
    item.transition_clicked.connect(
        lambda tid, trk: clicked.append((tid, trk)))

    for button in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
        pressed = {"accept": 0}

        def _accept() -> None:
            pressed["accept"] += 1

        event = SimpleNamespace(button=lambda b=button: b, accept=_accept)
        item.mousePressEvent(event)
        assert pressed["accept"] == 1, "locked press must be consumed"
        assert item._is_dragging is False
        assert item._drag_mode is None
        assert item._drag_duration_label_visible is False

    # 选中信号仍发出（与 ClipItem 锁定语义一致）
    assert clicked == [("t1", "v1")] * 2


def test_aud_21102_view_finished_handlers_guard_locked_track(qapp) -> None:
    """view 侧转场修剪/移动 finished handler 在锁轨时直接返回。"""
    from src.features.edit.ui.timeline_view import TimelineView

    guard_calls: list[str] = []

    class _FakeView:
        _timeline = object()
        _updating = False

        def _is_track_locked(self, track_id: str) -> bool:
            guard_calls.append(track_id)
            return True

    fake = _FakeView()
    TimelineView._on_transition_trim_left_finished(fake, "t1", "v1", 10, 20)
    TimelineView._on_transition_trim_right_finished(fake, "t1", "v1", 10, 20)
    TimelineView._on_transition_move_finished(fake, "t1", "v1", 100, 120)
    assert guard_calls == ["v1", "v1", "v1"]


# ============================================================================
# AUD-21103: ClipItem.hoverMoveEvent 差异检测
# ============================================================================

def test_aud_21103_hover_move_updates_only_on_state_change() -> None:
    src = _clip_item_class_source()
    hover_src = src[src.index("def hoverMoveEvent("):]
    hover_src = hover_src[: hover_src.index("def hoverEnterEvent(")]
    assert "new_edge != old_edge or new_fade != old_fade" in hover_src, (
        "hover state must be change-detected before update()"
    )
    # update() 只允许出现在差异检测分支内（无条件 update 已移除）
    assert hover_src.count("self.update()") == 1
    branch_idx = hover_src.index("new_edge != old_edge or new_fade != old_fade")
    assert hover_src.index("self.update()") > branch_idx


# ============================================================================
# AUD-21104: 轨道头 target/sync 动作 P4 提交一致性
# ============================================================================

def test_aud_21104_header_menu_actions_do_not_pre_write_state() -> None:
    from src.features.edit.ui.timeline_items import TrackHeaderItem

    src = inspect.getsource(TrackHeaderItem)
    assert "self._is_target = target_action.isChecked()" not in src
    assert "self._is_sync_locked = sync_action.isChecked()" not in src
    # 只 emit 请求（target 携带 track_id，sync 携带勾选态）
    assert "self.target_track_changed.emit(self._track_id)" in src
    assert "self.sync_lock_changed.emit(self._track_id, sync_action.isChecked())" in src


# ============================================================================
# AUD-21105: DragPreviewItem._draw_thumbnail 零尺寸防护
# ============================================================================

def test_aud_21105_draw_thumbnail_survives_zero_size_pixmap(qapp) -> None:
    from PyQt6.QtGui import QImage, QPainter, QPixmap

    item = DragPreviewItem()
    canvas = QPixmap(64, 64)
    painter = QPainter(canvas)
    try:
        # 空 QPixmap（width/height 为 0）——修复前此处 ZeroDivisionError
        item._draw_thumbnail(painter, 0.0, 0.0, 60.0, QPixmap())
        # 有效高度不足（avail_h <= 0）同样安全
        item._draw_thumbnail(
            painter, 0.0, 0.0, float(2 * item.ITEM_MARGIN + item.LABEL_HEIGHT),
            QPixmap(32, 32),
        )
        # 正常路径仍绘制
        item._draw_thumbnail(painter, 0.0, 0.0, 90.0, QPixmap(32, 24))
    finally:
        painter.end()
    assert not QImage(canvas).isNull()


# ============================================================================
# AUD-21200 / AUD-21201: 布局引擎
# ============================================================================

def _overflow_engine() -> TimelineLayoutEngine:
    """字幕轨在场 + 视频栈溢出视频面板的布局配置。"""
    engine = TimelineLayoutEngine()
    state = TimelineLayoutState(video_area_ratio=0.5, video_scroll_offset=-1)
    tracks = [TrackInfo("s1", "subtitle")]
    tracks += [TrackInfo(f"v{i}", "video") for i in range(1, 9)]
    engine.configure(tracks, state, 1000, 520, 5.0)
    return engine


def test_aud_21200_subtitle_top_clamped_below_ruler_when_overflow() -> None:
    engine = _overflow_engine()
    engine.track_rect("v1")  # force compute
    assert engine._area.subtitle_area.top() >= TRACK_AREA_TOP - 1e-9


def test_aud_21200_video_scroll_offset_drives_layout_with_subtitles() -> None:
    engine = _overflow_engine()
    engine.track_rect("v1")  # force _ensure_computed
    sv_bottom = engine._area.subtitle_splitter_rect.bottom()
    va_top = engine._area.splitter_rect.top()
    full_stack = engine._stack_height(list(reversed(engine._video_tracks)))

    assert full_stack > va_top - sv_bottom, "test config must overflow"

    # 默认 -1：底部对齐（V1 底边贴 V/A 分隔条）
    top_video_id = engine._video_tracks[-1].track_id  # 最高轨
    v1_bottom = engine.track_rect("v1").bottom()
    assert abs(v1_bottom - va_top) < 1e-6

    # offset=0：最高视频轨顶边贴 S/V 分隔条底（滚动条不再死条）
    state0 = replace(engine.state, video_scroll_offset=0)
    engine.set_state(state0)
    top_at_zero = engine.track_rect(top_video_id).top()
    assert abs(top_at_zero - sv_bottom) < 1e-6

    # 中间偏移：offset 增大 → 栈向上滚动（y 减小），与无字幕分支方向一致
    engine.set_state(replace(engine.state, video_scroll_offset=40))
    assert engine.track_rect(top_video_id).top() < top_at_zero


def test_aud_21200_scroll_range_matches_available_video_span() -> None:
    engine = _overflow_engine()
    engine.track_rect("v1")  # force _ensure_computed
    lo, hi, page = engine.scroll_range_for_area("video")
    assert lo == 0
    assert hi > 0, "overflow config must expose a scrollable range"
    sv_bottom = engine._area.subtitle_splitter_rect.bottom()
    va_top = engine._area.splitter_rect.top()
    full_stack = engine._stack_height(list(reversed(engine._video_tracks)))
    expected = int(math.ceil(full_stack - (va_top - sv_bottom)))
    assert hi == expected
    assert page == int(va_top - sv_bottom)


def test_aud_21200_no_subtitle_layout_unchanged() -> None:
    """无字幕轨的既有滚动语义保持不变（回归保护）。"""
    engine = TimelineLayoutEngine()
    state = TimelineLayoutState(video_area_ratio=0.5, video_scroll_offset=-1)
    tracks = [TrackInfo(f"v{i}", "video") for i in range(1, 9)]
    tracks += [TrackInfo("a1", "audio")]
    engine.configure(tracks, state, 1000, 300, 5.0)
    top_id = engine._video_tracks[-1].track_id
    bottom_anchored = engine.track_rect("v1").bottom()
    assert abs(bottom_anchored - engine._area.splitter_rect.top()) < 1e-6
    engine.set_state(replace(state, video_scroll_offset=0))
    assert engine.track_rect(top_id).top() >= engine._area.video_area.top() - 1e-6


def test_aud_21201_from_dict_tolerates_corrupt_payloads() -> None:
    state = TimelineLayoutState.from_dict({
        "video_area_ratio": "abc",          # 非法字符串
        "subtitle_area_ratio": None,        # None
        "track_heights": {"v1": "high", "v2": 90, 3: "42", "a1": None},
        "video_scroll_offset": [],          # 不可转换
        "audio_scroll_offset": {"x": 1},
    })
    assert state.video_area_ratio == 0.5
    assert state.subtitle_area_ratio == TimelineLayoutState().subtitle_area_ratio
    assert state.video_scroll_offset == -1
    assert state.audio_scroll_offset == 0
    # 合法项保留（int 键与 str 高度），非法项丢弃
    assert state.track_heights == {"v2": 90, "3": 42}


def test_aud_21201_from_dict_clamps_out_of_range_ratios() -> None:
    state = TimelineLayoutState.from_dict({
        "video_area_ratio": 7.5,
        "subtitle_area_ratio": -3.0,
        "video_scroll_offset": 1_000_000,
    })
    assert state.video_area_ratio == 0.9
    assert state.subtitle_area_ratio == 0.05
    # scroll offset 属于钳制职责在布局计算侧（_compute_track_rects），
    # from_dict 只保证类型安全
    assert state.video_scroll_offset == 1_000_000


def test_aud_21201_from_dict_non_dict_and_empty() -> None:
    assert TimelineLayoutState.from_dict(None).video_area_ratio == 0.5
    assert TimelineLayoutState.from_dict("junk").video_area_ratio == 0.5
    empty = TimelineLayoutState.from_dict({})
    assert empty == TimelineLayoutState()


# ============================================================================
# AUD-21301: 键盘切工具时按钮互斥状态经按钮表驱动
# ============================================================================

def test_aud_21301_on_view_tool_changed_uses_button_map() -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    src = inspect.getsource(TimelinePanel._on_view_tool_changed)
    assert "_get_tool_button_map()" in src
    assert "btn.setChecked(tool_enum.value == tool_name)" in src
    # 手写清单已移除（漂移源头）
    for stale in ("btn_roll.setChecked", "btn_zoom.setChecked",
                  "btn_rate_stretch.setChecked"):
        assert stale not in src


# ============================================================================
# AUD-21302: _on_timer_frame_ready 隐藏视图守卫
# ============================================================================

def test_aud_21302_timer_frame_ready_skips_hidden_view() -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    touched: list[str] = []
    hidden_view = SimpleNamespace(isVisible=lambda: False)
    panel = SimpleNamespace(
        timeline_view=hidden_view,
        _should_defer_playback_tick=lambda frame: touched.append("defer") or False,
        _update_timecode_display=lambda frame: touched.append("timecode"),
        _set_view_playhead_from_controller=lambda frame: touched.append("playhead"),
    )

    TimelinePanel._on_timer_frame_ready(panel, 12)  # type: ignore[arg-type]
    assert touched == [], "hidden view must short-circuit before any view work"


def test_aud_21302_timer_frame_ready_visible_view_proceeds() -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    visible_view = SimpleNamespace(isVisible=lambda: True)
    reached: list[int] = []
    panel = SimpleNamespace(
        timeline_view=visible_view,
        _should_defer_playback_tick=lambda frame: reached.append(frame) or False,
        _update_timecode_display=lambda frame: None,
        _set_view_playhead_from_controller=lambda frame: None,
    )
    TimelinePanel._on_timer_frame_ready(panel, 12)  # type: ignore[arg-type]
    assert reached == [12]


# ============================================================================
# AUD-21303: TimecodeInputDialog fps 下限
# ============================================================================

def test_aud_21303_timecode_dialog_survives_fractional_low_fps(qapp) -> None:
    from src.features.edit.ui.timeline_panel import (
        DEFAULT_FPS,
        TimecodeInputDialog,
    )

    dialog = TimecodeInputDialog(
        parent=None, current_frame=100, fps=0.1, total_frames=240,
    )
    assert dialog._safe_fps_int() == round(DEFAULT_FPS)

    # _parse_input 使用同一安全 fps（"00:00:02:10" = 2s + 10f）
    frame = dialog._parse_input("00:00:02:10")
    fps = round(DEFAULT_FPS)
    assert frame == 2 * fps + 10


@pytest.mark.parametrize("fps", [0.1, 0.0, -5.0, float("nan"), float("inf")])
def test_aud_21303_safe_fps_int_falls_back_for_invalid_fps(qapp, fps: float) -> None:
    from src.features.edit.ui.timeline_panel import DEFAULT_FPS, TimecodeInputDialog

    dialog = TimecodeInputDialog(
        parent=None, current_frame=0, fps=fps, total_frames=10,
    )
    assert dialog._safe_fps_int() == round(DEFAULT_FPS)


@pytest.mark.parametrize("fps,expected", [(24.0, 24), (29.97, 30), (0.6, 1)])
def test_aud_21303_safe_fps_int_valid_fps(qapp, fps: float, expected: int) -> None:
    from src.features.edit.ui.timeline_panel import TimecodeInputDialog

    dialog = TimecodeInputDialog(
        parent=None, current_frame=0, fps=fps, total_frames=10,
    )
    assert dialog._safe_fps_int() == expected


# ============================================================================
# AUD-21400 / AUD-21401 / AUD-21402: timeline_view
# ============================================================================

def _timeline_view_source() -> str:
    return (_SOURCE_ROOT / "src/features/edit/ui/timeline_view.py").read_text(
        encoding="utf-8"
    )


def test_aud_21400_gap_indicators_use_dedicated_reentrancy_lock() -> None:
    src = _timeline_view_source()
    fn_src = src[src.index("def _update_gap_indicators("):]
    fn_src = fn_src[: fn_src.index("def _update_gap_indicators_for_track(")]
    assert "if self._updating_gaps:" in fn_src
    assert "self._updating_gaps = True" in fn_src
    # 入口不再复用全局 _updating 锁（load/incremental 都持锁调用）
    assert "if self._updating:\n            return" not in fn_src


def test_aud_21400_gap_indicators_called_within_updating_blocks() -> None:
    """load_timeline 与 update_timeline_incremental 仍调用（锁内）间隙更新。"""
    src = _timeline_view_source()
    assert src.count("self._update_gap_indicators()") >= 2


def test_aud_21401_sync_lock_branch_promoted_to_top_level() -> None:
    src = _timeline_view_source()
    fn_src = src[src.index("def update_timeline_incremental("):]
    fn_src = fn_src[: fn_src.index("def _create_clip_item(")]
    # 修复后：sync-lock 分支与 is_locked 平级（4 级缩进 = 16 空格）
    assert "\n                    if 'is_sync_locked' in track_changes" in fn_src
    # 旧的嵌套形态（is_locked 分支体内 24 空格）不复存在
    assert "\n                        if 'is_sync_locked' in track_changes" not in fn_src


def test_aud_21402_drag_and_trim_snaps_exclude_moving_clip() -> None:
    src = _timeline_view_source()
    # 8 个片段拖拽/修剪吸附调用点全部显式传 exclude_source_ids
    assert src.count("exclude_source_ids={clip_id}") >= 8
    # _snap_frame 透传（SnapManager 路径）
    fn_src = src[src.index("def _snap_frame("):]
    assert "exclude_source_ids=exclude_source_ids" in fn_src
