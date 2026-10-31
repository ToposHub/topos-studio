"""
Composite Feature Module - Phase 4: Registry Integration.

This module provides the Composite feature integration with the
FeatureRegistry system, allowing Composite to be registered and
managed as a modular feature.
"""

import logging
from typing import List, Any, Optional

from src.app.page_contract import PageDescriptor
from src.app.app_context import AppContext

logger = logging.getLogger(__name__)


class CompositeModule:
    """
    Composite feature module implementation.

    Manages the Composite page and its related UI components:
    - CompositePage (node graph editor)
    - CompositeViewer (viewport for composite output)
    - NodePropertiesPanel
    - PerfPanel (performance diagnostics)
    - Node plugin discovery and registration

    This module allows Composite to be disabled via feature flags
    and cleanly separated from MainWindow.
    """

    feature_id = "composite"

    def __init__(self) -> None:
        self._page: Optional[Any] = None
        self._viewer: Optional[Any] = None
        self._properties_panel: Optional[Any] = None
        self._perf_panel: Optional[Any] = None
        self._available: bool = False
        self._plugins_registered: bool = False

        try:
            from src.features.composite.ui.composite_page import CompositePage  # noqa: F401 — 页面可用性探测（置 _available）
            self._available = True
        except ImportError as e:
            logger.warning(f"Composite module unavailable: {e}")
            self._available = False

        # Register AI cache callback so inference engine can save
        # results without importing src/features directly.
        self._register_ai_cache_callback()

        # AUD-6311: wire the production generation transport (Volcengine
        # adapter via user-level AI profiles) so AI Gen Expand nodes leave
        # the permanent NEEDS_CONFIG passthrough once a key is configured.
        self._wire_ai_generation_transport()

    def is_available(self) -> bool:
        """Check if Composite module is available."""
        return self._available

    @staticmethod
    def _wire_ai_generation_transport() -> None:
        """Install the production AI generation transport (AUD-6311).

        Delegates to
        :func:`src.features.composite.nodes.ai_gen_expand_node.install_default_generation_transport`,
        which resolves an image-capable profile from the user-level AI
        config (``~/.topos/ai_profiles.json``) and adapts the shared
        Volcengine Seedream provider to the node transport contract.
        Without a profile the node keeps its visible NEEDS_CONFIG
        passthrough — wiring is best-effort and never fatal.
        """
        try:
            from src.features.composite.nodes.ai_gen_expand_node import (
                install_default_generation_transport,
            )

            if install_default_generation_transport():
                logger.debug("AI generation transport wired for composite nodes")
        except ImportError as e:
            logger.debug(f"AI generation transport skipped (module missing): {e}")
        except Exception as e:
            logger.warning(f"AI generation transport wiring failed: {e}")

    @staticmethod
    def _register_ai_cache_callback() -> None:
        """
        Register the AI cache save callback on the inference engine.

        This allows src/shared/ai_models to save inference results
        without importing src/features/composite directly.
        """
        try:
            from src.shared.ai_models.inference_engine import get_inference_engine
            from src.features.composite.render.ai_cache_manager import AICacheManager

            # AUD-6212: one long-lived manager for the whole process.
            # Constructing an AICacheManager per write gave every callback
            # its own lock, reloaded the manifest from disk on every write,
            # and raced read-modify-write manifest updates between two
            # concurrently finishing inferences (the later writer silently
            # dropped the earlier one's entries).  A single instance keeps
            # the lock and manifest state process-wide consistent.
            cache_manager = AICacheManager()

            def _save_to_cache(cache_key: str, result: Any) -> None:
                cache_manager.put(cache_key, result)

            engine = get_inference_engine()
            engine.register_cache_fn(_save_to_cache)
            logger.debug("AI cache callback registered on inference engine")
        except ImportError:
            logger.debug("AI cache callback registration skipped (module not available)")
        except Exception as e:
            logger.warning(f"AI cache callback registration failed: {e}")

    def discover_and_register_plugins(self) -> int:
        """
        Discover and register node plugins.

        This is called during module registration to set up
        the node plugin system. Moved from main.py (R-004).

        Returns:
            Number of plugins registered
        """
        if not self._available or self._plugins_registered:
            return 0

        try:
            from src.features.composite.core.node_engine import discover_and_register_plugins
            plugin_count = discover_and_register_plugins()
            self._plugins_registered = True
            if plugin_count > 0:
                logger.info(f"Composite: loaded {plugin_count} node plugins")
            return plugin_count
        except ImportError as e:
            logger.warning(f"Composite node engine unavailable: {e}")
            return 0
        except Exception as e:
            logger.warning(f"Composite plugin registration failed: {e}")
            return 0

    def register_pages(self, context: AppContext) -> List[PageDescriptor]:
        """Register Composite page."""
        if not self.is_available():
            return []

        pages = []

        def create_composite_page(ctx: Any) -> Any:
            # Reuse the CompositePage instance from runtime if available
            # to avoid creating duplicate instances.
            from src.features.composite.ui.composite_page import CompositePage
            runtime = self.get_runtime()
            if runtime and runtime.page:
                # Reuse the existing page from runtime
                self._page = runtime.page
            else:
                # Create new page (will be reused by runtime later)
                self._page = CompositePage(parent=ctx.get_main_window())
            return self._page

        pages.append(PageDescriptor(
            page_id="composite",
            title=context.i18n.tr("page.composite", "Composite") if context.i18n else "Composite",
            factory=create_composite_page,
            default_enabled=True,
            shortcut="Ctrl+4",
        ))

        return pages

    def register_actions(self, context: AppContext) -> None:
        """Register Composite-related actions (menus, toolbar)."""
        if not self.is_available():
            return

        main_window = context.get_main_window()

        if hasattr(main_window, '_register_composite_actions'):
            main_window._register_composite_actions()

    def register_save_handlers(self, context: AppContext) -> None:
        """Register Composite save handler with SaveRouter."""
        if not self.is_available():
            return

        save_router = context.save_router
        if save_router is None:
            return

        # Note: Composite save is handled by SaveRouter._save_from_composite
        # which uses callbacks to get graph data from MainWindow.
        # We don't register a custom handler here to avoid empty saves.
        # The fallback method properly syncs UI state and writes to .topos.
        logger.info("Composite save will use SaveRouter fallback (callback-based)")

    def on_page_enter(self, page_id: str, context: AppContext) -> None:
        """Called when entering Composite page."""
        if page_id != "composite":
            return

        if self._page:
            self._page.on_enter()

    def on_page_leave(self, page_id: str, context: AppContext) -> None:
        """Called when leaving Composite page."""
        if page_id != "composite":
            return

        if self._page:
            self._page.on_leave()

    def create_runtime(self, context: AppContext) -> Optional[Any]:
        """
        Create a CompositeRuntime holding all Composite UI objects.

        This replaces direct Composite UI imports in MainWindow.
        MainWindow should use the returned runtime via MainWindowAdapter.

        AUD-9305: reuse the page already built by ``register_pages()``
        (``self._page``) via ``existing_page``; creating a second
        CompositePage/CompositeViewer here would duplicate the feature's
        render surface. Callers must still ensure this method is invoked
        at most once per window (the live path is MainWindowAdapter).
        """
        if not self.is_available():
            return None

        from src.features.composite.runtime import CompositeRuntime
        runtime = CompositeRuntime()
        main_window = context.get_main_window()
        if runtime.create(main_window, existing_page=self._page):
            self._runtime = runtime
            return runtime

        return None

    def get_runtime(self) -> Optional[Any]:
        """Get the CompositeRuntime, creating it if needed."""
        if hasattr(self, '_runtime') and self._runtime and self._runtime.is_available():
            return self._runtime
        return None

    def register_i18n(self) -> None:
        """Register composite module translations."""
        import os
        from src.shared.i18n.i18n import I18N
        locales_dir = os.path.join(os.path.dirname(__file__), "locales")
        I18N.load_module_translations(locales_dir)

    def register_menu_actions(self, action_registry) -> None:
        """Register Composite menu actions with the ActionRegistry."""
        from src.shared.actions import MenuActionDescriptor
        from src.shared.i18n.i18n import I18N

        if not self._available:
            return

        action_registry.register(MenuActionDescriptor(
            action_id="composite.add_node",
            menu_path="Composite",
            label=I18N.tr("action.add_node", "Add Node"),
            callback_name="on_add_composite_node",
            order=100,
            feature_id="composite",
            shortcut_id="composite.add_node",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="composite.node.bypass",
            menu_path="Composite",
            label=I18N.tr("action.bypass_node", "Bypass Node"),
            callback_name="on_bypass_composite_node",
            order=200,
            feature_id="composite",
            shortcut_id="composite.node.bypass",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="composite.viewer.render",
            menu_path="Composite",
            label=I18N.tr("action.render_composite", "Render Composite"),
            callback_name="on_render_composite",
            order=300,
            feature_id="composite",
            shortcut_id="composite.render",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="composite.delete_node",
            menu_path="Composite",
            label=I18N.tr("action.delete_node", "Delete Node"),
            callback_name="on_delete_composite_node",
            order=400,
            feature_id="composite",
            shortcut_id="composite.delete_node",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="composite.duplicate_node",
            menu_path="Composite",
            label=I18N.tr("action.duplicate_node", "Duplicate Node"),
            callback_name="on_duplicate_composite_node",
            order=500,
            feature_id="composite",
            shortcut_id="composite.duplicate_node",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="composite.group_nodes",
            menu_path="Composite",
            label=I18N.tr("action.group_nodes", "Group Nodes"),
            callback_name="on_group_composite_nodes",
            order=600,
            feature_id="composite",
            shortcut_id="composite.group_nodes",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="composite.ungroup_nodes",
            menu_path="Composite",
            label=I18N.tr("action.ungroup_nodes", "Ungroup Nodes"),
            callback_name="on_ungroup_composite_nodes",
            order=700,
            feature_id="composite",
            shortcut_id="composite.ungroup_nodes",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="composite.select_all",
            menu_path="Composite",
            label=I18N.tr("action.select_all_nodes", "Select All Nodes"),
            callback_name="on_select_all_composite_nodes",
            order=800,
            feature_id="composite",
            shortcut_id="composite.select_all",
        ))

    def register_preference_pages(self):
        """Register Composite preference pages.

        Returns:
            List of ModularPreferencePage instances for this module.
            Currently returns a placeholder; Composite-specific settings
            will be added in a future phase.
        """
        if not self._available:
            return []

        from src.features.composite.preferences import CompositePreferencePage
        return [CompositePreferencePage()]

    def register_shortcuts(self, shortcut_manager) -> None:
        """Register Composite module shortcuts with the ShortcutManager.

        These shortcuts are feature-specific and carry feature_id="composite"
        so they can be filtered when the Composite module is disabled.

        Args:
            shortcut_manager: ShortcutManager instance
        """
        if not self._available:
            return

        from src.shared.i18n.i18n import I18N

        # Composite node graph shortcuts
        shortcut_manager.register(
            "composite.add_node",
            I18N.tr("action.add_node", "Add Node"),
            "Composite",
            "",
            feature_id="composite",
        )
        shortcut_manager.register(
            "composite.node.bypass",
            I18N.tr("action.bypass_node", "Bypass Node"),
            "Composite",
            "Ctrl+B",
            feature_id="composite",
        )
        shortcut_manager.register(
            "composite.render",
            I18N.tr("action.composite_render", "Render Composite"),
            "Composite",
            "Ctrl+R",
            feature_id="composite",
        )
        shortcut_manager.register(
            "composite.delete_node",
            I18N.tr("action.delete_node", "Delete Node"),
            "Composite",
            "",  # 不注册快捷键：Delete 交由节点编辑器内部 widget 级处理。
            # 原来注册 "Delete" 会建一个空壳 QAction（callback 不存在），
            # Qt 窗口级匹配会抢先消费 Delete，导致节点编辑器收不到按键。
            feature_id="composite",
        )
        shortcut_manager.register(
            "composite.duplicate_node",
            I18N.tr("action.duplicate_node", "Duplicate Node"),
            "Composite",
            "Ctrl+D",
            feature_id="composite",
        )
        shortcut_manager.register(
            "composite.group_nodes",
            I18N.tr("action.group_nodes", "Group Nodes"),
            "Composite",
            "Ctrl+G",
            feature_id="composite",
        )
        shortcut_manager.register(
            "composite.ungroup_nodes",
            I18N.tr("action.ungroup_nodes", "Ungroup Nodes"),
            "Composite",
            "Ctrl+Shift+G",
            feature_id="composite",
        )
        shortcut_manager.register(
            "composite.select_all",
            I18N.tr("action.select_all_nodes", "Select All Nodes"),
            "Composite",
            "Ctrl+A",
            feature_id="composite",
        )

    def get_composite_page(self) -> Optional[Any]:
        """Get the CompositePage instance."""
        if hasattr(self, '_runtime') and self._runtime:
            return self._runtime.get_page()
        return self._page

    def get_viewer(self) -> Optional[Any]:
        """Get the CompositeViewer instance."""
        if hasattr(self, '_runtime') and self._runtime:
            return self._runtime.get_viewer()
        return self._viewer

    def register_panels(self, panel_registry) -> None:
        """Register Composite panel descriptors with the PanelRegistry.

        Args:
            panel_registry: PanelRegistry instance
        """
        if not self._available:
            return

        from src.shared.panels import PanelDescriptor

        panel_registry.register(PanelDescriptor(
            panel_id="composite.node_graph",
            title_key="panel.node_graph",
            fallback_title="Node Graph",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_composite",
            order=100,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="composite.node_properties",
            title_key="panel.node_properties",
            fallback_title="Node Properties",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_node_properties",
            order=120,
            default_visible=True,
        ))
        # Composite uses the same professional ScopePanel as Color. Keep it
        # in the registry so Window > Panels can show/hide the dedicated dock
        # and so layout profiles can track it by a stable panel id.
        panel_registry.register(PanelDescriptor(
            panel_id="composite.scopes",
            title_key="panel.composite_scopes",
            fallback_title="Scopes",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_composite_scope",
            order=140,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="composite.performance",
            title_key="panel.performance",
            fallback_title="Performance",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_perf",
            order=200,
            default_visible=False,
        ))
        # P3: Fusion-style Keyframes panel (bottom dock). Registered now; the
        # dock widget itself is created by CompositeRuntime + MainWindowAdapter.
        panel_registry.register(PanelDescriptor(
            panel_id="composite.keyframes",
            title_key="panel.keyframes",
            fallback_title="Keyframes",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_composite_keyframes",
            order=150,
            default_visible=True,
        ))
        # Phase 3: Spline curve editor panel. Default hidden so it doesn't
        # crowd the layout until the user opens it.
        panel_registry.register(PanelDescriptor(
            panel_id="composite.spline",
            title_key="panel.spline",
            fallback_title="Spline",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_composite_spline",
            order=160,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="composite.import_report",
            title_key="panel.import_report",
            fallback_title="Import Report",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_composite_import_report",
            order=180,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="composite.asset_dependency",
            title_key="panel.asset_dependency",
            fallback_title="Asset Dependencies",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_composite_asset_dependency",
            order=190,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="composite.scene_outliner",
            title_key="panel.scene_outliner",
            fallback_title="Scene Outliner",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_composite_scene_outliner",
            order=175,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="composite.material",
            title_key="panel.material",
            fallback_title="Material",
            feature_id="composite",
            category="Composite",
            page_ids=("Composite",),
            dock_attr="dock_composite_material",
            order=185,
            default_visible=False,
        ))
