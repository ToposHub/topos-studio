"""
CompositeRuntime — holds all Composite UI objects created at runtime.

Decouples MainWindow from direct Composite UI class imports.
All creation, wiring, and lifecycle management of Composite widgets
is centralized here so MainWindow only interacts through this interface.
"""

import logging
from typing import Any, Optional, Dict

logger = logging.getLogger(__name__)


class CompositeRuntime:
    """
    Holds all Composite UI objects created at runtime.

    AUD-9305: the live creation path is ``CompositeMainWindowAdapter.__init__``
    (src/features/composite/adapters/main_window_adapter.py), which instantiates
    this class directly and consumes it through MainWindow's dock layout.
    ``CompositeModule.create_runtime()`` is an unused alternative path — if it
    is ever wired up, it must reuse the page already registered by
    ``register_pages()`` (it now passes it as ``existing_page``) or it would
    create a duplicate CompositePage/CompositeViewer.
    """

    def __init__(self) -> None:
        self.page: Any = None
        self.viewer: Any = None
        self.scope_panel: Any = None  # P1-11: Composite QC scopes
        self.properties_panel: Any = None
        self.perf_panel: Any = None
        self.keyframes_panel: Any = None  # P3: Fusion-style Keyframes dock
        self.spline_panel: Any = None     # Phase 3: Spline curve editor
        self.import_report_panel: Any = None  # F0: Model3D import report
        self.asset_dependency_panel: Any = None  # P1-14: asset dependencies
        self.scene_outliner_panel: Any = None  # P1-18: Model3D scene hierarchy
        self.material_panel: Any = None  # P1-23: per-material editor
        self.dock_composite: Any = None
        self.dock_node_properties: Any = None  # Independent Node Properties dock
        self.dock_scope: Any = None  # P1-11: Composite QC scopes dock
        self.dock_perf: Any = None
        self.dock_keyframes: Any = None   # P3: bottom Keyframes dock
        self.dock_spline: Any = None      # Phase 3: Spline dock
        self.dock_import_report: Any = None  # F0: import report dock
        self.dock_asset_dependency: Any = None  # P1-14: asset dependencies dock
        self.dock_scene_outliner: Any = None  # P1-18: scene outliner dock
        self.dock_material: Any = None  # P1-23: material editor dock
        self._node_editor: Any = None
        self._available: bool = False

    def create(self, parent: Any, existing_page: Any = None) -> bool:
        """
        Create all Composite UI components.

        Args:
            parent: Parent widget.
            existing_page: Optional existing CompositePage instance to reuse,
                          typically from module.register_pages().

        Returns True if creation succeeded, False if the feature is unavailable.
        """
        try:
            from src.features.composite.ui.composite_page import CompositePage
            from src.features.composite.ui.composite_viewport import CompositeViewer
            from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
            from src.features.composite.ui.composite_scope_widgets import (
                CompositeScopePanel,
                configure_composite_scope_panel,
            )
        except ImportError:
            logger.warning("Composite UI imports unavailable; feature disabled")
            self._available = False
            return False

        # Reuse existing page if provided (from module.register_pages)
        # to avoid creating duplicate instances.
        if existing_page is not None:
            self.page = existing_page
        else:
            self.page = CompositePage(parent=parent)
        self.viewer = CompositeViewer(parent=parent)
        self.viewer.hide()
        self.scope_panel = configure_composite_scope_panel(
            CompositeScopePanel(parent)
        )
        self.properties_panel = NodePropertiesPanel(parent)
        self.page.set_viewer(self.viewer)
        self.page.properties_panel = self.properties_panel
        self._available = True

        try:
            from src.features.composite.ui.perf_panel import PerfPanel
            self.perf_panel = PerfPanel(parent)
            self.page._perf_panel = self.perf_panel
        except ImportError:
            logger.debug("PerfPanel unavailable, skipping")

        # P3: Fusion-style Keyframes panel. Created lazily and tolerant of
        # import failure (older builds without the new UI module).
        try:
            from src.features.composite.ui.keyframes_panel import KeyframesPanel
            self.keyframes_panel = KeyframesPanel(parent)
            # P5: wire edit signals back into the page (graph + undo + render).
            if hasattr(self.page, 'set_keyframes_panel'):
                self.page.set_keyframes_panel(self.keyframes_panel)
            else:
                self.page._keyframes_panel = self.keyframes_panel
        except ImportError:
            logger.debug("KeyframesPanel unavailable, skipping")
        except Exception as exc:  # pragma: no cover - defensive
            # AUD-9300：构建失败是真实缺陷（TypeError/状态初始化失败等），
            # debug 级别在默认 INFO 下完全不可见——至少 warning + 堆栈。
            logger.warning("KeyframesPanel creation failed: %s", exc,
                           exc_info=True)

        # Phase 3: Spline curve editor panel (lazy, fault-tolerant).
        try:
            from src.features.composite.ui.spline_panel import SplinePanel
            self.spline_panel = SplinePanel(parent)
            if hasattr(self.page, 'set_spline_panel'):
                self.page.set_spline_panel(self.spline_panel)
            else:
                self.page._spline_panel = self.spline_panel
        except ImportError:
            logger.debug("SplinePanel unavailable, skipping")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("SplinePanel creation failed: %s", exc,
                           exc_info=True)

        # F0: structured Model3D import diagnostics. It receives only the
        # render-thread-safe summary carried by Scene3D metadata.
        try:
            from src.features.composite.ui.import_report_panel import ImportReportPanel
            self.import_report_panel = ImportReportPanel(parent)
            if hasattr(self.page, 'set_import_report_panel'):
                self.page.set_import_report_panel(self.import_report_panel)
            else:
                self.page._import_report_panel = self.import_report_panel
        except ImportError:
            logger.debug("ImportReportPanel unavailable, skipping")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("ImportReportPanel creation failed: %s", exc,
                           exc_info=True)

        # P1-14: serialized asset dependency report and safe operation entry
        # points.  The panel only emits requests; CompositePage owns the graph,
        # service, undo stack, and project-path context.
        try:
            from src.features.composite.ui.asset_dependency_panel import (
                AssetDependencyPanel,
            )
            self.asset_dependency_panel = AssetDependencyPanel(parent)
            if hasattr(self.page, 'set_asset_dependency_panel'):
                self.page.set_asset_dependency_panel(self.asset_dependency_panel)
            else:
                self.page._asset_dependency_panel = self.asset_dependency_panel
        except ImportError:
            logger.debug("AssetDependencyPanel unavailable, skipping")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("AssetDependencyPanel creation failed: %s", exc,
                           exc_info=True)

        # P1-18: Model3D hierarchy/visibility outliner.  The panel receives
        # only metadata summaries from CompositePage; it never touches GL.
        try:
            from src.features.composite.ui.scene_outliner_panel import SceneOutlinerPanel
            self.scene_outliner_panel = SceneOutlinerPanel(parent)
            if hasattr(self.page, 'set_scene_outliner_panel'):
                self.page.set_scene_outliner_panel(self.scene_outliner_panel)
            else:
                self.page._scene_outliner_panel = self.scene_outliner_panel
        except ImportError:
            logger.debug("SceneOutlinerPanel unavailable, skipping")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("SceneOutlinerPanel creation failed: %s", exc,
                           exc_info=True)

        # P1-23: per-material PBR/texture-slot editor.  Like the other 3D
        # panels it receives render-safe summaries and emits node parameters;
        # it never owns or touches GL resources.
        try:
            from src.features.composite.ui.material_panel import MaterialPanel
            self.material_panel = MaterialPanel(parent)
            if hasattr(self.page, 'set_material_panel'):
                self.page.set_material_panel(self.material_panel)
            else:
                self.page._material_panel = self.material_panel
        except ImportError:
            logger.debug("MaterialPanel unavailable, skipping")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("MaterialPanel creation failed: %s", exc,
                           exc_info=True)

        try:
            self._node_editor = self.page.node_editor
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("CompositePage.node_editor unavailable: %s", exc,
                           exc_info=True)
            self._node_editor = None

        return True

    def is_available(self) -> bool:
        return self._available

    def get_page(self) -> Any:
        return self.page

    def get_viewer(self) -> Any:
        return self.viewer

    def get_graph_data(self) -> Optional[Dict]:
        """Get serialized composite graph data."""
        if not self.page:
            return None
        # U-11：优先走页面规范序列化入口（含 output target/fps/duration），
        # graph-only to_dict 会让保存丢失查看器输出目标等页面态。
        get_data = getattr(self.page, 'get_composite_data', None)
        if callable(get_data):
            return get_data()
        if hasattr(self.page, 'node_editor') and self.page.node_editor:
            self.page.node_editor.save_ui_state()
        return self.page.node_graph.to_dict()

    def get_viewer_state(self) -> Optional[Any]:
        """Get serialized composite viewer state."""
        if not self.viewer:
            return None
        return self.viewer.get_state()

    def on_color_output(self, texture_id: int, width: int, height: int, revision: int) -> None:
        """Forward color output texture to composite page."""
        if not self.page:
            return
        self.page.update_color_output(texture_id, width, height, revision)

    def on_enter(self, color_texture_id=None, color_width=0, color_height=0, color_revision=0) -> None:
        """Activate composite page."""
        if not self.page:
            return
        self.page.on_enter(color_texture_id, color_width, color_height, color_revision)

    def on_leave(self) -> None:
        """Deactivate composite page."""
        if not self.page:
            return
        self.page.on_leave()

    def load_composite_data(self, data: dict) -> None:
        """Load composite graph data into the page."""
        if not self.page:
            return
        self.page.load_composite_data(data)

    def _owned_panels(self) -> list:
        """Return the panels created by this runtime (page/viewer excluded)."""
        return [panel for panel in (
            self.scope_panel,
            self.properties_panel,
            self.perf_panel,
            self.keyframes_panel,
            self.spline_panel,
            self.import_report_panel,
            self.asset_dependency_panel,
            self.scene_outliner_panel,
            self.material_panel,
        ) if panel is not None]

    def cleanup(self) -> None:
        """Clean up composite resources.

        AUD-9301：除委托 page.cleanup() 外，运行时自建的面板也要停用——
        否则拆卸后 SplinePanel 的播放 QTimer 仍会以设定帧率触发
        seek_to_frame，向渲染器已停止的页面持续发信号。面板若提供
        dispose()（KeyframesPanel）优先调用；随后停掉各面板持有的所有
        QTimer（停止定时器不破坏面板状态，窗口销毁前不会再触发）。
        """
        if self.page:
            self.page.cleanup()
        from PyQt6.QtCore import QTimer
        for panel in self._owned_panels():
            dispose = getattr(panel, "dispose", None)
            if callable(dispose):
                try:
                    dispose()
                except Exception:
                    logger.warning("面板 dispose 失败: %r", panel, exc_info=True)
            try:
                for timer in panel.findChildren(QTimer):
                    timer.stop()
            except Exception:
                logger.warning("停止面板定时器失败: %r", panel, exc_info=True)
