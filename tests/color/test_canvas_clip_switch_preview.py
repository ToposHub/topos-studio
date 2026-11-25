"""Regression coverage for Color timeline-clip still-preview presentation."""

from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import QWheelEvent

from src.features.color.ui.canvas import Canvas
from src.shared.video.video_playback import EnhancedVideoPlaybackEngine


def test_canvas_hides_previous_front_texture_while_replacement_is_pending() -> None:
    """A selected clip must never be represented by the prior clip's frame."""
    renderer = SimpleNamespace(video_texture_pending=True)

    assert Canvas._should_present_renderer_front_texture(renderer) is False


def test_canvas_presents_front_texture_after_replacement_upload() -> None:
    renderer = SimpleNamespace(video_texture_pending=False)

    assert Canvas._should_present_renderer_front_texture(renderer) is True


def test_busy_gl_still_retries_paint_without_input(qtbot, monkeypatch) -> None:
    canvas = Canvas()
    qtbot.addWidget(canvas)
    renderer = SimpleNamespace(video_texture_pending=False, current_front_tex_id=42)
    draw = Mock(side_effect=(False, True))
    canvas.engine = SimpleNamespace(renderer=renderer, _is_video_mode=False, _draw_background_tex_id=draw)
    monkeypatch.setattr(canvas, "isVisible", lambda: True)
    nudge = Mock()
    monkeypatch.setattr(canvas.viewport(), "update", nudge)
    monkeypatch.setattr(canvas, "_record_presented_preview_frame", Mock())
    painter = Mock()

    canvas.drawBackground(painter, QRectF())
    assert canvas._present_retry.isActive()
    qtbot.waitUntil(lambda: nudge.called)

    canvas.drawBackground(painter, QRectF())
    assert not canvas._present_retry.isActive()
    assert draw.call_count == 2
    canvas.engine = None


def test_paused_front_recovers_after_long_gl_contention(qtbot, monkeypatch) -> None:
    canvas = Canvas()
    qtbot.addWidget(canvas)
    renderer = SimpleNamespace(video_texture_pending=False, current_front_tex_id=42, _video_generation_id=5)
    draw = Mock(side_effect=[False] * 6 + [True])
    canvas.engine = SimpleNamespace(renderer=renderer, _is_video_mode=True, scope_source_revision=2,
                                    viewer_waiting_for_gl=True, _draw_background_tex_id=draw)
    monkeypatch.setattr(canvas, 'isVisible', lambda: True)
    painter = Mock()
    monkeypatch.setattr(canvas.viewport(), 'update', lambda: canvas.drawBackground(painter, QRectF()))
    try:
        canvas.drawBackground(painter, QRectF())
        qtbot.waitUntil(lambda: draw.call_count == 7)
        assert not canvas._present_retry.isActive()
        assert canvas._video_present_retry_key is None
    finally:
        canvas.engine = None


def test_busy_gl_video_retries_once_per_front_revision(qtbot, monkeypatch) -> None:
    canvas = Canvas()
    qtbot.addWidget(canvas)
    renderer = SimpleNamespace(video_texture_pending=False, current_front_tex_id=42, _video_generation_id=5)
    canvas.engine = SimpleNamespace(renderer=renderer, _is_video_mode=True, scope_source_revision=2,
                                    _draw_background_tex_id=Mock(return_value=False))
    canvas.video_control_bar.is_playing = True
    monkeypatch.setattr(canvas, 'isVisible', lambda: True)
    nudge = Mock()
    monkeypatch.setattr(canvas.viewport(), 'update', nudge)
    painter = Mock()

    canvas.drawBackground(painter, QRectF())
    qtbot.waitUntil(lambda: nudge.called)
    canvas.drawBackground(painter, QRectF())
    assert not canvas._present_retry.isActive()

    canvas.engine.scope_source_revision = 3
    canvas.drawBackground(painter, QRectF())
    assert canvas._present_retry.isActive()
    renderer._video_generation_id += 1
    qtbot.waitUntil(lambda: not canvas._present_retry.isActive())
    assert nudge.call_count == 1
    canvas.engine = None


def test_first_blit_of_new_clip_replaces_pending_video_retry(qtbot, monkeypatch) -> None:
    canvas = Canvas()
    qtbot.addWidget(canvas)
    renderer = SimpleNamespace(video_texture_pending=False, current_front_tex_id=42, _video_generation_id=5)
    canvas.engine = SimpleNamespace(renderer=renderer, _is_video_mode=True, scope_source_revision=3,
                                    _draw_background_tex_id=Mock(return_value=False))
    monkeypatch.setattr(canvas, 'isVisible', lambda: True)
    nudge = Mock()
    monkeypatch.setattr(canvas.viewport(), 'update', nudge)
    painter = Mock()

    canvas.drawBackground(painter, QRectF())
    assert canvas._present_retry.isActive()
    renderer._video_generation_id = 6
    canvas.engine.scope_source_revision = 4
    canvas.drawBackground(painter, QRectF())
    qtbot.waitUntil(lambda: nudge.called)
    assert canvas._video_present_retry_key == (id(renderer), 6, 4)
    canvas.engine = None


def test_pending_static_preview_withdraws_the_previous_front_texture() -> None:
    """The old clip cannot remain drawable while the new first frame decodes."""
    viewport = SimpleNamespace(update=lambda: None)
    renderer = SimpleNamespace(
        video_texture_pending=False,
        current_front_tex_id=42,
        current_front_size=(1920, 1080),
    )
    engine = EnhancedVideoPlaybackEngine.__new__(EnhancedVideoPlaybackEngine)
    engine.engine = SimpleNamespace(renderer=renderer, canvas_ref=SimpleNamespace(viewport=lambda: viewport))

    EnhancedVideoPlaybackEngine._mark_renderer_video_texture_pending(engine)

    assert renderer.video_texture_pending is True
    assert renderer.current_front_tex_id is None
    assert renderer.current_front_size == (0, 0)


def test_canvas_wheel_zoom_does_not_accumulate_horizontal_or_vertical_drift(qapp) -> None:
    """Rapid vertical wheel input must remain a pure zoom operation."""
    canvas = Canvas()
    canvas.resize(800, 600)
    canvas.show()
    qapp.processEvents()
    try:
        canvas._update_lod = lambda: False
        canvas._refresh_video_frame_view = lambda: False
        canvas.logical_size = canvas.scene.sceneRect().size()
        canvas.logical_size.setWidth(1920)
        canvas.logical_size.setHeight(1080)
        canvas.scene.setSceneRect(QRectF(0, 0, 1920, 1080))
        canvas.fit_to_window()
        qapp.processEvents()

        viewport_center = canvas.viewport().rect().center()
        scene_center_before = canvas.mapToScene(viewport_center)
        transform_before = canvas.transform()
        wheel_position = QPointF(100, 100)

        for delta in [120] * 20 + [-120] * 20:
            event = QWheelEvent(
                wheel_position,
                wheel_position,
                QPoint(0, 0),
                QPoint(0, delta),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            canvas.wheelEvent(event)
            qapp.processEvents()

        scene_center_after = canvas.mapToScene(viewport_center)
        assert abs(scene_center_after.x() - scene_center_before.x()) < 1e-6
        assert abs(scene_center_after.y() - scene_center_before.y()) < 1e-6
        assert abs(canvas.transform().m11() - transform_before.m11()) < 1e-12
        assert abs(canvas.transform().m22() - transform_before.m22()) < 1e-12
    finally:
        canvas.close()
        canvas.deleteLater()
        qapp.processEvents()
