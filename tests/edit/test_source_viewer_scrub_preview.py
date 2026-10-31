from PyQt6.QtCore import Qt

from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.source_viewer import SourceViewerWidget


def test_source_viewer_scrub_previews_without_generation_churn(qapp, monkeypatch) -> None:
    widget = SourceViewerWidget()
    loaded: list[int] = []
    monkeypatch.setattr(widget, "_load_frame", lambda frame: loaded.append(int(frame)))
    monkeypatch.setattr(widget, "_sync_scrub_audio_for_asset", lambda: None)
    widget._scrub_request_interval_ms = 0
    asset = MediaAsset(
        asset_id="asset-1",
        path="/tmp/source.mov",
        media_type="video",
        display_name="source.mov",
        metadata={"fps": 24.0, "total_frames": 120, "width": 1920, "height": 1080},
    )

    widget.open_asset(asset)
    generation_after_open = widget._gpu_generation

    widget._on_slider_pressed()
    widget._on_slider_changed(12)
    widget._on_slider_changed(18)

    assert widget._gpu_generation == generation_after_open
    assert loaded[-1] == 18
    assert widget._current_frame == 18

    widget._on_slider_released()

    assert widget._gpu_generation == generation_after_open + 1
    assert loaded[-1] == 18


def test_source_viewer_gpu_slot_ack_protocol(qapp) -> None:
    """Present slots must be acked or the renderer pool exhausts (freeze).

    - An accepted frame acks the slot it supersedes.
    - A discarded frame (guard hit) acks its own slot immediately.
    - close_source acks the slot still on display.
    """
    widget = SourceViewerWidget()
    acked: list[int] = []
    widget.set_gpu_slot_ack_callback(acked.append)
    gen = widget._gpu_generation
    widget._gpu_expected_frame = 50

    # Frame 49 accepted (within trailing window) — nothing to ack yet.
    assert widget.set_gpu_frame(101, 320, 180, frame=49, generation=gen, slot_id=0) is True
    assert acked == []

    # Frame 50 accepted: supersedes slot 0 -> slot 0 acked.
    assert widget.set_gpu_frame(102, 320, 180, frame=50, generation=gen, slot_id=1) is True
    assert acked == [0]

    # Stale frame far behind the window: discarded, its slot acked.
    assert widget.set_gpu_frame(103, 320, 180, frame=20, generation=gen, slot_id=2) is False
    assert acked == [0, 2]

    # Mismatched-generation result: discarded, its slot acked.
    assert widget.set_gpu_frame(104, 320, 180, frame=50, generation=gen + 1, slot_id=3) is False
    assert acked == [0, 2, 3]

    # Closing the source acks the slot still on display (slot 1).
    widget._current_asset = object()
    widget.close_source()
    assert acked == [0, 2, 3, 1]
    assert widget._gpu_displayed_slot_id == -1


def test_source_viewer_in_out_shortcuts_are_local_to_its_children(qapp) -> None:
    """Source I/O uses WidgetWithChildrenShortcut, never a global binding."""
    widget = SourceViewerWidget()
    marked_in: list[int] = []
    marked_out: list[int] = []
    widget.in_point_changed.connect(marked_in.append)
    widget.out_point_changed.connect(marked_out.append)
    widget._current_asset = object()
    widget._current_frame = 42

    assert widget._mark_in_shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    assert widget._mark_out_shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    widget._mark_in_shortcut.activated.emit()
    widget._mark_out_shortcut.activated.emit()

    assert marked_in == [42]
    assert marked_out == [42]
