"""Regression tests for remediation batch D fixes (wd13-g67).

Covers 16 Major findings from the 2026-09-28 audit, edit UI d segment:

- AUD-20501: QuickProjectDialog presets use stable keys, not frozen
  import-time translations; default item located via itemData.
- AUD-20601: persistent-cache hit path bypasses the sender guard and the
  cached analysis is actually applied.
- AUD-20602: batch accept/reject popups format the count instead of
  showing a literal ``{0}`` placeholder.
- AUD-20603: cancelling a running detection worker uses a bounded wait
  and abandons (never destroys) a stuck thread.
- AUD-20700: MiniScrubber.set_total_frames holds the _updating lock so a
  range clamp does not leak frame_changed.
- AUD-20710: set_candidate converts timecodes/offsets with the real fps.
- AUD-20711: _format_timecode uses one consistent (rounded integer) fps
  for both total frames and h/m/s bucketing.
- AUD-20712: warning/accepted/rejected highlight styles are distinct.
- AUD-20713: panel clicks emit the documented frame_clicked signal.
- AUD-20800: SceneCutResultModel formats Start TC with the fps injected
  via set_fps (no hardcoded 24, no float/int fps mixing).
- AUD-20810: timeline_frame == 0 is honoured (no ``or`` falsy fallback).
- AUD-20811: update_num_tracks calls prepareGeometryChange.
- AUD-20812: marker line height uses the layout engine's real track
  area height, not num_tracks x video-track-height.
- AUD-20813: candidate_selected is emitted after releasing _updating.
- AUD-20814: hover tooltip area is inside boundingRect.
- AUD-20902: CPU frames are staged via submit_qimage and uploaded in
  paintGL; clear_texture wraps deletion in makeCurrent/doneCurrent.

Qt widgets run offscreen via tests/edit/conftest.py; GL entry points are
monkeypatched on the module-level ``_gl`` wrapper so the viewer logic can
be exercised headlessly without a GL context.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.scene_cut.models import (
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutDecision,
)
from src.shared.i18n.i18n import I18N


# ===========================================================================
# Helpers
# ===========================================================================


def _make_candidate(
    source_frame: int = 24,
    seconds: float = 1.0,
    timeline_frame: int | None = None,
    confidence: float = 0.9,
    decision: SceneCutDecision = SceneCutDecision.PENDING,
) -> SceneCutCandidate:
    return SceneCutCandidate(
        source_frame=source_frame,
        pts=None,
        seconds=seconds,
        timeline_frame=timeline_frame,
        score=0.9,
        confidence=confidence,
        decision=decision,
    )


def _make_analysis(candidates: tuple[SceneCutCandidate, ...]) -> SceneCutAnalysis:
    return SceneCutAnalysis(
        analysis_id="analysis-1",
        asset_id="asset-1",
        source_path="/tmp/fake.mp4",
        candidates=candidates,
    )


class _FakeLayoutEngine:
    """Minimal stand-in for TimelineLayoutEngine (records frame_to_x)."""

    def __init__(self, x: float = 0.0, track_area_height: float = 0.0) -> None:
        self._x = x
        self._track_area_height = track_area_height
        self.frames_seen: list[int] = []

    def frame_to_x(self, frame: int) -> float:
        self.frames_seen.append(frame)
        return self._x

    def area_layout(self) -> types.SimpleNamespace:
        rect = types.SimpleNamespace(height=lambda: self._track_area_height)
        return types.SimpleNamespace(total_track_area=rect)


# ===========================================================================
# AUD-20501 — QuickProjectDialog stable preset keys
# ===========================================================================


def test_quick_project_presets_use_stable_keys(qapp, tmp_path) -> None:
    from src.features.edit.ui.quick_project_dialog import QuickProjectDialog

    media = str(tmp_path / "clip.mov")  # 不存在的文件 → 探测失败走默认值
    dlg = QuickProjectDialog(media_path=media)

    # 类级预设键为语言无关稳定 key，翻译文本不再作字典键
    assert QuickProjectDialog.AUTO_PRESET_KEY in dlg.RESOLUTION_PRESETS
    assert QuickProjectDialog.CUSTOM_PRESET_KEY in dlg.RESOLUTION_PRESETS
    for key in dlg.RESOLUTION_PRESETS:
        assert "Auto" not in key or key == QuickProjectDialog.AUTO_PRESET_KEY

    # 默认选中 Auto —— 按 itemData 定位（语言切换后仍成立）
    keys = [
        dlg.resolution_combo.itemData(i)
        for i in range(dlg.resolution_combo.count())
    ]
    assert QuickProjectDialog.AUTO_PRESET_KEY in keys
    assert (
        dlg.resolution_combo.currentData() == QuickProjectDialog.AUTO_PRESET_KEY
    )

    # Auto 项填充探测元数据（此处为回退默认 1920x1080）
    dlg._on_resolution_changed(dlg.resolution_combo.currentIndex())
    assert dlg.width_spin.value() == 1920
    assert dlg.height_spin.value() == 1080


def test_quick_project_display_text_follows_runtime_locale(
    qapp, monkeypatch, tmp_path
) -> None:
    """切换语言（不重启）后新实例显示新译文，itemData 仍为稳定 key。"""
    from src.features.edit.ui.quick_project_dialog import QuickProjectDialog

    monkeypatch.setattr(
        I18N,
        "_translations",
        {"project": {"auto_from_media": "自动（来自媒体）"}},
    )
    monkeypatch.setattr(I18N, "_fallback_translations", {})

    dlg = QuickProjectDialog(media_path=str(tmp_path / "clip.mov"))
    auto_index = dlg.resolution_combo.findData(
        QuickProjectDialog.AUTO_PRESET_KEY
    )
    assert auto_index >= 0
    assert dlg.resolution_combo.itemText(auto_index) == "自动（来自媒体）"


# ===========================================================================
# AUD-20601 — cache hit path applies the analysis despite sender()
# ===========================================================================


def test_cache_hit_applies_analysis_inside_slot(qapp, monkeypatch) -> None:
    from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

    dlg = SceneCutDialog()
    analysis = _make_analysis((_make_candidate(),))
    monkeypatch.setattr(dlg, "_use_persistent_cache", True)
    monkeypatch.setattr(dlg, "_try_load_cache", lambda: analysis)
    monkeypatch.setattr(dlg, "_source_path", "/tmp/fake.mp4")
    monkeypatch.setattr(dlg, "_asset_id", "asset-1")

    applied: list[SceneCutAnalysis] = []
    emitted: list[SceneCutAnalysis] = []
    monkeypatch.setattr(dlg, "set_analysis", lambda a: applied.append(a))
    dlg.detection_completed.connect(lambda a: emitted.append(a))

    # 模拟从 BottomActionBar 信号槽内直调（sender() 非 worker 非 None）
    monkeypatch.setattr(dlg, "sender", lambda: dlg._bottom_bar)

    dlg._start_detection()

    assert applied == [analysis], "缓存命中必须应用分析结果"
    assert emitted == [analysis], "detection_completed 必须发射"
    # 缓存命中即返回，不得再启动新探测
    assert dlg._worker is None


# ===========================================================================
# AUD-20602 — batch popups format the count
# ===========================================================================


def test_format_count_message_positional_placeholder(monkeypatch) -> None:
    from src.features.edit.ui.scene_cut_dialog import _format_count_message

    monkeypatch.setattr(
        I18N,
        "_translations",
        {"scene_cut": {"accepted_count": "{0} 个候选点已接受。"}},
    )
    monkeypatch.setattr(I18N, "_fallback_translations", {})

    msg = _format_count_message(
        "scene_cut.accepted_count", "{0} candidates accepted.", 5
    )
    assert msg == "5 个候选点已接受。"
    assert "{0}" not in msg


def test_format_count_message_named_placeholder(monkeypatch) -> None:
    from src.features.edit.ui.scene_cut_dialog import _format_count_message

    monkeypatch.setattr(
        I18N,
        "_translations",
        {"scene_cut": {"rejected_count": "已拒绝 {count} 个。"}},
    )
    monkeypatch.setattr(I18N, "_fallback_translations", {})

    msg = _format_count_message(
        "scene_cut.rejected_count", "{0} candidates rejected.", 3
    )
    assert msg == "已拒绝 3 个。"


def test_format_count_message_untranslated_default(monkeypatch) -> None:
    from src.features.edit.ui.scene_cut_dialog import (
        _format_count_message,
        SCENE_CUT_I18N_KEYS,
        _format_cut_confirmation_message,
    )

    monkeypatch.setattr(I18N, "_translations", {})
    monkeypatch.setattr(I18N, "_fallback_translations", {})

    msg = _format_count_message(
        SCENE_CUT_I18N_KEYS["accepted_count"], "{0} candidates accepted.", 7
    )
    assert msg == "7 candidates accepted."

    # 既有确认消息仍走同一双格式兜底
    assert "7" in _format_cut_confirmation_message(7)
    assert "{0}" not in _format_cut_confirmation_message(7)


# ===========================================================================
# AUD-20603 — bounded wait + zombie abandon for detection worker
# ===========================================================================


class _StuckDetectionWorker:
    """is_running 恒真、wait 恒超时的取消线程替身。

    注意：cancelled/finished 等与真实 SceneCutWorker 信号同名的属性
    必须是信号替身（SimpleNamespace），取消请求状态用 cancel_requested。
    """

    def __init__(self) -> None:
        self.cancel_requested = False
        self.wait_timeouts: list[int] = []
        self.finished_connections: list[object] = []
        self.progress = types.SimpleNamespace(
            connect=lambda *a: None, disconnect=lambda *a: None
        )
        self.completed = types.SimpleNamespace(
            connect=lambda *a: None, disconnect=lambda *a: None
        )
        self.error = types.SimpleNamespace(
            connect=lambda *a: None, disconnect=lambda *a: None
        )
        self.cancelled = types.SimpleNamespace(
            connect=lambda *a: None, disconnect=lambda *a: None
        )
        self.finished = types.SimpleNamespace(
            connect=lambda slot: self.finished_connections.append(slot)
        )

    is_running = True

    def cancel(self) -> None:
        self.cancel_requested = True

    def wait(self, timeout: int | None = None) -> bool:
        if timeout is not None:
            self.wait_timeouts.append(timeout)
        return False  # 模拟容器打开阶段不响应取消

    def deleteLater(self) -> None:  # pragma: no cover - QThread API 替身
        pass


class _FreshDetectionWorker:
    """新 worker 替身：构造轻量、start_detection 立即成功。"""

    def __init__(self, **_kwargs: object) -> None:
        self.started = False

    is_running = False

    def cancel(self) -> None:  # pragma: no cover - not exercised
        pass

    def wait(self, timeout: int | None = None) -> bool:
        return True

    def start_detection(self) -> bool:
        self.started = True
        return True

    progress = types.SimpleNamespace(
        connect=lambda *a: None, disconnect=lambda *a: None
    )
    completed = types.SimpleNamespace(
        connect=lambda *a: None, disconnect=lambda *a: None
    )
    error = types.SimpleNamespace(
        connect=lambda *a: None, disconnect=lambda *a: None
    )
    cancelled = types.SimpleNamespace(
        connect=lambda *a: None, disconnect=lambda *a: None
    )


def test_start_detection_uses_bounded_wait_and_abandons(
    qapp, monkeypatch
) -> None:
    import src.features.edit.ui.scene_cut_dialog as scd

    dlg = scd.SceneCutDialog()
    monkeypatch.setattr(dlg, "_source_path", "/tmp/fake.mp4")
    monkeypatch.setattr(dlg, "_asset_id", "asset-1")
    monkeypatch.setattr(dlg, "_use_persistent_cache", False)
    monkeypatch.setattr(dlg, "_try_load_cache", lambda: None)

    stuck = _StuckDetectionWorker()
    monkeypatch.setattr(dlg, "_worker", stuck)
    monkeypatch.setattr(scd, "SceneCutWorker", _FreshDetectionWorker)
    # 探测启动后的代理暂停副作用隔离
    monkeypatch.setattr(dlg, "_stop_playback", lambda: None)
    monkeypatch.setattr(dlg, "_cancel_active_preview_worker", lambda: None)

    dlg._start_detection()

    # 有界等待（带超时参数），不再是无限 wait()
    assert stuck.wait_timeouts and all(t > 0 for t in stuck.wait_timeouts)
    assert stuck.cancel_requested
    # 超时线程转入 zombie 列表、不再被 _worker 引用，且未被销毁
    assert stuck in dlg._zombie_detection_workers
    assert dlg._worker is not stuck
    assert isinstance(dlg._worker, _FreshDetectionWorker)


def test_abandoned_worker_signals_are_disconnected(qapp, monkeypatch) -> None:
    from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

    dlg = SceneCutDialog()
    calls: list[str] = []
    worker = _StuckDetectionWorker()

    # 真实 QThread 信号对象才有 disconnect；替身用记录器模拟
    for name in ("progress", "completed", "error", "cancelled"):
        sig = types.SimpleNamespace(
            connect=lambda *a: None,
            disconnect=lambda slot, _n=name: calls.append(_n),
        )
        setattr(worker, name, sig)

    dlg._abandon_detection_worker(worker)  # type: ignore[arg-type]

    assert sorted(calls) == ["cancelled", "completed", "error", "progress"]
    # finished → deleteLater 自回收连接已建立（销毁安全）
    assert len(worker.finished_connections) == 1
    assert worker in dlg._zombie_detection_workers
    assert dlg._worker is None


# ===========================================================================
# AUD-20700 — MiniScrubber range clamp must not leak frame_changed
# ===========================================================================


def test_scrubber_set_total_frames_does_not_leak_frame_changed(qapp) -> None:
    from src.features.edit.ui.scene_cut_graph_widget import MiniScrubber

    scrubber = MiniScrubber()
    emitted: list[int] = []
    scrubber.frame_changed.connect(emitted.append)

    scrubber.set_frame(500)  # 程序化置值（持锁，不应发射）
    assert emitted == []

    # 范围缩小：Qt 钳制 500 → 100，但不许泄漏 frame_changed
    scrubber.set_total_frames(100)
    assert emitted == [], "范围钳制不得当作用户操作发射 frame_changed"
    assert scrubber.value() == 100
    assert scrubber.maximum() == 100


# ===========================================================================
# AUD-20710 / AUD-20711 — preview timecode fps correctness
# ===========================================================================


def test_set_candidate_timecode_uses_real_fps(qapp) -> None:
    from src.features.edit.ui.scene_cut_preview_widget import SceneCutPreviewWidget

    preview = SceneCutPreviewWidget()
    candidate = _make_candidate(source_frame=25, seconds=1.0)

    preview.set_candidate(candidate, fps=25.0)

    # before = 1.0 - 1/25 = 0.96s @25fps = 第 24 帧（24fps 旧代码显示 23）
    assert preview.get_before_panel()._timecode == "00:00:00:24"
    assert preview.get_current_panel()._timecode == "00:00:01:00"
    # after = 1.0 + 1/25 = 1.04s @25fps = 第 26 帧
    assert preview.get_after_panel()._timecode == "00:00:01:01"


def test_format_timecode_fractional_fps_consistent(qapp) -> None:
    from src.features.edit.ui.scene_cut_preview_widget import SceneCutPreviewWidget

    preview = SceneCutPreviewWidget()
    # 29.97：旧代码 total 按 29.97 计、除数按 29 截断 → 00:01:02:00
    assert preview._format_timecode(60.0, 29.97) == "00:01:00:00"
    # 整数帧率保持不变
    assert preview._format_timecode(1.0, 24.0) == "00:00:01:00"
    assert preview._format_timecode(1.5, 24.0) == "00:00:01:12"
    assert preview._format_timecode(-0.5, 25.0) == "00:00:00:00"


# ===========================================================================
# AUD-20712 — distinct status highlight styles
# ===========================================================================


def test_status_highlight_styles_are_distinct(qapp) -> None:
    from src.features.edit.ui.scene_cut_preview_widget import (
        COLORS,
        SceneCutPreviewWidget,
    )

    preview = SceneCutPreviewWidget()

    def _style() -> str:
        return preview.get_current_panel().styleSheet()

    # pending（普通）→ 无状态边框
    preview.set_candidate(_make_candidate(decision=SceneCutDecision.PENDING))
    assert "border: none" in _style()

    # accepted → 绿色边框
    preview.set_candidate(_make_candidate(decision=SceneCutDecision.ACCEPTED))
    assert COLORS["accepted"] in _style()

    # rejected → 灰色边框
    preview.set_candidate(_make_candidate(decision=SceneCutDecision.REJECTED))
    assert COLORS["rejected"] in _style()

    # warning（低置信度）→ 琥珀色边框
    preview.set_candidate(_make_candidate(confidence=0.3))
    assert preview.get_current_candidate().is_warning
    assert COLORS["warning"] in _style()


# ===========================================================================
# AUD-20713 — panel click emits frame_clicked
# ===========================================================================


def _mouse_press(widget) -> None:
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(widget.width() / 2, widget.height() / 2),
        widget.mapToGlobal(QPointF(widget.width() / 2, widget.height() / 2)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.mousePressEvent(event)


def test_panel_click_emits_frame_clicked(qapp) -> None:
    from src.features.edit.ui.scene_cut_preview_widget import (
        PreviewPanelType,
        SceneCutPreviewWidget,
    )

    preview = SceneCutPreviewWidget()
    received: list[tuple[object, int]] = []
    preview.frame_clicked.connect(lambda p, f: received.append((p, f)))

    # 无帧时不发射
    _mouse_press(preview.get_before_panel())
    assert received == []

    # 显示候选后，点击 before 面板 → (BEFORE, source_frame - 1)
    preview.set_candidate(_make_candidate(source_frame=30), fps=24.0)
    _mouse_press(preview.get_before_panel())
    assert received == [(PreviewPanelType.BEFORE, 29)]

    _mouse_press(preview.get_current_panel())
    assert received[-1] == (PreviewPanelType.CURRENT, 30)


# ===========================================================================
# AUD-20800 — result model Start TC honors set_fps
# ===========================================================================


def test_result_model_timecode_uses_injected_fps(qapp) -> None:
    from PyQt6.QtCore import Qt

    from src.features.edit.ui.scene_cut_result_model import (
        Column,
        SceneCutResultModel,
    )

    model = SceneCutResultModel()
    model.set_candidates([_make_candidate(seconds=1.5)])

    def _tc() -> str:
        idx = model.index(0, Column.TIMECODE.value)
        return str(model.data(idx, Qt.ItemDataRole.DisplayRole))

    # 默认 24fps：1.5s = 36 帧 → 00:00:01:12
    assert _tc() == "00:00:01:12"

    changed: list[object] = []
    model.dataChanged.connect(
        lambda *args: changed.append(args)
    )
    model.set_fps(30.0)
    # 30fps：1.5s = 45 帧 → 00:00:01:15，且 TIMECODE 列收到刷新通知
    assert _tc() == "00:00:01:15"
    assert changed, "set_fps 必须通知视图刷新时间码列"

    # 分数帧率不再出现 float/int 混用漂移
    assert model._format_timecode(60.0, 29.97) == "00:01:00:00"
    assert model.get_fps() == 30.0


def test_result_model_set_fps_is_idempotent(qapp) -> None:
    from src.features.edit.ui.scene_cut_result_model import SceneCutResultModel

    model = SceneCutResultModel()
    model.set_fps(25.0)
    changed: list[object] = []
    model.dataChanged.connect(lambda *args: changed.append(args))
    model.set_candidates([_make_candidate(seconds=1.0)])
    changed.clear()

    model.set_fps(25.0)  # 相同帧率 → 无通知
    assert changed == []
    model.set_fps(0.0)  # 非法帧率被钳到安全下限
    assert model.get_fps() == 1.0


# ===========================================================================
# AUD-20810 — timeline_frame == 0 is honoured
# ===========================================================================


def test_marker_uses_timeline_frame_zero(qapp) -> None:
    from src.features.edit.ui.scene_cut_timeline_overlay import CandidateMarkerItem

    engine = _FakeLayoutEngine()
    candidate = _make_candidate(source_frame=100, timeline_frame=0)
    marker = CandidateMarkerItem(
        candidate=candidate,
        pixels_per_frame=5.0,
        layout_engine=engine,  # type: ignore[arg-type]
    )
    marker._update_geometry()
    assert engine.frames_seen[-1] == 0, "第 0 帧是合法时间线帧位"


def test_marker_falls_back_to_source_frame_when_none(qapp) -> None:
    from src.features.edit.ui.scene_cut_timeline_overlay import CandidateMarkerItem

    engine = _FakeLayoutEngine()
    candidate = _make_candidate(source_frame=77, timeline_frame=None)
    marker = CandidateMarkerItem(
        candidate=candidate,
        pixels_per_frame=5.0,
        layout_engine=engine,  # type: ignore[arg-type]
    )
    assert engine.frames_seen[-1] == 77


# ===========================================================================
# AUD-20811 — prepareGeometryChange before height-affecting updates
# ===========================================================================


def test_update_num_tracks_prepares_geometry_change(qapp, monkeypatch) -> None:
    from src.features.edit.ui.scene_cut_timeline_overlay import CandidateMarkerItem

    marker = CandidateMarkerItem(
        candidate=_make_candidate(),
        pixels_per_frame=5.0,
        layout_engine=_FakeLayoutEngine(),  # type: ignore[arg-type]
    )
    calls: list[bool] = []
    monkeypatch.setattr(marker, "prepareGeometryChange", lambda: calls.append(True))

    marker.update_num_tracks(4)
    assert calls == [True], "轨道数变化影响 boundingRect，必须先 prepareGeometryChange"

    calls.clear()
    marker.update_num_tracks(4)  # 未变化 → 无几何通知
    assert calls == []

    calls.clear()
    marker.update_track_area_height(346.0)
    assert calls == [True]


# ===========================================================================
# AUD-20812 — line height uses real track area height
# ===========================================================================


def test_effective_track_height_prefers_layout_truth(qapp) -> None:
    from src.features.edit.ui.scene_cut_timeline_overlay import (
        CandidateMarkerItem,
        EDIT_SIZES,
    )

    # 1 视频 + 4 音频轨：估算 5×82=410，布局真值 346
    engine = _FakeLayoutEngine(track_area_height=346.0)
    marker = CandidateMarkerItem(
        candidate=_make_candidate(),
        pixels_per_frame=5.0,
        layout_engine=engine,  # type: ignore[arg-type]
        num_tracks=5,
    )
    assert marker._effective_track_height() == 346.0
    assert marker._effective_track_height() != 5 * EDIT_SIZES["track_h_video"]

    # 显式真值优先于引擎
    marker.update_track_area_height(999.0)
    assert marker._effective_track_height() == 999.0


def test_effective_track_height_falls_back_to_estimate(qapp) -> None:
    from src.features.edit.ui.scene_cut_timeline_overlay import (
        CandidateMarkerItem,
        EDIT_SIZES,
    )

    # 引擎无轨道（height=0）→ 回退 num_tracks 估算（独立使用场景）
    marker = CandidateMarkerItem(
        candidate=_make_candidate(),
        pixels_per_frame=5.0,
        layout_engine=_FakeLayoutEngine(track_area_height=0.0),  # type: ignore[arg-type]
        num_tracks=3,
    )
    assert marker._effective_track_height() == 3 * EDIT_SIZES["track_h_video"]


# ===========================================================================
# AUD-20813 — candidate_selected emitted outside the _updating lock
# ===========================================================================


def test_select_candidate_emits_after_releasing_lock(qapp) -> None:
    from src.features.edit.ui.scene_cut_timeline_overlay import (
        SceneCutTimelineOverlay,
    )

    overlay = SceneCutTimelineOverlay(layout_engine=_FakeLayoutEngine())  # type: ignore[arg-type]
    overlay.set_candidates((_make_candidate(source_frame=10),))

    lock_states: list[bool] = []
    overlay.candidate_selected.connect(
        lambda _cid: lock_states.append(overlay._updating)
    )
    overlay.select_candidate("10")

    assert lock_states == [False], "信号处理器执行时锁必须已释放"


def test_select_candidate_allows_handler_writeback(qapp) -> None:
    from src.features.edit.ui.scene_cut_timeline_overlay import (
        SceneCutTimelineOverlay,
    )

    overlay = SceneCutTimelineOverlay(layout_engine=_FakeLayoutEngine())  # type: ignore[arg-type]
    original = _make_candidate(source_frame=10)
    overlay.set_candidates((original,))

    # 审计场景：处理器在信号槽内同步回写 overlay（update_candidates）。
    # 旧实现在锁内 emit，回写被 _updating 拦截而静默丢失。
    updated = original.with_decision(SceneCutDecision.ACCEPTED)

    def _on_selected(_candidate_id: str) -> None:
        overlay.update_candidates((updated,))

    overlay.candidate_selected.connect(_on_selected)
    overlay.select_candidate("10")

    assert overlay.candidates == (updated,), "锁外发射使处理器回写生效"


# ===========================================================================
# AUD-20814 — tooltip contained by boundingRect
# ===========================================================================


def test_bounding_rect_covers_tooltip_when_hovered(qapp) -> None:
    from src.features.edit.ui.scene_cut_timeline_overlay import (
        CANDIDATE_TRIANGLE_SIZE,
        CandidateMarkerItem,
    )

    marker = CandidateMarkerItem(
        candidate=_make_candidate(),
        pixels_per_frame=5.0,
        layout_engine=_FakeLayoutEngine(),  # type: ignore[arg-type]
    )
    idle_rect = marker.boundingRect()
    assert idle_rect.top() == -CANDIDATE_TRIANGLE_SIZE

    marker._is_hovered = True
    hovered_rect = marker.boundingRect()
    tooltip = marker._tooltip_rect()
    assert hovered_rect.top() <= tooltip.top()
    assert hovered_rect.contains(tooltip), "tooltip 必须落在 boundingRect 内"
    assert hovered_rect.height() > idle_rect.height()
    assert tooltip.height() > CANDIDATE_TRIANGLE_SIZE * 3  # 真实 tooltip 高度


# ===========================================================================
# AUD-20902 — deferred upload inside paintGL + makeCurrent deletion
# ===========================================================================


def _make_viewer(qapp):
    import src.features.edit.ui.source_viewer as sv

    viewer = sv.SourceViewer()
    return viewer


def test_submit_qimage_stages_without_gl_calls(qapp) -> None:
    from PyQt6.QtGui import QImage

    viewer = _make_viewer(qapp)
    image = QImage(64, 48, QImage.Format.Format_RGB888)

    viewer._shared_tex_id = 42  # 模拟此前 GPU 路径在显示
    viewer.submit_qimage(image, display_width=1920, display_height=1080)

    assert viewer._pending_qimage is image
    assert viewer._pending_display_size == (1920, 1080)
    assert viewer._shared_tex_id is None  # CPU 路径接管显示
    assert viewer._video_aspect_ratio == pytest.approx(1920 / 1080)
    # 未初始化 GL 时不得触发任何 GL 调用（无上下文）
    assert viewer._current_tex_id is None


def test_paintgl_consumes_staged_image(qapp, monkeypatch) -> None:
    from PyQt6.QtGui import QImage

    import src.features.edit.ui.source_viewer as sv

    viewer = _make_viewer(qapp)
    monkeypatch.setattr(viewer, "_gl_initialized", True)
    monkeypatch.setattr(sv._gl, "glClear", lambda *a, **k: None)

    uploads: list[tuple[QImage, int, int]] = []
    monkeypatch.setattr(
        viewer,
        "set_texture_from_qimage_with_display_size",
        lambda img, w, h: uploads.append((img, w, h)),
    )

    image = QImage(64, 48, QImage.Format.Format_RGB888)
    viewer.submit_qimage(image, display_width=640, display_height=480)
    viewer.paintGL()

    assert uploads == [(image, 640, 480)]
    assert viewer._pending_qimage is None
    assert viewer._pending_display_size is None


def test_paintgl_consumes_staged_image_without_display_size(
    qapp, monkeypatch
) -> None:
    from PyQt6.QtGui import QImage

    import src.features.edit.ui.source_viewer as sv

    viewer = _make_viewer(qapp)
    monkeypatch.setattr(viewer, "_gl_initialized", True)
    monkeypatch.setattr(sv._gl, "glClear", lambda *a, **k: None)

    uploads: list[QImage] = []
    monkeypatch.setattr(
        viewer,
        "set_texture_from_qimage",
        lambda img: uploads.append(img),
    )

    image = QImage(32, 32, QImage.Format.Format_RGB888)
    viewer.submit_qimage(image)
    viewer.paintGL()

    assert uploads == [image]
    assert viewer._pending_qimage is None


def test_clear_texture_wraps_delete_in_make_current(qapp, monkeypatch) -> None:
    import src.features.edit.ui.source_viewer as sv

    viewer = _make_viewer(qapp)
    monkeypatch.setattr(viewer, "_gl_initialized", True)
    monkeypatch.setattr(viewer, "_current_tex_id", 7)

    order: list[str] = []
    monkeypatch.setattr(viewer, "makeCurrent", lambda: order.append("make"))
    monkeypatch.setattr(viewer, "doneCurrent", lambda: order.append("done"))
    monkeypatch.setattr(
        sv._gl, "glDeleteTextures", lambda ids: order.append(f"delete{ids}")
    )

    viewer.clear_texture()

    assert order == ["make", "delete[7]", "done"]
    assert viewer._current_tex_id is None
    assert viewer._pending_qimage is None


def test_clear_texture_survives_destroyed_widget(qapp, monkeypatch) -> None:
    def _raise() -> None:
        raise RuntimeError("wrapped C/C++ object of type SourceViewer has been deleted")

    viewer = _make_viewer(qapp)
    monkeypatch.setattr(viewer, "_gl_initialized", True)
    monkeypatch.setattr(viewer, "_current_tex_id", 9)
    monkeypatch.setattr(viewer, "makeCurrent", _raise)

    # 关闭中的部件：不抛异常（Qt 槽内不抛未处理异常），状态仍清理
    viewer.clear_texture()
    assert viewer._current_tex_id is None


def test_cpu_callers_stage_via_submit(qapp) -> None:
    """UI 槽的 CPU 帧路径不再直调 GL 上传方法（静态断言）。"""
    import inspect

    import src.features.edit.ui.source_viewer as sv

    source = inspect.getsource(sv)
    # 父控件（SourceViewerWidget）侧一律走 viewer.submit_qimage；
    # 上传方法仅允许 SourceViewer.paintGL 内经 self.set_texture_from_qimage*
    # 消费（"def " 行与 docstring 不计）。
    offenders = [
        line.strip()
        for line in source.splitlines()
        if ".set_texture_from_qimage" in line
        and ".submit_qimage" not in line
        and "self.set_texture_from_qimage" not in line
    ]
    assert offenders == [], f"GL 上传不得在 paintGL 之外被直调: {offenders}"
