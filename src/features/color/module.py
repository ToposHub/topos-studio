"""
Color Feature Module - Phase 6: Modularization.

Color is the core feature of Topos and is ALWAYS enabled.
This module provides the Color feature integration with the
FeatureRegistry system for consistent module management.

Note: Color core files (ImageEngine, ShaderPipeline, AsyncRenderer)
remain in their original locations. This module only provides
facade and registration logic, not the core implementation.
"""

import logging
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from src.shared.actions import ActionRegistry
    from src.shared.panels.registry import PanelRegistry
    from src.shared.ui.shortcut_manager import ShortcutManager

from src.app.page_contract import PageDescriptor
from src.app.app_context import AppContext

logger = logging.getLogger(__name__)


class ColorModule:
    """
    Color feature module implementation.
    
    Color is the primary feature of Topos Color and is always enabled.
    This module:
    - Provides ColorFacade for other modules to access color output
    - Registers Color page with FeatureRegistry
    - Does NOT modify core rendering pipeline
    
    Color remains in original locations (migrated to src/features/color/):
    - src/features/color/core/image_engine.py (was src/core/image_engine.py)
    - src/features/color/core/shader_compiler.py (was src/core/shader_compiler.py)
    - src/features/color/core/async_renderer.py (was src/core/async_renderer.py)
    - src/app/ui/canvas.py (was src/ui/canvas.py)
    - src/app/ui/adjustment_panel.py (was src/ui/adjustment_panel.py)
    """
    
    feature_id = "color"
    
    def __init__(self):
        self._available: bool = True  # Color is always available
    
    def is_available(self) -> bool:
        """
        Check if Color module is available.
        
        Color is ALWAYS enabled and available as the core feature.
        
        Returns:
            Always True
        """
        return True
    
    def register_pages(self, context: AppContext) -> List[PageDescriptor]:
        """
        Register Color page.
        
        Note: Color page creation remains in MainWindow for stability.
        This just provides registry integration for consistency.
        
        Args:
            context: AppContext instance
        
        Returns:
            Empty list (Color page created by MainWindow directly)
        """
        # Color page is created by MainWindow directly for stability
        # Just return empty list to mark Color as registered
        return []
    
    def register_actions(self, context: AppContext) -> None:
        """
        Register Color-related actions.
        
        Color actions are registered in MainWindow directly.
        This method is placeholder for future modularization.
        
        Args:
            context: AppContext instance
        """
        pass
    
    def register_save_handlers(self, context: AppContext) -> None:
        """
        Register Color save handler.
        
        Note: Color save is handled by SaveRouter._save_from_color
        for backward compatibility. Handler may be added in future.
        
        Args:
            context: AppContext instance with save_router
        """
        # Color save is handled by default SaveRouter methods
        # Handler registration is optional for future use
        pass
    
    def on_page_enter(self, page_id: str, context: AppContext) -> None:
        """Called when entering Color page."""
        if page_id != "color":
            return
        # Color page enter logic handled by MainWindow
    
    def on_page_leave(self, page_id: str, context: AppContext) -> None:
        """Called when leaving Color page."""
        if page_id != "color":
            return
        # Color page leave logic handled by MainWindow
    
    def register_i18n(self) -> None:
        """Register color module translations."""
        import os
        from src.shared.i18n.i18n import I18N
        locales_dir = os.path.join(os.path.dirname(__file__), "locales")
        I18N.load_module_translations(locales_dir)

    def register_menu_actions(self, action_registry: "ActionRegistry") -> None:
        """Register Color menu actions with the ActionRegistry."""
        from src.shared.actions import MenuActionDescriptor
        from src.shared.i18n.i18n import I18N

        action_registry.register(MenuActionDescriptor(
            action_id="color.reset_grade",
            menu_path="Color",
            label=I18N.tr("action.reset_grade", "Reset Grade"),
            callback_name="on_adjustments_reset",
            order=100,
            feature_id="color",
            shortcut_id="color.reset_grade",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="color.capture_still",
            menu_path="Color",
            label=I18N.tr("action.capture_still", "Capture Still"),
            callback_name="on_capture_still",
            order=200,
            feature_id="color",
            shortcut_id="color.capture_still",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="color.import_lut",
            menu_path="Color",
            label=I18N.tr("action.import_lut", "Import LUT..."),
            callback_name="on_import_lut",
            order=300,
            feature_id="color",
            shortcut_id="color.import_lut",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="color.export_lut",
            menu_path="Color",
            label=I18N.tr("action.export_lut", "Export LUT..."),
            callback_name="on_export_lut",
            order=400,
            feature_id="color",
            shortcut_id="color.export_lut",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="color.toggle_scopes",
            menu_path="Color",
            label=I18N.tr("action.toggle_scopes", "Toggle Scopes"),
            callback_name="on_toggle_scopes",
            order=500,
            feature_id="color",
            shortcut_id="color.toggle_scopes",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="color.toggle_comparison",
            menu_path="Color",
            label=I18N.tr("action.toggle_comparison", "Toggle Comparison"),
            callback_name="on_toggle_comparison",
            order=600,
            feature_id="color",
            shortcut_id="color.toggle_comparison",
        ))

    def register_preference_pages(self):
        """Register Color preference pages.

        Returns:
            List of ModularPreferencePage instances for this module.
        """
        from src.features.color.preferences import ColorProcessingPreferencePage
        return [ColorProcessingPreferencePage()]

    def register_shortcuts(self, shortcut_manager: "ShortcutManager") -> None:
        """Register Color module shortcuts with the ShortcutManager.

        These shortcuts are feature-specific and carry feature_id="color"
        so they can be filtered when the Color module is disabled.

        Note: Color is always enabled, so the feature_id is mainly for
        organizational grouping in the Keyboard Shortcuts dialog.

        Args:
            shortcut_manager: ShortcutManager instance
        """
        from src.shared.i18n.i18n import I18N

        # AUD-24601: Ctrl+Shift+S/D 原默认与 file.save_as / 画布 Capture
        # Reference Wipe 的 QAction 同键序，Qt 歧义快捷键会导致按
        # Ctrl+Shift+S 触发"另存工程"或无响应而非 Capture Still。改用
        # 独占键序（canvas.py 的对应 QAction 同步修改）。
        color_defaults = {
            "color.capture_still": "Ctrl+Alt+S",
            "color.reset_grade": "",
            "color.import_lut": "",
            "color.export_lut": "",
            "color.toggle_scopes": "Ctrl+Shift+W",
            "color.toggle_comparison": "Ctrl+Alt+D",
        }
        labels = {
            "color.capture_still": I18N.tr("action.capture_still", "Capture Still"),
            "color.reset_grade": I18N.tr("action.reset_grade", "Reset Grade"),
            "color.import_lut": I18N.tr("action.import_lut", "Import LUT..."),
            "color.export_lut": I18N.tr("action.export_lut", "Export LUT..."),
            "color.toggle_scopes": I18N.tr("action.toggle_scopes", "Toggle Scopes"),
            "color.toggle_comparison": I18N.tr("action.toggle_comparison", "Toggle Comparison"),
        }
        for action_id, default_sequence in color_defaults.items():
            # 注册期重复键序检测：ShortcutManager 目录内同键序会形成
            # Qt 歧义快捷键（先注册的 file.* 已在目录中）。检测为尽力而为
            # 的诊断——测试替身可能只实现 register，缺失能力时跳过。
            find_conflicts = getattr(shortcut_manager, "find_conflicts", None)
            if default_sequence and callable(find_conflicts):
                conflicts = find_conflicts(default_sequence)
                if conflicts:
                    logger.warning(
                        "Default shortcut %s for %s conflicts with %s",
                        default_sequence, action_id,
                        ", ".join(sa.id for sa in conflicts))
            shortcut_manager.register(
                action_id,
                labels[action_id],
                "Color",
                default_sequence,
                feature_id="color",
            )

    def register_panels(self, panel_registry: "PanelRegistry") -> None:
        """Register Color panel descriptors with the PanelRegistry.

        Args:
            panel_registry: PanelRegistry instance
        """
        from src.shared.panels import PanelDescriptor

        # Workspace panels (cross-product)
        panel_registry.register(PanelDescriptor(
            panel_id="workspace.viewport",
            title_key="panel.viewport",
            fallback_title="Viewport",
            feature_id="color",
            category="Workspace",
            dock_attr="dock_viewport",
            order=100,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="workspace.library",
            title_key="panel.library",
            fallback_title="Library",
            feature_id="color",
            category="Workspace",
            dock_attr="dock_library",
            order=200,
            default_visible=True,
            work_mode="image",
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="workspace.files",
            title_key="panel.files",
            fallback_title="Files",
            feature_id="color",
            category="Workspace",
            dock_attr="dock_files",
            order=300,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="workspace.presets",
            title_key="panel.gallery",
            fallback_title="Gallery",
            feature_id="color",
            category="Workspace",
            dock_attr="dock_presets",
            order=350,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="workspace.info",
            title_key="panel.info",
            fallback_title="Info",
            feature_id="color",
            category="Workspace",
            dock_attr="dock_info",
            order=400,
            default_visible=False,
            # Info 显示的是单张静态图的 EXIF/相机元数据，视频流程无用。
            work_mode="image",
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="workspace.layers",
            title_key="color_nodes.nodes",
            fallback_title="Nodes",
            feature_id="color",
            category="Workspace",
            dock_attr="dock_layers",
            order=500,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="workspace.export",
            title_key="panel.image_export",
            fallback_title="Image Export",
            feature_id="color",
            category="Workspace",
            dock_attr="dock_export",
            order=600,
            default_visible=False,
            work_mode="image",
        ))

        # Color grading panels
        panel_registry.register(PanelDescriptor(
            panel_id="color.grading",
            title_key="panel.grading",
            fallback_title="Color Wheel",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_grading",
            order=100,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.curves",
            title_key="panel.curves",
            fallback_title="Curves",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_curves",
            order=200,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.mapping",
            title_key="panel.color_mapping",
            fallback_title="Color Mapping",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_color_mapping",
            order=300,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.effects",
            title_key="panel.effects",
            fallback_title="Effects",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_effects",
            order=600,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.grain",
            title_key="panel.grain",
            fallback_title="Film Grain",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_grain",
            order=700,
            default_visible=False,
        ))

        # Mask & qualifier panels
        panel_registry.register(PanelDescriptor(
            panel_id="color.masks",
            title_key="panel.masks",
            fallback_title="Masks",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_masks",
            order=800,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.qualifier",
            title_key="panel.qualifier",
            fallback_title="Qualifier",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_qualifier",
            order=810,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.tracker",
            title_key="panel.tracker",
            fallback_title="Tracker",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_tracker",
            order=820,
            default_visible=False,
        ))

        # Output / analysis panels
        panel_registry.register(PanelDescriptor(
            panel_id="color.lut",
            title_key="panel.lut",
            fallback_title="3D LUT",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_lut",
            order=900,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.raw",
            title_key="panel.raw",
            fallback_title="Camera Raw",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_raw",
            order=920,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.analysis",
            title_key="panel.analysis",
            fallback_title="Scopes",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_analysis",
            order=930,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.keyframes",
            title_key="color_keyframes.title",
            fallback_title="Keyframes",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_color_keyframes",
            order=935,
            default_visible=False,
            work_mode="video",
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.plugin",
            title_key="panel.plugin",
            fallback_title="Plugins",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_plugin",
            order=940,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.crop",
            title_key="panel.crop",
            fallback_title="Crop & Transform",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_crop",
            order=950,
            default_visible=False,
            # 裁切/变换针对单张图片设计，剪辑（视频）模式不提供。
            work_mode="image",
        ))

        # AI tools (denoise/deblur — bundled with color but conditional)
        panel_registry.register(PanelDescriptor(
            panel_id="color.denoise",
            title_key="panel.denoise",
            fallback_title="Denoise",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_denoise",
            order=1000,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="color.deblur",
            title_key="panel.deblur",
            fallback_title="Deblur",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_deblur",
            order=1010,
            default_visible=False,
            # 去模糊针对单张图片设计，剪辑（视频）模式不提供。
            work_mode="image",
        ))

        # Advanced panels
        panel_registry.register(PanelDescriptor(
            panel_id="advanced.script",
            title_key="panel.script",
            fallback_title="Script",
            feature_id="color",
            category="Advanced",
            dock_attr="dock_script",
            order=100,
            default_visible=False,
        ))

        # Timeline clip strip (P9.1: Color page)
        panel_registry.register(PanelDescriptor(
            panel_id="color.timeline_clips",
            title_key="panel.timeline_clips",
            fallback_title="Timeline Clips",
            feature_id="color",
            category="Color",
            page_ids=("Color",),
            dock_attr="dock_color_timeline_clips",
            order=150,
            default_visible=True,
            work_mode="video",
        ))
