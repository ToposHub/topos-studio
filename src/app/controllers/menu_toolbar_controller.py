"""MenuToolbarMixin — extracted from MainWindow.

Handles menu bar construction, toolbar, status bar, shortcuts,
and various menu-driven actions.
"""

from __future__ import annotations

import os
import logging

from typing import Any

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout,
    QPushButton, QToolBar, QStatusBar, QMessageBox, QMenu,
    QInputDialog, QApplication, QProgressBar, QToolButton,
    QSizePolicy, QComboBox, QDialog, QLabel,
)
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, QSize, pyqtSignal

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import apply_combo_dropdown_style
from src.shared.assets.icon_manager import icon_reset, icon_settings, icon_home
from src.features.color.core.plugin_manager import get_plugin_manager

logger = logging.getLogger(__name__)


class _DependencyReportWorker(QRunnable):
    """AUD-3010: 在线程池构建工程依赖报告（LUT/插件/AI 模型文件系统扫描）。

    ``build_dependency_report`` 同步遍历工程引用的文件系统，库目录较大或
    网络卷路径时会冻结 UI；扫描在 worker 执行，``ready`` 信号（auto →
    queued）把报告送回 UI 线程弹对话框。
    """

    class _Signals(QObject):
        #: 依赖报告对象；构建失败时为 None。
        ready = pyqtSignal(object)
        finished = pyqtSignal()

    def __init__(self, project: Any, base_dir: Any,
                 parent: QObject | None = None) -> None:
        super().__init__()
        self._project = project
        self._base_dir = base_dir
        self.signals = self._Signals(parent)

    def run(self) -> None:
        from src.features.color.core.project_dependencies import (
            build_dependency_report,
        )
        report = None
        try:
            report = build_dependency_report(self._project, base_dir=self._base_dir)
        except Exception as exc:
            logger.warning("dependency report build failed: %s", exc)
        self.signals.ready.emit(report)
        self.signals.finished.emit()


class MenuToolbarMixin:
    """Mixin that provides menu, toolbar, statusbar and shortcut methods."""

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    def setup_menus(self) -> None:
        menu_bar = self.menuBar()

        # --- File Menu ---
        self.menu_file = menu_bar.addMenu(I18N.tr("menu.file", "&File"))
        file_menu = self.menu_file
        new_action = QAction(I18N.tr("action.new_project", "&New Project"), self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self.new_project)
        file_menu.addAction(new_action)

        # Project lifecycle is deliberately mode-agnostic.  The selected
        # .topos package decides whether it is an image or edit project.
        open_action = QAction(I18N.tr("action.open_project", "&Open Project..."), self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_project)
        file_menu.addAction(open_action)

        # The video-mode project manager can be reopened after startup so
        # users can create, switch, or delete projects without restarting.
        project_manager_action = QAction(
            I18N.tr("action.open_project_manager", "Open Project Manager..."),
            self,
        )
        project_manager_action.setShortcut("Ctrl+Shift+P")
        project_manager_action.triggered.connect(self._show_project_manager)
        project_manager_action.setVisible(self._is_video_work_mode())
        file_menu.addAction(project_manager_action)

        # Recent Projects is available in both Image and Video work modes.
        self.recent_projects_menu = file_menu.addMenu(
            I18N.tr("menu.recent_projects", "Recent Projects")
        )
        self._rebuild_recent_projects_menu()

        file_menu.addSeparator()

        open_media_action = QAction(
            I18N.tr("action.open_media", "Open Media..."), self
        )
        open_media_action.triggered.connect(self.open_file)
        file_menu.addAction(open_media_action)

        file_menu.addSeparator()

        save_action = QAction(I18N.tr("action.save_project", "&Save Project"), self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        # macOS 上，菜单 action 默认用 WidgetShortcut/WindowShortcut 作用域，
        # 当某个 dock 面板（如带输入框的面板）的子控件持有焦点时，Cmd+S 会被
        # 吞掉而菜单 action 不触发。改用 ApplicationShortcut 让快捷键在整个
        # 应用范围内生效，不依赖当前聚焦的 widget。
        #
        # ⚠ 代价（见 src/shared/ui/shortcut_scope.py）：应用级 Cmd+S 在
        # Motion Graphic 编辑器（modeless Tool 窗口）激活时同样触发，且抢在
        # 编辑器自己的 Ctrl+S action 之前消费按键。因此 save_file() 开头会把
        # 命令路由给激活中的编辑器（保存场景而非工程）。新增应用级命令时
        # 必须做同样的路由，否则会静默作用于主窗口（Cmd+W/close_project
        # 同理，路由为关闭激活的编辑器窗口）。
        save_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        # ShortcutManager normally applies the user's window/global setting to
        # every registered action. Project saving is the one application-level
        # command that must remain available while any editor or dock has focus.
        save_action.setProperty("force_application_shortcut", True)
        save_action.triggered.connect(self.save_file)
        file_menu.addAction(save_action)

        save_as_action = QAction(
            I18N.tr("action.save_project_as", "Save Project &As..."), self
        )
        save_as_action.setShortcut("Ctrl+Shift+S")
        save_as_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        save_as_action.triggered.connect(self.save_project_as)
        file_menu.addAction(save_as_action)

        # DESK-07B：打包交接到移动端——生成自包含单文件 .topos（渲染
        # 必需素材内容寻址内嵌 + 来源未知模块随行；02C 审计 blocked 时
        # 拒绝产出并展示问题清单）。
        handoff_action = QAction(
            I18N.tr("action.export_handoff", "导出交接包（移动端...）"), self
        )
        handoff_action.triggered.connect(self.export_handoff_to_mobile)
        file_menu.addAction(handoff_action)

        close_action = QAction(
            I18N.tr("action.close_project", "&Close Project"), self
        )
        close_action.setShortcut("Ctrl+W")
        close_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        close_action.triggered.connect(self.close_project)
        file_menu.addAction(close_action)

        # P1-COLOR-23: 工程依赖报告（LUT/插件/AI 模型可移植性清单）
        deps_action = QAction(
            I18N.tr("action.dependency_report", "工程依赖报告..."), self)
        deps_action.triggered.connect(self.on_dependency_report_requested)
        file_menu.addAction(deps_action)

        # Marker: Export action (from MenuBuilder / deliver module) is
        # inserted before this invisible action so it appears after the
        # project lifecycle actions and before Recent Files.
        self._file_export_anchor = QAction(self)
        self._file_export_anchor.setVisible(False)
        file_menu.addAction(self._file_export_anchor)

        file_menu.addSeparator()

        # Recent Files submenu
        self.recent_menu = file_menu.addMenu(
            I18N.tr("menu.recent_files", "Recent Files")
        )
        self._rebuild_recent_menu()

        file_menu.addSeparator()

        exit_action = QAction(I18N.tr("action.exit", "E&xit"), self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # --- Edit Menu ---
        self.menu_edit = menu_bar.addMenu(I18N.tr("menu.edit", "&Edit"))
        edit_menu = self.menu_edit
        undo_act = edit_menu.addAction(I18N.tr("action.undo", "Undo"))
        undo_act.setShortcut(QKeySequence.StandardKey.Undo)
        undo_act.triggered.connect(self.undo)
        self.undo_action_menu = undo_act # Keep reference to update state

        redo_act = edit_menu.addAction(I18N.tr("action.redo", "Redo"))
        redo_act.setShortcut(QKeySequence.StandardKey.Redo)
        redo_act.triggered.connect(self.redo)
        self.redo_action_menu = redo_act

        edit_menu.addSeparator()

        reset_act = QAction(I18N.tr("action.reset_all", "Reset All Adjustments"), self)
        reset_act.setShortcut("Ctrl+Shift+R")
        reset_act.triggered.connect(self.on_reset_all_click)
        edit_menu.addAction(reset_act)

        edit_menu.addSeparator()

        # Language submenu (moved from Settings menu)
        lang_menu = edit_menu.addMenu(I18N.tr("settings.language", "Language"))
        for locale_code, locale_name in I18N.get_available_locales().items():
            lang_action = QAction(locale_name, self)
            lang_action.setCheckable(True)
            lang_action.setChecked(locale_code == I18N.get_current_locale())
            lang_action.triggered.connect(lambda checked, lc=locale_code: self.change_language(lc))
            lang_menu.addAction(lang_action)

        edit_menu.addSeparator()

        # Keyboard Shortcuts dialog
        shortcut_act = QAction(I18N.tr("action.keyboard_shortcuts", "Keyboard Shortcuts..."), self)
        shortcut_act.setShortcut("Ctrl+K")
        shortcut_act.triggered.connect(self.show_shortcut_dialog)
        edit_menu.addAction(shortcut_act)
        self.shortcut_manager.register_action("edit.keyboard_shortcuts",
            I18N.tr("action.keyboard_shortcuts", "Keyboard Shortcuts..."), "Edit", "Ctrl+K", shortcut_act)

        pref_act = QAction(I18N.tr("action.preferences", "Preferences..."), self)
        pref_act.setShortcut("Ctrl+,")
        pref_act.triggered.connect(self.show_preferences_dialog)
        edit_menu.addAction(pref_act)
        self.pref_act = pref_act  # 供 _register_all_shortcuts 注册

        # Image operations (merged from Image menu)
        edit_menu.addSeparator()

        resize_act = QAction(I18N.tr("action.resize_image", "Resize Image..."), self)
        resize_act.setShortcut("Ctrl+Shift+I")
        resize_act.triggered.connect(self.show_resize_dialog)
        edit_menu.addAction(resize_act)

        edit_menu.addSeparator()

        rot_cw_act = QAction(I18N.tr("action.rotate_cw", "Rotate 90° Clockwise"), self)
        rot_cw_act.setShortcut("Ctrl+]")
        rot_cw_act.triggered.connect(lambda: self.rotate_image(True))
        edit_menu.addAction(rot_cw_act)

        rot_ccw_act = QAction(I18N.tr("action.rotate_ccw", "Rotate 90° Counter-Clockwise"), self)
        rot_ccw_act.setShortcut("Ctrl+[")
        rot_ccw_act.triggered.connect(lambda: self.rotate_image(False))
        edit_menu.addAction(rot_ccw_act)

        edit_menu.addSeparator()

        flip_h_act = QAction(I18N.tr("action.flip_horizontal", "Flip Horizontal"), self)
        flip_h_act.triggered.connect(lambda: self.flip_image(True))
        edit_menu.addAction(flip_h_act)

        flip_v_act = QAction(I18N.tr("action.flip_vertical", "Flip Vertical"), self)
        flip_v_act.triggered.connect(lambda: self.flip_image(False))
        edit_menu.addAction(flip_v_act)

        edit_menu.addSeparator()

        crop_act = QAction(I18N.tr("action.crop_tool", "Crop Tool..."), self)
        crop_act.setShortcut("C")
        crop_act.triggered.connect(lambda: self.dock_crop.setVisible(True) or self.dock_crop.raise_() or self.crop_panel.set_crop_active(True))
        edit_menu.addAction(crop_act)

        # --- Layer submenu (under Edit) ---
        self.menu_layer = edit_menu.addMenu(I18N.tr("menu.layer", "&Layer"))

        add_layer_act = QAction(I18N.tr("action.new_layer", "New Layer"), self)
        add_layer_act.setShortcut("Ctrl+Shift+N")
        add_layer_act.triggered.connect(self.on_layer_added)
        self.menu_layer.addAction(add_layer_act)

        del_layer_act = QAction(I18N.tr("action.delete_layer", "Delete Current Layer"), self)
        del_layer_act.setShortcut("Ctrl+Shift+Delete")
        del_layer_act.triggered.connect(lambda: self.on_layer_removed(self.engine.active_layer_index))
        self.menu_layer.addAction(del_layer_act)

        self.menu_layer.addSeparator()

        rename_layer_act = QAction(I18N.tr("action.rename_layer", "Rename Layer..."), self)
        rename_layer_act.setShortcut("F2")
        rename_layer_act.triggered.connect(self.rename_current_layer)
        self.menu_layer.addAction(rename_layer_act)

        toggle_vis_act = QAction(I18N.tr("action.toggle_visibility", "Toggle Layer Visibility"), self)
        toggle_vis_act.setShortcut("Ctrl+D")
        toggle_vis_act.triggered.connect(self.toggle_current_layer_visibility)
        self.menu_layer.addAction(toggle_vis_act)

        disable_all_act = QAction(I18N.tr("action.disable_all_layers", "Disable All Layers"), self)
        disable_all_act.setShortcut("Alt+D")
        disable_all_act.triggered.connect(self.disable_all_layers)
        self.menu_layer.addAction(disable_all_act)

        self.menu_layer.addSeparator()

        reset_layer_act = QAction(I18N.tr("action.reset_layer", "Reset Current Layer"), self)
        reset_layer_act.setShortcut("Ctrl+Shift+L")
        reset_layer_act.triggered.connect(lambda: self.on_layer_reset(self.engine.active_layer_index))
        self.menu_layer.addAction(reset_layer_act)

        self.menu_layer.addSeparator()

        # Layer Copy/Paste/Duplicate actions
        copy_layer_act = QAction(I18N.tr("action.copy_layer", "Copy Layer Parameters"), self)
        copy_layer_act.setShortcut("Ctrl+Shift+C")
        copy_layer_act.triggered.connect(self.on_layer_copy)
        self.menu_layer.addAction(copy_layer_act)

        paste_layer_act = QAction(I18N.tr("action.paste_layer", "Paste Layer Parameters"), self)
        paste_layer_act.setShortcut("Ctrl+Shift+V")
        paste_layer_act.triggered.connect(self.on_layer_paste)
        self.menu_layer.addAction(paste_layer_act)

        duplicate_layer_act = QAction(I18N.tr("action.duplicate_layer", "Duplicate Layer"), self)
        duplicate_layer_act.setShortcut("Shift+D")
        duplicate_layer_act.triggered.connect(self.on_layer_duplicate)
        self.menu_layer.addAction(duplicate_layer_act)

        # --- Effects Menu ---
        self.menu_effects = menu_bar.addMenu(I18N.tr("menu.effects", "&Effects"))
        effects_menu = self.menu_effects

        # Show Effects Panel action
        show_panel_act = QAction(I18N.tr("effects.show_panel", "Show Effects Panel"), self)
        # 注意：曾用 Ctrl+Shift+E，与 deliver.export.current_source 冲突。改用 Ctrl+Alt+E。
        show_panel_act.setShortcut("Ctrl+Alt+E")
        show_panel_act.triggered.connect(lambda: self.dock_plugin.setVisible(True) or self.dock_plugin.raise_())
        effects_menu.addAction(show_panel_act)
        self.show_panel_act = show_panel_act  # 供 _register_all_shortcuts 注册

        effects_menu.addSeparator()

        # Available effects/plugins
        all_plugins = {}
        if self.config_manager.get("auto_scan_plugins"):
            plugin_dir = self.config_manager.get("plugin_directory") or "plugins"
            get_plugin_manager().scan_plugins(plugin_dir)
            all_plugins = get_plugin_manager().get_all_plugins()
            # Remove disabled plugins
            for pid in (self.config_manager.get("disabled_plugins") or []):
                get_plugin_manager().plugins.pop(pid, None)
                all_plugins.pop(pid, None)
        dctl_plugins = {k: v for k, v in all_plugins.items() if v.get('type', 'dctl') == 'dctl'}
        ai_plugins = {k: v for k, v in all_plugins.items() if v.get('type') == 'ai'}

        if dctl_plugins:
            shader_menu = effects_menu.addMenu(I18N.tr("effects.shader_effects", "Shader Effects"))
            for plugin in dctl_plugins.values():
                plugin_act = QAction(plugin.get('name', plugin['plugin_id']), self)
                plugin_act.triggered.connect(lambda checked, pid=plugin['plugin_id'], pname=plugin.get('name', plugin['plugin_id']):
                                            self.add_plugin_to_current_layer(pid, pname))
                shader_menu.addAction(plugin_act)

        if ai_plugins:
            if dctl_plugins:
                effects_menu.addSeparator()
            ai_menu = effects_menu.addMenu(I18N.tr("effects.ai_effects", "AI Effects"))
            for plugin in ai_plugins.values():
                plugin_act = QAction(plugin.get('name', plugin['plugin_id']), self)
                plugin_act.triggered.connect(lambda checked, pid=plugin['plugin_id'], pname=plugin.get('name', plugin['plugin_id']):
                                            self.add_plugin_to_current_layer(pid, pname))
                ai_menu.addAction(plugin_act)

        if not all_plugins:
            no_effects_act = QAction(I18N.tr("effects.no_effects", "No effects available"), self)
            no_effects_act.setEnabled(False)
            effects_menu.addAction(no_effects_act)

        enabled_features = {"library", "color", "edit", "composite", "deliver"}
        if hasattr(self, 'feature_flags') and self.feature_flags:
            enabled_features = set(self.feature_flags.get_enabled_features())

        # Feature-owned top-level menus. MenuBuilder attaches contributed
        # actions here and feature flags keep product variants clean.
        self.menu_color = None
        self.menu_composite = None
        self.menu_deliver = None
        if "color" in enabled_features:
            self.menu_color = menu_bar.addMenu(I18N.tr("menu.color", "&Color"))
        if "composite" in enabled_features:
            self.menu_composite = menu_bar.addMenu(I18N.tr("menu.composite", "&Composite"))
        if "deliver" in enabled_features:
            self.menu_deliver = menu_bar.addMenu(I18N.tr("menu.deliver", "&Deliver"))

        # --- Window Menu ---
        self.menu_window = menu_bar.addMenu(I18N.tr("menu.window", "&Window"))
        window_menu = self.menu_window

        # Invisible shortcut actions (synced with Viewport panel menus)
        self.mask_overlay_act = QAction(I18N.tr("action.toggle_mask_preview", "Toggle Mask Preview"), self)
        self.mask_overlay_act.setShortcut("Shift+H")
        self.mask_overlay_act.setCheckable(True)
        self.mask_overlay_act.triggered.connect(self.toggle_mask_overlay)
        self.addAction(self.mask_overlay_act)  # Add to main window for global shortcut, not to menu

        self.inspector_act = QAction(I18N.tr("action.toggle_color_inspector", "Toggle Color Inspector"), self)
        # I/O are context-local edit shortcuts for timeline and source viewer.
        # Keep the inspector on a modified shortcut so a main-window QAction
        # can never intercept Mark In before the active edit surface sees it.
        self.inspector_act.setShortcut("Shift+I")
        self.inspector_act.triggered.connect(self.toggle_color_inspector)
        self.addAction(self.inspector_act)

        # Register panel descriptors before building Window > Panels.
        self._register_module_panels()

        # Panels submenu — built from PanelRegistry
        panels_menu = window_menu.addMenu(I18N.tr("menu.panels", "Panels"))
        self._build_panels_menu(panels_menu)

        # Layout actions
        window_menu.addAction(I18N.tr("action.reset_layout", "Reset to Default"), self.reset_layout_to_default)
        # 出厂默认布局只能通过提交 default_layout.json 变更；“设为默认”入口
        # 仅开发者模式可见（偏好设置开启并重启后生效），普通用户无法改动
        # 默认布局。
        if bool(self.config_manager.get("developer_mode")):
            window_menu.addAction(
                I18N.tr("action.set_as_default", "Set as Default"),
                self.set_current_layout_as_default,
            )
        window_menu.addAction(I18N.tr("action.save_layout", "Save Layout..."), self.save_layout_dialog)
        window_menu.addAction(I18N.tr("action.delete_layout", "Delete Layout..."), self.delete_layout_dialog)

        self.load_layout_menu = window_menu.addMenu(I18N.tr("action.load_layout", "Load Layout"))
        self.load_layout_menu.aboutToShow.connect(self.update_load_layout_menu)

        # P1-24: built-in Edit workspace presets (Editing/Audio/Captions/Review).
        # Populated on aboutToShow so availability reflects the live edit flag.
        self.workspace_menu = window_menu.addMenu(I18N.tr("menu.workspaces", "Workspaces"))
        self.workspace_menu.aboutToShow.connect(self.update_workspace_menu)

        window_menu.addSeparator()

        # Window controls
        min_act = QAction(I18N.tr("action.minimize", "Minimize"), self)
        min_act.setShortcut("Ctrl+M")
        min_act.triggered.connect(self.showMinimized)
        window_menu.addAction(min_act)

        max_act = QAction(I18N.tr("action.maximize", "Maximize"), self)
        max_act.setShortcut("Ctrl+Shift+M")
        max_act.triggered.connect(self.showMaximized)
        window_menu.addAction(max_act)
        self.max_act = max_act  # 供 _register_all_shortcuts 注册

        fs_act = QAction(I18N.tr("action.fullscreen", "Toggle Full Screen"), self)
        fs_act.setShortcut(Qt.Key.Key_F11)
        fs_act.triggered.connect(self.toggle_fullscreen)
        window_menu.addAction(fs_act)

        # Scripts (merged from Scripts menu)
        window_menu.addSeparator()

        scripts_menu = window_menu.addMenu(I18N.tr("menu.scripts", "&Scripts"))
        self._scripts_menu = scripts_menu
        self._script_action_items = {}  # name -> QAction
        scripts_menu.aboutToShow.connect(self._refresh_scripts_menu)
        # Placeholder for when no actions are registered
        self._scripts_no_actions_act = scripts_menu.addAction(
            I18N.tr("scripts.no_actions", "No registered actions")
        )
        self._scripts_no_actions_act.setEnabled(False)

        # --- Help Menu ---
        self.menu_help = menu_bar.addMenu(I18N.tr("menu.help", "&Help"))

        about_act = QAction(I18N.tr("action.about", "About Topos Color..."), self)
        about_act.triggered.connect(self.show_about_dialog)
        self.menu_help.addAction(about_act)

        # ── Register all shortcuts with ShortcutManager ──
        self._register_all_shortcuts(
            # (action_id, label, category, default_shortcut, action_ref)
            # File
            new_action=new_action,
            open_action=open_action,
            project_manager_action=project_manager_action,
            save_action=save_action,
            save_as_action=save_as_action,
            close_action=close_action,
            exit_action=exit_action,
            # Edit
            undo_act=undo_act,
            redo_act=redo_act,
            reset_act=reset_act,
            resize_act=resize_act,
            rot_cw_act=rot_cw_act,
            rot_ccw_act=rot_ccw_act,
            flip_h_act=flip_h_act,
            flip_v_act=flip_v_act,
            crop_act=crop_act,
            # Layer
            add_layer_act=add_layer_act,
            del_layer_act=del_layer_act,
            rename_layer_act=rename_layer_act,
            toggle_vis_act=toggle_vis_act,
            disable_all_act=disable_all_act,
            reset_layer_act=reset_layer_act,
            copy_layer_act=copy_layer_act,
            paste_layer_act=paste_layer_act,
            duplicate_layer_act=duplicate_layer_act,
            # Window
            min_act=min_act,
            fs_act=fs_act,
        )

        # Register menu actions contributed by enabled feature modules
        self._register_module_menu_actions()
        # Build actual QMenu/QAction entries from module contributions
        self._build_module_menus()

        # 确保 toolbar 弹出菜单中显示快捷键
        # macOS 上 QToolButton 弹出的 QMenu 可能不自动渲染 setShortcut() 的快捷键
        # 通过在 action text 末尾追加 \t{shortcut} 显式显示
        # (Must run after _build_module_menus so module actions are included)
        self._update_menu_shortcut_display()

    # ------------------------------------------------------------------
    # Shortcut display helpers
    # ------------------------------------------------------------------

    def _update_menu_shortcut_display(self) -> None:
        """将已注册的快捷键以 \\t 格式追加到菜单项文本，确保 toolbar 弹出菜单中可见。"""
        for menu in (self.menu_file, self.menu_edit, self.menu_effects,
                     self.menu_window, self.menu_help, self.menu_layer):
            if menu:
                self._inject_shortcut_text(menu)

    def _inject_shortcut_text(self, menu) -> None:
        for action in menu.actions():
            if action.menu():
                self._inject_shortcut_text(action.menu())
            elif not action.isSeparator():
                seq = action.shortcut()
                if not seq.isEmpty() and '\t' not in action.text():
                    action.setText(
                        action.text() + '\t' +
                        seq.toString(QKeySequence.SequenceFormat.NativeText)
                    )

    # ------------------------------------------------------------------
    # Shortcut registration
    # ------------------------------------------------------------------

    def _register_all_shortcuts(self, **kwargs) -> None:
        """Register all menu action shortcuts with the ShortcutManager.

        Maps action_id → (label, category, default_shortcut, QAction) for each
        menu action that has a keyboard shortcut.
        """
        sm = self.shortcut_manager

        # File actions
        sm.register("file.new", I18N.tr("action.new_project", "New Project"), "File", "Ctrl+N", kwargs["new_action"])
        sm.register("file.open", I18N.tr("action.open_project", "Open Project..."), "File", "Ctrl+O", kwargs["open_action"])
        sm.register("file.project_manager", I18N.tr("action.open_project_manager", "Open Project Manager..."), "File", "Ctrl+Shift+P", kwargs["project_manager_action"])
        sm.register("file.save", I18N.tr("action.save_project", "Save Project"), "File", "Ctrl+S", kwargs["save_action"])
        sm.register("file.save_as", I18N.tr("action.save_project_as", "Save Project As..."), "File", "Ctrl+Shift+S", kwargs["save_as_action"])
        sm.register("file.close", I18N.tr("action.close_project", "Close Project"), "File", "Ctrl+W", kwargs["close_action"])
        # file.export is registered by MenuBuilder from deliver module contribution
        sm.register("file.exit", I18N.tr("action.exit", "Exit"), "File", "Ctrl+Q", kwargs["exit_action"])

        # Edit actions
        sm.register("edit.undo", I18N.tr("action.undo", "Undo"), "Edit", "Ctrl+Z", kwargs["undo_act"])
        sm.register("edit.redo", I18N.tr("action.redo", "Redo"), "Edit", "Ctrl+Shift+Z", kwargs["redo_act"])
        sm.register("edit.reset_all", I18N.tr("action.reset_all", "Reset All Adjustments"), "Edit", "Ctrl+Shift+R", kwargs["reset_act"])

        # Image actions (now under Edit menu)
        sm.register("image.resize", I18N.tr("action.resize_image", "Resize Image..."), "Edit", "Ctrl+Shift+I", kwargs["resize_act"])
        sm.register("image.rotate_cw", I18N.tr("action.rotate_cw", "Rotate 90° Clockwise"), "Edit", "Ctrl+]", kwargs["rot_cw_act"])
        sm.register("image.rotate_ccw", I18N.tr("action.rotate_ccw", "Rotate 90° Counter-CW"), "Edit", "Ctrl+[", kwargs["rot_ccw_act"])
        sm.register("image.flip_h", I18N.tr("action.flip_horizontal", "Flip Horizontal"), "Edit", "", kwargs["flip_h_act"])
        sm.register("image.flip_v", I18N.tr("action.flip_vertical", "Flip Vertical"), "Edit", "", kwargs["flip_v_act"])
        sm.register("image.crop", I18N.tr("action.crop_tool", "Crop Tool..."), "Edit", "C", kwargs["crop_act"])

        # Layer actions
        sm.register("layer.new", I18N.tr("action.new_layer", "New Layer"), "Layer", "Ctrl+Shift+N", kwargs["add_layer_act"])
        sm.register("layer.delete", I18N.tr("action.delete_layer", "Delete Current Layer"), "Layer", "Ctrl+Shift+Delete", kwargs["del_layer_act"])
        sm.register("layer.rename", I18N.tr("action.rename_layer", "Rename Layer..."), "Layer", "F2", kwargs["rename_layer_act"])
        sm.register("layer.toggle_vis", I18N.tr("action.toggle_visibility", "Toggle Visibility"), "Layer", "Ctrl+D", kwargs["toggle_vis_act"])
        sm.register("layer.disable_all", I18N.tr("action.disable_all_layers", "Disable All Layers"), "Layer", "Alt+D", kwargs["disable_all_act"])
        sm.register("layer.reset", I18N.tr("action.reset_layer", "Reset Current Layer"), "Layer", "Ctrl+Shift+L", kwargs["reset_layer_act"])
        sm.register("layer.copy", I18N.tr("action.copy_layer", "Copy Layer Params"), "Layer", "Ctrl+Shift+C", kwargs["copy_layer_act"])
        sm.register("layer.paste", I18N.tr("action.paste_layer", "Paste Layer Params"), "Layer", "Ctrl+Shift+V", kwargs["paste_layer_act"])
        sm.register("layer.duplicate", I18N.tr("action.duplicate_layer", "Duplicate Layer"), "Layer", "Shift+D", kwargs["duplicate_layer_act"])

        # Window actions
        sm.register("window.minimize", I18N.tr("action.minimize", "Minimize"), "Window", "Ctrl+M", kwargs["min_act"])
        sm.register("window.fullscreen", I18N.tr("action.fullscreen", "Toggle Full Screen"), "Window", "F11", kwargs["fs_act"])

        # 以下 action 之前只 setShortcut 未注册到管理器（面板不可见、不参与冲突检测）。
        # 现纳入管理器，使其可在快捷键面板查看/自定义/冲突检测。
        # 这些 action 是 self 属性（不是 kwargs 局部变量）。
        sm.register("edit.preferences", I18N.tr("action.preferences", "Preferences..."),
                    "Edit", "Ctrl+,", getattr(self, "pref_act", None))
        sm.register("view.show_effects_panel", I18N.tr("effects.show_panel", "Show Effects Panel"),
                    "View", "Ctrl+Alt+E", getattr(self, "show_panel_act", None))
        sm.register("view.toggle_mask_preview", I18N.tr("action.toggle_mask_preview", "Toggle Mask Preview"),
                    "View", "Shift+H", getattr(self, "mask_overlay_act", None))
        sm.register("view.toggle_inspector", "Toggle Color Inspector",
                    "View", "Shift+I", getattr(self, "inspector_act", None))
        sm.register("window.maximize", I18N.tr("action.maximize", "Maximize"),
                    "Window", "Ctrl+Shift+M", getattr(self, "max_act", None))

    # ------------------------------------------------------------------
    # Image manipulation actions
    # ------------------------------------------------------------------

    def show_resize_dialog(self) -> None:
        if self.engine.img_full_original is None: return
        from src.app.ui.resize_dialog import ResizeDialog
        h, w = self.engine.img_full_original.shape[:2]
        dlg = ResizeDialog(w, h, self)
        if dlg.exec():
            nw, nh = dlg.get_dimensions()
            self.engine.resize_image(nw, nh)
            self.status_bar.showMessage(f"Resized to {nw}x{nh}", 3000)

    def rotate_image(self, clockwise) -> None:
        self.engine.rotate_image(clockwise)
        self.status_bar.showMessage("Image Rotated", 2000)

    def flip_image(self, horizontal) -> None:
        self.engine.flip_image(horizontal)
        self.status_bar.showMessage("Image Flipped", 2000)

    def rename_current_layer(self):
        # [UX-6] F2 是 Qt QAction 快捷键，即使槽里 return 事件也不会到达
        # QWebEngineView——焦点在 Files 面板内时，主动把"重命名"转发给
        # 素材浏览器的前端实现
        w = QApplication.focusWidget()
        while w is not None:
            if w.__class__.__name__ == 'FilesPanel':
                try:
                    # [ARCH-4] 带回调记录执行结果：JS 显式返回 true/false，
                    # None 表示页面未就绪或渲染进程异常
                    w.browser.page().runJavaScript(
                        "(() => { if (window.__fxRenameSelected) {"
                        " window.__fxRenameSelected(); return true; }"
                        " return false; })()", 0,
                        lambda r: logging.getLogger(__name__).warning(
                            "F2 重命名转发未生效（result=%r）", r)
                        if r is not True else None)
                except Exception:
                    logging.getLogger(__name__).exception("F2 重命名转发失败")
                return
            w = w.parentWidget()
        layer = self.engine.get_active_layer()
        if not layer: return
        name, ok = QInputDialog.getText(self, "Rename Layer", "New Name:", text=layer.name)
        if ok and name:
            self.on_layer_renamed(self.engine.active_layer_index, name)
            self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)

    def toggle_current_layer_visibility(self) -> None:
        idx = self.engine.active_layer_index
        layer = self.engine.get_active_layer()
        if layer:
            self.on_layer_visibility_changed(idx, not layer.visible)
            self.layer_panel.update_layers(self.engine.layers, idx)

    def disable_all_layers(self) -> None:
        """关闭所有图层的可见性。"""
        for i, layer in enumerate(self.engine.layers):
            layer.visible = False
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.request_render()
        self._trigger_autosave()
        self.status_bar.showMessage("All layers disabled", 2000)

    # ------------------------------------------------------------------
    # Window management actions
    # ------------------------------------------------------------------

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def show_about_dialog(self) -> None:
        QMessageBox.about(self, I18N.tr("dialog.about_title", "About Topos Color"),
                          I18N.tr("dialog.about_text", "<h3>Topos Color</h3><p>Version 3.0</p><p>Advanced Image Processing & Color Grading Software.</p><p>© 2026 ToposHub | 北京泊恒色彩科技有限公司</p><p>© 2026 ToposHub · Boheng Color Technology (Beijing) Co., Ltd.</p>"))

    # ------------------------------------------------------------------
    # Language / restart
    # ------------------------------------------------------------------

    def change_language(self, locale_code: str) -> None:
        """
        Change the application language.

        Saves the new locale preference and prompts the user to restart the application.

        Args:
            locale_code: The locale code to switch to (e.g., "en_US", "zh_CN")
        """
        if locale_code == I18N.get_current_locale():
            return

        # Save the new locale preference
        self.config_manager.set("locale", locale_code)
        self.config_manager.save_config()

        # Load the new locale translations immediately (with module re-merge)
        if hasattr(self, 'feature_registry') and self.feature_registry:
            self.feature_registry.reload_i18n(locale_code)
        else:
            I18N.load_locale(locale_code)

        # Get display name for the new locale

        # Show restart prompt
        restart_dialog = QMessageBox(self)
        restart_dialog.setWindowTitle(I18N.tr("dialog.restart_title", "Restart Required"))
        restart_dialog.setText(I18N.tr("dialog.restart_message",
            "The application needs to restart for the language change to take effect.\n\nWould you like to restart now?"))
        restart_dialog.setIcon(QMessageBox.Icon.Information)

        restart_btn = restart_dialog.addButton(
            I18N.tr("dialog.restart", "Restart"),
            QMessageBox.ButtonRole.AcceptRole
        )
        later_btn = restart_dialog.addButton(
            I18N.tr("dialog.later", "Later"),
            QMessageBox.ButtonRole.RejectRole
        )

        logger.debug("Showing restart dialog...")
        restart_dialog.exec()
        logger.debug(f"Dialog closed, clicked button: {restart_dialog.clickedButton()}")
        logger.debug(f"restart_btn: {restart_btn}, later_btn: {later_btn}")

        if restart_dialog.clickedButton() == restart_btn:
            logger.debug("User clicked Restart, calling _restart_application()")
            # Restart the application
            self._restart_application()
        else:
            logger.debug("User clicked Later or closed dialog")

    def _restart_application(self) -> None:
        """Restart the application with the same arguments."""
        # [R2-6] 统一走共享的 detached-subprocess 重启实现
        self.close()
        from src.shared.app_restart import restart_application
        restart_application()

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------

    def show_shortcut_dialog(self) -> None:
        """Open the keyboard shortcut customization dialog."""
        from src.app.ui.shortcut_dialog import ShortcutDialog

        # Determine enabled features for filtering shortcuts.
        enabled = None
        if hasattr(self, 'feature_flags') and self.feature_flags:
            enabled = set(self.feature_flags.get_enabled_features())

        dialog = ShortcutDialog(self.shortcut_manager, self,
                                enabled_features=enabled)
        dialog.exec()
        # After dialog closes, shortcuts are already applied to QActions
        # and persisted to config.json by ShortcutManager

    def show_preferences_dialog(self, initial_page_id: str | None = None) -> None:
        """Show the Preferences dialog for application settings."""
        from src.app.ui.preferences_dialog import PreferencesDialog

        # Collect module preference pages from registered modules
        module_pages = []
        if hasattr(self, 'feature_registry') and self.feature_registry:
            for fid in self.feature_registry.get_available_features():
                mod = self.feature_registry._modules.get(fid)
                if mod and hasattr(mod, 'register_preference_pages'):
                    try:
                        module_pages.extend(mod.register_preference_pages())
                    except Exception as exc:
                        logger.warning("Failed to register preference pages for %s: %s", fid, exc)

        dialog = PreferencesDialog(
            self.config_manager, self.shortcut_manager, self,
            feature_flags=self.feature_flags,
            module_pages=module_pages,
        )
        if initial_page_id:
            dialog.select_page(initial_page_id)

        # Connect appearance preview signal for instant preview
        dialog.appearance_preview_changed.connect(self._on_appearance_preview)

        dialog.exec()

        # Disconnect appearance preview signal (defensive programming)
        try:
            dialog.appearance_preview_changed.disconnect(self._on_appearance_preview)
        except TypeError:
            pass  # Connection may have been automatically disconnected on dialog destruction

        # AI provider profile 可能在 Preferences 里被修改/切换；重新装配 AI 子系统
        # 使新 profile 立即生效（_setup_ai_editing 会从 config 重新加载 profile 并
        # 注入对应的 LLM planner）。
        if hasattr(self, "_setup_ai_editing"):
            try:
                self._setup_ai_editing()
            except Exception as exc:
                # 装配失败不阻塞；AI 面板保持原状态
                logger.warning("Failed to re-assemble AI subsystem after preferences: %s", exc)

        # Re-apply settings that need immediate effect after dialog closes
        self._start_backup_timer()
        self.log_manager.reconfigure()
        self.color_manager.reconfigure(self.config_manager)
        global_mode = self.config_manager.get("global_shortcuts") or False
        self.shortcut_manager.set_global_mode(global_mode)
        save_interval = self.config_manager.get("auto_save_interval") or 5
        if getattr(self, "edit_project_io", None) is not None:
            self.edit_project_io.set_autosave_interval(save_interval)
        tangent_bridge = getattr(self, "tangent_color_bridge", None)
        if tangent_bridge is not None:
            try:
                tangent_bridge.reconfigure()
            except Exception:
                logger.exception("Failed to reconfigure Tangent control surface")

        # 预览关滤波（P6 v2.1 后续）：偏好面板改完立即生效（切换 source 并重渲
        # 当前帧），不必重启或重开项目。
        renderer = getattr(self, "timeline_preview_renderer", None)
        set_no_deblock = getattr(renderer, "set_no_deblock", None)
        if callable(set_no_deblock):
            try:
                set_no_deblock(bool(
                    self.config_manager.get("video_preview_no_deblock")))
            except Exception:
                logger.exception("Failed to apply preview no-deblock preference")

    # ------------------------------------------------------------------
    # Appearance preview
    # ------------------------------------------------------------------

    def _on_appearance_preview(self, key: str, value: object) -> None:
        """Handle instant appearance preview from Preferences dialog."""
        if key == 'ui_scale':
            # Apply UI scale preview
            scale = value / 100.0 if isinstance(value, (int, float)) else 1.0
            font = self.font()
            base_size = getattr(self, '_base_font_size', None)
            if base_size is None:
                self._base_font_size = font.pointSize() if font.pointSize() > 0 else 13
                base_size = self._base_font_size
            new_size = max(8, int(base_size * scale))
            font.setPointSize(new_size)
            self.setFont(font)
            for dock in getattr(self, 'all_docks', []):
                dock.setFont(font)
        elif key == 'panel_opacity':
            # Apply panel opacity preview
            opacity = value / 100.0 if isinstance(value, (int, float)) else 1.0
            self.apply_panel_opacity(opacity)
        elif key == 'accent_color':
            # Apply accent color preview by replacing colors in global stylesheet
            self._apply_accent_preview(value)

    def apply_panel_opacity(self, opacity: float) -> None:
        """把面板透明度应用到全部 dock（AUD-3001）。

        浮动 dock 是顶层窗口，``setWindowOpacity`` 直接生效；停靠中的
        dock 是 QMainWindow 的子部件，windowOpacity 对其无效——改用逐
        dock 的 ``QGraphicsOpacityEffect``。``opacity >= 1.0`` 时移除
        全部效果，默认配置保持零合成开销。停靠/浮动状态切换时由
        ``_wire_panel_opacity_reapply`` 重新套用正确机制。
        """
        from PyQt6.QtWidgets import QGraphicsOpacityEffect

        try:
            opacity = min(1.0, max(0.0, float(opacity)))
        except (TypeError, ValueError):
            opacity = 1.0
        self._panel_opacity = opacity
        for dock in getattr(self, 'all_docks', []):
            try:
                if dock.isFloating():
                    dock.setWindowOpacity(opacity)
                    self._remove_dock_opacity_effect(dock)
                elif opacity < 1.0:
                    dock.setWindowOpacity(1.0)
                    effect = dock.graphicsEffect()
                    if not isinstance(effect, QGraphicsOpacityEffect):
                        effect = QGraphicsOpacityEffect(dock)
                        dock.setGraphicsEffect(effect)
                    effect.setOpacity(opacity)
                else:
                    dock.setWindowOpacity(1.0)
                    self._remove_dock_opacity_effect(dock)
            except Exception:
                logger.debug("apply_panel_opacity: dock failed", exc_info=True)

    @staticmethod
    def _remove_dock_opacity_effect(dock: QWidget) -> None:
        """移除先前安装的透明度效果（恢复完全不透明渲染路径）。"""
        from PyQt6.QtWidgets import QGraphicsOpacityEffect

        effect = dock.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            dock.setGraphicsEffect(None)

    def _wire_panel_opacity_reapply(self) -> None:
        """dock 浮动/停靠切换时重套面板透明度（两态机制不同，AUD-3001）。"""
        if getattr(self, '_panel_opacity_wired', False):
            return
        self._panel_opacity_wired = True
        for dock in getattr(self, 'all_docks', []):
            dock.topLevelChanged.connect(
                lambda _floating: self.apply_panel_opacity(
                    getattr(self, '_panel_opacity', 1.0)))

    def _apply_accent_preview(self, hex_color: str) -> None:
        """Replace accent color in global stylesheet for live preview."""
        from src.shared.ui.style_sheet import DARK_THEME
        from PyQt6.QtGui import QColor

        qcolor = QColor(hex_color)
        if not qcolor.isValid():
            return

        r, g, b = qcolor.red(), qcolor.green(), qcolor.blue()
        lighter = qcolor.lighter(130)
        darker = qcolor.darker(130)

        style = DARK_THEME
        style = style.replace('#ff8800', hex_color)
        style = style.replace('#ffaa44', lighter.name())
        style = style.replace('#cc6600', darker.name())
        style = style.replace('rgba(255, 136, 0,', f'rgba({r}, {g}, {b},')
        self.setStyleSheet(style)

    # ------------------------------------------------------------------
    # Toggle actions
    # ------------------------------------------------------------------

    def toggle_mask_overlay(self, checked=None) -> None:
        if checked is None:
            checked = not self.canvas.viewport_menu.mask_act.isChecked()
        self.canvas.viewport_menu.mask_act.setChecked(checked)
        self.mask_overlay_act.setChecked(checked)

    def toggle_color_inspector(self) -> None:
        if self.canvas.inspector_mode_active:
            self.canvas.set_tool_mode('none')
        else:
            self.canvas.set_tool_mode('inspector')

    def reset_view_to_actual(self) -> None:
        self.canvas.resetTransform()
        if self.canvas.engine:
            self.canvas.engine.render_pipeline(render_reason='viewport_refresh')
        self.canvas.viewport().update()

    def toggle_focus_mode(self) -> None:
        """Toggle focus mode - hide/show all panels except viewport."""
        # Check if we're already in focus mode (only viewport visible)
        other_docks_visible = any(dock.isVisible() for dock in self.all_docks if dock != self.dock_viewport)

        if other_docks_visible:
            # Enter focus mode: hide all except viewport
            self._focus_mode_state = [dock.isVisible() for dock in self.all_docks]
            for dock in self.all_docks:
                if dock != self.dock_viewport:
                    dock.setVisible(False)
        else:
            # Exit focus mode: restore previous state
            if hasattr(self, '_focus_mode_state'):
                for dock, visible in zip(self.all_docks, self._focus_mode_state):
                    dock.setVisible(visible)

    # ------------------------------------------------------------------
    # Welcome / update / recent files
    # ------------------------------------------------------------------

    def _show_welcome_dialog(self) -> None:
        """Show the welcome dialog on startup."""
        from src.app.ui.welcome_dialog import WelcomeDialog
        dlg = WelcomeDialog(self.config_manager, self)
        dlg.open_file_requested.connect(self.open_file)
        dlg.recent_file_selected.connect(self._open_recent_from_welcome)
        dlg.recent_project_selected.connect(self._open_recent_project_from_welcome)
        dlg.exec()

    def _is_video_work_mode(self) -> bool:
        """Return whether the video project manager belongs in this UI."""
        from src.shared.feature_flags import get_work_mode
        return get_work_mode(getattr(self, "config_manager", None)) == "video"

    def _show_project_manager(self, startup: bool = False) -> None:
        """Show the video-mode Project Manager.

        At startup, closing without choosing a project creates an unsaved
        ``Untitled Project`` workspace instead of exiting. When opened from
        the running app, closing the manager simply returns to the current
        workspace.
        """
        from src.app.ui.project_manager_dialog import ProjectManagerDialog
        dlg = ProjectManagerDialog(
            self.config_manager,
            self,
            active_project_path=getattr(self, "_current_edit_project_filepath", None),
        )
        dlg.project_selected.connect(
            lambda filepath: self._open_project_from_manager(filepath, dlg)
        )
        dlg.project_renamed.connect(self._on_project_renamed_from_manager)
        dlg.new_project_requested.connect(lambda: self._new_project_from_manager(dlg))
        result = dlg.exec()
        # Keep the application usable even when the user closes the startup
        # manager without selecting a saved project.  The temporary project is
        # deliberately in-memory only; the first Save/Save As persists it.
        if startup and result != QDialog.DialogCode.Accepted:
            if not getattr(self, "_current_edit_project", None):
                name = self._next_untitled_project_name()
                try:
                    self._auto_create_edit_project(name=name)
                except Exception:
                    logger.exception("Could not create the default untitled project")
                    # A failure here is exceptional (for example, the Edit
                    # module may be unavailable). Preserve the old safe
                    # fallback rather than leaving the startup flow unusable.
                    QApplication.quit()

    def _next_untitled_project_name(self) -> str:
        """Return a unique in-memory project name for startup fallback.

        Names are compared case-insensitively against the same project
        metadata used by Project Manager, including all configured library
        folders.  The unsaved document therefore cannot visually collide with
        a saved project card.
        """
        from src.shared.project.library import scan_video_projects

        try:
            metas = scan_video_projects(getattr(self, "config_manager", None))
        except Exception:
            logger.debug("Could not scan project names for startup fallback", exc_info=True)
            metas = []
        used = {
            str(getattr(meta, "name", "") or "").strip().casefold()
            for meta in metas
        }
        used.discard("")

        base = "Untitled Project"
        index = 1
        while True:
            candidate = base if index == 1 else f"{base} {index}"
            if candidate.casefold() not in used:
                return candidate
            index += 1

    def _open_project_from_manager(self, filepath: str, manager_dialog=None):
        """Open a .topos project chosen from the Project Manager."""
        # Use the canonical project switch path so dirty-state confirmation,
        # playback teardown, SaveRouter and mode validation all run together.
        previous_project = getattr(self, "_current_edit_project", None)
        if self._open_project_path(filepath):
            if manager_dialog is not None:
                manager_dialog.accept()
            return True
        # A cancelled save or a failed load leaves the manager open so the
        # user can choose another project or retry.
        return getattr(self, "_current_edit_project", None) is not previous_project

    def _on_project_renamed_from_manager(self, old_path: str, new_path: str) -> None:
        """Keep the active document bound to a renamed project package."""
        current = getattr(self, "_current_edit_project_filepath", None)
        if current and os.path.abspath(current) == os.path.abspath(old_path):
            self._current_edit_project_filepath = new_path
            self.save_router.set_project_path(new_path)
            if getattr(self, "_current_edit_project", None) is not None:
                from dataclasses import replace
                self._current_edit_project = replace(
                    self._current_edit_project,
                    name=os.path.splitext(os.path.basename(new_path))[0],
                )
                self.setWindowTitle(f"Topos - {self._current_edit_project.name}")

    def _new_project_from_manager(self, manager_dialog) -> None:
        """Create a new empty video project directly in the library directory.

        No full "New Project" dialog — the user only types a name and picks a
        location (timelines are created later by importing/dragging media into
        the timeline). The project is created, saved and opened; the manager
        dialog then closes.
        """
        from pathlib import Path
        from PyQt6.QtWidgets import (
            QDialog as _QDialog,
            QVBoxLayout as _QVBoxLayout,
            QFormLayout as _QFormLayout,
            QHBoxLayout as _QHBoxLayout,
            QLineEdit as _QLineEdit,
            QPushButton as _QPushButton,
            QFileDialog as _QFileDialog,
        )
        from src.shared.project.library import (
            get_project_library_dir,
            normalize_project_name,
        )

        # ── Compact "New Project" dialog: name + location only ──
        lib_dir = get_project_library_dir(self.config_manager)
        dlg = _QDialog(self)
        dlg.setWindowTitle(I18N.tr("pm.new_project", "New Project"))
        dlg.setFixedWidth(440)
        dlg.setStyleSheet("""
            QDialog { background: #27272b; color: #ddd; }
            QLabel { color: #aaa; font-size: 12px; }
            QLineEdit {
                background: #1e1e22; color: #ddd;
                border: 1px solid #444; border-radius: 4px;
                padding: 6px 10px; font-size: 13px;
            }
            QLineEdit:focus { border-color: #ff8800; }
            QPushButton {
                background: transparent; color: #ccc;
                border: 1px solid #555; border-radius: 4px;
                padding: 6px 16px; font-size: 12px;
            }
            QPushButton:hover { color: #ddd; border-color: #777; }
            QPushButton#primaryBtn {
                color: #ff8800; border-color: #ff8800; font-weight: bold;
            }
            QPushButton#primaryBtn:hover { background: rgba(255,136,0,0.12); }
            QPushButton#primaryBtn:disabled { color: #555; border-color: #444; }
        """)
        form = _QFormLayout()
        form.setSpacing(12)
        name_edit = _QLineEdit()
        name_edit.setPlaceholderText(I18N.tr("project.name_placeholder", "My Project"))
        name_edit.setText(I18N.tr("project.default_name", "Untitled Project"))
        form.addRow(I18N.tr("dialog.project_name", "Project Name:"), name_edit)

        loc_row = _QHBoxLayout()
        loc_edit = _QLineEdit()
        loc_edit.setText(str(lib_dir))
        browse_btn = _QPushButton(I18N.tr("project.browse", "Browse..."))

        def _browse() -> None:
            chosen = _QFileDialog.getExistingDirectory(
                dlg,
                I18N.tr("project.select_location", "Select Project Location"),
                loc_edit.text() or str(lib_dir),
            )
            if chosen:
                loc_edit.setText(chosen)
        browse_btn.clicked.connect(_browse)
        loc_row.addWidget(loc_edit, 1)
        loc_row.addWidget(browse_btn)
        form.addRow(I18N.tr("project.location", "Location:"), loc_row)

        v = _QVBoxLayout(dlg)
        v.setContentsMargins(24, 20, 24, 18)
        v.setSpacing(16)
        v.addLayout(form)
        v.addStretch()

        btn_row = _QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = _QPushButton(I18N.tr("pm.cancel", "Cancel"))
        create_btn = _QPushButton(I18N.tr("pm.create", "Create"))
        create_btn.setObjectName("primaryBtn")
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(create_btn)
        v.addLayout(btn_row)

        def _update_create_btn(_text="") -> None:
            create_btn.setEnabled(bool(name_edit.text().strip()))
        name_edit.textChanged.connect(_update_create_btn)
        _update_create_btn()
        cancel_btn.clicked.connect(dlg.reject)
        create_btn.clicked.connect(dlg.accept)

        # Enter in the name field triggers Create.
        name_edit.returnPressed.connect(create_btn.click)

        if dlg.exec() != _QDialog.DialogCode.Accepted:
            return

        name = normalize_project_name(name_edit.text())
        location = loc_edit.text().strip()
        if not name:
            return
        # Ensure the chosen location exists (create it if the user typed a new
        # path); fall back to the library dir on any error.
        try:
            Path(location).mkdir(parents=True, exist_ok=True)
        except OSError:
            location = str(lib_dir)

        # Build the target path and avoid clobbering an existing project.
        base = name
        candidate = Path(location) / f"{normalize_project_name(base)}.topos"
        idx = 1
        while candidate.exists():
            idx += 1
            candidate = Path(location) / f"{base} {idx}.topos"
        project_path = str(candidate)

        # Create an empty EditProject + default timeline, then persist it.
        try:
            # Creating from the manager is still a project switch. Resolve the
            # current document first so unsaved work cannot be replaced.
            if getattr(self, "_current_edit_project", None) is not None:
                if not self.close_project():
                    return

            self._auto_create_edit_project(name=name)
            if getattr(self, "_current_edit_project", None) is None:
                raise RuntimeError("project creation produced no current project")
            edit_project = self._current_edit_project

            from src.features.edit.core.edit_project_io import EditProjectIO
            # Pass the in-memory TimelineSequence entities so they are
            # serialized alongside the project. Without this, only the
            # lightweight TimelineSequenceRef map is saved; reload then
            # yields an empty _current_timeline_sequences and the active
            # timeline id can't be resolved → Program viewer stays black.
            # force_project_mode="edit_project" prevents a brand-new empty
            # project (no timelines yet) from being misclassified as
            # standalone_image, which would trigger a mode-mismatch warning
            # the next time it is opened in Video mode.
            ok_save = EditProjectIO().save_project(
                project_path,
                edit_project,
                timeline_sequences=getattr(self, "_current_timeline_sequences", None),
                force_project_mode="edit_project",
            )
            if not ok_save:
                raise RuntimeError("save_project returned False")
            self._current_edit_project_filepath = project_path
            self.save_router.set_project_path(project_path)
            from src.shared.project.library import register_project_library_dir
            register_project_library_dir(self.config_manager, location)
            self._add_to_recent_projects(project_path)
        except Exception as exc:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self,
                I18N.tr("pm.new_project", "New Project"),
                I18N.tr("pm.create_failed", "Failed to create the project: {error}").format(error=str(exc)),
            )
            return

        # Load the freshly created project so autosave / state are bound to it,
        # then close the manager.
        if self._open_project_path(project_path):
            manager_dialog.accept()

    def _open_recent_project_from_welcome(self, filepath: str) -> None:
        """Open a .topos project selected from the welcome dialog."""
        self._open_project_path(filepath)

    def _open_recent_from_welcome(self, filepath: str) -> None:
        """Open a file selected from the welcome dialog."""
        from src.shared.media.format_registry import is_video as _is_video_file
        # Image Grading mode rejects videos outright (no Edit page/timeline).
        if _is_video_file(filepath) and self._get_work_mode() == "image":
            self._reject_video_in_image_mode(filepath)
            return
        if _is_video_file(filepath):
            if self.current_page in ("Color", "Library"):
                self._reject_standalone_video_for_color(filepath)
                return
            self.load_video(filepath)
        else:
            self.load_image(filepath)

    def _on_update_available(self, version: str, url: str, changelog: str) -> None:
        """Show a non-modal update notification."""
        from src.shared.i18n.i18n import I18N
        msg = QMessageBox(
            QMessageBox.Icon.Information,
            I18N.tr("update.title", "Update Available"),
            I18N.tr("update.message",
                    "A new version {version} is available.\n\n{changelog}").format(
                version=version, changelog=changelog[:500]),
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Ignore,
            self,
        )
        msg.setWindowModality(Qt.WindowModality.NonModal)
        button = msg.exec()
        if button == QMessageBox.StandardButton.Open and url:
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(url))

    def _on_no_update(self) -> None:
        """[R0-4] 检查完成但无更新：状态栏短暂提示（此前完全不可见）。"""
        self.status_bar.showMessage(
            I18N.tr("update.no_update", "You are on the latest version"), 5000)

    def _on_update_error(self, message: str) -> None:
        """[R0-4] 更新检查失败：状态栏提示 + 日志（此前完全不可见）。"""
        logger.warning("Update check failed: %s", message)
        self.status_bar.showMessage(
            I18N.tr("update.check_failed", "Update check failed: {message}").format(
                message=message), 5000)

    def _rebuild_recent_menu(self) -> None:
        """Rebuild the Recent Files submenu from config."""
        self.recent_menu.clear()
        recent = self.config_manager.get("recent_files") or []
        if not recent:
            empty = self.recent_menu.addAction(
                I18N.tr("menu.no_recent_files", "No Recent Files")
            )
            empty.setEnabled(False)
            return

        # Image Grading mode cannot open videos, so filter them out of the
        # Recent Files list rather than showing entries that only produce a
        # rejection dialog when clicked.
        from src.shared.feature_flags import get_work_mode
        image_mode = get_work_mode(self.config_manager) == "image"
        if image_mode:
            try:
                from src.shared.media.format_registry import is_video as _is_video_file
                recent = [p for p in recent if not _is_video_file(p)]
            except Exception:
                pass

        if not recent:
            empty = self.recent_menu.addAction(
                I18N.tr("menu.no_recent_files", "No Recent Files")
            )
            empty.setEnabled(False)
            return

        for filepath in recent:
            action = self.recent_menu.addAction(os.path.basename(filepath))
            action.setToolTip(filepath)
            action.setData(filepath)
            action.triggered.connect(self._open_recent_file)

    def _rebuild_recent_projects_menu(self) -> None:
        """Rebuild the Recent Projects submenu from config."""
        if getattr(self, "recent_projects_menu", None) is None:
            return
        self.recent_projects_menu.clear()
        recent_projects = self.config_manager.get("recent_projects") or []
        if not recent_projects:
            empty = self.recent_projects_menu.addAction(
                I18N.tr("menu.no_recent_projects", "No Recent Projects")
            )
            empty.setEnabled(False)
            return

        for filepath in recent_projects:
            action = self.recent_projects_menu.addAction(os.path.basename(filepath))
            action.setToolTip(filepath)
            action.setData(filepath)
            action.triggered.connect(self._open_recent_project)

    def _add_to_recent_projects(self, filepath: str) -> None:
        """Add a project to the recent projects list and rebuild menu."""
        if not filepath:
            return
        recent_projects = list(self.config_manager.get("recent_projects") or [])
        max_count = self.config_manager.get("max_recent_projects") or 10
        # Remove duplicate if present, then prepend
        abs_path = os.path.abspath(filepath)
        recent_projects = [p for p in recent_projects if os.path.abspath(p) != abs_path]
        recent_projects.insert(0, filepath)
        recent_projects = recent_projects[:max_count]
        self.config_manager.set("recent_projects", recent_projects)
        # [R2-10] 记住上次打开的项目，供 Project Manager "继续上次项目"使用
        self.config_manager.set("last_project", abs_path)
        self._rebuild_recent_projects_menu()

    def _open_recent_project(self) -> None:
        """Open a project from the Recent Projects menu."""
        action = self.sender()
        if not action:
            return
        filepath = action.data()
        if filepath and os.path.exists(filepath):
            self._open_project_path(filepath)

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def setup_toolbar(self) -> None:
        toolbar = QToolBar("Main Toolbar")
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(toolbar)

        # Actions for Undo/Redo (Hidden from toolbar but exist for shortcuts)
        self.undo_action = QAction(I18N.tr("action.undo", "Undo"), self)
        self.undo_action.triggered.connect(self.undo)
        self.redo_action = QAction(I18N.tr("action.redo", "Redo"), self)
        self.redo_action.triggered.connect(self.redo)

        # === 创建工具栏主容器 ===
        toolbar_container = QWidget()
        main_layout = QHBoxLayout(toolbar_container)
        main_layout.setContentsMargins(10, 0, 10, 0)
        main_layout.setSpacing(0)

        # --- 菜单按钮区域（替代 Preferences 按钮） ---
        menu_button_style = """
            QToolButton {
                background-color: transparent; border: none; border-radius: 4px;
                color: #aaa; font-size: 12px; padding: 4px 10px; font-weight: bold;
            }
            QToolButton:hover { background-color: #3d3d3d; color: #fff; }
            QToolButton:pressed { background-color: #2d2d2d; }
            QToolButton::menu-indicator { image: none; }
        """

        menu_configs = [
            (self.menu_file, I18N.tr("menu.file", "&File")),
            (self.menu_edit, I18N.tr("menu.edit", "&Edit")),
            (self.menu_effects, I18N.tr("menu.effects", "&Effects")),
            (self.menu_window, I18N.tr("menu.window", "&Window")),
            (self.menu_help, I18N.tr("menu.help", "&Help")),
        ]

        for menu, label in menu_configs:
            # Remove & mnemonic prefix for toolbar display
            display_text = label.replace("&", "")
            btn = QToolButton()
            btn.setText(display_text)
            btn.setMenu(menu)
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            btn.setStyleSheet(menu_button_style)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            main_layout.addWidget(btn)

        # --- 左侧弹性间距，将 Page Switcher 推到中间 ---
        left_spacer = QWidget()
        left_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        main_layout.addWidget(left_spacer)

        # --- 中间 Page Switcher（居中突出显示） ---
        switcher_container = QWidget()
        switcher_layout = QHBoxLayout(switcher_container)
        switcher_layout.setContentsMargins(4, 0, 4, 0)
        switcher_layout.setSpacing(0)

        self.page_buttons = {}
        pages = ["Library", "Edit", "Color", "Composite", "Deliver"]
        # Filter out disabled modules
        if not self._composite_available:
            pages = [p for p in pages if p != "Composite"]
        if not self._edit_available:
            pages = [p for p in pages if p != "Edit"]
        if not self._deliver_available:
            pages = [p for p in pages if p != "Deliver"]
        for idx, page in enumerate(pages):
            page_key = f"page.{page.lower()}"
            page_text = I18N.tr(page_key, page).upper()
            btn = QPushButton(page_text)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

            # 根据位置设置圆角：第一个左圆角，最后一个右圆角，中间无圆角
            if len(pages) == 1:
                radius = "border-radius: 8px;"
            elif idx == 0:
                radius = "border-top-left-radius: 8px; border-bottom-left-radius: 8px;"
            elif idx == len(pages) - 1:
                radius = "border-top-right-radius: 8px; border-bottom-right-radius: 8px;"
            else:
                radius = "border-radius: 0px;"

            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgb(70, 70, 77); border: none;
                    color: #bbb; font-size: 12px; font-weight: bold;
                    padding: 6px 16px; {radius}
                }}
                QPushButton:hover {{ color: #fff; background-color: #3d3d3d; }}
                QPushButton:checked {{
                    color: #fff;
                    background-color: rgb(204, 102, 0);
                }}
            """)
            btn.clicked.connect(lambda checked, p=page: self.switch_to_page(p))
            if page == self.current_page:
                btn.setChecked(True)
            switcher_layout.addWidget(btn)
            self.page_buttons[page] = btn

        # 外框容器
        switcher_container.setStyleSheet("""
            QWidget {
                background-color: transparent;
                border: none;
            }
        """)
        main_layout.addWidget(switcher_container)

        # --- 右侧弹性间距 ---
        right_spacer = QWidget()
        right_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        main_layout.addWidget(right_spacer)

        # --- 右侧工具区域（添加到工具栏末尾）---
        # Reset button (放在最前面)
        self.reset_all_btn = QPushButton()
        self.reset_all_btn.setIcon(icon_reset())
        self.reset_all_btn.setToolTip(I18N.tr("action.reset_all_layers", "Reset All Layer Adjustments"))
        self.reset_all_btn.setFixedSize(28, 28)
        self.reset_all_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent; border: none; border-radius: 4px;
            }
            QPushButton:hover { background-color: #3d3d3d; }
            QPushButton:pressed { background-color: #2d2d2d; }
        """)
        self.reset_all_btn.clicked.connect(self.on_reset_all_click)
        main_layout.addWidget(self.reset_all_btn)

        # Preferences button
        self.pref_btn = QPushButton()
        self.pref_btn.setIcon(icon_settings())
        self.pref_btn.setToolTip(I18N.tr("action.preferences", "Preferences..."))
        self.pref_btn.setFixedSize(28, 28)
        self.pref_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent; border: none; border-radius: 4px;
            }
            QPushButton:hover { background-color: #3d3d3d; }
            QPushButton:pressed { background-color: #2d2d2d; }
        """)
        self.pref_btn.clicked.connect(self.show_preferences_dialog)
        main_layout.addWidget(self.pref_btn)

        # Project Manager button — video mode only. It remains available after
        # the startup manager is closed, matching the File menu action.
        if self._is_video_work_mode():
            self.project_manager_btn = QPushButton()
            self.project_manager_btn.setIcon(icon_home())
            self.project_manager_btn.setToolTip(
                I18N.tr("action.open_project_manager", "Open Project Manager...")
            )
            self.project_manager_btn.setAccessibleName(
                I18N.tr("action.open_project_manager", "Open Project Manager...")
            )
            self.project_manager_btn.setFixedSize(28, 28)
            self.project_manager_btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent; border: none; border-radius: 4px;
                }
                QPushButton:hover { background-color: #3d3d3d; }
                QPushButton:pressed { background-color: #2d2d2d; }
            """)
            self.project_manager_btn.clicked.connect(self._show_project_manager)
            main_layout.addWidget(self.project_manager_btn)
        main_layout.addSpacing(8)

        # Layout combo
        self.layout_combo = QComboBox()
        self.layout_combo.setFixedWidth(130)
        self.layout_combo.setToolTip(I18N.tr("workspace.select_layout", "Select Workspace Layout"))
        self.layout_combo.setStyleSheet("""
            QComboBox {
                background-color: #252529; border: 1px solid #444; border-radius: 12px;
                padding: 2px 12px; color: #aaa; font-size: 11px; font-weight: bold;
            }
            QComboBox:hover { background-color: #333; border: 1px solid #ff8800; color: #eee; }
            QComboBox::drop-down { border: 0; width: 20px; background: transparent; subcontrol-origin: padding; subcontrol-position: right; }
        """)
        apply_combo_dropdown_style(self.layout_combo)
        self.layout_combo.activated.connect(self.on_layout_combo_activated)
        main_layout.addWidget(self.layout_combo)
        main_layout.addSpacing(8)
        self.update_layout_combobox()

        # Export button
        self.export_btn = QPushButton(I18N.tr("action.export", "Export"))
        self.export_btn.setToolTip(I18N.tr("tooltip.export", "导出图像"))
        self.export_btn.setStyleSheet("""
            QPushButton {
                background-color: #cc6600; border: none; border-radius: 4px;
                color: white; font-size: 12px; font-weight: bold; padding: 6px 16px;
            }
            QPushButton:hover { background-color: #e67300; }
            QPushButton:pressed { background-color: #b35900; }
        """)
        self.export_btn.clicked.connect(self.export_file_as)
        main_layout.addWidget(self.export_btn)

        # 将容器添加到工具栏
        toolbar.addWidget(toolbar_container)

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def setup_statusbar(self) -> None:
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        # Progress Bar for long operations
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(12)
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #444;
                border-radius: 6px;
                background-color: #222;
                margin-right: 10px;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
                border-radius: 5px;
            }
        """)
        self.progress_bar.hide()
        self.status_bar.addPermanentWidget(self.progress_bar)

        # Timeline video exports continue after the Quick Export dialog
        # closes.  Keep their controls in the status bar so cancel/pause are
        # available without reopening the dialog or switching pages.
        self._video_export_pause_btn = QPushButton(
            I18N.tr("export.pause", "暂停")
        )
        self._video_export_pause_btn.setFixedHeight(22)
        self._video_export_pause_btn.setToolTip(
            I18N.tr("export.pause_video_tooltip", "暂停/继续视频导出")
        )
        self._video_export_pause_btn.clicked.connect(self._toggle_video_export_pause)
        self._video_export_pause_btn.hide()
        self.status_bar.addPermanentWidget(self._video_export_pause_btn)

        self._video_export_cancel_btn = QPushButton(
            I18N.tr("export.cancel", "取消")
        )
        self._video_export_cancel_btn.setFixedHeight(22)
        self._video_export_cancel_btn.setToolTip(
            I18N.tr("export.cancel_video_tooltip", "取消视频导出")
        )
        self._video_export_cancel_btn.clicked.connect(self._cancel_video_export)
        self._video_export_cancel_btn.hide()
        self.status_bar.addPermanentWidget(self._video_export_cancel_btn)

        # GPU info label (shown when show_gpu_info is enabled)
        if self.config_manager.get("show_gpu_info"):
            try:
                from src.shared.ai_models.onnx_env import get_gpu_provider_info
                gpu_info = get_gpu_provider_info()
                # AUD-3000：QLabel 此前未导入，NameError 被下方宽泛 except
                # 吞掉——GPU 信息标签从不显示且失败不可见。导入已补齐；
                # 其余失败（如 onnx_env 不可用）记录原因，不再静默。
                gpu_label = QLabel(f"GPU: {gpu_info.get('description', 'N/A')}")
                gpu_label.setStyleSheet("color: #888; font-size: 11px; margin-right: 8px;")
                self.status_bar.addPermanentWidget(gpu_label)
            except Exception:
                logger.debug("GPU info label unavailable", exc_info=True)

        # AUD-3001: panel_opacity 此前只在偏好对话框里有瞬时预览，全库
        # 无任何启动应用路径（保存的设置重启后不生效）。docks 由
        # setup_docks() 先于 setup_statusbar() 创建，此处统一套用持久值。
        raw_panel_opacity = self.config_manager.get("panel_opacity")
        if not isinstance(raw_panel_opacity, (int, float)):
            raw_panel_opacity = 100
        self._wire_panel_opacity_reapply()
        self.apply_panel_opacity(float(raw_panel_opacity) / 100.0)

        self.status_bar.showMessage("Ready")

    # ------------------------------------------------------------------
    # Interaction tracking
    # ------------------------------------------------------------------

    def on_dependency_report_requested(self) -> None:
        """P1-COLOR-23: File 菜单「工程依赖报告」— 扫描 LUT/插件/AI 模型。

        AUD-3010: ``build_dependency_report`` 的文件系统扫描移到线程池
        worker；期间防重入（重复触发给出状态栏提示），报告就绪后经 queued
        信号回 UI 线程弹对话框，失败以 QMessageBox 提示并留日志。
        """

        project = getattr(self, "_current_edit_project", None)
        if project is None:
            QMessageBox.information(
                self,
                I18N.tr("deps.title", "工程依赖报告"),
                I18N.tr("deps.no_project", "当前没有打开的工程。"),
            )
            return

        if getattr(self, "_dependency_report_inflight", False):
            self.status_bar.showMessage(
                I18N.tr("deps.scanning", "正在扫描工程依赖…"), 2000)
            return

        base_dir = None
        try:
            path = self.save_router.get_current_project_path()
            if path:
                base_dir = os.path.dirname(path)
        except Exception:
            base_dir = None

        self._dependency_report_inflight = True
        worker = _DependencyReportWorker(project, base_dir, parent=self)
        worker.signals.ready.connect(self._on_dependency_report_ready)
        worker.signals.finished.connect(self._on_dependency_report_finished)
        self._dependency_report_worker = worker  # 持引用防 GC
        QThreadPool.globalInstance().start(worker)
        self.status_bar.showMessage(
            I18N.tr("deps.scanning", "正在扫描工程依赖…"))

    def _on_dependency_report_ready(self, report: Any) -> None:
        """AUD-3010: UI 线程展示依赖报告（worker 经 queued 信号送达）。"""
        from src.features.color.ui.dependency_report_dialog import (
            DependencyReportDialog,
        )

        if report is None:
            QMessageBox.warning(
                self,
                I18N.tr("deps.title", "工程依赖报告"),
                I18N.tr("deps.failed", "依赖扫描失败，请查看日志。"),
            )
            return
        DependencyReportDialog(self, report).exec()

    def _on_dependency_report_finished(self) -> None:
        """AUD-3010: 扫描 worker 结束（主线程清防重入标志与引用）。"""
        self._dependency_report_inflight = False
        self._dependency_report_worker = None

    def on_interaction_started(self) -> None:
        self.idle_render_timer.stop()
        self._freeze_curves_scope_for_interaction()
        itype = "general"
        if self.sender() == self.canvas: itype = self.canvas.current_interaction_type
        # set_interaction_mode already submits the first preview render.  A
        # second request here only competes with the first at gesture start.
        self.engine.set_interaction_mode(True, itype)

        # 保存历史状态，用于撤销调色操作
        if self.engine.img_full_original is not None:
            self.engine.history.push_state(
                self.engine.img_full_original,
                self.engine.layers,
                self.engine.active_layer_index,
                copy_image=False,
            )

    def on_interaction_ended(self) -> None:
        # Mask controls throttle UI and model delivery independently. Commit
        # their final value before autosave and before the next undo gesture.
        if getattr(self, "pending_mask_updates", None):
            self.process_mask_update()
        self.idle_render_timer.start(); self._trigger_autosave()

        # Optimization: Do NOT trigger full quality decode here synchronously.
        # Let perform_idle_render (which is triggered by idle_render_timer)
        # handle it after a short delay (500ms) to keep UI responsive.

    # ------------------------------------------------------------------
    # Module menu contribution system
    # ------------------------------------------------------------------

    def _register_module_menu_actions(self) -> None:
        """Register menu actions from enabled feature modules via ActionRegistry.

        Iterates over registered feature modules and calls
        ``register_menu_actions`` on any that provide it.  The registry
        itself is stored as ``self._action_registry`` on MainWindow.

        Also calls ``register_shortcuts`` on each module so feature-specific
        shortcuts carry the correct ``feature_id`` for filtering.
        """
        if not hasattr(self, '_action_registry') or self._action_registry is None:
            return

        if not hasattr(self, 'feature_registry') or not self.feature_registry:
            return

        for feature_id in self.feature_registry.get_available_features():
            module = self.feature_registry._modules.get(feature_id)
            if module and hasattr(module, 'register_menu_actions'):
                try:
                    module.register_menu_actions(self._action_registry)
                except Exception as exc:
                    logger.warning(
                        "Failed to register menu actions for %s: %s",
                        feature_id, exc,
                    )

        # Register feature-specific shortcuts so they carry feature_id
        # for proper filtering in ShortcutDialog.
        for fid, mod in self.feature_registry._modules.items():
            if hasattr(mod, 'register_shortcuts'):
                try:
                    mod.register_shortcuts(self.shortcut_manager)
                except Exception as exc:
                    logger.warning(
                        "Failed to register shortcuts for %s: %s",
                        fid, exc,
                    )

    def _build_module_menus(self) -> None:
        """Build feature-specific menu items from ActionRegistry contributions.

        Uses MenuBuilder to convert MenuActionDescriptors into actual
        QActions and insert them into the appropriate menus.
        """
        from src.app.menu.menu_builder import MenuBuilder

        if not hasattr(self, '_action_registry') or self._action_registry is None:
            return

        builder = MenuBuilder(
            menu_bar=self.menuBar(),
            action_registry=self._action_registry,
            callback_resolver=self._resolve_menu_callback,
            shortcut_manager=self.shortcut_manager,
        )

        # Register existing menus so the builder can add items to them.
        builder.register_existing_menu("File", self.menu_file)
        if hasattr(self, 'menu_edit') and self.menu_edit:
            builder.register_existing_menu("Edit", self.menu_edit)
        if hasattr(self, 'menu_color') and self.menu_color:
            builder.register_existing_menu("Color", self.menu_color)
        if hasattr(self, 'menu_composite') and self.menu_composite:
            builder.register_existing_menu("Composite", self.menu_composite)
        if hasattr(self, 'menu_deliver') and self.menu_deliver:
            builder.register_existing_menu("Deliver", self.menu_deliver)

        # Determine enabled features for filtering.
        enabled = None
        if hasattr(self, 'feature_flags') and self.feature_flags:
            enabled = set(self.feature_flags.get_enabled_features())

        # Build with an insert-before anchor for module actions that belong
        # in File (currently Export).  Project lifecycle actions are owned by
        # the app-level menu and are intentionally not feature contributions.
        insert_before = {}
        if hasattr(self, '_file_export_anchor'):
            insert_before["File"] = self._file_export_anchor
        self._module_actions = builder.build(enabled, insert_before)
        self._scope_timeline_delete_shortcuts()
        self._sync_checkable_module_actions()

    def _scope_timeline_delete_shortcuts(self) -> None:
        """Keep timeline Delete actions local to the timeline view.

        The media pool has its own Delete/Backspace handlers.  If these menu
        actions remain window-scoped, Qt sees two handlers for the same key
        when a media-pool child has focus and reports an ambiguous shortcut;
        neither handler is then invoked.  The timeline view already owns the
        corresponding key operation, so the menu actions only need to be
        active while that view is focused.
        """
        timeline_panel = getattr(self, "timeline_panel", None)
        timeline_view = getattr(timeline_panel, "timeline_view", None)
        actions = getattr(self, "_module_actions", None) or {}
        if timeline_view is None:
            return

        for action_id in (
            "edit.timeline.delete",
            "edit.timeline.ripple_delete",
        ):
            action = actions.get(action_id)
            if action is None:
                continue
            action.setProperty("force_widget_shortcut", True)
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            if action not in timeline_view.actions():
                timeline_view.addAction(action)

    def _sync_checkable_module_actions(self) -> None:
        """Sync checkable feature menu actions with their persisted state.

        B1: the Scene Cut Overlay toggle lives in ConfigManager (written by
        EditPage on every change).  QActions created by MenuBuilder start
        unchecked, so restore the saved state here at build time; afterwards
        EditPage's ``scene_cut_overlay_visibility_changed`` signal keeps the
        action in sync at runtime.
        """
        actions = getattr(self, "_module_actions", None)
        if not actions:
            return
        sync_specs = (
            ("edit.timeline.scene_cut_overlay", "edit_scene_cut_overlay_visible"),
        )
        config = None
        for action_id, config_key in sync_specs:
            action = actions.get(action_id)
            if action is None or not action.isCheckable():
                continue
            if config is None:
                try:
                    from src.shared.config.config_manager import ConfigManager

                    config = ConfigManager()
                except Exception:  # noqa: BLE001
                    config = False
            if not config:
                continue
            try:
                action.setChecked(bool(config.get(config_key)))
            except Exception:  # noqa: BLE001
                logger.debug("sync checkable action %s failed", action_id)

    def _resolve_menu_callback(self, callback_name: str):
        """Resolve a callback name to an actual method on this object."""
        return getattr(self, callback_name, None)

    # ------------------------------------------------------------------
    # 升级计划 M 面板动作（经 upgrade_panel_actions 统一注册表实体接线）
    # ------------------------------------------------------------------

    def _upgrade_panel_context(self) -> dict:
        """收集面板构造上下文（工程/时间线/引擎——从主窗口状态取）。"""
        project = getattr(self, "_current_edit_project", None) \
            or getattr(self, "current_edit_project", None)
        timeline = None
        try:
            if project is not None and getattr(
                    project, "active_timeline_id", None):
                timeline = (project.timelines or {}).get(
                    project.active_timeline_id)
        except Exception:  # noqa: BLE001 — 上下文尽力而为
            timeline = None
        engine = getattr(self, "engine", None) \
            or getattr(self, "image_engine", None)
        return {"project": project, "timeline": timeline, "engine": engine}

    # ── N24 · 升级面板共用宿主（生命周期+提交 adapter+过期守卫） ──

    @property
    def _panel_host(self):
        """UpgradePanelHost 懒建（主窗口拥有；全菜单回调共用）。"""
        host = getattr(self, "__upgrade_panel_host", None)
        if host is None:
            from src.app.controllers.upgrade_panel_host import (
                UpgradePanelHost,
            )
            host = UpgradePanelHost(self)
            self.__upgrade_panel_host = host
        return host

    def _open_upgrade_panel(self, action_id: str, **extra) -> None:
        """经共用宿主打开面板（N24：单实例/引用保持/过期守卫/失败明示）。

        result_handler 经 extra.pop 由调用方注入；accepted 后宿主回调
        （处理器内先 ``host.guard(panel)`` 再提交，换工程拒绝旧写入）。
        """
        result_handler = extra.pop("result_handler", None)
        context = self._upgrade_panel_context()
        context.update(extra)
        self._panel_host.open_panel(
            action_id, context, result_handler=result_handler)

    def _submit_shot_match_result(self, panel) -> bool:
        """镜头匹配 accepted → 守卫 → 主工程命令栈（N10 提交链）。"""
        host = self._panel_host
        if not host.guard(panel):
            return False
        command = getattr(panel, "applied_command", None)
        if command is None:
            return False
        return host.project_command_executor()(command)

    # 各动作的菜单回调（MenuActionDescriptor.callback_name 对应项）
    def on_open_shot_match_panel(self) -> None:
        """M22.2b：批量镜头匹配面板。"""
        ctx = self._upgrade_panel_context()
        project, timeline = ctx["project"], ctx["timeline"]
        if project is None or timeline is None:
            self._upgrade_panel_unavailable("镜头匹配需要已加载的工程与时间线")
            return
        clip_ids = [
            clip.clip_id
            for track in (getattr(timeline, "video_tracks", ()) or ())
            for clip in (getattr(track, "clips", ()) or ())]
        source, clip_frames = self._build_shot_match_frame_source(
            project, timeline)
        self._open_upgrade_panel(
            "color.shot_match", project=project, timeline=timeline,
            engine=ctx["engine"], clip_ids=clip_ids,
            frame_source=source,
            clip_frame_fn=clip_frames,
            result_handler=self._submit_shot_match_result)

    def _build_shot_match_frame_source(self, project, timeline):
        """N10 · 逐镜头真实帧源（解码管理器 + YUV→RGB float）。

        Returns:
            (frame_source, clip_frame_fn)——frame_source(clip_id, frame)
            → ndarray(H,W,3) RGB float（scene-linear 口径经
            color_decode.yuv_to_rgb）；clip_frame_fn 取剪辑源入点为代
            表帧。解码管理器复用窗口生命周期单例（与编辑页同模式）。
        """
        clip_map = {}
        for track in (getattr(timeline, "video_tracks", ()) or ()):
            for clip in (getattr(track, "clips", ()) or ()):
                cid = getattr(clip, "clip_id", None)
                aid = getattr(clip, "asset_id", None)
                if cid and aid:
                    clip_map[str(cid)] = (
                        aid, int(getattr(clip, "source_start", 0) or 0))

        def clip_frame_fn(clip_id):
            entry = clip_map.get(str(clip_id))
            return entry[1] if entry else 0

        assets = getattr(project, "assets", None) or {}
        manager = getattr(self, "_shot_match_decode_manager", None)
        if manager is None:
            try:
                from src.features.edit.core.edit_gpu_decode_manager import (
                    EditGpuDecodeManager,
                )
                manager = EditGpuDecodeManager()
            except Exception:  # noqa: BLE001 —— 解码栈不可用即无真实源
                manager = None
            self._shot_match_decode_manager = manager
        if manager is None:
            return None, clip_frame_fn

        from src.shared.color.types import ColorMatrix, ColorRange
        from src.shared.media.color_decode import ColorConverter
        decoder = ColorConverter()

        def frame_source(clip_id, frame):
            entry = clip_map.get(str(clip_id))
            if entry is None:
                raise ValueError(f"镜头 {clip_id} 不在当前时间线")
            asset = assets.get(entry[0])
            path = getattr(asset, "path", None) if asset else None
            if not path:
                raise ValueError(f"镜头 {clip_id} 素材路径缺失")
            decoded, _ftype = manager.get_frame(str(path), int(frame))
            planes = getattr(decoded, "planes", None) if decoded else None
            if not planes or len(planes) < 3:
                raise ValueError(f"镜头 {clip_id} 帧 {frame} 解码失败")
            metadata = getattr(asset, "metadata", None) or {}
            matrix_name = str(metadata.get("color_matrix") or "").lower()
            matrix = (ColorMatrix.BT2020_NC if "2020" in matrix_name
                      else ColorMatrix.BT601 if "601" in matrix_name
                      else ColorMatrix.BT709)
            depth = int(planes[0].dtype.itemsize * 8) \
                if planes[0].dtype.kind in "ui" else 8
            return decoder.yuv_to_rgb(
                planes[0], planes[1], planes[2],
                matrix, ColorRange.LIMITED, bit_depth=depth)

        return frame_source, clip_frame_fn

    def on_open_version_tools_panel(self) -> None:
        """M07.3：调色版本工具面板（活动栈；命令入主工程栈，N24/N04）。"""
        ctx = self._upgrade_panel_context()
        project = ctx["project"]
        if project is None:
            self._upgrade_panel_unavailable("版本工具需要已加载的工程")
            return
        stack_id = self._active_color_stack_id(project)
        if not stack_id:
            self._upgrade_panel_unavailable("当前无活动调色栈")
            return
        # N04 前置接线：面板命令不再落私有副本，统一经宿主 adapter 入
        # media_pool 命令栈（save/activate/lock 可撤销、主工程回读）。
        self._open_upgrade_panel(
            "color.version_tools", project=project, stack_id=stack_id,
            command_executor=self._panel_host.project_command_executor())

    def on_open_filler_removal_panel(self) -> None:
        """M11.3/N07：删除口头语——词级字幕在库即真实进入。

        旧实现恒提示"从转写服务入口启动"（永不可达的占位）。现从当前
        时间线字幕轨提取词级时间戳（clip.caption_metadata.word_timings），
        有词级数据即检测并打开面板；计划经 on_plan_ready 走真实事务
        （compile → revision 守卫 → 时间线命令栈一次 Undo）。
        """
        ctx = self._upgrade_panel_context()
        project, timeline = ctx["project"], ctx["timeline"]
        if project is None or timeline is None:
            self._upgrade_panel_unavailable(
                "删除口头语需要已加载的工程与时间线")
            return
        timings = self._extract_word_timings(timeline)
        if not timings:
            self._upgrade_panel_unavailable(
                "当前时间线无词级时间戳字幕——请先转写生成词级字幕")
            return
        from src.features.edit.ai_editing.filler_removal import (
            detect_filler_words,
        )
        detection = detect_filler_words(timings)
        if not detection.hits and not detection.review_hits:
            self._upgrade_panel_unavailable("未检出候选口头语（词表内无命中）")
            return
        base_revision = int(getattr(project, "revision", 0) or 0)
        self._open_upgrade_panel(
            "edit.filler_removal", detection=detection, project=project,
            timeline=timeline, base_revision=base_revision,
            on_plan_ready=lambda plan: self._apply_filler_plan(
                plan, base_revision,
                str(getattr(timeline, "timeline_id", ""))))

    def _extract_word_timings(self, timeline) -> list:
        """字幕轨全部 clip 的词级时间戳（时间线帧域，按出现顺序）。"""
        timings: list = []
        for track in (getattr(timeline, "subtitle_tracks", ()) or ()):
            for clip in (getattr(track, "clips", ()) or ()):
                meta = getattr(clip, "caption_metadata", None)
                words = getattr(meta, "word_timings", None) or ()
                timings.extend(words)
        return timings

    def _apply_filler_plan(self, plan, base_revision: int,
                           timeline_id: str) -> None:
        """N07 · PREVIEW 计划 → 真实事务（编译+revision 守卫+一次 Undo）。"""
        project = getattr(self, "_current_edit_project", None)
        current = int(getattr(project, "revision", -1) or -1)
        if project is None or current != base_revision:
            self._upgrade_panel_unavailable(
                f"工程已变化（计划基于 revision {base_revision}，当前 "
                f"{current}）——计划作废，请重新检测")
            return
        from src.features.edit.ai_editing.plan_compiler import (
            CompileError, compile_plan,
        )
        try:
            compiled = compile_plan(plan)
        except CompileError as exc:
            self._upgrade_panel_unavailable(f"删除计划编译失败：{exc}")
            return
        ok = self._panel_host.timeline_command_executor(timeline_id)(
            compiled.macro)
        if ok:
            logger.info("[FillerRemoval] applied plan %s (%d ops)",
                        compiled.plan_id, compiled.operation_count)

    def on_open_snapshot_compare_panel(self) -> None:
        """M15.1b：工程快照比较面板。"""
        ctx = self._upgrade_panel_context()
        if ctx["project"] is None:
            self._upgrade_panel_unavailable("快照比较需要已加载的工程")
            return
        self._open_upgrade_panel(
            "project.snapshot_compare", project=ctx["project"])

    def on_open_proxy_cleanup_panel(self) -> None:
        """M12.2b：代理与缓存管理面板。"""
        manager = getattr(self, "_proxy_manager", None)
        if manager is None:
            # 从文件控制器取共享 manager（懒建）
            fc = getattr(self, "file_controller", None)
            manager = getattr(fc, "proxy_manager", None) if fc else None
        if manager is None:
            self._upgrade_panel_unavailable("代理服务不可用")
            return
        self._open_upgrade_panel("project.proxy_cleanup",
                                 proxy_manager=manager)

    def on_open_tracker_corrections_panel(self) -> None:
        """M23.1c/N13：跟踪修正面板（选中 Tracker 节点）。

        N13 接线：undo_sink 接 Composite 页 UndoStack（修正/重跟合并
        可撤销一次）；retrack_fn 由真实 CPU 引擎 + 节点上游逐帧源构成
        ——逐帧源 API 在生产页尚缺（诚实前提），可用时经
        ``get_node_input_frame(node_id, frame)`` 探测接入，缺省面板
        明示"未提供跟踪引擎回调"不冒充。
        """
        node = self._selected_tracker_node()
        if node is None:
            self._upgrade_panel_unavailable(
                "请先在 Composite 页选中一个 Tracker 节点")
            return
        self._open_upgrade_panel(
            "composite.tracker_corrections", tracker_node=node,
            retrack_fn=self._build_tracker_retrack_fn(node),
            on_changed=self._tracker_view_refresh(),
            undo_sink=self._tracker_undo_sink())

    # ── N13 上下文 ──

    def _composite_host(self):
        comp = getattr(self, "composite_controller", None) \
            or getattr(self, "composite_page", None)
        return comp

    def _tracker_undo_sink(self):
        """把面板 track_data 变更注册到 Composite 页 UndoStack。"""
        comp = self._composite_host()
        stack = getattr(comp, "_undo_stack", None)
        if stack is None:
            inner = getattr(comp, "composite_page", None)
            stack = getattr(inner, "_undo_stack", None) if inner else None
        if stack is None:
            return None

        def sink(description, undo_fn, redo_fn) -> None:
            from src.features.composite.core.undo_stack import UndoAction
            stack.push(UndoAction(
                "tracker_correction", {"description": description},
                undo_fn, redo_fn, description=description))

        return sink

    def _tracker_view_refresh(self):
        comp = self._composite_host()
        for name in ("request_render", "schedule_render", "refresh_graph"):
            fn = getattr(comp, name, None)
            if callable(fn):
                return lambda fn=fn: fn()
        return lambda: None  # 视图刷新尽力而为（无页面宿主时无操作）

    def _build_tracker_retrack_fn(self, node):
        """真实 CPU 引擎 + 节点上游逐帧源（缺逐帧源 API 时如实 None）。

        模板窗从锚帧尺寸 + 节点归一化参数（center/size）派生像素坐标。
        """
        comp = self._composite_host()
        frame_getter = getattr(comp, "get_node_input_frame", None)
        if not callable(frame_getter):
            return None  # 生产页无逐帧源 API——面板明示，不伪造回调
        from src.features.composite.core.tracker_corrections import (
            retrack_translation_segment,
        )
        params = dict(getattr(node, "parameters", {}) or {})

        def retrack_fn(segment, is_cancelled=None):
            def getter(frame):
                return frame_getter(node.node_id, frame)

            anchor_img = getter(segment.anchor_frame)
            if anchor_img is None:
                return {}, "anchor frame unavailable"
            height, width = anchor_img.shape[:2]
            cx = float(params.get("center_x", 0.5) or 0.5) * width
            cy = float(params.get("center_y", 0.5) or 0.5) * height
            pw = max(8, int(float(params.get("size_x", 0.12) or 0.12)
                            * width))
            ph = max(8, int(float(params.get("size_y", 0.12) or 0.12)
                            * height))
            rect = (int(cx - pw / 2), int(cy - ph / 2), pw, ph)
            return retrack_translation_segment(
                getter, segment, rect, is_cancelled=is_cancelled)

        return retrack_fn

    def on_open_bake_dialog(self) -> None:
        """M08.2c/N05：Bake——选中节点为目标，真实渲染回调端到端。

        链路：UI 线程**预建全帧 NodeEngine snapshot**（snapshot 契约
        须 UI 线程；帧域=活动时间线时长）→ worker 内
        CompositeExportProvider.render_full_res 逐帧渲染 → EXR 落盘
        （execute_bake 六阶段事务：freeze→render→verify→source_changed
        →publish→rewrite）→ 图改写经 rewrite_fn 回 UI 线程（复合页
        UndoStack 一次撤销）→ 对话框经宿主打开并真正 start()。
        """
        context = self._build_bake_context()
        if context is None:
            return  # 原因已用户可见（_upgrade_panel_unavailable）
        self._open_upgrade_panel("composite.bake", **context)
        dialog = self._panel_host.active_panel("composite.bake")
        if dialog is not None:
            dialog.start()  # 真正启动 worker（非 stub）

    def _build_bake_context(self):
        """N05 · Bake 上下文（快照预建+真实 render_fn+帧域+改写回调）。

        Returns:
            dict（composite.bake 工厂入参）；前置缺失时 None（原因已
            用户可见）。
        """
        import tempfile
        node = self._selected_tracker_node()
        target_id = getattr(node, "node_id", None)
        if target_id is None:
            self._upgrade_panel_unavailable("请先选中要烘焙的节点")
            return None
        comp = self._composite_host()
        ng = getattr(comp, "node_graph", None) \
            or getattr(comp, "engine", None)
        snapshot = getattr(ng, "snapshot", None)
        if ng is None or not callable(snapshot):
            self._upgrade_panel_unavailable(
                "Composite 图不可用（无 node_graph 快照入口）——无法烘焙")
            return None
        ctx = self._upgrade_panel_context()
        timeline = ctx["timeline"]
        if timeline is None:
            self._upgrade_panel_unavailable("烘焙需要已加载的时间线")
            return None
        fps = float(getattr(timeline, "fps", 24.0) or 24.0)
        width = int(getattr(timeline, "width", 1920) or 1920)
        height = int(getattr(timeline, "height", 1080) or 1080)
        # 帧域 = 活动时间线时长（全部轨道最右端帧）
        total = 0
        for group in ("video_tracks", "audio_tracks"):
            for track in (getattr(timeline, group, ()) or ()):
                for clip in (getattr(track, "clips", ()) or ()):
                    end = int(getattr(clip, "timeline_start", 0) or 0) + int(
                        getattr(clip, "timeline_duration", 0) or 0)
                    total = max(total, end)
        if total <= 0:
            self._upgrade_panel_unavailable("时间线为空——无烘焙区间")
            return None
        frame_start, frame_end = 0, total - 1
        # 预建全帧快照（UI 线程——snapshot 契约；拓扑/参数快照为轻量
        # 引用，不随帧数膨胀像素内存）
        snapshots = {}
        for frame in range(frame_start, frame_end + 1):
            try:
                snapshots[frame] = snapshot(
                    color_texture_id=0, color_width=width,
                    color_height=height, frame_number=frame, fps=fps)
            except Exception:  # noqa: BLE001 — 快照失败=烘焙前置缺失
                self._upgrade_panel_unavailable(
                    f"图快照构建失败（帧 {frame}）——无法烘焙")
                return None

        from src.features.composite.export_provider import (
            CompositeExportProvider,
        )
        provider = CompositeExportProvider()

        def render_fn(node_id, frame, out_path):
            """worker 线程：预建快照 → 全分辨率渲染 → EXR 落盘。"""
            snap = snapshots.get(int(frame))
            if snap is None:
                return False
            result = provider.render_full_res(snapshot=snap)
            image = getattr(result, "image", None) if result else None
            if image is None:
                return False
            import cv2
            import numpy as np
            arr = np.asarray(image)
            if arr.dtype != np.float32:
                arr = arr.astype(np.float32)
            try:
                return bool(cv2.imwrite(str(out_path), arr))
            except Exception:  # noqa: BLE001 — 写盘失败=该帧失败
                return False

        def rewrite_fn(result):
            """UI 线程：发布成功后图改写（复合页 UndoStack 一次撤销）。"""
            from src.features.composite.bake_service import apply_bake_rewrite
            plan = getattr(result, "plan", None)
            if plan is None:
                return False
            apply_bake_rewrite(ng, plan)
            return True

        return {
            "graph": ng,
            "target_id": str(target_id),
            "output_dir": tempfile.mkdtemp(prefix="topos-bake-"),
            "frame_start": frame_start,
            "frame_end": frame_end,
            "render_fn": render_fn,
            "rewrite_fn": rewrite_fn,
        }

    def on_open_multi_version_qc_panel(self) -> None:
        """M21.2c：多版本交付 QC 面板（呈现最近一次批量测量结果）。

        面板只呈现已测结果（M21.2b producer 输出），不在此发起测量；
        宿主可经 ``_last_multi_version_qc`` 属性缓存最近报告
        （{lufs: …, subtitle: …}），无缓存时如实显示"无数据"。
        """
        cached = getattr(self, "_last_multi_version_qc", None) or {}
        self._open_upgrade_panel(
            "deliver.multi_version_qc",
            lufs_report=cached.get("lufs") or {},
            subtitle_summary=cached.get("subtitle") or {})

    # ── 上下文辅助 ──

    def _upgrade_panel_unavailable(self, message: str) -> None:
        try:
            from src.shared.i18n.i18n import I18N
            # 无 QApplication（headless 测试）时 QMessageBox 会硬崩——
            # 先查实例，退化为纯日志
            from PyQt6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance()
            if app is None:
                logger.info("面板不可用（headless）：%s", message)
                return
            # N24：非模态 show——菜单回调绝不进模态 exec（headless/
            # offscreen 下 information() 会永久阻塞主线程；桌面用户仍
            # 得到可见提示，且不被对话框打断工作流）
            box = QMessageBox(
                QMessageBox.Icon.Information,
                I18N.tr("panel.unavailable_title", "功能不可用"),
                I18N.tr("panel.unavailable", message))
            box.setAttribute(
                __import__("PyQt6.QtCore", fromlist=["Qt"])
                .Qt.WidgetAttribute.WA_DeleteOnClose)
            box.show()
        except Exception:  # noqa: BLE001 — 提示失败只剩日志
            logger.info("面板不可用：%s", message)

    def _active_color_stack_id(self, project) -> "str | None":
        """当前活动调色栈 id（Color 会话优先，退化到任一栈）。"""
        session = getattr(self, "color_session", None)
        if session is not None:
            sid = getattr(session, "color_stack_id", None)
            if sid:
                return sid
        stacks = getattr(project, "color_stacks", None) or {}
        return next(iter(stacks), None) if stacks else None

    def _selected_tracker_node(self):
        """当前选中的 Composite 节点（Tracker 修正/Bake 共用）。"""
        comp = getattr(self, "composite_controller", None) \
            or getattr(self, "composite_page", None)
        for getter_name in ("selected_node", "get_selected_node",
                            "current_node"):
            getter = getattr(comp, getter_name, None)
            if callable(getter):
                try:
                    return getter()
                except Exception:  # noqa: BLE001
                    continue
        return None

    # ------------------------------------------------------------------
    # Panel Registry helpers
    # ------------------------------------------------------------------

    def _register_module_panels(self) -> None:
        """Register panel descriptors from all enabled feature modules."""
        from src.shared.panels import get_panel_registry

        registry = get_panel_registry()
        if not hasattr(self, 'feature_registry') or not self.feature_registry:
            return

        for fid in self.feature_registry.get_available_features():
            mod = self.feature_registry._modules.get(fid)
            if mod and hasattr(mod, 'register_panels'):
                try:
                    mod.register_panels(registry)
                except Exception as exc:
                    logger.warning("Failed to register panels for %s: %s", fid, exc)

    def _build_panels_menu(self, panels_menu: QMenu) -> None:
        """Build the Window > Panels submenu from PanelRegistry.

        Groups panels by category (Workspace, Color, Edit, Composite,
        Deliver, Advanced) and filters by enabled features and the active
        work mode (image/video).
        """
        from src.shared.panels import get_panel_registry

        registry = get_panel_registry()

        # Determine enabled features for filtering
        enabled_features = None
        if hasattr(self, 'feature_flags') and self.feature_flags:
            enabled_features = set(self.feature_flags.get_enabled_features())

        # Active work mode: image-only/video-only panels are hidden from the
        # menu when the opposite mode is active.
        work_mode = None
        try:
            from src.shared.feature_flags import get_work_mode
            work_mode = get_work_mode(getattr(self, "config_manager", None))
        except Exception:
            pass

        grouped = registry.get_panels_by_category(enabled_features, work_mode)
        first_category = True

        for category in registry.get_categories(enabled_features, work_mode):
            descriptors = grouped[category]
            if not descriptors:
                continue

            if not first_category:
                panels_menu.addSeparator()
            first_category = False

            # If only one category or small number, add directly
            # If multiple categories, use submenus
            if len(registry.get_categories(enabled_features, work_mode)) <= 2:
                # Flat list for small menus
                for desc in descriptors:
                    action = self._get_panel_toggle_action(desc)
                    if action:
                        panels_menu.addAction(action)
            else:
                # Grouped submenus
                cat_menu = panels_menu.addMenu(
                    I18N.tr(f"menu.panels.{category.lower()}", category)
                )
                for desc in descriptors:
                    action = self._get_panel_toggle_action(desc)
                    if action:
                        cat_menu.addAction(action)

    def _get_panel_toggle_action(self, descriptor):
        """Get the toggleViewAction for a panel descriptor's dock widget.

        Returns the QAction if the dock exists, None otherwise.
        """
        dock_attr = descriptor.dock_attr
        if not dock_attr:
            return None

        dock = getattr(self, dock_attr, None)
        if dock is None:
            logger.debug(
                "Panel %s dock_attr=%s not found on MainWindow",
                descriptor.panel_id, dock_attr,
            )
            return None

        try:
            return dock.toggleViewAction()
        except Exception:
            logger.debug(
                "Panel %s dock_attr=%s has no toggleViewAction",
                descriptor.panel_id, dock_attr,
            )
            return None
