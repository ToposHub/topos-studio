"""Regression tests for context-aware Quick Export tab selection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.app.controllers.deliver_controller import DeliverControllerMixin
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.export.quick_export_dialog import QuickExportDialog


def _make_export_controller(
    page: str, timeline_info: dict[str, object], work_mode: str = "video",
) -> DeliverControllerMixin:
    """Build the minimal controller state required to open Quick Export."""
    controller = DeliverControllerMixin()
    controller.current_page = page
    controller.config_manager = MagicMock()
    # get_work_mode() reads the "work_mode" config key; every other key stays
    # None so the controller's `or` fallbacks take over.
    controller.config_manager.get = MagicMock(
        side_effect=lambda key=None, *args, **kwargs: (
            work_mode if key == "work_mode" else None
        )
    )
    controller.engine = SimpleNamespace(
        img_full_original=None,
        img_high=None,
        get_active_layer=lambda: None,
    )
    controller.current_file_path = ""
    controller._composite_available = False
    controller.composite_page = None
    controller._build_timeline_info = MagicMock(return_value=timeline_info)
    return controller


def _captured_initial_export_type(
    page: str, timeline_info: dict[str, object], work_mode: str = "video",
) -> str:
    """Open the controller path with a mocked dialog and return its default tab."""
    controller = _make_export_controller(page, timeline_info, work_mode=work_mode)
    with patch("src.shared.export.quick_export_dialog.QuickExportDialog") as dialog_class:
        dialog_class.EXPORT_IMAGE = QuickExportDialog.EXPORT_IMAGE
        dialog_class.EXPORT_VIDEO = QuickExportDialog.EXPORT_VIDEO
        dialog_class.return_value.exec.return_value = False
        controller.export_file_as()
    return dialog_class.call_args.kwargs["initial_export_type"]


def test_edit_timeline_opens_quick_export_on_video_tab() -> None:
    """In video work mode the active timeline defaults to video output."""
    initial_type = _captured_initial_export_type(
        "Edit", {"has_timeline": True}, work_mode="video",
    )

    assert initial_type == QuickExportDialog.EXPORT_VIDEO


def test_color_image_opens_quick_export_on_image_tab() -> None:
    """In image work mode a color still stays image-first even with a timeline."""
    initial_type = _captured_initial_export_type(
        "Color", {"has_timeline": True}, work_mode="image",
    )

    assert initial_type == QuickExportDialog.EXPORT_IMAGE


def test_timeline_info_expands_an_in_only_range_to_the_final_clip_frame() -> None:
    """The quick-export dialog receives the effective range for a lone In point."""
    clip = ClipInstance(
        clip_id="clip", asset_id="asset", track_id="video",
        timeline_start=0, timeline_duration=240,
        source_start=0, source_duration=240,
    )
    timeline = TimelineSequence(
        timeline_id="timeline", name="Timeline", fps=24.0,
        video_tracks=(
            EditTrack(track_id="video", name="Video", track_type="video", clips=(clip,)),
        ),
    ).with_range(48, None)
    controller = DeliverControllerMixin()
    controller.edit_page = SimpleNamespace(_current_timeline=timeline)
    controller._current_edit_project = None

    timeline_info = controller._build_timeline_info()

    assert timeline_info["has_range"] is True
    assert timeline_info["range_start"] == 48
    assert timeline_info["range_end"] == 239


def test_quick_export_activates_requested_video_tab(qapp) -> None:
    """An available requested video export updates tab, page, and settings mode."""
    dialog = QuickExportDialog(
        timeline_info={"has_timeline": True},
        initial_export_type=QuickExportDialog.EXPORT_VIDEO,
    )

    assert dialog.tab_bar.currentIndex() == 2
    assert dialog.content_stack.currentIndex() == 2
    assert dialog.export_type == QuickExportDialog.EXPORT_VIDEO

    dialog.deleteLater()


def test_quick_export_keeps_image_tab_when_video_is_unavailable(qapp) -> None:
    """In image work mode a stale video request falls back to the image tab."""
    dialog = QuickExportDialog(
        timeline_info={"has_timeline": False},
        initial_export_type=QuickExportDialog.EXPORT_VIDEO,
        work_mode="image",
    )

    assert dialog.tab_bar.currentIndex() == 0
    assert dialog.export_type == QuickExportDialog.EXPORT_IMAGE
    assert not dialog.tab_bar.isTabEnabled(2)

    dialog.deleteLater()
