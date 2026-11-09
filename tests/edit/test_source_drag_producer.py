"""I04-1（复查计划）：Source 拖出生产端——画面阈值拖拽与流意图图标。

- ``source_drag_payload_for_intent``：无 In/Out → 全段（Out 包含）；
  有 In/Out → 当前源窗口；流不存在 → 原因拒绝；指纹/帧率取自时间域
  （可信 metadata），随载荷携带；
- 画面左键拖出阈值（canvas ``source_drag_requested``）→ 统一生产端；
- Video Only／Audio Only 图标按下即起拖（经鸭子 ``start_source_clip_drag``）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from src.features.edit.core.project import MediaAsset
from src.features.edit.core.source_time_domain import (
    media_fingerprint_for_path,
)
from src.features.edit.ui.source_viewer import SourceViewerWidget

pytestmark = pytest.mark.edit_ui


def _asset(asset_id: str = "i04p", *, media_type: str = "video",
           audio: bool = True, total: int = 240) -> MediaAsset:
    metadata: dict[str, Any] = {
        "fps": 24.0, "total_frames": total, "width": 640, "height": 480}
    if audio:
        metadata["audio_info"] = {"channels": 2}
    return MediaAsset(
        asset_id=asset_id, path=f"/tmp/{asset_id}.mov",
        media_type=media_type, display_name=f"{asset_id}.mov",
        metadata=metadata)


def _widget(qapp: Any, asset: MediaAsset | None = None) -> SourceViewerWidget:
    widget = SourceViewerWidget()
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._load_video_frame = lambda *_a, **_k: None  # type: ignore[method-assign]
    if asset is not None:
        widget.open_asset(asset)
    return widget


# ── 载荷生产政策 ───────────────────────────────────────────────────


def test_i04_producer_full_range_when_no_marks(qapp):
    widget = _widget(qapp, _asset())
    encoded, reason = widget.source_drag_payload_for_intent("av")
    assert reason is None and encoded is not None
    data = json.loads(encoded)
    assert (data["in_frame"], data["out_frame"]) == (0, 239)  # 全段，Out 包含


def test_i04_producer_uses_current_window(qapp):
    widget = _widget(qapp, _asset())
    widget.set_in_point(10)
    widget.set_out_point(49)
    data = json.loads(widget.source_drag_payload_for_intent("av")[0])
    assert (data["in_frame"], data["out_frame"]) == (10, 49)


def test_i04_producer_carries_fingerprint_and_domain_fps(qapp):
    widget = _widget(qapp, _asset())  # fps 24 → Rational(24,1)
    data = json.loads(widget.source_drag_payload_for_intent("video")[0])
    assert data["media_fingerprint"] == media_fingerprint_for_path(
        "/tmp/i04p.mov")
    assert data["source_fps_num"] == 24
    assert data["source_fps_den"] == 1


def test_i04_producer_refuses_missing_streams(qapp):
    silent = _widget(qapp, _asset("silent", audio=False))
    encoded, reason = silent.source_drag_payload_for_intent("audio")
    assert encoded is None and reason == "audio_stream_unavailable"

    audio_only = _widget(qapp, _asset("tone", media_type="audio"))
    encoded, reason = audio_only.source_drag_payload_for_intent("video")
    assert encoded is None and reason == "video_stream_unavailable"


def test_i04_producer_refuses_without_asset(qapp):
    widget = _widget(qapp, None)
    encoded, reason = widget.source_drag_payload_for_intent("av")
    assert encoded is None and reason == "no_source_asset"


# ── 画面阈值拖拽 ───────────────────────────────────────────────────


def _press(widget: SourceViewerWidget, x: int, y: int) -> None:
    widget.viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _move(widget: SourceViewerWidget, x: int, y: int) -> None:
    widget.viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def test_i04_canvas_drag_threshold_starts_single_drag(qapp):
    widget = _widget(qapp, _asset())
    started: list[str] = []
    widget.start_source_clip_drag = (  # type: ignore[method-assign]
        lambda intent, parent=None: started.append(intent))

    _press(widget, 100, 100)
    _move(widget, 103, 102)                 # 5px：未过阈值
    assert started == []
    _move(widget, 112, 100)                 # 12px：过阈值 → 发起一次
    assert started == ["av"]
    _move(widget, 130, 100)                 # 同一次按下的后续移动不再发起
    assert started == ["av"]


def test_i04_canvas_click_without_move_never_drags(qapp):
    widget = _widget(qapp, _asset())
    started: list[str] = []
    widget.start_source_clip_drag = (  # type: ignore[method-assign]
        lambda intent, parent=None: started.append(intent))
    _press(widget, 100, 100)
    widget.viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(100, 100), QPointF(100, 100),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    assert started == []


def test_i04_canvas_left_drag_does_not_pan(qapp):
    """平移仍归中键：左键拖出后 pan 状态不变（互不干扰）。"""
    widget = _widget(qapp, _asset())
    widget.start_source_clip_drag = lambda *a, **k: None  # type: ignore[method-assign]
    _press(widget, 100, 100)
    _move(widget, 130, 100)
    assert widget.viewer._is_panning is False


# ── EditViewport 流意图图标（拖出源） ───────────────────────────────


def test_i04_viewport_stream_buttons_start_drag(qtbot, qapp):
    from tests.edit.test_edit_viewport import _Program, _Source
    from src.features.edit.ui.edit_viewport import EditViewport
    from PyQt6.QtWidgets import QWidget

    shelter = QWidget()
    qtbot.addWidget(shelter)
    source = _Source()
    program = _Program()
    viewport = EditViewport(program, source, shelter)
    calls: list[str] = []
    source.start_source_clip_drag = (  # type: ignore[method-assign]
        lambda intent, parent=None: calls.append(intent))
    drag_buttons = [b for b in viewport.source_footer.findChildren(QWidget)
                    if hasattr(b, "_intent")]
    assert {b._intent for b in drag_buttons} == {"video", "audio"}
    for button in drag_buttons:
        button.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
    assert calls == ["video", "audio"]
