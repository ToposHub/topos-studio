"""
Edit Feature Module - Phase 8: Modularization.

This module provides the Edit feature integration with the
FeatureRegistry system, allowing Edit to be disabled via feature flags.

Edit manages:
- Media Pool (project media management)
- Timeline (editing workspace)
- Viewport (single/dual Source and Timeline previews)
- Properties (selection properties)
- Effects (effects library)

Phase 8 Goal: Make Edit page disableable without breaking Color.
"""

import logging
from typing import TYPE_CHECKING, List, Any, Optional

if TYPE_CHECKING:
    from src.features.edit.ui.edit_page import EditPage
    from src.shared.actions import ActionRegistry
    from src.shared.panels.registry import PanelRegistry
    from src.shared.ui.shortcut_manager import ShortcutManager

from src.app.page_contract import PageDescriptor
from src.app.app_context import AppContext

logger = logging.getLogger(__name__)


class EditModule:
    """
    Edit feature module implementation.
    
    Edit provides timeline-based editing capabilities and can be
    disabled when not needed (e.g., for pure color grading workflows).
    
    This module:
    - Registers Edit page with FeatureRegistry
    - Provides Edit signal hub coordination
    - Does NOT modify core Edit data models
    
    Edit remains in original locations:
    - src/features/edit/core/ (data models, migrated from src/core/edit/)
    - src/features/edit/ui/ (UI panels, migrated from src/ui/edit/)
    """
    
    feature_id = "edit"
    
    def __init__(self):
        self._page: Optional["EditPage"] = None
        self._available: bool = False

        try:
            from src.features.edit.ui.edit_page import EditPage  # noqa: F401 — 页面可用性探测（置 _available）
            self._available = True
        except ImportError as e:
            logger.warning(f"Edit module unavailable: {e}")
            self._available = False

        # Register EditProject adapter so src/shared can dispatch
        # EditProject operations without importing src/features.
        self._register_adapter()

    @staticmethod
    def _register_adapter() -> None:
        """
        Register EditProject adapter callbacks.

        This allows src/shared/project modules to dispatch EditProject
        operations (isinstance checks, relink, create, etc.) through the
        adapter instead of importing src/features directly.
        """
        if not EditModule._edit_adapter_registered:
            EditModule._do_register_adapter()
            EditModule._edit_adapter_registered = True

    _edit_adapter_registered: bool = False

    @staticmethod
    def _do_register_adapter() -> None:
        try:
            from src.shared.project.edit_project_adapter import edit_project_adapter
            from src.features.edit.core.project import (
                EditProject,
                relink_asset,
                batch_relink_assets,
                update_offline_status,
            )

            def _show_relink_dialog(
                offline_assets: list, parent: Any = None
            ) -> dict[str, str] | None:
                """Show RelinkMediaDialog and return relink_map or None."""
                from src.features.edit.ui.relink_media_dialog import RelinkMediaDialog
                dialog = RelinkMediaDialog.from_asset_refs(offline_assets, parent)
                if dialog.exec() == RelinkMediaDialog.DialogCode.Accepted:
                    return dialog.get_relink_map()
                return None

            edit_project_adapter.register(
                edit_project_type=EditProject,
                create_project=EditProject.create,
                relink_asset_fn=relink_asset,
                batch_relink_fn=batch_relink_assets,
                update_offline_status_fn=update_offline_status,
                show_relink_dialog_fn=_show_relink_dialog,
            )
            logger.debug("EditProject adapter registered")
        except ImportError as e:
            logger.warning(f"EditProject adapter registration skipped: {e}")
    
    def is_available(self) -> bool:
        """
        Check if Edit module is available.
        
        Returns False if:
        - Feature flag is disabled
        - Module cannot be imported
        """
        return self._available
    
    def register_pages(self, context: AppContext) -> List[PageDescriptor]:
        """
        Register Edit page.
        
        Args:
            context: AppContext instance
        
        Returns:
            List containing Edit page descriptor
        """
        if not self.is_available():
            return []
        
        pages = []
        
        def create_edit_page(ctx: Any) -> "EditPage":
            from src.features.edit.ui.edit_page import EditPage
            self._page = EditPage()
            return self._page
        
        pages.append(PageDescriptor(
            page_id="edit",
            title=context.i18n.tr("page.edit", "Edit") if context.i18n else "Edit",
            factory=create_edit_page,
            default_enabled=True,
            shortcut="Ctrl+2",
        ))
        
        return pages
    
    def register_actions(self, context: AppContext) -> None:
        """
        Register Edit-related actions.
        
        Edit actions are registered in MainWindow directly.
        This method is placeholder for future modularization.
        
        Args:
            context: AppContext instance
        """
        pass
    
    def register_save_handlers(self, context: AppContext) -> None:
        """
        Register Edit save handler.
        
        Note: Edit save is handled by SaveRouter._save_from_edit
        for backward compatibility. Handler may be added in future.
        
        Args:
            context: AppContext instance with save_router
        """
        # Edit save is handled by default SaveRouter methods
        # Handler registration is optional for future use
        pass
    
    def on_page_enter(self, page_id: str, context: AppContext) -> None:
        """Called when entering Edit page."""
        if page_id != "edit":
            return
        # Edit page enter logic handled by MainWindow
    
    def on_page_leave(self, page_id: str, context: AppContext) -> None:
        """Called when leaving Edit page."""
        if page_id != "edit":
            return
        # Edit page leave logic handled by MainWindow
    
    def register_i18n(self) -> None:
        """Register edit module translations."""
        import os
        from src.shared.i18n.i18n import I18N
        locales_dir = os.path.join(os.path.dirname(__file__), "locales")
        I18N.load_module_translations(locales_dir)

    def register_menu_actions(self, action_registry: "ActionRegistry") -> None:
        """Register Edit menu actions with the ActionRegistry."""
        from src.shared.actions import MenuActionDescriptor
        from src.shared.i18n.i18n import I18N

        if not self._available:
            return

        # Timeline operations in the Edit menu.
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.cut",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_cut", "Cu&t"),
            callback_name="on_timeline_cut",
            order=100,
            feature_id="edit",
            shortcut_id="edit.timeline.cut",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.copy",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_copy", "&Copy"),
            callback_name="on_timeline_copy",
            order=110,
            feature_id="edit",
            shortcut_id="edit.timeline.copy",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.paste",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_paste", "&Paste"),
            callback_name="on_timeline_paste",
            order=120,
            feature_id="edit",
            shortcut_id="edit.timeline.paste",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.ripple_delete",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_ripple_delete", "Ripple &Delete"),
            callback_name="on_timeline_ripple_delete",
            order=130,
            feature_id="edit",
            shortcut_id="edit.timeline.ripple_delete",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.split",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_split", "&Split at Playhead"),
            callback_name="on_timeline_split",
            order=140,
            feature_id="edit",
            shortcut_id="edit.timeline.split",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.delete",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_delete", "&Delete"),
            callback_name="on_timeline_delete",
            order=150,
            feature_id="edit",
            shortcut_id="edit.timeline.delete",
        ))

        # Timeline insert/overwrite modes
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.insert",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_insert", "&Insert"),
            callback_name="on_timeline_insert",
            order=160,
            feature_id="edit",
            shortcut_id="edit.timeline.insert",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.overwrite",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_overwrite", "&Overwrite"),
            callback_name="on_timeline_overwrite",
            order=170,
            feature_id="edit",
            shortcut_id="edit.timeline.overwrite",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.append",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_append", "&Append"),
            callback_name="on_timeline_append",
            order=180,
            feature_id="edit",
            shortcut_id="edit.timeline.append",
        ))

        # Timeline navigation
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.zoom_in",
            menu_path="View/Timeline",
            label=I18N.tr("action.timeline_zoom_in", "Zoom &In"),
            callback_name="on_timeline_zoom_in",
            order=100,
            feature_id="edit",
            shortcut_id="edit.timeline.zoom_in",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.zoom_out",
            menu_path="View/Timeline",
            label=I18N.tr("action.timeline_zoom_out", "Zoom &Out"),
            callback_name="on_timeline_zoom_out",
            order=110,
            feature_id="edit",
            shortcut_id="edit.timeline.zoom_out",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.fit",
            menu_path="View/Timeline",
            label=I18N.tr("action.timeline_fit", "&Fit to Window"),
            callback_name="on_timeline_fit",
            order=120,
            feature_id="edit",
            shortcut_id="edit.timeline.fit",
        ))
        # P1-23: 播放性能 HUD（fps/丢帧/代理就绪度叠加层）
        action_registry.register(MenuActionDescriptor(
            action_id="edit.playback.hud",
            menu_path="View/Timeline",
            label=I18N.tr("action.playback_hud", "Playback &HUD"),
            callback_name="on_toggle_playback_hud",
            order=130,
            feature_id="edit",
        ))
        # P0-10: 一键导出脱敏性能报告（帧 trace/遥测/GPU pass/拷贝字节）
        action_registry.register(MenuActionDescriptor(
            action_id="edit.playback.export_perf_report",
            menu_path="View/Timeline",
            label=I18N.tr("action.export_perf_report", "Export Performance &Report"),
            callback_name="on_export_performance_report",
            order=131,
            feature_id="edit",
        ))
        # B1: 场景检测候选点时间线 overlay（checkable 开关，状态经 ConfigManager
        # 持久化）。挂在 Edit/Timeline（与 Scene Cut Detection 相邻）。历史上的
        # "View 顶级菜单未注册 → View/Timeline 路径不可达" 问题已由 AUD-3600
        # 修复（MenuBuilder._resolve_menu 现在会在持有 menu_bar 时自动补建
        # 未注册的顶层菜单），上方 View/Timeline 组动作因此恢复可达。
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.scene_cut_overlay",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.scene_cut_overlay", "Scene Cut &Overlay"),
            callback_name="on_toggle_scene_cut_overlay",
            order=215,
            feature_id="edit",
            checkable=True,
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.add_marker",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.timeline_add_marker", "Add &Marker"),
            callback_name="on_timeline_add_marker",
            order=200,
            feature_id="edit",
            shortcut_id="edit.timeline.add_marker",
        ))

        # P9-1: Scene Cut Detection menu item
        action_registry.register(MenuActionDescriptor(
            action_id="edit.timeline.scene_cut_detect",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.scene_cut_detect", "Scene Cut Detection..."),
            callback_name="on_scene_cut_detect",
            order=210,
            feature_id="edit",
        ))

        # P1-16: FCPXML/OTIO 互操作入口（File 菜单，位于 Save/Close 之后、
        # Deliver Export（order=200）之前）
        action_registry.register(MenuActionDescriptor(
            action_id="edit.interchange.import",
            menu_path="File",
            label=I18N.tr("action.interchange_import", "Import Timeline (FCPXML/OTIO)..."),
            callback_name="import_interchange_timeline",
            order=160,
            feature_id="edit",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.interchange.export.fcpxml",
            menu_path="File",
            label=I18N.tr("action.interchange_export_fcpxml", "Export Timeline as FCPXML..."),
            callback_name="export_timeline_fcpxml",
            order=171,
            feature_id="edit",
        ))
        action_registry.register(MenuActionDescriptor(
            action_id="edit.interchange.export.otio",
            menu_path="File",
            label=I18N.tr("action.interchange_export_otio", "Export Timeline as OTIO..."),
            callback_name="export_timeline_otio",
            order=172,
            feature_id="edit",
        ))
        # P1-21: 序列级字幕交付（多语言轨批量导出 + manifest）
        action_registry.register(MenuActionDescriptor(
            action_id="edit.subtitle.export_tracks",
            menu_path="File",
            label=I18N.tr(
                "action.subtitle_export_tracks", "Export Subtitle Tracks..."
            ),
            callback_name="export_timeline_subtitles",
            order=173,
            feature_id="edit",
        ))
        # B7: 占位符片段创建（粗剪定结构）
        action_registry.register(MenuActionDescriptor(
            action_id="edit.placeholder.create",
            menu_path="Edit/Timeline",
            label=I18N.tr("action.create_placeholder", "New Placeholder..."),
            callback_name="on_create_placeholder_clip",
            order=216,
            feature_id="edit",
        ))
        # B2: Stem 分轨导出（WAV 48kHz/24bit，QThread worker）
        action_registry.register(MenuActionDescriptor(
            action_id="edit.stem.export",
            menu_path="File",
            label=I18N.tr("action.stem_export", "Export Stems..."),
            callback_name="export_stem_tracks",
            order=174,
            feature_id="edit",
        ))
        # T02/G1a: 只读洞察工具（跨序列搜索/总表/统计/联系表/资料包/对照）。
        # 回调落在 EditControllerMixin.on_insights_open；窗口惰性创建，
        # 重复打开激活同一实例（factory 去重）。
        action_registry.register(MenuActionDescriptor(
            action_id="edit.insights.open",
            menu_path="Edit/Insights",
            label=I18N.tr("action.insights_open", "&Insights"),
            callback_name="on_insights_open",
            order=100,
            feature_id="edit",
        ))

        # ── 升级计划 M 面板动作（经统一注册表实体接线） ──
        upgrade_panels = [
            ("color.shot_match.open", "Color/Batch Tools",
             I18N.tr("action.shot_match_open", "&Batch Shot Match…"),
             "on_open_shot_match_panel", 900),
            ("color.version_tools.open", "Color/Version Tools",
             I18N.tr("action.version_tools_open", "Color Version &Tools…"),
             "on_open_version_tools_panel", 910),
            ("edit.filler_removal.open", "Edit/AI Editing",
             I18N.tr("action.filler_removal_open", "Remove &Filler Words…"),
             "on_open_filler_removal_panel", 950),
            ("project.snapshot_compare.open", "File/Snapshots",
             I18N.tr("action.snapshot_compare_open", "&Compare Snapshots…"),
             "on_open_snapshot_compare_panel", 940),
            ("project.proxy_cleanup.open", "File/Project Maintenance",
             I18N.tr("action.proxy_cleanup_open", "Proxies & Cache…"),
             "on_open_proxy_cleanup_panel", 960),
            ("composite.tracker_corrections.open", "Composite/Tracking",
             I18N.tr("action.tracker_corrections_open",
                     "Tracker &Corrections…"),
             "on_open_tracker_corrections_panel", 900),
            ("composite.bake.open", "Composite/Render-in-Place",
             I18N.tr("action.bake_open", "&Bake Node…"),
             "on_open_bake_dialog", 910),
            ("deliver.multi_version_qc.open", "Deliver/Quality",
             I18N.tr("action.multi_version_qc_open",
                     "Multi-Version &QC…"),
             "on_open_multi_version_qc_panel", 900),
        ]
        for action_id, menu_path, label, callback, order in upgrade_panels:
            action_registry.register(MenuActionDescriptor(
                action_id=action_id,
                menu_path=menu_path,
                label=label,
                callback_name=callback,
                order=order,
                feature_id="edit",
            ))

    def register_preference_pages(self):
        """Register Edit preference pages.

        Returns:
            List of ModularPreferencePage instances for this module.
        """
        if not self._available:
            return []

        from src.features.edit.preferences import EditPreferencePage
        from src.features.edit.ui.ai_preferences_page import AiEditingPreferencePage
        return [EditPreferencePage(), AiEditingPreferencePage()]

    def register_shortcuts(self, shortcut_manager: "ShortcutManager") -> None:
        """Register Edit module shortcuts with the ShortcutManager.

        These shortcuts are feature-specific and carry feature_id="edit"
        so they can be filtered when the Edit module is disabled.

        Args:
            shortcut_manager: ShortcutManager instance
        """
        if not self._available:
            return

        from src.shared.i18n.i18n import I18N
        from src.features.edit.ui.scene_cut_shortcuts import register_scene_cut_shortcuts
        from src.features.edit.ui.timeline_shortcuts import (
            register_timeline_shortcuts,
        )

        # Register local Scene Cut transport shortcuts centrally so they are
        # customizable and visible in the shortcut settings UI.  The dialog
        # matches them locally; no global QShortcut is installed here.
        register_scene_cut_shortcuts(shortcut_manager)
        # B5: 时间线内嵌编辑键（工具/标记/IO）集中注册——可改键、可见、冲突检测
        register_timeline_shortcuts(shortcut_manager)

        # Timeline operation shortcuts
        shortcut_manager.register(
            "edit.timeline.cut",
            I18N.tr("action.timeline_cut", "Cut"),
            "Edit",
            "Ctrl+X",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.copy",
            I18N.tr("action.timeline_copy", "Copy"),
            "Edit",
            "Ctrl+C",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.paste",
            I18N.tr("action.timeline_paste", "Paste"),
            "Edit",
            "Ctrl+V",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.ripple_delete",
            I18N.tr("action.timeline_ripple_delete", "Ripple Delete"),
            "Edit",
            "Delete",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.split",
            I18N.tr("action.timeline_split", "Split at Playhead"),
            "Edit",
            "Ctrl+Shift+X",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.delete",
            I18N.tr("action.timeline_delete", "Delete Clip"),
            "Edit",
            "Backspace",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.insert",
            I18N.tr("action.timeline_insert", "Insert"),
            "Edit",
            ",",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.overwrite",
            I18N.tr("action.timeline_overwrite", "Overwrite"),
            "Edit",
            ".",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.append",
            I18N.tr("action.timeline_append", "Append"),
            "Edit",
            "Shift+A",
            feature_id="edit",
        )

        # Timeline navigation shortcuts
        shortcut_manager.register(
            "edit.timeline.zoom_in",
            I18N.tr("action.timeline_zoom_in", "Timeline Zoom In"),
            "View",
            "Ctrl+=",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.zoom_out",
            I18N.tr("action.timeline_zoom_out", "Timeline Zoom Out"),
            "View",
            "Ctrl+-",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.fit",
            I18N.tr("action.timeline_fit", "Timeline Fit"),
            "View",
            "Shift+Z",
            feature_id="edit",
        )
        shortcut_manager.register(
            "edit.timeline.add_marker",
            I18N.tr("action.timeline_add_marker", "Add Marker"),
            "Edit",
            "M",
            feature_id="edit",
        )

    def get_edit_page(self) -> Optional["EditPage"]:
        """Get the EditPage instance."""
        return self._page

    def register_panels(self, panel_registry: "PanelRegistry") -> None:
        """Register Edit panel descriptors with the PanelRegistry.

        Args:
            panel_registry: PanelRegistry instance
        """
        if not self._available:
            return

        from src.shared.panels import PanelDescriptor

        panel_registry.register(PanelDescriptor(
            panel_id="edit.media_pool",
            title_key="panel.media_pool",
            fallback_title="Media Pool",
            feature_id="edit",
            category="Edit",
            page_ids=("Edit", "Composite"),
            dock_attr="dock_edit_media_pool",
            order=100,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="edit.viewport",
            title_key="panel.edit_viewport",
            fallback_title="Viewport",
            feature_id="edit",
            category="Edit",
            page_ids=("Edit",),
            dock_attr="dock_program_viewer",
            order=300,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="edit.timeline",
            title_key="panel.timeline",
            fallback_title="Timeline",
            feature_id="edit",
            category="Edit",
            page_ids=("Edit",),
            dock_attr="dock_edit_timeline",
            order=400,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="edit.properties",
            title_key="panel.properties",
            fallback_title="Properties",
            feature_id="edit",
            category="Edit",
            page_ids=("Edit",),
            dock_attr="dock_properties",
            order=500,
            default_visible=True,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="edit.effects",
            title_key="panel.edit_effects",
            fallback_title="Edit Effects",
            feature_id="edit",
            category="Edit",
            page_ids=("Edit",),
            dock_attr="dock_edit_effects",
            order=600,
            default_visible=False,
        ))
        panel_registry.register(PanelDescriptor(
            panel_id="edit.audio_mixer",
            title_key="panel.audio_mixer",
            fallback_title="Audio Mixer",
            feature_id="edit",
            category="Edit",
            page_ids=("Edit",),
            dock_attr="dock_audio_mixer",
            order=700,
            default_visible=False,
        ))
        # TAEP AI 编辑助手（UI-0）：Edit/Studio 右侧 dock，默认隐藏。
        # 仅 Edit feature enabled 时可见；不持有 AI session 业务状态。
        panel_registry.register(PanelDescriptor(
            panel_id="edit.ai_assistant",
            title_key="panel.ai_edit_assistant",
            fallback_title="AI assistant",
            feature_id="edit",
            category="Edit",
            page_ids=("Edit",),
            dock_attr="dock_ai_edit_assistant",
            order=800,
            default_visible=False,
        ))
