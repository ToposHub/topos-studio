"""
MainWindowAdapter — bridges MainWindow signals to CompositeRuntime.

This adapter is the single point of contact between MainWindow and the
Composite feature. MainWindow owns the adapter and forwards events through
it, rather than directly importing or referencing Composite UI classes.

Responsibilities:
- Receive MainWindow's color output signal
- Receive SaveRouter callback wiring
- Receive export callbacks
- Bridge page lifecycle (enter/leave)
- Does NOT implement UI business logic
"""

import logging
from typing import Any, Callable, TYPE_CHECKING

from src.features.composite.runtime import CompositeRuntime

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QMainWindow

logger = logging.getLogger(__name__)

# AUD-4401: the Comp Clip Media Pool poster identity is re-resolved at most
# once per this interval (and immediately after a graph/user-edit signal),
# instead of running a full-graph serialization on every preview frame.
_COMP_CLIP_POSTER_ID_REFRESH_SECONDS = 1.0


class MainWindowAdapter:
    """
    Adapter between MainWindow and Composite feature.

    MainWindow only interacts with Composite through this adapter,
    reducing direct coupling and making it possible to disable/delete
    the Composite feature without breaking other pages.
    """

    def __init__(self, main_window: "QMainWindow"):
        self._mw = main_window
        self._runtime = CompositeRuntime()
        self._connected = False
        self._last_readback_time: float = 0.0
        self._last_scope_submit_time: float = 0.0
        self._last_presented_preview_request_id: int = 0
        self._latest_gpu_scope_demand = None
        # AUD-4401: cached Comp Clip identity for Media Pool poster
        # publishing — refreshed only on graph change / TTL expiry.
        self._comp_clip_asset_id: str = ""
        self._comp_clip_asset_id_valid: bool = False
        self._comp_clip_asset_id_time: float = 0.0

    @property
    def runtime(self) -> CompositeRuntime:
        return self._runtime

    def is_available(self) -> bool:
        return self._runtime.is_available()

    def create(self, parent: Any) -> bool:
        """
        Create Composite UI components and wire signals.

        Returns True if creation succeeded.
        """
        # Try to get the existing CompositePage from module.register_pages()
        # to avoid creating duplicate instances.
        existing_page = None
        if hasattr(self._mw, 'feature_registry') and self._mw.feature_registry:
            composite_module = self._mw.feature_registry.get_module("composite")
            if composite_module and hasattr(composite_module, '_page'):
                existing_page = composite_module._page

        if not self._runtime.create(parent, existing_page=existing_page):
            return False

        self._wire_signals()
        return True

    def _wire_signals(self) -> None:
        """Connect Composite page signals to MainWindow handler methods."""
        if not self._runtime.page or self._connected:
            return

        page = self._runtime.page

        page.switch_to_color_requested.connect(
            lambda: self._mw.switch_to_page("Color")
        )
        page.composite_frame_ready.connect(self._mw._on_composite_frame_ready)
        page.composite_metadata_ready.connect(self._mw._on_composite_metadata_ready)
        page.status_message.connect(self._mw._on_composite_status_message)
        page.composite_user_edit_started.connect(self._mw._on_composite_user_edit_started)
        page.composite_graph_changed.connect(self._mw._on_composite_graph_changed)
        # AUD-4401: graph edits may change the composition identity — drop
        # the cached Media Pool poster id so the next frame re-resolves it.
        page.composite_graph_changed.connect(self._on_composite_graph_changed_for_poster)
        page.composite_user_edit_started.connect(self._on_user_edit_started_for_poster)
        page.composite_preview_image_ready.connect(self._on_preview_image_ready)
        page.composite_gpu_scope_ready.connect(self._on_gpu_scope_ready)
        # Output node "Render Output" button → composite render-output flow
        page.render_output_requested.connect(self._mw._on_composite_render_output)
        # Empty-state CTA reuses the existing Media Pool creation workflow.
        create_comp_clip = getattr(self._mw, "_on_create_empty_comp_clip", None)
        if callable(create_comp_clip):
            page.create_empty_comp_clip_requested.connect(create_comp_clip)

        if self._runtime._node_editor:
            editor = self._runtime._node_editor
            editor.node_selected.connect(self._mw._on_node_editor_selected)
            editor.selection_cleared.connect(self._runtime.properties_panel.clear)
            editor.node_removed.connect(lambda _: self._runtime.properties_panel.clear())

        # Scope demand state changes (mode / zoom / false color variant /
        # panel resize) must re-submit immediately — a paused composite
        # produces no new frames that would otherwise carry the demand.
        scope_panel = self._runtime.scope_panel
        if scope_panel is not None and hasattr(scope_panel, "demand_changed"):
            scope_panel.demand_changed.connect(self._on_scope_demand_changed)

        self._connected = True

    def _on_scope_demand_changed(self) -> None:
        """Re-submit the GPU scope demand after a mode/zoom/size change."""
        if getattr(self._mw, "current_page", "") != "Composite":
            return
        if not self._is_scope_visible():
            return
        page = self._runtime.page
        request_id = int(getattr(page, "_last_submitted_request_id", 0) or 0)
        self._maybe_submit_gpu_scope_demand(request_id, force=True)

    def create_docks(self, create_dock_func: Callable) -> dict:
        """
        Create dock widgets for Composite feature.

        Returns a dict keyed by panel role. Each entry is present only when
        the corresponding runtime panel was created.
        """

        from src.shared.i18n.i18n import I18N

        docks = {}

        if self._runtime.page:
            self._runtime.dock_composite = create_dock_func(
                I18N.tr("panel.composite", "Node Graph"),
                self._runtime.page,
                "dockComposite"
            )
            docks["composite"] = self._runtime.dock_composite

        # Independent Node Properties dock. Previously the NodePropertiesPanel
        # was borrowed into Edit's shared ``dock_properties``; that dock is
        # only created when the Edit feature is available, so in Image mode
        # (Edit disabled) the panel had no container. Composite now owns its
        # own dock so it works in every work mode.
        if self._runtime.properties_panel:
            self._runtime.dock_node_properties = create_dock_func(
                I18N.tr("panel.node_properties", "Node Properties"),
                self._runtime.properties_panel,
                "dockCompositeNodeProperties"
            )
            docks["node_properties"] = self._runtime.dock_node_properties

        if self._runtime.scope_panel:
            self._runtime.dock_scope = create_dock_func(
                I18N.tr("panel.composite_scopes", "Scopes"),
                self._runtime.scope_panel,
                "dockCompositeScopes",
            )
            self._runtime.dock_scope.setMinimumHeight(220)
            self._runtime.dock_scope.visibilityChanged.connect(
                self._on_scope_visibility_changed
            )
            docks["scope"] = self._runtime.dock_scope

        if self._runtime.perf_panel:
            self._runtime.dock_perf = create_dock_func(
                I18N.tr("panel.perf", "Performance"),
                self._runtime.perf_panel,
                "dockPerf"
            )
            docks["perf"] = self._runtime.dock_perf

        # P3: Fusion-style Keyframes panel (bottom dock).
        if self._runtime.keyframes_panel:
            self._runtime.dock_keyframes = create_dock_func(
                I18N.tr("panel.keyframes", "Keyframes"),
                self._runtime.keyframes_panel,
                "dockCompositeKeyframes"
            )
            docks["keyframes"] = self._runtime.dock_keyframes

        # Phase 3: Spline curve editor (independent bottom dock for now).
        if self._runtime.spline_panel:
            self._runtime.dock_spline = create_dock_func(
                I18N.tr("panel.spline", "Spline"),
                self._runtime.spline_panel,
                "dockCompositeSpline"
            )
            # A QDockWidget may show/hide its child repeatedly while the main
            # window restores its layout. Do not treat those lifecycle events
            # as opening Spline: only the explicit Panels-menu/toggle action
            # should redirect to the shared Graph Editor.
            if hasattr(self._runtime.spline_panel, "set_dock_hosted"):
                self._runtime.spline_panel.set_dock_hosted(True)
            spline_action = self._runtime.dock_spline.toggleViewAction()
            if hasattr(self._runtime.spline_panel,
                       "request_graph_mode_redirect"):
                spline_action.triggered.connect(
                    lambda checked=False,
                    panel=self._runtime.spline_panel:
                    panel.request_graph_mode_redirect() if checked else None
                )
            docks["spline"] = self._runtime.dock_spline

        # F0: structured model import report (right-side optional dock).
        if self._runtime.import_report_panel:
            self._runtime.dock_import_report = create_dock_func(
                I18N.tr("panel.import_report", "Import Report"),
                self._runtime.import_report_panel,
                "dockCompositeImportReport"
            )
            docks["import_report"] = self._runtime.dock_import_report

        # P1-14: serialized asset dependency report and safe collect/relink
        # entry points.  The page remains the owner of graph mutations.
        if self._runtime.asset_dependency_panel:
            self._runtime.dock_asset_dependency = create_dock_func(
                I18N.tr("panel.asset_dependency", "Asset Dependencies"),
                self._runtime.asset_dependency_panel,
                "dockCompositeAssetDependency"
            )
            docks["asset_dependency"] = self._runtime.dock_asset_dependency

        # P1-18: Model3D hierarchy/visibility outliner (right-side optional dock).
        if self._runtime.scene_outliner_panel:
            self._runtime.dock_scene_outliner = create_dock_func(
                I18N.tr("panel.scene_outliner", "Scene Outliner"),
                self._runtime.scene_outliner_panel,
                "dockCompositeSceneOutliner"
            )
            docks["scene_outliner"] = self._runtime.dock_scene_outliner

        # P1-23: per-material PBR/texture-slot editor.
        if self._runtime.material_panel:
            self._runtime.dock_material = create_dock_func(
                I18N.tr("panel.material", "Material"),
                self._runtime.material_panel,
                "dockCompositeMaterial"
            )
            docks["material"] = self._runtime.dock_material

        return docks

    def get_save_callbacks(self) -> dict:
        """Return callbacks for SaveRouter integration."""
        return {
            "get_current_composite_id": lambda: getattr(self._mw, '_current_composite_id', None),
            "get_composite_graph_data": self._get_graph_data,
            "get_composite_viewer_state": self._get_viewer_state,
            "on_composite_id_created": self._mw._on_composite_id_created,
        }

    def _get_graph_data(self):
        return self._runtime.get_graph_data()

    def _get_viewer_state(self):
        return self._runtime.get_viewer_state()

    def on_color_output_changed(self, texture_id: int, width: int, height: int, revision: int) -> None:
        """Handle color output change from the engine."""
        if not self._runtime.page:
            return

        self._runtime.page.mark_color_dirty()
        if not self._runtime.page.requires_color_output_input():
            # For TimelineClipInput graphs, still trigger re-evaluation during
            # video playback so the composite page gets new frames.
            current_page = getattr(self._mw, 'current_page', '')
            if current_page == "Composite" and self._runtime.page._is_active:
                self._runtime.page._evaluate_graph()
            return

        current_page = getattr(self._mw, 'current_page', '')
        if current_page == "Composite":
            if not self._runtime.page._is_active:
                self._runtime.page._is_active = True

            if getattr(self._mw, '_composite_startup_pending', False):
                self._mw._composite_startup_pending = False
                if self._runtime.viewer and hasattr(self._mw, 'dock_viewport') and self._mw.dock_viewport:
                    self._mw.dock_viewport.setWidget(self._runtime.viewer)

            if self._runtime.page._renderer is None:
                self._runtime.page.initialize_renderer()

            # Inject the *real* source dimensions (from the full-res image
            # buffer) so Project Settings shows 4688x7040, not the 2048-px
            # proxy that ``width``/``height`` carry here.
            self._inject_source_image_size()

            self._runtime.page.update_color_output(texture_id, width, height, revision)

            # First-launch-on-Composite: enter_page_hook was skipped during
            # startup (same-page early return in switch_to_page). Run it now
            # so the Properties panel is wired and the graph is bound.
            if getattr(self._mw, '_composite_enter_needed_on_startup', False):
                self._mw._composite_enter_needed_on_startup = False
                try:
                    self._mw.enter_page_hook("Composite")
                except Exception as e:
                    logger.warning(f"Composite on_color_output_changed enter_page_hook failed: {e}")

    def on_enter_page(self, color_texture_id=None, color_width=0, color_height=0, color_revision=0) -> None:
        """Handle entering Composite page."""
        if not self._runtime.is_available():
            return

        # The Node Properties dock permanently owns its widget (set at
        # creation time), so there is no setWidget swap here. We only ensure
        # the dock is visible when entering the Composite page.
        dock_np = getattr(self._mw, 'dock_node_properties', None)
        if dock_np is not None and not dock_np.isVisible():
            dock_np.show()

        if self._runtime.viewer and hasattr(self._mw, 'dock_viewport') and self._mw.dock_viewport:
            self._mw.dock_viewport.setWidget(self._runtime.viewer)

        needs_color_output = self._runtime.page.requires_color_output_input()
        if not needs_color_output:
            color_texture_id = None
            color_width = 0
            color_height = 0
            color_revision = 0

        # Inject the real source size before on_enter runs the sync so Project
        # Settings opens with the full-res dimensions, not the proxy size.
        self._inject_source_image_size()

        self._runtime.on_enter(color_texture_id, color_width, color_height, color_revision)

        if (
            self._runtime.viewer
            and not getattr(self._runtime.page, "_runtime_context", None)
            and self._runtime.page._last_texture_id
        ):
            self._present_frame_to_viewer(
                getattr(self._runtime.page, "_last_submitted_request_id", 0),
                self._runtime.page._last_texture_id,
                self._runtime.page._last_texture_width,
                self._runtime.page._last_texture_height,
            )
            if self._runtime.page._last_metadata:
                self._mw._on_composite_metadata_ready(self._runtime.page._last_metadata)

    def _inject_source_image_size(self) -> None:
        """Push the full-resolution source dimensions into the Composite page.

        The Composite preview renders at a 2048-px proxy, so the width/height
        arriving through ``on_color_output_changed``/``on_enter_page`` are the
        proxy dimensions. Project Settings and the Output node should reflect
        the *real* source size so the readout is honest and exports default to
        full resolution. We read ``engine.img_full_original.shape`` (the
        full-res BGR ndarray) for the authoritative dimensions.
        """
        page = self._runtime.page
        if page is None or not hasattr(page, "set_source_image_size"):
            return
        engine = getattr(self._mw, "engine", None)
        img = getattr(engine, "img_full_original", None)
        if img is None:
            return
        try:
            h, w = img.shape[:2]
        except (AttributeError, IndexError, TypeError):
            return
        if w > 0 and h > 0:
            page.set_source_image_size(w, h)

    def on_leave_page(self) -> None:
        """Handle leaving Composite page."""
        if not self._runtime.is_available():
            return

        self._runtime.on_leave()

        if hasattr(self._mw, 'viewport_container') and hasattr(self._mw, 'dock_viewport') and self._mw.dock_viewport:
            self._mw.dock_viewport.setWidget(self._mw.viewport_container)

        if self._runtime.viewer:
            self._runtime.viewer.hide()

        if hasattr(self._mw, 'engine') and self._mw.engine and self._mw.engine.renderer:
            self._mw.engine.render_pipeline(render_reason='viewport_refresh')

    def handle_frame_ready(self, request_id: int, tex_id: int, width: int, height: int) -> None:
        """Forward composite frame to viewer."""
        current_page = getattr(self._mw, 'current_page', '')
        logger.info(
            f"[COMP-PLAY] handle_frame_ready: req={request_id}, tex={tex_id}, "
            f"size={width}x{height}, page={current_page}"
        )
        if current_page == "Composite" and self._runtime.viewer:
            self._present_frame_to_viewer(request_id, tex_id, width, height)

    def _present_frame_to_viewer(self, request_id: int, tex_id: int, width: int, height: int) -> None:
        """Present the renderer output in the Composite viewer.

        Strategy:
        1. Try direct texture sharing (set_texture) - zero-copy, no readback
        2. If that fails, submit READBACK_PRESENT_ASYNC to render thread
           (non-blocking for UI). Result arrives via preview_image_ready signal.
        3. Rate-limit async readback to avoid overwhelming the GPU
        """
        if not self._runtime.viewer:
            return

        page = self._runtime.page

        timeline_backed = bool(page and getattr(page, "_runtime_context", None) is not None)

        if tex_id <= 0 or width <= 0 or height <= 0:
            self._runtime.viewer.set_texture(0, 0, 0)
            logger.warning(
                "[COMPOSITE-PRESENT] empty renderer output; viewer cleared "
                f"request_id={request_id} timeline_backed={timeline_backed}"
            )
            return

        # Scope analysis rides on every presented frame: submit a GPU scope
        # demand regardless of which viewer presentation path succeeds, so
        # the QC dock mirrors the Color page's GPU-only realtime behavior.
        self._maybe_submit_gpu_scope_demand(request_id)

        # Try direct texture sharing first (GPU-side copy, no readback).
        # The viewer validates that the renderer texture is accessible from
        # its context; timeline-backed playback should not be forced down the
        # much slower GPU->CPU->GPU fallback when sharing works.
        if tex_id > 0 and self._runtime.viewer.set_texture(tex_id, width, height):
            return

        # Rate-limit readback to max ~24fps to avoid GPU saturation
        import time as _time
        now = _time.monotonic()
        if now - self._last_readback_time < 1.0 / 24.0:
            return  # Skip — too soon since last readback
        self._last_readback_time = now

        # Submit async readback to render thread (non-blocking for UI)
        if page and page._renderer:
            from src.features.composite.render.composite_renderer import CompositeCommand
            page._renderer.submit(
                CompositeCommand.READBACK_PRESENT_ASYNC,
                {"request_id": request_id},
            )
            logger.debug(
                "[COMPOSITE-PRESENT] direct_texture_sharing=false, mode=async_readback"
            )
        else:
            logger.debug(
                "[COMPOSITE-PRESENT] direct_texture_sharing=false, "
                "mode=readback_fallback (renderer unavailable)"
            )

    def _is_scope_visible(self) -> bool:
        """Return whether the Composite QC dock is actually exposed."""
        from PyQt6.QtWidgets import QDockWidget, QWidget

        dock = getattr(self._runtime, "dock_scope", None)
        panel = getattr(self._runtime, "scope_panel", None)
        if not isinstance(dock, QDockWidget) or not isinstance(panel, QWidget):
            return False
        try:
            return bool(dock.isVisible() and panel.isVisible())
        except Exception:
            return False

    def _maybe_submit_gpu_scope_demand(self, request_id: int = 0,
                                       force: bool = False) -> None:
        """Submit a native GPU scope demand for the QC dock (latest wins).

        Mirrors the Color page controller: the demand carries only demand
        state (mode/size/zoom/color state), never pixels. The renderer runs
        the shared scope engine on the most recent output texture and emits
        a ``ScopeGpuFrame`` descriptor back. Rate-limited to ~15 Hz; the
        renderer-side submit() coalesces any burst down to one queued pass.
        """
        if not self._is_scope_visible():
            return
        page = self._runtime.page
        renderer = getattr(page, "_renderer", None) if page is not None else None
        if renderer is None:
            return
        panel = getattr(self._runtime, "scope_panel", None)
        if panel is None:
            return
        import time as _time
        now = _time.monotonic()
        if not force and now - self._last_scope_submit_time < 1.0 / 15.0:
            return
        self._last_scope_submit_time = now

        from src.features.color.core.scope_types import (
            ScopeDemand, PLAYBACK_ANALYSIS_SIZE, HIGH_QUALITY_ANALYSIS_SIZE,
        )
        from src.features.composite.render.composite_renderer import (
            CompositeCommand,
        )
        state = panel.demand_state()
        demand = ScopeDemand(
            revision=panel.gpu_demand_revision,
            # The composite request id doubles as the freshness marker so
            # stale frames from an older frame presentation are rejected.
            source_revision=int(request_id),
            mode=state['mode'],
            display_size=state['display_size'],
            # Forced submits happen while paused (dock shown / mode change):
            # spend the high-quality budget like the Color page's idle tier.
            analysis_size=(
                HIGH_QUALITY_ANALYSIS_SIZE if force else PLAYBACK_ANALYSIS_SIZE
            ),
            vectorscope_zoom=state['vectorscope_zoom'],
            is_playing=False,
            scope_transfer=state.get('scope_transfer', 'srgb'),
            scope_primaries=state.get('scope_primaries', 'bt709'),
            falsecolor_mode=state.get('falsecolor_mode', 'sdr'),
            zebra_threshold=state.get('zebra_threshold', 1.0),
        )
        self._latest_gpu_scope_demand = demand
        renderer.submit(CompositeCommand.RENDER_GPU_SCOPE, demand)

    def _on_preview_image_ready(self, request_id: int, data: object, width: int, height: int) -> None:
        """Handle async preview image from renderer — upload to viewer.

        Called via signal from the UI thread after render thread completes
        a non-blocking readback. No UI thread blocking occurs. Scope
        analysis no longer consumes readbacks: it runs on the GPU scope
        lane exclusively.
        """
        import numpy as np
        current_page = getattr(self._mw, 'current_page', '')
        if current_page != "Composite":
            return
        if request_id > 0 and request_id <= self._last_presented_preview_request_id:
            return
        if isinstance(data, np.ndarray):
            self._publish_comp_clip_media_pool_preview(data, width, height)
            if not self._runtime.viewer:
                return
            self._runtime.viewer.set_image_data(data, width, height)
            if request_id > 0:
                self._last_presented_preview_request_id = request_id

    def _on_gpu_scope_ready(self, frame) -> None:
        """Forward a ``ScopeGpuFrame`` to the QC panel if still valid."""
        if getattr(self._mw, "current_page", "") != "Composite":
            return
        # Color-page parity: a hidden dock never consumes frames, so a stale
        # texture id cannot re-populate a presenter cleared by hideEvent.
        if not self._is_scope_visible():
            return
        panel = getattr(self._runtime, "scope_panel", None)
        if frame is None or panel is None:
            return
        from src.features.color.core.scope_types import is_frame_valid_for_demand
        if not is_frame_valid_for_demand(frame, self._latest_gpu_scope_demand):
            return
        try:
            panel.update_gpu_scope(frame)
        except Exception as exc:
            logger.debug("Composite GPU scope present failed: %s", exc)

    def _on_scope_visibility_changed(self, visible: bool) -> None:
        """Populate the QC dock from the latest frame when it is shown."""
        if not visible or getattr(self._mw, "current_page", "") != "Composite":
            return
        page = self._runtime.page
        request_id = int(getattr(page, "_last_submitted_request_id", 0) or 0)
        self._last_scope_submit_time = 0.0
        self._maybe_submit_gpu_scope_demand(request_id, force=True)

    def _on_composite_graph_changed_for_poster(self, composite_id: str,
                                                graph_data: dict) -> None:
        """Graph changed — the cached poster identity may be stale (AUD-4401)."""
        self._comp_clip_asset_id_valid = False

    def _on_user_edit_started_for_poster(self, composite_id: str) -> None:
        """User edit started — re-resolve the poster identity (AUD-4401)."""
        self._comp_clip_asset_id_valid = False

    def _resolve_comp_clip_asset_id(self, page: Any) -> str:
        """Resolve the current composition's Media Pool asset id.

        AUD-4401: ``page.get_composite_data()`` serializes the whole graph
        (``save_ui_state()`` + ``NodeGraph.to_dict()``) — O(nodes) on the UI
        thread. Poster publishing runs on every async-readback preview frame
        (up to ~24fps), so the identity is cached and only re-resolved when
        a graph/user-edit signal invalidated it or the TTL expired.
        """
        import time as _time
        now = _time.monotonic()
        if (getattr(self, "_comp_clip_asset_id_valid", False)
                and now - getattr(self, "_comp_clip_asset_id_time", 0.0)
                < _COMP_CLIP_POSTER_ID_REFRESH_SECONDS):
            return getattr(self, "_comp_clip_asset_id", "")
        try:
            graph_data = page.get_composite_data()
            asset_id = str((graph_data or {}).get("comp_asset_id") or "")
        except Exception:
            logger.debug("Failed to resolve Comp Clip asset id", exc_info=True)
            asset_id = ""
        self._comp_clip_asset_id = asset_id
        self._comp_clip_asset_id_time = now
        self._comp_clip_asset_id_valid = True
        return asset_id

    def _publish_comp_clip_media_pool_preview(
        self, data: object, width: int, height: int
    ) -> None:
        """Cache the current rendered Composite frame as a Media Pool poster."""
        import numpy as np
        from PyQt6.QtGui import QImage, QPixmap

        media_pool = getattr(self._mw, "media_pool_panel", None)
        page = self._runtime.page
        if media_pool is None or page is None or not isinstance(data, np.ndarray):
            return

        try:
            comp_asset_id = self._resolve_comp_clip_asset_id(page)
            if not comp_asset_id or data.ndim != 3 or data.shape[2] < 3:
                return
            rgb8 = (np.clip(data[:, :, :3], 0.0, 1.0) * 255.0).astype(np.uint8)
            rgb8 = rgb8[:, :, [2, 1, 0]]  # renderer readback is BGR
            image = QImage(
                rgb8.data.tobytes(),
                int(width),
                int(height),
                int(width) * 3,
                QImage.Format.Format_RGB888,
            ).copy()
            media_pool.set_comp_clip_preview(comp_asset_id, QPixmap.fromImage(image))
        except Exception:
            logger.debug("Failed to publish Comp Clip Media Pool preview", exc_info=True)

    def handle_metadata_ready(self, meta: dict) -> None:
        """Forward composite metadata to viewer overlay."""
        if not self._runtime.viewer or not self._runtime.is_available():
            return

        try:
            from src.features.composite.core.format import ImageFormat, BBox
        except ImportError:
            logger.warning("Composite format module unavailable")
            return

        fmt = None
        fmt_data = meta.get("format")
        if fmt_data:
            fmt = ImageFormat(
                width=fmt_data["width"], height=fmt_data["height"],
                pixel_aspect=fmt_data.get("pixel_aspect", 1.0),
                name=fmt_data.get("name", ""),
                origin_x=fmt_data.get("origin_x", 0),
                origin_y=fmt_data.get("origin_y", 0),
            )
        bbox = None
        bbox_data = meta.get("bbox")
        if bbox_data:
            bbox = BBox(
                x=bbox_data["x"], y=bbox_data["y"],
                width=bbox_data["width"], height=bbox_data["height"],
            )
        self._runtime.viewer.set_overlay_data(
            image_format=fmt if fmt_data else {},
            bbox=bbox if bbox_data else {},
        )

    def restore_graph(self, graph_data: dict, project_dir: str) -> None:
        """Restore composite graph from project data.

        AUD-4400: ``graph_data`` comes from a persisted project file
        (external data). A corrupt composite section must not abort the
        whole project-open flow — on failure the page falls back to a clean
        empty workspace so the rest of the project (Layers/RAW/...) keeps
        loading.
        """
        if not self._runtime.is_available() or not self._runtime.page:
            return

        from src.features.composite.core.node_engine import NodeGraph

        nodes_data = (graph_data.get("nodes", [])
                      if isinstance(graph_data, dict) else [])
        if not nodes_data:
            # Empty composite section: reset the page explicitly instead of
            # silently keeping the previous project's graph (cross-project
            # leakage — AUD-4400).
            self._reset_composite_workspace()
            return

        if project_dir:
            graph_data = self._absolutize_paths(graph_data, project_dir)

        try:
            restored_graph = NodeGraph.from_dict(graph_data)
        except (KeyError, TypeError, ValueError) as exc:
            logger.exception(
                "Composite graph restore failed (corrupt project data), "
                "falling back to an empty composite workspace: %s", exc)
            self._reset_composite_workspace()
            return

        page = self._runtime.page
        page._node_graph = restored_graph
        try:
            if hasattr(page, "set_asset_project_dir"):
                page.set_asset_project_dir(project_dir)
            if hasattr(page, "set_asset_search_roots"):
                restored_state = getattr(restored_graph, "_viewer_state", {}) or {}
                restored_roots = restored_state.get("asset_search_roots", ())
                if isinstance(restored_roots, (list, tuple)):
                    page.set_asset_search_roots(
                        tuple(str(root) for root in restored_roots), persist=False)
            page._node_editor.build_from_graph(restored_graph)
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            logger.exception(
                "Composite page rebuild after restore failed, falling back "
                "to an empty composite workspace: %s", exc)
            self._reset_composite_workspace()
            return

        if hasattr(page, '_undo_stack'):
            page._undo_stack.clear()

    def _reset_composite_workspace(self) -> None:
        """Reset the Composite page to an unbound empty workspace (AUD-4400).

        Uses the page's own reset helper when available (same convention as
        page_controller / file_controller); otherwise swaps in a fresh empty
        graph so no stale graph from a previous project survives.
        """
        page = self._runtime.page
        if page is None:
            return
        reset = getattr(page, "reset_to_empty_workspace", None)
        if callable(reset):
            try:
                reset()
                return
            except Exception:
                logger.exception("Composite empty-workspace reset failed")
        try:
            from src.features.composite.core.node_engine import NodeGraph
            fresh_graph = NodeGraph()
            page._node_graph = fresh_graph
            editor = getattr(page, "_node_editor", None)
            if editor is not None:
                editor.build_from_graph(fresh_graph)
        except Exception:
            logger.exception("Composite emergency graph reset failed")

    def restore_viewer_state(self, state_data: dict) -> None:
        """Restore composite viewer state from project data."""
        if self._runtime.is_available() and self._runtime.viewer and state_data:
            self._runtime.viewer.set_state(state_data)

    @staticmethod
    def _absolutize_paths(graph_data: dict, project_dir: str) -> dict:
        """Convert relative image paths in ImageInputNodes back to absolute paths."""
        import os
        if not graph_data or not project_dir:
            return graph_data
        for node_data in graph_data.get("nodes", []):
            if node_data.get("type") == "image_input":
                params = node_data.get("parameters", {})
                fp = params.get("file_path", "")
                if fp and not os.path.isabs(fp):
                    abs_path = os.path.normpath(os.path.join(project_dir, fp))
                    if os.path.exists(abs_path):
                        params["file_path"] = abs_path
        return graph_data

    def composite_startup_fallback(self) -> None:
        """Fallback: swap to CompositeViewer if no color output arrived within timeout."""
        if not getattr(self._mw, '_composite_startup_pending', False):
            return
        self._mw._composite_startup_pending = False
        current_page = getattr(self._mw, 'current_page', '')
        if current_page == "Composite" and self._runtime.is_available():
            if self._runtime.viewer and hasattr(self._mw, 'dock_viewport') and self._mw.dock_viewport:
                self._mw.dock_viewport.setWidget(self._runtime.viewer)
            self._runtime.page._is_active = True
            self._runtime.page.initialize_renderer()
            self._runtime.viewer.update()

            # First-launch-on-Composite: enter_page_hook was skipped during
            # startup (same-page early return in switch_to_page). Run it now
            # so the Properties panel is wired and the graph is bound.
            # Skip when the timeline clip context was already established by the
            # video startup path (_establish_composite_timeline_context) — re-
            # entering would re-run the bind logic against an already-bound graph.
            ctx_already_established = getattr(
                self._mw, '_composite_timeline_context_established', False)
            if (getattr(self._mw, '_composite_enter_needed_on_startup', False)
                    and not ctx_already_established):
                self._mw._composite_enter_needed_on_startup = False
                try:
                    self._mw.enter_page_hook("Composite")
                except Exception as e:
                    logger.warning(f"Composite adapter fallback enter_page_hook failed: {e}")

            # When the app starts directly on the Composite page, the Color
            # engine has loaded the image but render_pipeline() only marks
            # ColorOutput dirty — it never emits color_output_changed on its
            # own. That signal is what normally drives the first Composite
            # evaluation, so without it the viewer stays black until the user
            # manually switches to the Color page and back. Mirror what
            # page_controller does on a real page enter: if this graph needs
            # the Color output and it is stale, kick off a ColorOutput render
            # now. The resulting color_output_changed will feed the Composite
            # page the texture it needs.
            page = self._runtime.page
            if page.requires_color_output_input():
                engine = getattr(self._mw, 'engine', None)
                if engine is not None:
                    co = engine.color_output
                    if not co.is_valid or co.is_dirty:
                        try:
                            engine.request_color_output()
                        except Exception:
                            logger.exception("[COMPOSITE-STARTUP] request_color_output failed in fallback")
