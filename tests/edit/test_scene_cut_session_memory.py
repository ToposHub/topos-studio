"""Tests for scene-cut session memory and preview divider styling."""

from __future__ import annotations

from src.features.edit.core.project import EditProject
from src.features.edit.core.scene_cut.models import (
    SceneCutAnalysis,
    SceneCutSettings,
)
from src.features.edit.core.scene_cut_controller import (
    DetectionContext,
    SceneCutController,
)
from src.features.edit.ui.scene_cut_preview_widget import SceneCutPreviewWidget
from PyQt6.QtWidgets import QFrame


def _make_analysis(asset_id: str) -> SceneCutAnalysis:
    return SceneCutAnalysis(
        analysis_id=f"analysis-{asset_id}",
        asset_id=asset_id,
        source_path=f"/tmp/{asset_id}.mov",
        settings=SceneCutSettings(),
    )


def _make_context(asset_id: str) -> DetectionContext:
    return DetectionContext(
        asset_id=asset_id,
        asset_path=f"/tmp/{asset_id}.mov",
    )


def test_scene_cut_analysis_is_kept_until_session_cache_is_cleared() -> None:
    """Closing a dialog must not discard its analysis within the project."""
    project = EditProject(project_id="project-session-1", name="Session")
    controller = SceneCutController(project_getter=lambda: project)
    context = _make_context("asset-1")
    analysis = _make_analysis("asset-1")

    controller._get_project()
    controller._on_detection_completed(analysis, context)

    assert controller._session_analyses[controller._analysis_key(context)] == analysis

    controller.clear_session_cache()

    assert controller._session_analyses == {}


def test_switching_project_clears_previous_session_analysis() -> None:
    """A different project must not inherit scene-cut points from the old one."""
    projects = [EditProject(project_id="project-1", name="One")]
    controller = SceneCutController(project_getter=lambda: projects[0])
    context = _make_context("asset-1")
    controller._get_project()
    controller._on_detection_completed(_make_analysis("asset-1"), context)

    projects[0] = EditProject(project_id="project-2", name="Two")
    controller._get_project()

    assert controller._session_analyses == {}


def test_preview_dividers_are_dark_flat_lines(qapp) -> None:
    """Preview separators must not use the bright native VLine frame."""
    preview = SceneCutPreviewWidget()
    preview.show()
    qapp.processEvents()

    first = preview.layout().itemAt(1).widget()
    second = preview.layout().itemAt(3).widget()
    assert isinstance(first, QFrame)
    assert isinstance(second, QFrame)
    assert first.frameShape() == QFrame.Shape.NoFrame
    assert second.frameShape() == QFrame.Shape.NoFrame
    assert first.width() == 1
    assert second.width() == 1
    assert preview.layout().spacing() == 0
    assert "border: none" in preview.get_current_panel().styleSheet()
