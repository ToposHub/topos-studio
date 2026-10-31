"""
Scene Cut Controller - Unified entry point for scene cut detection.

This controller coordinates scene cut detection workflow:
- Entry points: Media Pool right-click, Timeline clip right-click, menu action
- Dialog creation and display
- Worker management
- Result handling (Add Cuts to Media Pool)

Design Principles:
- Pure functions where possible
- Signal-based coordination with UI
- No direct UI modifications from controller
- All mutations through project commands

Reference:
- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md P9
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any, Optional, Callable, Tuple

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.scene_cut import (
    SceneCutSettings,
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutDecision,
    SceneSubclipSpec,
    generate_scene_subclips,
    add_subclips_to_project,
    DUPLICATE_APPEND,
    AddSubclipsResult,
)
from src.features.edit.core.scene_cut.worker import (
    get_active_tasks_registry,
)
from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

from src.shared.i18n.i18n import I18N


logger = logging.getLogger(__name__)


# ================================================================
# Data Classes
# ================================================================

@dataclass(frozen=True)
class DetectionContext:
    """
    Context for scene cut detection operation.

    Contains all information needed to start detection:
    - Source media asset
    - Optional timeline clip context
    - Detection settings
    """
    asset_id: str
    asset_path: str
    clip_id: Optional[str] = None
    track_id: Optional[str] = None
    source_start: int = 0
    source_duration: Optional[int] = None
    fps: float = 24.0
    media_fingerprint: str = ""


# ================================================================
# AddSubclipsToProjectCommand — AUD-18200
# ================================================================

@dataclass(frozen=True)
class AddSubclipsToProjectCommand:
    """Undoable project command wrapping ``add_subclips_to_project``.

    AUD-18200: "Add Cuts to Media Pool" used to mutate and publish the
    project directly, bypassing the command/undo system. This command
    routes the same domain function through ``ProjectCommandStack`` so the
    operation becomes a single Ctrl+Z unit, matching the file header
    principle "All mutations through project commands".

    Undo removes exactly the assets/bin memberships recorded during
    ``apply`` and restores anything the DUPLICATE_REPLACE strategy
    removed. Redo simply re-applies (subclip asset ids are regenerated).
    """

    command_id: str
    label: str
    analysis: SceneCutAnalysis
    specs: Tuple[SceneSubclipSpec, ...]
    parent_asset: MediaAsset
    target_bin_id: Optional[str] = None
    duplicate_strategy: str = DUPLICATE_APPEND

    # ── Snapshots recorded during apply (undo payload) ──
    _result: Optional[AddSubclipsResult] = None
    _added_asset_ids: Tuple[str, ...] = ()
    _removed_assets: Tuple[MediaAsset, ...] = ()
    # asset_id -> bins that referenced it before REPLACE removed it
    _removed_memberships: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    _created_bin_id: Optional[str] = None

    def apply(self, project: EditProject) -> EditProject:
        """Apply the command: add scene subclips to the project.

        Args:
            project: Current project.

        Returns:
            New project with the subclips added.
        """
        result = add_subclips_to_project(
            project=project,
            analysis=self.analysis,
            specs=self.specs,
            parent_asset=self.parent_asset,
            target_bin_id=self.target_bin_id,
            duplicate_strategy=self.duplicate_strategy,
        )

        # Snapshot pre-state needed to invert DUPLICATE_REPLACE removals.
        removed_memberships: list[tuple[str, tuple[str, ...]]] = []
        for removed in result.removed_assets:
            removed_memberships.append(
                (
                    removed.asset_id,
                    tuple(
                        bin_id
                        for bin_id, bin_obj in project.bins.items()
                        if removed.asset_id in bin_obj.asset_ids
                    ),
                )
            )

        created_bin_id = (
            result.bin_id
            if result.bin_id not in project.bins
            else None
        )

        # Persist snapshots on the frozen dataclass (same escape hatch the
        # rest of the command system uses).
        object.__setattr__(self, "_result", result)
        object.__setattr__(
            self, "_added_asset_ids", tuple(result.added_asset_ids)
        )
        object.__setattr__(self, "_removed_assets", result.removed_assets)
        object.__setattr__(
            self, "_removed_memberships", tuple(removed_memberships)
        )
        object.__setattr__(self, "_created_bin_id", created_bin_id)

        logger.debug(
            "AddSubclipsToProjectCommand applied: %d subclips -> bin %s",
            result.added_count,
            result.bin_id,
        )
        return result.project

    def undo(self, project: EditProject) -> EditProject:
        """Undo the command: remove added subclips, restore replaced ones.

        Args:
            project: Current project.

        Returns:
            Project without the subclips added by this command.
        """
        result = self._result
        if result is None:
            logger.warning(
                "AddSubclipsToProjectCommand undo: nothing applied yet"
            )
            return project

        new_assets = dict(project.assets)
        new_bins = dict(project.bins)

        # 1. Remove every asset this command added.
        for asset_id in self._added_asset_ids:
            new_assets.pop(asset_id, None)
            for bin_id, bin_obj in list(new_bins.items()):
                if asset_id in bin_obj.asset_ids:
                    new_bins[bin_id] = bin_obj.with_asset_removed(asset_id)

        # 2. Restore assets removed by DUPLICATE_REPLACE, with the exact
        #    bin memberships captured before the mutation.
        for removed in self._removed_assets:
            new_assets[removed.asset_id] = removed
        for asset_id, bin_ids in self._removed_memberships:
            for bin_id in bin_ids:
                bin_obj = new_bins.get(bin_id)
                if bin_obj is not None and asset_id not in bin_obj.asset_ids:
                    new_bins[bin_id] = bin_obj.with_asset_added(asset_id)

        # 3. Drop the scene-cut bin if this command created it and undo has
        #    emptied it, so repeated add/undo does not litter empty bins.
        if (
            self._created_bin_id is not None
            and self._created_bin_id in new_bins
            and not new_bins[self._created_bin_id].asset_ids
        ):
            del new_bins[self._created_bin_id]

        # dataclasses.replace carries every other field — including the
        # optimistic-locking `revision` — across the rebuild (AUD-18100).
        undone = replace(project, assets=new_assets, bins=new_bins)
        logger.debug(
            "AddSubclipsToProjectCommand undone: removed %d subclips",
            len(self._added_asset_ids),
        )
        return undone

    @property
    def result(self) -> Optional[AddSubclipsResult]:
        """The AddSubclipsResult produced by the last apply()."""
        return self._result

    @staticmethod
    def create(
        analysis: SceneCutAnalysis,
        specs: Tuple[SceneSubclipSpec, ...],
        parent_asset: MediaAsset,
        duplicate_strategy: str = DUPLICATE_APPEND,
        label: str = "Add Cuts to Media Pool",
    ) -> AddSubclipsToProjectCommand:
        """Factory method to create the command."""
        return AddSubclipsToProjectCommand(
            command_id=str(uuid.uuid4()),
            label=label,
            analysis=analysis,
            specs=specs,
            parent_asset=parent_asset,
            duplicate_strategy=duplicate_strategy,
        )


# ================================================================
# SceneCutController
# ================================================================

class SceneCutController(QObject):
    """
    Controller for scene cut detection workflow.

    Responsibilities:
    - Receive detection requests from UI (signals)
    - Create and display SceneCutDialog
    - Manage active detection workers
    - Handle results (Add Cuts to Media Pool)
    - Coordinate with project for mutations

    Signals:
        detection_started: Detection started (asset_id)
        detection_completed: Detection completed (analysis)
        detection_error: Detection failed (asset_id, error_message)
        subclips_added: Subclips added to project (result)
        dialog_closed: Dialog was closed
        timeline_overlay_requested: B1 — the dialog's "Show on Timeline"
            toggle changed (checked). EditPage routes this to the same
            switch as the View menu entry.
    """

    detection_started = pyqtSignal(str)  # asset_id
    detection_completed = pyqtSignal(object)  # SceneCutAnalysis
    detection_error = pyqtSignal(str, str)  # asset_id, error_message
    subclips_added = pyqtSignal(object)  # AddSubclipsResult
    dialog_closed = pyqtSignal()
    timeline_overlay_requested = pyqtSignal(bool)  # B1: checked state

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        project_getter: Optional[Callable[[], Optional[EditProject]]] = None,
        project_setter: Optional[Callable[[EditProject], None]] = None,
        timeline_getter: Optional[Callable[[], Optional[Any]]] = None,
        command_stack_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        """
        Initialize scene cut controller.

        Args:
            parent: Parent widget for dialogs.
            project_getter: Function to get current EditProject.
            project_setter: Function to set updated EditProject.
            timeline_getter: Function to get the current TimelineSequence.
                Used by ``start_detection_for_clip`` to resolve the clip's
                source range (P1-11) — clip-scoped detection must never
                silently fall back to the full asset range.
            command_stack_getter: Function returning the owning context's
                ``ProjectCommandStack`` (or None). When available, project
                mutations go through undoable commands (AUD-18200).
        """
        super().__init__(parent)

        self._parent_widget = parent
        self._project_getter = project_getter
        self._project_setter = project_setter
        self._timeline_getter = timeline_getter
        self._command_stack_getter = command_stack_getter

        # Active dialog reference
        self._active_dialog: Optional[SceneCutDialog] = None
        self._active_analysis_key: Optional[tuple[str, str, str, str]] = None

        # Session-only analysis memory. This intentionally lives on the
        # controller instead of the dialog, so closing and reopening the
        # dialog keeps the detected points while closing the project clears
        # them without writing another project artifact to disk.
        self._session_analyses: dict[tuple[str, str, str, str], SceneCutAnalysis] = {}
        self._session_project_id: Optional[str] = None

        # Task registry for concurrent detection management
        self._task_registry = get_active_tasks_registry()

        # Default settings from preferences (will be loaded later)
        self._default_settings: Optional[SceneCutSettings] = None

        logger.debug("SceneCutController initialized")

    # ==================== Public Methods ====================

    def start_detection_for_asset(
        self,
        asset_id: str,
        settings: Optional[SceneCutSettings] = None,
    ) -> None:
        """
        Start scene cut detection for a media pool asset.

        This is the main entry point for Media Pool right-click action.

        Args:
            asset_id: Media asset ID to detect.
            settings: Optional detection settings, defaults used if None.
        """
        project = self._get_project()
        if project is None:
            logger.warning("No project available for scene cut detection")
            self.detection_error.emit(asset_id, "No project loaded")
            return

        asset = project.assets.get(asset_id)
        if asset is None:
            logger.warning(f"Asset {asset_id} not found")
            self.detection_error.emit(asset_id, "Asset not found")
            return

        if asset.media_type != "video":
            logger.warning(f"Asset {asset_id} is not a video, cannot detect scene cuts")
            self.detection_error.emit(asset_id, "Scene cut detection only works on video assets")
            return

        # Build detection context
        fps = asset.metadata.get("fps", 24.0) or 24.0
        total_frames = asset.metadata.get("total_frames", 0) or 0

        context = DetectionContext(
            asset_id=asset_id,
            asset_path=asset.path,
            clip_id=None,
            track_id=None,
            source_start=0,
            source_duration=total_frames if total_frames > 0 else None,
            fps=fps,
            media_fingerprint=_build_media_fingerprint(asset),
        )

        # Use provided settings or defaults
        detection_settings = settings or self._get_default_settings()

        # Show dialog and start detection
        self._show_dialog_for_context(context, detection_settings, asset)

    def start_detection_for_clip(
        self,
        clip_id: str,
        track_id: str,
        asset_id: str,
        settings: Optional[SceneCutSettings] = None,
    ) -> None:
        """
        Start scene cut detection for a timeline clip.

        This is the entry point for Timeline clip right-click action.

        Args:
            clip_id: Timeline clip ID.
            track_id: Track ID containing the clip.
            asset_id: Source asset ID.
            settings: Optional detection settings.
        """
        project = self._get_project()
        if project is None:
            logger.warning("No project available for scene cut detection")
            self.detection_error.emit(asset_id, "No project loaded")
            return

        asset = project.assets.get(asset_id)
        if asset is None:
            logger.warning(f"Asset {asset_id} not found")
            self.detection_error.emit(asset_id, "Asset not found")
            return

        if asset.media_type != "video":
            logger.warning(f"Asset {asset_id} is not a video")
            self.detection_error.emit(asset_id, "Scene cut detection only works on video assets")
            return

        # P1-11: restrict detection to the clip's source range. The sampler,
        # settings hash (and therefore task/disk-cache keys) and the subclip
        # generator all honour start/end_source_frame, so baking the range
        # into the settings keeps every downstream stage consistent.
        # source_start/source_duration are source-media frames — a re-timed
        # clip keeps its source range, and ClipSourceMapper handles the
        # timeline mapping separately.
        timeline = self._timeline_getter() if self._timeline_getter else None
        clip = timeline.get_clip_by_id(clip_id) if timeline is not None else None
        if clip is None:
            reason = I18N.tr(
                "scene_cut.clip_not_on_timeline",
                "Clip not found on the current timeline — cannot restrict detection",
            )
            logger.warning(f"start_detection_for_clip: {reason} (clip_id={clip_id})")
            self.detection_error.emit(asset_id, reason)
            return

        fps = asset.metadata.get("fps", 24.0) or 24.0
        total_frames = asset.metadata.get("total_frames", 0) or 0

        source_start = max(0, clip.source_start)
        source_end = clip.source_start + clip.source_duration
        if total_frames > 0:
            source_end = min(source_end, total_frames)
        if source_end <= source_start:
            reason = I18N.tr(
                "scene_cut.clip_range_invalid",
                "Clip has an empty source range — nothing to detect",
            )
            logger.warning(
                f"start_detection_for_clip: {reason} "
                f"(source [{clip.source_start}, {clip.source_start + clip.source_duration}))"
            )
            self.detection_error.emit(asset_id, reason)
            return

        context = DetectionContext(
            asset_id=asset_id,
            asset_path=asset.path,
            clip_id=clip_id,
            track_id=track_id,
            source_start=source_start,
            source_duration=source_end - source_start,
            fps=fps,
            media_fingerprint=_build_media_fingerprint(asset),
        )

        detection_settings = (settings or self._get_default_settings()).with_range(
            source_start, source_end
        )

        self._show_dialog_for_context(context, detection_settings, asset)

    def add_cuts_to_media_pool(
        self,
        analysis: SceneCutAnalysis,
        parent_asset: MediaAsset,
        duplicate_strategy: str = DUPLICATE_APPEND,
    ) -> Optional[AddSubclipsResult]:
        """
        Add detected cuts to media pool as virtual subclips.

        This implements the "Add Cuts to Media Pool" workflow.

        Args:
            analysis: Scene cut analysis result.
            parent_asset: Original media asset.
            duplicate_strategy: How to handle existing subclips.

        Returns:
            AddSubclipsResult if successful, None otherwise.
        """
        project = self._get_project()
        if project is None:
            logger.warning("No project available for adding subclips")
            return None

        # Get accepted candidates
        accepted = [c for c in analysis.candidates if c.is_accepted]
        if not accepted:
            logger.info("No accepted cuts to add to media pool")
            return None

        # Generate subclip specs
        total_frames = parent_asset.metadata.get("total_frames", 0) or 0
        total_duration = parent_asset.metadata.get("duration_seconds", 0.0) or 0.0
        fps = parent_asset.metadata.get("fps", 24.0) or 24.0

        specs = generate_scene_subclips(
            analysis=analysis,
            total_source_frames=total_frames,
            total_duration_seconds=total_duration,
            parent_display_name=parent_asset.display_name,
            fps=fps,
        )

        if not specs:
            logger.info("No subclip specs generated")
            return None

        # Add to project — AUD-18200: route through the project command
        # stack so the operation is a single undo unit. Fall back to the
        # direct domain call when no stack is available (test stubs).
        result: Optional[AddSubclipsResult] = None
        stack = (
            self._command_stack_getter()
            if self._command_stack_getter is not None
            else None
        )
        if stack is not None:
            command = AddSubclipsToProjectCommand.create(
                analysis=analysis,
                specs=specs,
                parent_asset=parent_asset,
                duplicate_strategy=duplicate_strategy,
            )
            new_project = stack.execute(command, project)
            result = command.result
            if result is not None:
                # Publish the command's project (identical to
                # result.project) through the normal update path.
                if self._project_setter:
                    self._project_setter(new_project)
            else:
                logger.error(
                    "AddSubclipsToProjectCommand produced no result"
                )

        if result is None:
            result = add_subclips_to_project(
                project=project,
                analysis=analysis,
                specs=specs,
                parent_asset=parent_asset,
                duplicate_strategy=duplicate_strategy,
            )

            # Update project
            if self._project_setter:
                self._project_setter(result.project)

        # Emit signal
        self.subclips_added.emit(result)

        logger.info(
            f"Added {result.added_count} subclips to media pool "
            f"for analysis {analysis.analysis_id}"
        )

        return result

    def get_active_dialog(self) -> Optional[SceneCutDialog]:
        """Get currently active SceneCutDialog."""
        return self._active_dialog

    def has_active_dialog(self) -> bool:
        """Check if there's an active detection dialog."""
        return self._active_dialog is not None

    def close_active_dialog(self) -> None:
        """Close any active detection dialog."""
        if self._active_dialog:
            self._active_dialog.close()
            self._active_dialog = None

    def set_default_settings(self, settings: SceneCutSettings) -> None:
        """Set default detection settings from preferences."""
        self._default_settings = settings

    def clear_session_cache(self) -> None:
        """Forget temporary scene-cut analyses when the project is closed."""
        self._session_analyses.clear()
        self._active_analysis_key = None
        self._session_project_id = None

    def _analysis_key(self, context: DetectionContext) -> tuple[str, str, str, str]:
        """Build a session cache key for an asset/clip detection context.

        The source range is part of the key: re-trimming a clip and
        re-running detection must not serve the previous range's analysis
        from the session cache (P1-11).
        """
        duration = (
            context.source_duration if context.source_duration is not None else "full"
        )
        return (
            context.asset_id,
            context.clip_id or "",
            context.asset_path,
            f"{context.source_start}-{duration}",
        )

    # ==================== Private Methods ====================

    def _get_project(self) -> Optional[EditProject]:
        """Get current project via getter function."""
        project = self._project_getter() if self._project_getter else None
        project_id = getattr(project, "project_id", None)
        if project_id != self._session_project_id:
            if self._session_project_id is not None:
                self.clear_session_cache()
            self._session_project_id = project_id
        return project

    def _get_default_settings(self) -> SceneCutSettings:
        """Get default detection settings."""
        if self._default_settings:
            return self._default_settings

        # Create default settings
        return SceneCutSettings(
            sensitivity=0.55,
            min_scene_frames=8,
            use_proxy=True,
        )

    def _show_dialog_for_context(
        self,
        context: DetectionContext,
        settings: SceneCutSettings,
        asset: MediaAsset,
    ) -> None:
        """
        Show SceneCutDialog for detection context.

        Args:
            context: Detection context with asset/clip info.
            settings: Detection settings.
            asset: MediaAsset for display.
        """
        # Close existing dialog if any
        if self._active_dialog:
            self._active_dialog.close()

        analysis_key = self._analysis_key(context)
        self._active_analysis_key = analysis_key

        # Create new dialog
        self._active_dialog = SceneCutDialog(
            source_path=context.asset_path,
            asset_id=context.asset_id,
            settings=settings,
            proxy_path=asset.proxy_path if asset.has_proxy else None,
            use_persistent_cache=False,
            parent=self._parent_widget,
        )

        # Connect dialog signals
        self._connect_dialog_signals(self._active_dialog, context, asset)

        # Restore only the in-memory result for this open project session.
        # Disk cache remains an explicit detection fallback, not an automatic
        # cross-project restore.
        cached_analysis = self._session_analyses.get(analysis_key)
        if cached_analysis is not None:
            self._active_dialog.set_analysis(cached_analysis)

        # Show dialog
        self._active_dialog.show()

        logger.debug(f"SceneCutDialog shown for asset {context.asset_id}")

    def _connect_dialog_signals(
        self,
        dialog: SceneCutDialog,
        context: DetectionContext,
        asset: MediaAsset,
    ) -> None:
        """Connect dialog signals to controller handlers."""
        # Detection completion
        dialog.detection_completed.connect(
            lambda analysis: self._on_detection_completed(analysis, context)
        )

        # Add cuts to media pool (use get_analysis() to ensure decisions are synced)
        dialog.cuts_added_to_pool.connect(
            lambda candidates: self._on_add_to_media_pool(
                dialog.get_analysis(), asset, candidates
            )
        )

        # Dialog closed
        dialog.finished.connect(self._on_dialog_closed)

        # B1: forward the "Show on Timeline" toggle; EditPage owns the
        # overlay switch and the persisted state behind it.
        dialog.show_on_timeline_toggled.connect(
            self.timeline_overlay_requested.emit
        )

    def _on_detection_completed(
        self,
        analysis: SceneCutAnalysis,
        context: DetectionContext,
    ) -> None:
        """Handle detection completion."""
        logger.info(
            f"Scene cut detection completed for {context.asset_id}: "
            f"{len(analysis.candidates)} candidates found"
        )

        self._session_analyses[self._analysis_key(context)] = analysis

        self.detection_completed.emit(analysis)

    def _on_add_to_media_pool(
        self,
        analysis: Optional[SceneCutAnalysis],
        asset: MediaAsset,
        candidates: Optional[list[SceneCutCandidate]] = None,
    ) -> None:
        """Handle Add Cuts to Media Pool request."""
        if analysis is None:
            return

        # The dialog can submit active Pending candidates directly. The core
        # subclip generator intentionally consumes Accepted candidates only,
        # so promote this explicit user-confirmed selection at the boundary.
        if candidates:
            decisions = {
                candidate.source_frame: SceneCutDecision.ACCEPTED
                for candidate in candidates
            }
            analysis = analysis.with_all_decisions(decisions)

        try:
            result = self.add_cuts_to_media_pool(analysis, asset)
        except ValueError as exc:
            # AUD-18201: add_subclips_to_project raises ValueError when the
            # parent asset is no longer in the (switched) project. Convert
            # to the controller's error channel instead of letting the
            # exception escape into the Qt signal dispatcher.
            logger.warning(
                "Add Cuts to Media Pool failed for asset %s: %s",
                asset.asset_id,
                exc,
            )
            reason = I18N.tr(
                "scene_cut.add_to_pool_failed",
                "Could not add cuts to the media pool: {reason}",
            ).format(reason=str(exc))
            self.detection_error.emit(asset.asset_id, reason)
            return

        if result and result.added_count > 0:
            # Close dialog after successful add
            if self._active_dialog:
                self._active_dialog.close()

    def _on_dialog_closed(self, result: int) -> None:
        """Handle dialog closed."""
        if self._active_dialog is not None and self._active_analysis_key is not None:
            analysis = self._active_dialog.get_analysis()
            if analysis is not None:
                self._session_analyses[self._active_analysis_key] = analysis
        self._active_dialog = None
        self._active_analysis_key = None
        self.dialog_closed.emit()
        logger.debug("SceneCutDialog closed")


# ================================================================
# Helper Functions
# ================================================================

def _build_media_fingerprint(asset: MediaAsset) -> str:
    """
    Build media fingerprint for cache validation.

    Includes file size, mtime, and path hash.

    Args:
        asset: MediaAsset to fingerprint.

    Returns:
        Fingerprint string.
    """
    import hashlib
    import os

    try:
        stat = os.stat(asset.path)
        data = f"{asset.path}|{stat.st_size}|{stat.st_mtime_ns}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    except OSError:
        # File not accessible, use path only
        return hashlib.sha256(asset.path.encode()).hexdigest()[:16]


def create_scene_cut_controller(
    parent: Optional[QWidget] = None,
    edit_page: Optional[Any] = None,
) -> SceneCutController:
    """
    Factory function to create SceneCutController with EditPage bindings.

    Args:
        parent: Parent widget for dialogs.
        edit_page: EditPage instance for project access.

    Returns:
        SceneCutController instance.
    """
    # Build project getter/setter from EditPage
    project_getter = None
    project_setter = None
    timeline_getter = None
    command_stack_getter = None

    if edit_page:
        timeline_getter = lambda: getattr(edit_page, "_current_timeline", None)
        media_pool = edit_page._media_pool_panel
        if media_pool:
            project_getter = lambda: media_pool.get_project()

            # AUD-18200: hand the controller the media pool's
            # ProjectCommandStack so "Add Cuts to Media Pool" becomes an
            # undoable project command instead of a direct mutation.
            stack_getter = getattr(media_pool, "get_command_stack", None)
            if callable(stack_getter):
                command_stack_getter = stack_getter

            def set_project(project: EditProject) -> None:
                """Publish a scene-cut project update to every Edit consumer."""
                media_pool.set_project(project)

                # The scene-cut controller is owned by EditPage, while the
                # normal import path publishes through MainWindow's
                # ``project_changed`` routing.  Updating only the media pool
                # leaves TimelineView with the previous asset map, which is
                # why freshly-created subclip IDs were rejected during drag.
                timeline_panel = getattr(edit_page, "_timeline_panel", None)
                if timeline_panel is not None and hasattr(timeline_panel, "set_project"):
                    timeline_panel.set_project(project)

                main_window = getattr(edit_page, "_main_window", None)
                if main_window is not None:
                    main_window._current_edit_project = project

                project_changed = getattr(media_pool, "project_changed", None)
                if project_changed is not None:
                    project_changed.emit(project)

            project_setter = set_project

    return SceneCutController(
        parent=parent,
        project_getter=project_getter,
        project_setter=project_setter,
        timeline_getter=timeline_getter,
        command_stack_getter=command_stack_getter,
    )


# ================================================================
# Export
# ================================================================

__all__ = [
    "SceneCutController",
    "DetectionContext",
    "AddSubclipsToProjectCommand",
    "create_scene_cut_controller",
]
