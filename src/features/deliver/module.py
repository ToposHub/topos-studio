"""
Deliver Feature Module - Phase 9: Modularization.

This module provides the Deliver feature integration with the
FeatureRegistry system, allowing Deliver to be disabled via feature flags.

Deliver manages:
- Export configuration UI
- Timeline/Color/Composite export
- Export progress tracking

Phase 9 Goal: Make Deliver page disableable without breaking other features.
"""

import logging
from typing import TYPE_CHECKING, List, Any, Optional

if TYPE_CHECKING:
    from src.shared.actions import ActionRegistry
    from src.shared.panels.registry import PanelRegistry
    from src.shared.ui.shortcut_manager import ShortcutManager

from src.app.page_contract import PageDescriptor
from src.app.app_context import AppContext

logger = logging.getLogger(__name__)


class DeliverModule:
    """
    Deliver feature module implementation.
    
    Deliver provides export capabilities and can be disabled when
    not needed (e.g., for pure editing/color grading workflows).
    
    This module:
    - Registers Deliver page with FeatureRegistry
    - Provides export source registration
    - Does NOT modify core export logic
    
    Deliver remains in original locations (migrated):
    - src/app/ui/deliver_page.py (UI, was src/ui/deliver_page.py)
    - src/features/edit/core/timeline_export.py (export logic, was src/core/edit/timeline_export.py)
    """
    
    feature_id = "deliver"
    
    def __init__(self):
        self._page: Optional[Any] = None
        self._available: bool = False

        try:
            # Import the new video batch workspace (which itself imports the
            # old DeliverPage bits it still needs). If the edit feature is
            # unavailable the workspace still boots in a degraded mode.
            from src.features.deliver.ui.deliver_workspace import DeliverWorkspace  # noqa: F401
            self._available = True
        except (ImportError, AttributeError) as e:
            logger.warning(f"Deliver module unavailable: {e}")
            self._available = False
    
    def is_available(self) -> bool:
        """
        Check if Deliver module is available.
        
        Returns False if:
        - Feature flag is disabled
        - Module cannot be imported
        """
        return self._available
    
    def register_pages(self, context: AppContext) -> List[PageDescriptor]:
        """
        Register Deliver page.
        
        Args:
            context: AppContext instance
        
        Returns:
            List containing Deliver page descriptor
        """
        if not self.is_available():
            return []
        
        pages = []

        def create_deliver_page(ctx: Any) -> Any:
            # Prefer the new video-batch workspace; fall back to the legacy
            # single-form DeliverPage if the new workspace can't be built
            # (e.g. edit feature disabled at runtime).
            try:
                from src.features.deliver.ui.deliver_workspace import DeliverWorkspace
                self._page = DeliverWorkspace()
            except Exception as e:  # noqa: BLE001 — keep the page booting
                logger.exception("DeliverWorkspace unavailable, falling back to DeliverPage: %s", e)
                from src.features.deliver.ui.deliver_page import DeliverPage
                self._page = DeliverPage()
            return self._page
        
        pages.append(PageDescriptor(
            page_id="deliver",
            title=context.i18n.tr("page.deliver", "Deliver") if context.i18n else "Deliver",
            factory=create_deliver_page,
            default_enabled=True,
            shortcut="Ctrl+5",
        ))
        
        return pages
    
    def register_actions(self, context: AppContext) -> None:
        """
        Register Deliver-related actions.
        
        Args:
            context: AppContext instance
        """
        pass
    
    def register_save_handlers(self, context: AppContext) -> None:
        """
        Register Deliver save handler.
        
        Note: Deliver save is handled by SaveRouter._save_from_deliver.
        
        Args:
            context: AppContext instance with save_router
        """
        pass
    
    def on_page_enter(self, page_id: str, context: AppContext) -> None:
        """Called when entering Deliver page."""
        if page_id != "deliver":
            return
    
    def on_page_leave(self, page_id: str, context: AppContext) -> None:
        """Called when leaving Deliver page."""
        if page_id != "deliver":
            return
    
    def register_i18n(self) -> None:
        """Register deliver module translations."""
        import os
        from src.shared.i18n.i18n import I18N
        locales_dir = os.path.join(os.path.dirname(__file__), "locales")
        I18N.load_module_translations(locales_dir)

    def register_menu_actions(self, action_registry: "ActionRegistry") -> None:
        """Register Deliver menu actions with the ActionRegistry."""
        from src.shared.actions import MenuActionDescriptor
        from src.shared.i18n.i18n import I18N

        if not self._available:
            return

        action_registry.register(MenuActionDescriptor(
            action_id="deliver.export",
            menu_path="File",
            label=I18N.tr("action.export", "&Export..."),
            callback_name="export_file_as",
            order=200,
            feature_id="deliver",
            shortcut_id="deliver.export",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="deliver.export.current_source",
            menu_path="Deliver",
            label=I18N.tr("action.export_current_source", "Export Current Source"),
            callback_name="on_export_current_source",
            order=100,
            feature_id="deliver",
            shortcut_id="deliver.export.current_source",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="deliver.export.settings",
            menu_path="Deliver",
            label=I18N.tr("action.export_settings", "Export Settings..."),
            callback_name="on_export_settings",
            order=200,
            feature_id="deliver",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="deliver.export.timeline",
            menu_path="Deliver",
            label=I18N.tr("action.export_timeline", "Export Timeline"),
            callback_name="on_export_timeline",
            order=300,
            feature_id="deliver",
            shortcut_id="deliver.export.timeline",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="deliver.export.composite",
            menu_path="Deliver",
            label=I18N.tr("action.export_composite", "Export Composite Graph"),
            callback_name="on_export_composite_graph",
            order=400,
            feature_id="deliver",
            shortcut_id="deliver.export.composite",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="deliver.export.srt",
            menu_path="Deliver",
            label=I18N.tr("action.export_srt", "Export Subtitles (SRT)..."),
            callback_name="export_srt",
            order=450,
            feature_id="deliver",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="deliver.queue.start",
            menu_path="Deliver",
            label=I18N.tr("action.queue_start", "Start Queue"),
            callback_name="on_queue_start",
            order=500,
            feature_id="deliver",
            shortcut_id="deliver.queue.start",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="deliver.queue.stop",
            menu_path="Deliver",
            label=I18N.tr("action.queue_stop", "Stop Queue"),
            callback_name="on_queue_stop",
            order=600,
            feature_id="deliver",
            shortcut_id="deliver.queue.stop",
        ))

    def register_preference_pages(self):
        """Register Deliver preference pages.

        Returns:
            List of ModularPreferencePage instances for this module.
        """
        if not self._available:
            return []

        from src.features.deliver.preferences import ExportPreferencePage
        return [ExportPreferencePage()]

    def register_shortcuts(self, shortcut_manager: "ShortcutManager") -> None:
        """Register Deliver module shortcuts with the ShortcutManager.

        These shortcuts are feature-specific and carry feature_id="deliver"
        so they can be filtered when the Deliver module is disabled.

        Args:
            shortcut_manager: ShortcutManager instance
        """
        if not self._available:
            return

        from src.shared.i18n.i18n import I18N

        # Export shortcut
        shortcut_manager.register(
            "deliver.export",
            I18N.tr("action.export", "Export..."),
            "File",
            "Ctrl+E",
            feature_id="deliver",
        )
        shortcut_manager.register(
            "deliver.export.current_source",
            I18N.tr("action.export_current_source", "Export Current Source"),
            "Deliver",
            "Ctrl+Shift+E",
            feature_id="deliver",
        )
        shortcut_manager.register(
            "deliver.export.timeline",
            I18N.tr("action.export_timeline", "Export Timeline"),
            "Deliver",
            "",
            feature_id="deliver",
        )
        shortcut_manager.register(
            "deliver.export.composite",
            I18N.tr("action.export_composite", "Export Composite Graph"),
            "Deliver",
            "",
            feature_id="deliver",
        )
        shortcut_manager.register(
            "deliver.queue.start",
            I18N.tr("action.queue_start", "Start Queue"),
            "Deliver",
            "",
            feature_id="deliver",
        )
        shortcut_manager.register(
            "deliver.queue.stop",
            I18N.tr("action.queue_stop", "Stop Queue"),
            "Deliver",
            "",
            feature_id="deliver",
        )

    def get_deliver_page(self) -> Optional[Any]:
        """Get the DeliverPage instance."""
        return self._page

    def register_panels(self, panel_registry: "PanelRegistry") -> None:
        """Register Deliver panel descriptors with the PanelRegistry.

        Args:
            panel_registry: PanelRegistry instance
        """
        if not self._available:
            return

        from src.shared.panels import PanelDescriptor

        panel_registry.register(PanelDescriptor(
            panel_id="deliver.export",
            title_key="panel.deliver_export",
            fallback_title="Export",
            feature_id="deliver",
            category="Deliver",
            page_ids=("Deliver",),
            dock_attr="dock_export",
            order=100,
            default_visible=True,
        ))

        # The video queue is embedded in the Deliver workspace by default.
        # Toggling the Deliver dock shows/hides the complete settings + queue
        # workspace; the queue itself can be floated from its More menu.
        panel_registry.register(PanelDescriptor(
            panel_id="deliver.batch_export",
            title_key="panel.deliver_video_batch",
            fallback_title="Video Deliver",
            feature_id="deliver",
            category="Deliver",
            page_ids=("Deliver",),
            dock_attr="dock_deliver",
            order=110,
            default_visible=True,
            work_mode="video",
        ))
