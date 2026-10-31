"""
PreferencesDialog - DaVinci Resolve-style application preferences panel.

Features:
- Left sidebar category navigation with right content area
- 8 categories: General, Appearance, Color & Processing, Performance, Export,
  Shortcuts, File & Storage, Advanced
- Dark theme matching the global application style
- Settings persist to config.json via ConfigManager
- Full i18n support (en_US, zh_CN)
- Instant preview for appearance settings
- Apply button for testing settings without closing
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QWidget, QGroupBox,
    QListWidget, QListWidgetItem, QStackedWidget, QSplitter,
    QScrollArea, QSpinBox, QSlider, QCheckBox, QLineEdit,
    QFileDialog, QMessageBox, QApplication
)
from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QFont

import logging
import os
import sys
import json
import platform
import subprocess
import threading

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import apply_combo_dropdown_style, C
from src.shared.ui.widgets import ChevronComboBox
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.widgets.pref_drag_value import PrefDragValue
from src.shared.ui.widgets.pref_form_layout import (
    make_pref_form, make_pref_label,
)
from src.shared.export.export_panel import (
    _apply_text_input_style,
    _apply_compact_button_style,
)

logger = logging.getLogger(__name__)


class PreferencesDialog(QDialog):
    """DaVinci Resolve-style preferences dialog with category navigation."""

    # Signal for instant appearance preview
    appearance_preview_changed = pyqtSignal(str, object)

    # AUD-03908: background cache-size scan results (worker thread → UI thread)
    cache_size_ready = pyqtSignal(int)
    # AUD-03912: background diagnostics export completion (ok, path_or_error)
    diagnostics_export_finished = pyqtSignal(bool, str)

    # Settings that support instant preview
    PREVIEWABLE_KEYS = {
        'ui_scale', 'panel_opacity',
        'accent_color'
    }

    # Video playback resolution options (label -> max_long_edge value)
    VIDEO_RESOLUTIONS = {
        "4K (3840)": 3840,
        "2K (2560)": 2560,
        "1080p": 1920,
        "720p": 1280,
        "480p": 854,
        "Original": 0,
    }

    # Category page indices
    PAGE_GENERAL = 0
    PAGE_APPEARANCE = 1
    PAGE_COLOR_PROCESSING = 2
    PAGE_PERFORMANCE = 3
    PAGE_EXPORT = 4
    PAGE_SHORTCUTS = 5
    PAGE_FILE_STORAGE = 6
    PAGE_ADVANCED = 7

    def __init__(self, config_manager, shortcut_manager=None, parent=None, feature_flags=None, module_pages=None) -> None:
        super().__init__(parent)
        self._config_manager = config_manager
        self._shortcut_manager = shortcut_manager
        self._feature_flags = feature_flags
        self._enabled_features = None
        if self._feature_flags:
            self._enabled_features = set(self._feature_flags.get_enabled_features())
        self._initial_locale = config_manager.config.get('locale', 'en_US')
        self._initial_work_mode = config_manager.config.get('work_mode', 'all')
        self._initial_developer_mode = bool(config_manager.config.get('developer_mode', False))
        self._needs_restart = False
        self._preview_snapshot = {}  # Store snapshot for preview rollback
        self._config_snapshot = dict(config_manager.config)  # Full config snapshot for Cancel rollback
        self._fe_config_snapshot: dict | None = None  # [AUD-03903] FE config snapshot for Cancel rollback
        self._applied_config_keys: set = set()  # [AUD-03904] keys touched by Apply
        self._applied = False  # Track if Apply was pressed

        # AUD-03908: single background cache-size scan at a time
        self._cache_scan_in_progress = False
        self.cache_size_ready.connect(self._on_cache_size_ready)
        # AUD-03912: background diagnostics export completion
        self.diagnostics_export_finished.connect(self._on_diagnostics_export_finished)

        # Store all widget references for load/save (app-level pages only)
        self._widgets = {}
        self._disabled_plugins = []  # Initialized before any plugin refresh

        # Module preference pages (ModularPreferencePage instances)
        self._module_pages = {}  # page_id -> page_object

        self.setWindowTitle(I18N.tr("preferences.title", "Preferences"))
        self.setMinimumSize(780, 560)
        self.resize(860, 900)

        # Register all preference pages into registry
        self._page_registry = self._build_page_registry(module_pages or [])

        self._setup_ui()
        self._apply_styles()
        self._load_current_settings()
        self._connect_preview_signals()
        self._save_preview_snapshot()  # Save initial state for Cancel rollback

    def _build_page_registry(self, module_pages=None):
        """Build PreferencesRegistry with all page descriptors.

        Args:
            module_pages: List of ModularPreferencePage instances contributed
                          by feature modules. Their descriptors are registered
                          with page_object set so load/apply/defaults are
                          delegated to them.
        """
        from src.shared.preferences import PreferencePageDescriptor, PreferencesRegistry
        registry = PreferencesRegistry()

        # Image Grading mode hides video-only preference pages (Video Playback,
        # Timeline Defaults) so the dialog matches the active workflow.
        from src.shared.feature_flags import get_work_mode
        image_mode = get_work_mode(self._config_manager) == "image"

        # App-level pages (always shown)
        registry.register(PreferencePageDescriptor(
            page_id="app.general", title=I18N.tr("preferences.categories.general", "General"),
            category="General", factory=self._create_general_page, order=100))
        registry.register(PreferencePageDescriptor(
            page_id="app.appearance", title=I18N.tr("preferences.categories.appearance", "Appearance"),
            category="Appearance", factory=self._create_appearance_page, order=200))

        # Module-contributed pages replace the old inline pages.
        # Register ModularPreferencePage instances from feature modules.
        for page_obj in (module_pages or []):
            pid = page_obj.page_id

            # Determine category, title, order, feature_id based on page_id
            if pid == "color.processing":
                registry.register(PreferencePageDescriptor(
                    page_id=pid,
                    title=I18N.tr("preferences.categories.color_processing", "Color & Processing"),
                    category="Color & Processing",
                    order=300, feature_id="color",
                    page_object=page_obj,
                ))
            elif pid == "deliver.export":
                registry.register(PreferencePageDescriptor(
                    page_id=pid,
                    title=I18N.tr("preferences.categories.export", "Export"),
                    category="Export",
                    order=500, feature_id="deliver",
                    page_object=page_obj,
                ))
            elif pid == "edit.video_playback":
                # Image mode has no video playback surface; skip this page.
                if image_mode:
                    self._module_pages[pid] = page_obj
                    continue
                registry.register(PreferencePageDescriptor(
                    page_id=pid,
                    title=I18N.tr("preferences.categories.video_playback", "Video Playback"),
                    category="Video Playback",
                    order=350, feature_id="edit",
                    page_object=page_obj,
                ))
            elif pid == "composite.settings":
                registry.register(PreferencePageDescriptor(
                    page_id=pid,
                    title=I18N.tr("preferences.categories.composite", "Composite"),
                    category="Composite",
                    order=550, feature_id="composite",
                    page_object=page_obj,
                ))
            elif pid == "edit.ai_editing":
                registry.register(PreferencePageDescriptor(
                    page_id=pid,
                    title=I18N.tr("preferences.categories.ai_editing", "AI Editing"),
                    category="AI Editing",
                    order=650, feature_id="edit",
                    page_object=page_obj,
                ))
            else:
                import logging
                logging.getLogger(__name__).warning(
                    f"Unknown module preference page_id: {pid}"
                )

            # Track module page objects for delegation
            self._module_pages[pid] = page_obj

        # App-level Performance page (base settings remain here;
        # Video Playback section moved to EditPreferencePage)
        registry.register(PreferencePageDescriptor(
            page_id="app.performance", title=I18N.tr("preferences.categories.performance", "Performance"),
            category="Performance", factory=self._create_performance_page, order=400))

        # Timeline Defaults (hidden in image mode — no timeline workflow).
        if not image_mode:
            registry.register(PreferencePageDescriptor(
                page_id="app.timeline_defaults", title=I18N.tr("preferences.categories.timeline_defaults", "Timeline Defaults"),
                category="Timeline Defaults", factory=self._create_timeline_defaults_page, order=450))

        # App-level utility pages
        registry.register(PreferencePageDescriptor(
            page_id="app.shortcuts", title=I18N.tr("preferences.categories.shortcuts", "Shortcuts"),
            category="Shortcuts", factory=self._create_shortcuts_page, order=600))
        registry.register(PreferencePageDescriptor(
            page_id="app.file_storage", title=I18N.tr("preferences.categories.file_storage", "File & Storage"),
            category="File & Storage", factory=self._create_file_storage_page, order=700))
        registry.register(PreferencePageDescriptor(
            page_id="app.advanced", title=I18N.tr("preferences.categories.advanced", "Advanced"),
            category="Advanced", factory=self._create_advanced_page, order=800))

        return registry

    # ── UI Setup ──────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        """Build the main dialog layout: splitter with category list + content pages."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Title bar
        title_bar = QWidget()
        title_bar.setObjectName("PrefTitleBar")
        title_bar.setFixedHeight(48)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(20, 0, 20, 0)
        title_label = QLabel(I18N.tr("preferences.title", "Preferences"))
        title_label.setObjectName("PrefTitleLabel")
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        main_layout.addWidget(title_bar)

        # Splitter: category list | content pages
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("PrefSplitter")
        splitter.setChildrenCollapsible(False)

        # Left: Category list
        self._category_list = QListWidget()
        self._category_list.setObjectName("PrefCategoryList")
        self._category_list.setFixedWidth(180)
        self._category_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._category_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)

        pages = self._page_registry.get_pages(self._enabled_features)
        for desc in pages:
            item = QListWidgetItem(desc.title)
            item.setSizeHint(QSize(0, 40))
            self._category_list.addItem(item)
        self._category_list.setCurrentRow(0)
        self._category_list.currentRowChanged.connect(self._on_category_changed)
        splitter.addWidget(self._category_list)

        # Right: Stacked pages
        self._pages = QStackedWidget()
        self._pages.setObjectName("PrefPages")
        for desc in pages:
            # Modular pages get the dialog as parent; app-level pages
            # use the factory bound to self (no parent arg needed).
            if desc.page_object is not None:
                page_widget = desc.factory(self)
            else:
                page_widget = desc.factory()
            self._pages.addWidget(page_widget)
        splitter.addWidget(self._pages)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([180, 680])
        main_layout.addWidget(splitter, 1)

        # Bottom buttons
        btn_bar = QWidget()
        btn_bar.setObjectName("PrefBtnBar")
        btn_bar.setFixedHeight(56)
        btn_layout = QHBoxLayout(btn_bar)
        btn_layout.setContentsMargins(20, 0, 20, 0)

        self._restore_btn = QPushButton(
            I18N.tr("preferences.restore_defaults", "Restore Defaults")
        )
        self._restore_btn.setObjectName("PrefRestoreBtn")
        self._restore_btn.clicked.connect(self._on_restore_defaults)

        self._cancel_btn = QPushButton(
            I18N.tr("preferences.cancel", "Cancel")
        )
        self._cancel_btn.setObjectName("PrefCancelBtn")
        self._cancel_btn.clicked.connect(self._on_cancel)

        self._apply_btn = QPushButton(
            I18N.tr("preferences.apply", "Apply")
        )
        self._apply_btn.setObjectName("PrefApplyBtn")
        self._apply_btn.setToolTip(I18N.tr(
            "preferences.apply_tooltip",
            "Apply changes and keep Preferences open.",
        ))
        self._apply_btn.clicked.connect(self._on_apply)

        self._save_btn = QPushButton(
            I18N.tr("preferences.save", "Save")
        )
        self._save_btn.setObjectName("PrefSaveBtn")
        self._save_btn.setToolTip(I18N.tr(
            "preferences.save_tooltip",
            "Save changes and close Preferences.",
        ))
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self._on_save)

        btn_layout.addWidget(self._restore_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self._cancel_btn)
        btn_layout.addSpacing(8)
        btn_layout.addWidget(self._apply_btn)
        btn_layout.addSpacing(8)
        btn_layout.addWidget(self._save_btn)
        main_layout.addWidget(btn_bar)

    # ── Instant Preview Mechanism ─────────────────────────────────

    def _save_preview_snapshot(self) -> None:
        """Save current appearance settings for potential rollback."""
        self._preview_snapshot = {
            'ui_scale': self._widgets['ui_scale'].currentData(),
            'panel_opacity': self._widgets['panel_opacity'].value(),
            'accent_color': self._widgets['accent_color'].currentData(),
        }

    def _restore_preview_snapshot(self) -> None:
        """Restore appearance settings from snapshot."""
        if not self._preview_snapshot:
            return

        # [AUD-03901] 程序化更新控件须状态锁：不阻断时下面的
        # setCurrentIndex/setValue 会逐项触发 _on_preview_setting_changed，
        # 末尾 _emit_batch_preview 再全量发一遍——Cancel 路径每个外观项
        # 双重发射。与 _on_restore_defaults 同一模式加锁。
        self._preview_blocked = True
        try:
            # Restore UI scale
            scale = self._preview_snapshot.get('ui_scale', 100)
            scale_combo = self._widgets['ui_scale']
            for i in range(scale_combo.count()):
                if scale_combo.itemData(i) == scale:
                    scale_combo.setCurrentIndex(i)
                    break

            # Restore other settings
            self._widgets['panel_opacity'].setValue(
                self._preview_snapshot.get('panel_opacity', 100))
            accent = self._preview_snapshot.get('accent_color', '#ff8800')
            accent_combo = self._widgets['accent_color']
            for i in range(accent_combo.count()):
                if accent_combo.itemData(i) == accent:
                    accent_combo.setCurrentIndex(i)
                    break
        finally:
            self._preview_blocked = False

        # Notify main window to rollback appearance preview
        self._emit_batch_preview()

    def _on_preview_setting_changed(self, key: str, value: object) -> None:
        """Emit preview signal when a previewable setting changes."""
        if getattr(self, '_preview_blocked', False):
            return
        if key in self.PREVIEWABLE_KEYS:
            self.appearance_preview_changed.emit(key, value)

    def _connect_preview_signals(self) -> None:
        """Connect appearance widgets to preview signal."""
        self._widgets['ui_scale'].currentIndexChanged.connect(
            lambda: self._on_preview_setting_changed('ui_scale', self._widgets['ui_scale'].currentData())
        )
        self._widgets['panel_opacity'].valueChanged.connect(
            lambda v: self._on_preview_setting_changed('panel_opacity', v)
        )
        self._widgets['accent_color'].currentIndexChanged.connect(
            lambda: self._on_preview_setting_changed('accent_color', self._widgets['accent_color'].currentData())
        )

    def _emit_batch_preview(self) -> None:
        """Emit preview signals for all previewable keys with current widget values."""
        for key in self.PREVIEWABLE_KEYS:
            widget = self._widgets.get(key)
            if isinstance(widget, QComboBox):
                self._on_preview_setting_changed(key, widget.currentData())
            elif isinstance(widget, QSlider):
                self._on_preview_setting_changed(key, widget.value())
            elif isinstance(widget, QCheckBox):
                self._on_preview_setting_changed(key, widget.isChecked())

    # ── Page: General ─────────────────────────────────────────────

    def _create_general_page(self) -> QWidget:
        page = self._wrap_in_scroll_area()
        content = page.widget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        group = self._create_group("preferences.general.section_title", "Application")
        form = make_pref_form(group)

        # Language
        lang_combo = ChevronComboBox()
        apply_combo_dropdown_style(lang_combo)
        lang_combo.setMinimumWidth(120)
        for code, name in I18N.SUPPORTED_LOCALES.items():
            lang_combo.addItem(name, code)
        self._widgets['locale'] = lang_combo
        lang_label = make_pref_label(I18N.tr("preferences.general.language", "Language:"))
        form.addRow(lang_label, lang_combo)

        # Language note
        note = QLabel(I18N.tr("preferences.general.language_note",
                               "Language change requires application restart."))
        note.setObjectName("PrefNoteLabel")
        note.setWordWrap(True)
        form.addRow("", note)

        # Work Mode (studio only): image grading vs video grading.
        # Only shown for the full studio product; other launchers are
        # pre-configured and ignore this option.
        self._widgets['work_mode'] = None
        from src.shared.feature_flags import get_product_mode
        if get_product_mode() == "studio":
            work_mode_combo = ChevronComboBox()
            apply_combo_dropdown_style(work_mode_combo)
            work_mode_combo.setMinimumWidth(180)
            work_mode_combo.addItem(
                I18N.tr("preferences.general.work_mode.image", "Image Grading"),
                "image",
            )
            work_mode_combo.addItem(
                I18N.tr("preferences.general.work_mode.video", "Video Grading"),
                "video",
            )
            self._widgets['work_mode'] = work_mode_combo
            work_mode_label = make_pref_label(
                I18N.tr("preferences.general.work_mode_label", "Work Mode:")
            )
            form.addRow(work_mode_label, work_mode_combo)

            # Restart note (work mode is applied on next launch)
            work_mode_note = QLabel(I18N.tr(
                "preferences.general.work_mode_note",
                "Work mode change requires application restart. "
                "Image mode hides the Edit page and disables keyframes in "
                "Composite; Video mode keeps all features.",
            ))
            work_mode_note.setObjectName("PrefNoteLabel")
            work_mode_note.setWordWrap(True)
            form.addRow("", work_mode_note)

        # Auto-save interval
        auto_save = PrefDragValue(min_v=1, max_v=60, default=5, suffix=" min")
        self._widgets['auto_save_interval'] = auto_save
        auto_save_label = make_pref_label(I18N.tr("preferences.general.auto_save_interval",
                                                   "Auto-save interval (minutes):"))
        form.addRow(auto_save_label, auto_save)

        # Max recent files
        max_recent = PrefDragValue(min_v=5, max_v=20, default=10)
        self._widgets['max_recent_files'] = max_recent
        max_label = make_pref_label(I18N.tr("preferences.general.max_recent_files",
                                             "Max recent files:"))
        form.addRow(max_label, max_recent)

        # Show welcome
        show_welcome = QCheckBox(
            I18N.tr("preferences.general.show_welcome", "Show welcome on startup")
        )
        self._widgets['show_welcome_on_startup'] = show_welcome
        form.addRow("", show_welcome)

        # Confirm before reset (NEW)
        confirm_reset = QCheckBox(
            I18N.tr("preferences.general.confirm_before_reset", "Confirm before reset")
        )
        self._widgets['confirm_before_reset'] = confirm_reset
        form.addRow("", confirm_reset)

        # Auto-load last project (NEW)
        auto_load = QCheckBox(
            I18N.tr("preferences.general.auto_load_last_project", "Auto-load last project on startup")
        )
        self._widgets['auto_load_last_project'] = auto_load
        form.addRow("", auto_load)

        # Undo history limit (NEW)
        undo_limit = PrefDragValue(min_v=10, max_v=200, default=50)
        undo_limit.setSingleStep(10)
        self._widgets['undo_history_limit'] = undo_limit
        undo_label = make_pref_label(I18N.tr("preferences.general.undo_history_limit", "Undo history limit:"))
        form.addRow(undo_label, undo_limit)

        # Check updates on startup (NEW)
        check_updates = QCheckBox(
            I18N.tr("preferences.general.check_updates", "Check for updates on startup")
        )
        self._widgets['check_updates_on_startup'] = check_updates
        form.addRow("", check_updates)

        layout.addWidget(group)
        layout.addStretch()
        return page

    # ── Page: Appearance ──────────────────────────────────────────

    def _create_appearance_page(self) -> QWidget:
        page = self._wrap_in_scroll_area()
        content = page.widget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        group = self._create_group("preferences.appearance.section_title", "UI Settings")
        form = make_pref_form(group)

        # UI Scale
        scale_combo = ChevronComboBox()
        apply_combo_dropdown_style(scale_combo)
        scale_combo.setMinimumWidth(120)
        for s in range(90, 160, 10):
            scale_combo.addItem(f"{s}%", s)
        self._widgets['ui_scale'] = scale_combo
        scale_label = make_pref_label(I18N.tr("preferences.appearance.ui_scale", "UI Scale:"))
        form.addRow(scale_label, scale_combo)

        # Panel Opacity
        opacity_widget = QWidget()
        opacity_widget.setStyleSheet("background: transparent;")
        opacity_layout = QHBoxLayout(opacity_widget)
        opacity_layout.setContentsMargins(0, 0, 0, 0)
        opacity_layout.setSpacing(8)
        opacity_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        opacity_slider.setRange(80, 100)
        opacity_slider.setObjectName("PrefSlider")
        opacity_value = QLabel("100%")
        opacity_value.setObjectName("PrefSliderValue")
        opacity_value.setFixedWidth(40)
        mono_font = QFont("Menlo", 11)
        if not mono_font.exactMatch():
            mono_font = QFont("Courier", 11)
        opacity_value.setFont(mono_font)
        opacity_slider.valueChanged.connect(
            lambda v: opacity_value.setText(f"{v}%")
        )
        opacity_layout.addWidget(opacity_slider, 1)
        opacity_layout.addWidget(opacity_value)
        self._widgets['panel_opacity'] = opacity_slider
        opacity_label = make_pref_label(I18N.tr("preferences.appearance.panel_opacity",
                                                 "Panel Opacity:"))
        form.addRow(opacity_label, opacity_widget)

        # Show tooltips
        show_tooltips = QCheckBox(
            I18N.tr("preferences.appearance.show_tooltips", "Show tooltips")
        )
        self._widgets['show_tooltips'] = show_tooltips
        form.addRow("", show_tooltips)

        # Accent Color (NEW)
        accent_combo = ChevronComboBox()
        apply_combo_dropdown_style(accent_combo)
        accent_combo.setMinimumWidth(120)
        accent_colors = [
            ("#ff8800", I18N.tr("preferences.appearance.accent_default", "Default Orange")),
            ("#4488ff", I18N.tr("preferences.appearance.accent_blue", "Blue")),
            ("#00aa99", I18N.tr("preferences.appearance.accent_teal", "Teal")),
            ("#ff4444", I18N.tr("preferences.appearance.accent_red", "Red")),
            ("#9944ff", I18N.tr("preferences.appearance.accent_purple", "Purple")),
        ]
        for color, label in accent_colors:
            accent_combo.addItem(label, color)
        self._widgets['accent_color'] = accent_combo
        accent_label = make_pref_label(I18N.tr("preferences.appearance.accent_color", "Highlight Color:"))
        form.addRow(accent_label, accent_combo)

        # Show panel titles (NEW)
        show_titles = QCheckBox(
            I18N.tr("preferences.appearance.show_panel_titles", "Show panel title bars")
        )
        self._widgets['show_panel_titles'] = show_titles
        form.addRow("", show_titles)

        # Zoom speed (NEW)
        zoom_widget = QWidget()
        zoom_widget.setStyleSheet("background: transparent;")
        zoom_layout = QHBoxLayout(zoom_widget)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(8)
        zoom_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        zoom_slider.setRange(50, 200)
        zoom_slider.setSingleStep(10)
        zoom_slider.setObjectName("PrefSlider")
        zoom_value = QLabel("100")
        zoom_value.setObjectName("PrefSliderValue")
        zoom_value.setFixedWidth(40)
        zoom_value.setFont(mono_font)
        zoom_slider.valueChanged.connect(
            lambda v: zoom_value.setText(str(v))
        )
        zoom_layout.addWidget(zoom_slider, 1)
        zoom_layout.addWidget(zoom_value)
        self._widgets['zoom_speed'] = zoom_slider
        zoom_label = make_pref_label(I18N.tr("preferences.appearance.zoom_speed", "Mouse wheel zoom speed:"))
        form.addRow(zoom_label, zoom_widget)

        # Preview note
        preview_note = QLabel(I18N.tr("preferences.appearance.preview_note",
                                       "Appearance changes preview in real-time."))
        preview_note.setObjectName("PrefNoteLabel")
        preview_note.setWordWrap(True)
        form.addRow("", preview_note)

        layout.addWidget(group)
        layout.addStretch()
        return page

    # ── Page: Performance ─────────────────────────────────────────

    def _create_performance_page(self) -> QWidget:
        page = self._wrap_in_scroll_area()
        content = page.widget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        group = self._create_group("preferences.performance.section_title",
                                    "Performance Settings")
        form = make_pref_form(group)

        # GPU acceleration
        gpu_acc = QCheckBox(
            I18N.tr("preferences.performance.gpu_acceleration",
                    "GPU acceleration")
        )
        self._widgets['gpu_acceleration'] = gpu_acc
        form.addRow("", gpu_acc)

        # Timeline output graphics backend. D3D11 is exposed on Windows, but
        # the exporter still applies the full native-surface gate before using
        # it; an unavailable explicit choice is reported and falls back with a
        # reason rather than being silently labelled D3D11.
        graphics_combo = ChevronComboBox()
        apply_combo_dropdown_style(graphics_combo)
        graphics_combo.setMinimumWidth(160)
        graphics_combo.addItem(
            I18N.tr("preferences.performance.graphics_auto", "Auto (Recommended)"),
            "auto",
        )
        graphics_combo.addItem(
            I18N.tr("preferences.performance.graphics_opengl", "OpenGL"),
            "opengl",
        )
        graphics_combo.addItem(
            I18N.tr("preferences.performance.graphics_d3d11", "D3D11 (Windows export)"),
            "d3d11",
        )
        graphics_combo.addItem(
            I18N.tr("preferences.performance.graphics_metal", "Metal"),
            "metal",
        )
        metal_available, metal_reason = False, "Metal is only available on macOS."
        if platform.system() == "Darwin":
            try:
                from src.shared.gpu.metal import MetalComputeContext

                metal_available, metal_reason = MetalComputeContext.availability_reason()
            except Exception as error:  # optional runtime capability probe
                metal_reason = str(error)
        metal_index = graphics_combo.findData("metal")
        metal_item = graphics_combo.model().item(metal_index)
        if not metal_available and metal_item is not None:
            metal_item.setFlags(metal_item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            graphics_combo.setItemData(
                metal_index, metal_reason, Qt.ItemDataRole.ToolTipRole)
        d3d11_index = graphics_combo.findData("d3d11")
        d3d11_item = graphics_combo.model().item(d3d11_index)
        # GPU probing belongs to the export worker, not the UI thread.  Keep
        # the option visible on Windows and let the exporter run the complete
        # native-surface gate when a job starts.
        d3d11_available = platform.system() == "Windows"
        d3d11_reason = (
            "D3D11 capability is checked in the export worker."
            if d3d11_available else "D3D11 is only available on Windows."
        )
        if d3d11_item is not None:
            if platform.system() != "Windows":
                d3d11_item.setFlags(d3d11_item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            # Keep the choice visible on Windows even before the native bridge
            # is installed, so an explicit request can produce a diagnostic
            # fallback reason in the exporter.
            if not d3d11_available:
                graphics_combo.setItemData(
                    d3d11_index, d3d11_reason, Qt.ItemDataRole.ToolTipRole)
            else:
                graphics_combo.setItemData(
                    d3d11_index, d3d11_reason, Qt.ItemDataRole.ToolTipRole)
        self._widgets['timeline_graphics_backend'] = graphics_combo
        graphics_label = make_pref_label(
            I18N.tr("preferences.performance.timeline_graphics_backend",
                    "Timeline render backend:")
        )
        form.addRow(graphics_label, graphics_combo)
        graphics_desc = QLabel(
            I18N.tr(
                "preferences.performance.timeline_graphics_backend_desc",
                "For timeline exports, Auto selects D3D11 only after the complete "
                "native-surface and zero-readback gate passes. Otherwise it keeps "
                "the OpenGL path. Eligible single-layer "
                "clips run source color conversion, primary "
                "grading, scaling and H.264 YUV conversion on the selected GPU; "
                "unsupported effects or complex stacks use the CPU path.",
            )
        )
        graphics_desc.setObjectName("PrefNoteLabel")
        graphics_desc.setWordWrap(True)
        form.addRow("", graphics_desc)

        # macOS VideoToolbox zero-copy GPU decode
        vt_combo = ChevronComboBox()
        apply_combo_dropdown_style(vt_combo)
        vt_combo.setMinimumWidth(120)
        vt_combo.addItem(
            I18N.tr("preferences.performance.vt_zero_copy_auto",
                    "Auto (Recommended)"),
            "auto"
        )
        vt_combo.addItem(
            I18N.tr("preferences.performance.vt_zero_copy_off",
                    "Disabled"),
            "off"
        )
        vt_combo.addItem(
            I18N.tr("preferences.performance.vt_zero_copy_force",
                    "Force Enable"),
            "force"
        )
        self._widgets['video_macos_vt_zero_copy'] = vt_combo
        vt_label = make_pref_label(I18N.tr("preferences.performance.vt_zero_copy",
                                            "macOS VT Zero-Copy Decode:"))
        form.addRow(vt_label, vt_combo)

        # VT zero-copy description
        vt_desc = QLabel(
            I18N.tr("preferences.performance.vt_zero_copy_desc",
                    "Use macOS VideoToolbox for hardware decode with zero GPU memory copy. "
                    "Auto: enable when available. Disabled: force CPU decode. "
                    "Force: attempt even on unsupported configurations.")
        )
        vt_desc.setObjectName("PrefNoteLabel")
        vt_desc.setWordWrap(True)
        form.addRow("", vt_desc)

        # Video playback resolution
        res_combo = ChevronComboBox()
        apply_combo_dropdown_style(res_combo)
        res_combo.setMinimumWidth(120)
        for label in self.VIDEO_RESOLUTIONS.keys():
            res_combo.addItem(label)
        self._widgets['video_playback_resolution'] = res_combo
        res_label = make_pref_label(I18N.tr("preferences.performance.playback_resolution",
                                             "Video Playback Resolution:"))
        form.addRow(res_label, res_combo)

        # Resolution description
        res_desc = QLabel(I18N.tr("preferences.performance.resolution_desc",
                                   "Maximum resolution for video playback. "
                                   "Higher values use more memory."))
        res_desc.setObjectName("PrefNoteLabel")
        res_desc.setWordWrap(True)
        form.addRow("", res_desc)

        # Processing threads
        threads_spin = PrefDragValue(min_v=0, max_v=32, default=0)
        threads_spin.setSpecialValueText("Auto")
        self._widgets['processing_threads'] = threads_spin
        threads_label = make_pref_label(I18N.tr("preferences.performance.processing_threads",
                                                 "Processing threads (0=auto):"))
        form.addRow(threads_label, threads_spin)

        # Pass Fusion Mode
        fusion_combo = ChevronComboBox()
        apply_combo_dropdown_style(fusion_combo)
        fusion_combo.setMinimumWidth(120)
        fusion_combo.addItem(
            I18N.tr("preferences.performance.fusion_conservative", "Conservative (Recommended)"),
            "conservative"
        )
        fusion_combo.addItem(
            I18N.tr("preferences.performance.fusion_aggressive", "Aggressive"),
            "aggressive"
        )
        fusion_combo.addItem(
            I18N.tr("preferences.performance.fusion_off", "Disabled"),
            "off"
        )
        self._widgets['pass_fusion_mode'] = fusion_combo
        fusion_label = make_pref_label(I18N.tr("preferences.performance.pass_fusion_mode",
                                                "Pass Fusion Mode:"))
        form.addRow(fusion_label, fusion_combo)

        # Pass Fusion description
        fusion_desc = QLabel(I18N.tr("preferences.performance.fusion_desc",
                                      "Merges consecutive color-only nodes into a single GPU pass. "
                                      "Conservative: only fuse when texture sizes match. "
                                      "Aggressive: fuse with auto-resize. "
                                      "Disabled: no fusion for maximum quality."))
        fusion_desc.setObjectName("PrefNoteLabel")
        fusion_desc.setWordWrap(True)
        form.addRow("", fusion_desc)

        layout.addWidget(group)
        layout.addStretch()
        return page

    # ── Page: Timeline Defaults ─────────────────────────────────────

    def _create_timeline_defaults_page(self) -> QWidget:
        """Create Timeline Defaults preference page."""
        page = self._wrap_in_scroll_area()
        content = page.widget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # ── Default Video Settings ──
        video_group = self._create_group("preferences.timeline.video_group",
                                          "Default Video Settings")
        video_form = make_pref_form(video_group)

        # Default FPS
        fps_combo = ChevronComboBox()
        apply_combo_dropdown_style(fps_combo)
        fps_items = [
            ("23.976 fps", "23.976"),
            ("24 fps", "24"),
            ("25 fps", "25"),
            ("29.97 fps", "29.97"),
            ("30 fps", "30"),
            ("50 fps", "50"),
            ("59.94 fps", "59.94"),
            ("60 fps", "60"),
        ]
        for label, val in fps_items:
            fps_combo.addItem(label, val)
        self._widgets['default_timeline_fps'] = fps_combo
        video_form.addRow(make_pref_label(I18N.tr("preferences.timeline.default_fps", "Frame Rate:")), fps_combo)

        # Default Resolution
        res_layout = QHBoxLayout()
        res_combo = ChevronComboBox()
        apply_combo_dropdown_style(res_combo)
        res_preset_items = [
            ("4K UHD (3840x2160)", 3840, 2160),
            ("2K QHD (2560x1440)", 2560, 1440),
            ("1080p (1920x1080)", 1920, 1080),
            ("720p (1280x720)", 1280, 720),
            (I18N.tr("preferences.timeline.custom", "Custom"), 0, 0),
        ]
        for item in res_preset_items:
            # [BUG-B3-03] 预设尺寸作为 itemData 保存，供 _load_current_settings
            # 按配置的宽高反查匹配的预设（无匹配时落到 "Custom"）。
            res_combo.addItem(item[0], (item[1], item[2]))
        res_combo.currentIndexChanged.connect(
            lambda idx: self._on_timeline_res_preset_changed(idx, res_preset_items, width_spin, height_spin)
        )
        res_layout.addWidget(res_combo)
        self._widgets['_timeline_res_combo'] = res_combo

        width_spin = PrefDragValue(min_v=64, max_v=8192, default=1920, suffix=" px")
        res_layout.addWidget(width_spin)
        self._widgets['default_timeline_width'] = width_spin

        res_layout.addWidget(QLabel("x"))

        height_spin = PrefDragValue(min_v=64, max_v=8192, default=1080, suffix=" px")
        res_layout.addWidget(height_spin)
        self._widgets['default_timeline_height'] = height_spin

        video_form.addRow(make_pref_label(I18N.tr("preferences.timeline.resolution", "Resolution:")), res_layout)

        # Default Pixel Aspect Ratio
        par_combo = ChevronComboBox()
        apply_combo_dropdown_style(par_combo)
        par_items = [
            ("Square Pixels (1.0)", "1.0"),
            ("DV NTSC (0.9091)", "0.9091"),
            ("DV PAL (1.0926)", "1.0926"),
            ("HD Anamorphic (1.3333)", "1.3333"),
        ]
        for label, val in par_items:
            par_combo.addItem(label, val)
        self._widgets['default_timeline_pixel_aspect_ratio'] = par_combo
        video_form.addRow(make_pref_label(I18N.tr("preferences.timeline.pixel_aspect", "Pixel Aspect Ratio:")), par_combo)

        # Default Field Order
        field_combo = ChevronComboBox()
        apply_combo_dropdown_style(field_combo)
        field_items = [
            (I18N.tr("preferences.timeline.progressive", "Progressive (No Fields)"), "progressive"),
            (I18N.tr("preferences.timeline.upper_field", "Upper Field First"), "upper"),
            (I18N.tr("preferences.timeline.lower_field", "Lower Field First"), "lower"),
        ]
        for label, val in field_items:
            field_combo.addItem(label, val)
        self._widgets['default_timeline_field_order'] = field_combo
        video_form.addRow(make_pref_label(I18N.tr("preferences.timeline.field_order", "Field Order:")), field_combo)

        layout.addWidget(video_group)

        # ── Default Audio Settings ──
        audio_group = self._create_group("preferences.timeline.audio_group",
                                          "Default Audio Settings")
        audio_form = make_pref_form(audio_group)

        # Default Sample Rate
        sr_combo = ChevronComboBox()
        apply_combo_dropdown_style(sr_combo)
        for label, val in [("44.1 kHz", 44100), ("48 kHz", 48000), ("96 kHz", 96000)]:
            sr_combo.addItem(label, val)
        self._widgets['default_timeline_sample_rate'] = sr_combo
        audio_form.addRow(make_pref_label(I18N.tr("preferences.timeline.sample_rate", "Sample Rate:")), sr_combo)

        # Default Audio Track Type
        at_combo = ChevronComboBox()
        apply_combo_dropdown_style(at_combo)
        for label, val in [("Mono", "mono"), ("Stereo", "stereo"), ("5.1 Surround", "51")]:
            at_combo.addItem(label, val)
        self._widgets['default_timeline_audio_track_type'] = at_combo
        audio_form.addRow(make_pref_label(I18N.tr("preferences.timeline.audio_track_type", "Track Type:")), at_combo)

        layout.addWidget(audio_group)

        # ── Subtitle Settings ──
        sub_group = self._create_group("preferences.timeline.subtitle_section",
                                       "Subtitle")
        sub_form = make_pref_form(sub_group)

        # Default subtitle clip duration (stored as frames; 72 = 3s @ 24fps).
        # Range 6-1200 frames (0.25s - 50s @ 24fps), step 6 frames (0.25s).
        sub_dur = PrefDragValue(min_v=6, max_v=1200, default=72, suffix=" f")
        sub_dur.setSingleStep(6)
        self._widgets['default_subtitle_clip_duration_frames'] = sub_dur
        sub_form.addRow(
            make_pref_label(I18N.tr(
                "preferences.timeline.subtitle_default_duration",
                "Default Subtitle Clip Duration:")),
            sub_dur,
        )

        layout.addWidget(sub_group)

        # ── Default Start Timecode ──
        tc_layout = QHBoxLayout()
        tc_edit = QLineEdit("00:00:00:00")
        tc_edit.setMaximumWidth(160)
        tc_edit.setInputMask("00:00:00:00")
        self._widgets['default_timeline_start_timecode'] = tc_edit
        tc_layout.addWidget(tc_edit)
        tc_layout.addStretch()

        tc_form = make_pref_form()
        tc_form.addRow(make_pref_label(I18N.tr("preferences.timeline.start_timecode", "Start Timecode:")), tc_layout)
        layout.addLayout(tc_form)

        layout.addStretch()
        return page

    def _on_timeline_res_preset_changed(self, idx: int, preset_items: list, width_spin: QSpinBox, height_spin: QSpinBox) -> None:
        """Handle timeline resolution preset change."""
        if idx < 0 or idx >= len(preset_items):
            return
        _, w, h = preset_items[idx]
        if w > 0 and h > 0:
            width_spin.setValue(w)
            height_spin.setValue(h)

    def _sync_timeline_res_combo(self, width: int, height: int) -> None:
        """[BUG-B3-03] Sync the resolution preset combo to the configured size.

        Reverse-maps ``(width, height)`` onto the preset items stored as combo
        itemData; selects the trailing "Custom" entry when nothing matches, so
        the combo never disagrees with the width/height spinboxes after load.
        Setting the index re-triggers ``_on_timeline_res_preset_changed``,
        which only rewrites identical values (Custom leaves them untouched).
        """
        combo = self._widgets.get('_timeline_res_combo')
        if combo is None:
            return  # Timeline page is hidden in Image mode — no widget
        for i in range(combo.count()):
            preset = combo.itemData(i)
            if isinstance(preset, tuple) and preset == (width, height):
                combo.setCurrentIndex(i)
                return
        if combo.count() > 0:
            combo.setCurrentIndex(combo.count() - 1)  # "Custom"

    # ── Page: Shortcuts ───────────────────────────────────────────

    def _create_shortcuts_page(self) -> QWidget:
        page = self._wrap_in_scroll_area()
        content = page.widget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        group = self._create_group("preferences.shortcuts.section_title",
                                    "Keyboard Shortcuts")
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(16, 20, 16, 16)
        group_layout.setSpacing(12)

        desc = QLabel(I18N.tr("preferences.shortcuts.open_editor_desc",
                               "Customize keyboard shortcuts for all actions."))
        desc.setObjectName("PrefNoteLabel")
        desc.setWordWrap(True)
        group_layout.addWidget(desc)

        # Open shortcut editor button
        open_btn = QPushButton(
            I18N.tr("preferences.shortcuts.open_editor", "Open Shortcut Editor")
        )
        open_btn.setObjectName("PrefActionBtn")
        open_btn.setFixedHeight(36)
        open_btn.clicked.connect(self._open_shortcut_editor)
        group_layout.addWidget(open_btn)

        # Reset all shortcuts button
        reset_btn = QPushButton(
            I18N.tr("preferences.shortcuts.reset_all", "Reset All Shortcuts")
        )
        reset_btn.setObjectName("PrefDangerBtn")
        reset_btn.setFixedHeight(36)
        reset_btn.clicked.connect(self._reset_all_shortcuts)
        group_layout.addWidget(reset_btn)

        # Global shortcuts (NEW)
        global_shortcuts = QCheckBox(
            I18N.tr("preferences.shortcuts.global_shortcuts", "Enable global shortcuts")
        )
        self._widgets['global_shortcuts'] = global_shortcuts
        group_layout.addWidget(global_shortcuts)

        # Show shortcut conflicts (NEW)
        show_conflicts = QCheckBox(
            I18N.tr("preferences.shortcuts.show_conflicts", "Show shortcut conflict warnings")
        )
        self._widgets['show_shortcut_conflicts'] = show_conflicts
        group_layout.addWidget(show_conflicts)

        group_layout.addStretch()
        layout.addWidget(group)
        layout.addStretch()
        return page

    # ── Page: File & Storage ──────────────────────────────────────

    def _create_file_storage_page(self) -> QWidget:
        page = self._wrap_in_scroll_area()
        content = page.widget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # File Handling group
        file_group = self._create_group("preferences.file_storage.section_file_title", "File Handling")
        file_form = make_pref_form(file_group)

        # Default open directory
        open_dir_edit = self._create_path_edit("Select Default Open Directory")
        self._widgets['default_open_directory'] = open_dir_edit._line_edit
        open_dir_widget = open_dir_edit
        file_form.addRow(
            make_pref_label(I18N.tr("preferences.file_storage.default_open_dir", "Default Open Directory:")),
            open_dir_widget
        )

        # Auto-backup interval
        backup_interval = PrefDragValue(min_v=0, max_v=120, default=15, suffix=" min")
        backup_interval.setSpecialValueText("Off")
        self._widgets['auto_backup_interval'] = backup_interval
        file_form.addRow(
            make_pref_label(I18N.tr("preferences.file_storage.auto_backup_interval", "Auto-backup interval (min, 0=off):")),
            backup_interval
        )

        # Backup directory
        backup_dir_edit = self._create_path_edit("Select Backup Directory")
        self._widgets['backup_directory'] = backup_dir_edit._line_edit
        backup_dir_widget = backup_dir_edit
        file_form.addRow(
            make_pref_label(I18N.tr("preferences.file_storage.backup_directory", "Backup Directory:")),
            backup_dir_widget
        )

        # Max backup count
        max_backup = PrefDragValue(min_v=1, max_v=50, default=5)
        self._widgets['max_backup_count'] = max_backup
        file_form.addRow(
            make_pref_label(I18N.tr("preferences.file_storage.max_backup_count", "Max backup count:")),
            max_backup
        )

        layout.addWidget(file_group)

        # Cache group
        cache_group = self._create_group("preferences.file_storage.section_cache_title", "Cache & Temporary Files")
        cache_form = make_pref_form(cache_group)

        # Cache directory
        cache_dir_edit = self._create_path_edit("Select Cache Directory")
        self._widgets['cache_directory'] = cache_dir_edit._line_edit
        cache_dir_widget = cache_dir_edit
        cache_form.addRow(
            make_pref_label(I18N.tr("preferences.file_storage.cache_directory", "Cache Directory:")),
            cache_dir_widget
        )

        # Cache size limit
        cache_spin = PrefDragValue(min_v=1, max_v=100, default=10, suffix=" GB")
        self._widgets['cache_size_limit_gb'] = cache_spin
        cache_form.addRow(
            make_pref_label(I18N.tr("preferences.performance.cache_size", "Cache size limit (GB):")),
            cache_spin
        )

        # Cleanup temp on exit
        cleanup = QCheckBox(I18N.tr("preferences.file_storage.cleanup_temp", "Cleanup temp files on exit"))
        self._widgets['cleanup_temp_on_exit'] = cleanup
        cache_form.addRow("", cleanup)

        # Temp file max age
        temp_age = PrefDragValue(min_v=1, max_v=365, default=7, suffix=" days")
        self._widgets['temp_file_max_age_days'] = temp_age
        cache_form.addRow(
            make_pref_label(I18N.tr("preferences.file_storage.temp_max_age", "Temp file max age (days):")),
            temp_age
        )

        # Cache size label (placeholder)
        cache_size_label = QLabel(I18N.tr("preferences.file_storage.cache_size_label", "Current cache size:") + " --")
        cache_size_label.setObjectName("PrefNoteLabel")
        self._widgets['cache_size_label'] = cache_size_label
        cache_form.addRow("", cache_size_label)

        # Clear cache button
        clear_cache_btn = QPushButton(I18N.tr("preferences.file_storage.clear_cache", "Clear Cache"))
        clear_cache_btn.setObjectName("PrefDangerBtn")
        clear_cache_btn.setFixedHeight(36)
        clear_cache_btn.clicked.connect(self._clear_cache)
        cache_form.addRow("", clear_cache_btn)

        layout.addWidget(cache_group)

        # Video Proxy group（2026-09-19 缓存审计补齐：非 topos 源的素材
        # 代理此前恒为 ProRes 且无 UI 入口——ProxyFormat 枚举连 topos 档
        # 都没有。默认与渲染缓存同策略取 topos_proxy；topos 源不受此设置
        # 影响（proxy_generator 内容嗅探恒走 topos）。
        proxy_group = self._create_group(
            "preferences.file_storage.section_proxy_title", "Video Proxy")
        proxy_form = make_pref_form(proxy_group)

        proxy_codec = ChevronComboBox()
        apply_combo_dropdown_style(proxy_codec)
        proxy_codec.setMinimumWidth(160)
        proxy_codec.addItem("Topos 422 Proxy (recommended)", "topos_proxy")
        proxy_codec.addItem("ProRes 422 Proxy", "prores_proxy")
        proxy_codec.addItem("H.264", "h264")
        self._widgets['proxy_format'] = proxy_codec
        proxy_form.addRow(
            make_pref_label(I18N.tr("preferences.file_storage.proxy_format", "Proxy Format:")),
            proxy_codec
        )

        proxy_note = QLabel(I18N.tr(
            "preferences.file_storage.proxy_format_note",
            "Topos proxies are intra-frame 4:2:2 10-bit (video-only; audio "
            "always plays from the master). Changing the format takes "
            "effect for newly generated proxies and invalidates existing "
            "ones.",
        ))
        proxy_note.setWordWrap(True)
        proxy_note.setObjectName("PrefNoteLabel")
        proxy_form.addRow("", proxy_note)

        layout.addWidget(proxy_group)

        # Render Cache group (separate from general cache)
        rc_group = self._create_group("preferences.file_storage.section_render_cache_title", "Render Cache")
        rc_form = make_pref_form(rc_group)

        # Auto mode
        rc_mode = ChevronComboBox()
        apply_combo_dropdown_style(rc_mode)
        rc_mode.setMinimumWidth(120)
        rc_mode.addItem(I18N.tr("preferences.render_cache.mode_off", "Off"), "off")
        rc_mode.addItem(I18N.tr("preferences.render_cache.mode_user", "User (manual)"), "user")
        rc_mode.addItem(I18N.tr("preferences.render_cache.mode_smart", "Smart (auto)"), "smart")
        self._widgets['render_cache_auto_mode'] = rc_mode
        rc_form.addRow(
            make_pref_label(I18N.tr("preferences.render_cache.auto_mode", "Render Cache Mode:")),
            rc_mode
        )

        # B3: smart 模式阈值 spinbox（score ≥ 阈值的片段自动入队）
        rc_threshold = QSpinBox()
        rc_threshold.setRange(1, 30)
        rc_threshold.setValue(6)
        rc_threshold.setToolTip(I18N.tr(
            "preferences.render_cache.threshold_tooltip",
            "Smart 模式的自动缓存分数阈值（分数 = 效果数×2 + 调色×3 + "
            "变速×2 + 合成×4 + 动画参数×1），仅 smart 模式生效",
        ))
        self._widgets['render_cache_auto_threshold'] = rc_threshold
        rc_form.addRow(
            make_pref_label(I18N.tr(
                "preferences.render_cache.threshold",
                "Smart Score Threshold:")),
            rc_threshold
        )

        # Codec
        rc_codec = ChevronComboBox()
        apply_combo_dropdown_style(rc_codec)
        rc_codec.setMinimumWidth(160)
        rc_codec.addItem("Topos 422 Proxy (recommended)", "topos_proxy")
        rc_codec.addItem("Topos 422 (Standard)", "topos")
        rc_codec.addItem("H.264 Proxy", "h264_proxy")
        rc_codec.addItem("DNxHR LB", "dnxhr_lb")
        rc_codec.addItem("DNxHR SQ", "dnxhr_sq")
        self._widgets['render_cache_codec'] = rc_codec
        rc_form.addRow(
            make_pref_label(I18N.tr("preferences.render_cache.codec", "Cache Codec:")),
            rc_codec
        )

        # Render-cache files are intentionally separate from baked/image
        # caches: moving this location must not invalidate unrelated caches.
        rc_dir_edit = self._create_path_edit("Select Render Cache Directory")
        self._widgets['render_cache_directory'] = rc_dir_edit._line_edit
        rc_form.addRow(
            make_pref_label(I18N.tr("preferences.render_cache.directory", "Cache Location:")),
            rc_dir_edit
        )

        # Resolution mode
        rc_resolution = ChevronComboBox()
        apply_combo_dropdown_style(rc_resolution)
        rc_resolution.setMinimumWidth(120)
        rc_resolution.addItem(I18N.tr("preferences.render_cache.res_timeline", "Timeline"), "timeline")
        rc_resolution.addItem(I18N.tr("preferences.render_cache.res_half", "Half"), "half")
        rc_resolution.addItem(I18N.tr("preferences.render_cache.res_quarter", "Quarter"), "quarter")
        self._widgets['render_cache_resolution_mode'] = rc_resolution
        rc_form.addRow(
            make_pref_label(I18N.tr("preferences.render_cache.resolution", "Cache Resolution:")),
            rc_resolution
        )

        # Max size
        rc_max_size = PrefDragValue(min_v=1, max_v=500, default=50, suffix=" GB")
        self._widgets['render_cache_max_size_gb'] = rc_max_size
        rc_form.addRow(
            make_pref_label(I18N.tr("preferences.render_cache.max_size", "Max cache size (GB):")),
            rc_max_size
        )

        layout.addWidget(rc_group)
        layout.addStretch()
        return page

    # ── Page: Advanced ──────────────────────────────────────────────

    def _create_advanced_page(self) -> QWidget:
        page = self._wrap_in_scroll_area()
        content = page.widget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Logging group
        log_group = self._create_group("preferences.advanced.section_logging_title", "Logging")
        log_form = make_pref_form(log_group)

        # Log level
        log_level = ChevronComboBox()
        apply_combo_dropdown_style(log_level)
        log_level.setMinimumWidth(120)
        for value, label in [
            ("debug", I18N.tr("preferences.logs_diagnostics.level_debug", "Debug")),
            ("info", I18N.tr("preferences.logs_diagnostics.level_info", "Info")),
            ("warning", I18N.tr("preferences.logs_diagnostics.level_warning", "Warning")),
            ("error", I18N.tr("preferences.logs_diagnostics.level_error", "Error")),
            ("critical", I18N.tr("preferences.logs_diagnostics.level_critical", "Critical")),
        ]:
            log_level.addItem(label, value)
        self._widgets['log_level'] = log_level
        log_form.addRow(
            make_pref_label(I18N.tr("preferences.logs_diagnostics.log_level", "Log Level:")),
            log_level
        )

        # Log file path
        log_path_edit = self._create_path_edit("Select Log File", is_file=True)
        self._widgets['log_file_path'] = log_path_edit._line_edit
        log_form.addRow(
            make_pref_label(I18N.tr("preferences.logs_diagnostics.log_file_path", "Log File Location:")),
            log_path_edit
        )

        # Enable file logging
        enable_file_log = QCheckBox(I18N.tr("preferences.logs_diagnostics.enable_file_logging", "Enable file logging"))
        self._widgets['enable_file_logging'] = enable_file_log
        log_form.addRow("", enable_file_log)

        # Max log file size
        max_log_size = PrefDragValue(min_v=1, max_v=100, default=10, suffix=" MB")
        self._widgets['max_log_file_size_mb'] = max_log_size
        log_form.addRow(
            make_pref_label(I18N.tr("preferences.logs_diagnostics.max_log_size", "Max log file size (MB):")),
            max_log_size
        )

        # Open log directory button
        open_log_dir_btn = QPushButton(I18N.tr("preferences.logs_diagnostics.open_log_dir", "Open Log Directory"))
        open_log_dir_btn.setObjectName("PrefActionBtn")
        open_log_dir_btn.setFixedHeight(36)
        open_log_dir_btn.clicked.connect(self._open_log_directory)
        log_form.addRow("", open_log_dir_btn)

        layout.addWidget(log_group)

        # Diagnostics group
        diag_group = self._create_group("preferences.advanced.section_diag_title", "Diagnostics")
        diag_form = make_pref_form(diag_group)

        # Show version in title
        show_version = QCheckBox(I18N.tr("preferences.logs_diagnostics.show_version_in_title", "Show version in title bar"))
        self._widgets['show_version_in_title'] = show_version
        diag_form.addRow("", show_version)

        # Show GPU info
        show_gpu = QCheckBox(I18N.tr("preferences.logs_diagnostics.show_gpu_info", "Show GPU info label"))
        self._widgets['show_gpu_info'] = show_gpu
        diag_form.addRow("", show_gpu)

        # Export diagnostics button
        export_diag_btn = QPushButton(I18N.tr("preferences.logs_diagnostics.export_diagnostics", "Export Diagnostics Report"))
        export_diag_btn.setObjectName("PrefActionBtn")
        export_diag_btn.setFixedHeight(36)
        export_diag_btn.clicked.connect(self._export_diagnostics)
        diag_form.addRow("", export_diag_btn)

        layout.addWidget(diag_group)

        # Developer group — maintainer-only tools, applied on next launch
        dev_group = self._create_group("preferences.developer.section_title", "Developer")
        dev_form = make_pref_form(dev_group)

        dev_mode = QCheckBox(
            I18N.tr("preferences.developer.enable", "Enable developer mode")
        )
        self._widgets['developer_mode'] = dev_mode
        dev_form.addRow("", dev_mode)

        dev_note = QLabel(I18N.tr(
            "preferences.developer.note",
            "Developer mode change requires application restart. It exposes "
            "maintainer tools such as Window > Set as Default, which updates "
            "the committed default_layout.json (ship it via git commit)."))
        dev_note.setObjectName("PrefNoteLabel")
        dev_note.setWordWrap(True)
        dev_form.addRow("", dev_note)

        layout.addWidget(dev_group)

        # Plugin Settings group
        plugin_group = self._create_group("preferences.plugin_management.section_settings_title", "Plugin Settings")
        plugin_form = make_pref_form(plugin_group)

        # Plugin directory
        plugin_dir_edit = self._create_path_edit("Select Plugin Directory")
        self._widgets['plugin_directory'] = plugin_dir_edit._line_edit
        plugin_form.addRow(
            make_pref_label(I18N.tr("preferences.plugin_management.plugin_directory", "Plugin Directory:")),
            plugin_dir_edit
        )

        # Auto-scan plugins
        auto_scan = QCheckBox(I18N.tr("preferences.plugin_management.auto_scan", "Auto-scan plugins on startup"))
        self._widgets['auto_scan_plugins'] = auto_scan
        plugin_form.addRow("", auto_scan)

        layout.addWidget(plugin_group)

        # Installed Plugins group
        installed_group = self._create_group("preferences.plugin_management.section_list_title", "Installed Plugins")
        installed_layout = QVBoxLayout(installed_group)
        installed_layout.setContentsMargins(16, 20, 16, 16)
        installed_layout.setSpacing(12)

        self._plugin_list = QListWidget()
        self._plugin_list.setObjectName("PrefPluginList")
        self._plugin_list.setMinimumHeight(150)
        installed_layout.addWidget(self._plugin_list)

        btn_layout = QHBoxLayout()
        refresh_btn = QPushButton(I18N.tr("preferences.plugin_management.refresh_plugins", "Refresh Plugins"))
        refresh_btn.setObjectName("PrefActionBtn")
        refresh_btn.setFixedHeight(36)
        refresh_btn.clicked.connect(self._refresh_plugins)
        btn_layout.addWidget(refresh_btn)

        open_plugin_dir_btn = QPushButton(I18N.tr("preferences.plugin_management.open_plugin_dir", "Open Plugin Directory"))
        open_plugin_dir_btn.setObjectName("PrefActionBtn")
        open_plugin_dir_btn.setFixedHeight(36)
        open_plugin_dir_btn.clicked.connect(self._open_plugin_directory)
        btn_layout.addWidget(open_plugin_dir_btn)

        installed_layout.addLayout(btn_layout)
        layout.addWidget(installed_group)
        layout.addStretch()
        return page

    # ── Helper Methods ─────────────────────────────────────────────

    def _wrap_in_scroll_area(self) -> QScrollArea:
        """Create a QScrollArea wrapping a QWidget for page content."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("PrefPageContent")
        scroll.setWidget(content)
        return scroll

    def _create_group(self, title_key: str, default_title: str) -> QGroupBox:
        """Create a styled QGroupBox with i18n title."""
        group = QGroupBox(I18N.tr(title_key, default_title))
        group.setObjectName("PrefSettingsGroup")
        return group

    def _create_path_edit(self, dialog_title: str, is_file: bool = False) -> QWidget:
        """Create a path edit widget with browse button. Returns container QWidget."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        edit = QLineEdit()
        edit.setObjectName("PrefPathEdit")
        edit.setPlaceholderText(dialog_title)
        # 统一文本框样式（与导出面板输入框一致）
        _apply_text_input_style(edit)
        browse_btn = QPushButton(I18N.tr("preferences.export.browse", "Browse"))
        browse_btn.setObjectName("PrefBrowseBtn")
        browse_btn.setFixedWidth(80)
        # 统一按钮样式（与导出面板 Browse/Choose 同款）
        _apply_compact_button_style(browse_btn)
        browse_btn.clicked.connect(lambda: self._browse_path(edit, dialog_title, is_file=is_file))
        layout.addWidget(edit, 1)
        layout.addWidget(browse_btn)
        # Store reference to QLineEdit for data access
        widget._line_edit = edit
        return widget

    def _browse_path(self, widget_or_edit, title: str, is_file: bool = False) -> None:
        """Open directory or file picker for path input."""
        # Handle both QWidget (with _line_edit) and QLineEdit
        if hasattr(widget_or_edit, '_line_edit'):
            line_edit = widget_or_edit._line_edit
        else:
            line_edit = widget_or_edit
        current = line_edit.text()
        if is_file:
            path, _ = QFileDialog.getOpenFileName(
                self, title, current if current else "", "All Files (*)"
            )
        else:
            path = QFileDialog.getExistingDirectory(
                self, title, current if current else ""
            )
        if path:
            line_edit.setText(path)

    def _open_shortcut_editor(self) -> None:
        """Open the shortcut editor dialog."""
        if self._shortcut_manager:
            from src.app.ui.shortcut_dialog import ShortcutDialog
            dialog = ShortcutDialog(self._shortcut_manager, self)
            dialog.exec()

    def _reset_all_shortcuts(self) -> None:
        """Reset all keyboard shortcuts to defaults."""
        reply = QMessageBox.question(
            self,
            I18N.tr("preferences.shortcuts.reset_all", "Reset All Shortcuts"),
            I18N.tr("preferences.shortcuts.reset_all_confirm",
                    "Are you sure you want to reset all shortcuts to defaults?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes and self._shortcut_manager:
            self._shortcut_manager.reset_all()

    # ── New Page Action Handlers ───────────────────────────────────

    def _clear_cache(self) -> None:
        """Clear application cache."""
        reply = QMessageBox.question(
            self,
            I18N.tr("preferences.file_storage.clear_cache", "Clear Cache"),
            I18N.tr("preferences.file_storage.clear_cache_confirm",
                    "Clear all cached data? This cannot be undone."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            from src.shared.io.file_io import FileIO
            cleaned = FileIO.cleanup_old_cache(max_age_days=0)
            # Also clean the .images_cache directory
            images_cache = FileIO.CACHE_DIR
            extra = 0
            if os.path.exists(images_cache):
                for f in os.listdir(images_cache):
                    fp = os.path.join(images_cache, f)
                    try:
                        if os.path.isfile(fp):
                            os.remove(fp)
                            extra += 1
                        elif os.path.isdir(fp):
                            import shutil
                            shutil.rmtree(fp)
                            extra += 1
                    except OSError:
                        pass
            total = cleaned + extra
            self._update_cache_size_label()
            QMessageBox.information(
                self,
                I18N.tr("preferences.file_storage.clear_cache", "Clear Cache"),
                I18N.tr("preferences.file_storage.cache_cleared",
                        "Cache cleared. {count} files removed.").format(count=total)
            )

    def _export_diagnostics(self) -> None:
        """Export diagnostic report (AUD-03912: heavy probes off the UI thread).

        onnxruntime / torch 的冷导入可达数秒 —— 选定保存路径后把重依赖
        探测与 JSON 写盘移入后台线程，期间显示忙碌光标，完成后经
        ``diagnostics_export_finished`` 信号回 UI 线程提示结果。
        """
        from datetime import datetime

        diagnostics = {
            "timestamp": datetime.now().isoformat(),
            "application": "Topos Color",
            "python_version": sys.version,
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
            },
            "config_snapshot": dict(self._config_manager.config),
        }

        # Save dialog first (UI thread), then collect in background.
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("preferences.advanced.export_diagnostics", "Export Diagnostics"),
            "topos_diagnostics.json",
            "JSON Files (*.json);;All Files (*)"
        )
        if not file_path:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        threading.Thread(
            target=self._write_diagnostics_report,
            args=(diagnostics, file_path),
            name="prefs-diagnostics-export",
            daemon=True,
        ).start()

    @staticmethod
    def _probe_runtime_diagnostics() -> dict:
        """Probe optional heavy runtimes. Safe to call on a worker thread."""
        probes: dict = {}

        # ONNX / GPU info
        try:
            import onnxruntime as ort
            probes["onnx_runtime"] = {
                "version": ort.__version__,
                "available_providers": ort.get_available_providers(),
            }
        except ImportError:
            probes["onnx_runtime"] = "not installed"

        # CUDA info
        try:
            import torch
            probes["cuda"] = {
                "available": torch.cuda.is_available(),
                "device_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
            }
            if torch.cuda.is_available():
                probes["cuda"]["device_name"] = torch.cuda.get_device_name(0)
        except ImportError:
            pass

        return probes

    def _write_diagnostics_report(self, diagnostics: dict, file_path: str) -> None:
        """Worker: probe runtimes, write the JSON, report back via signal."""
        ok = False
        detail = file_path
        try:
            diagnostics.update(self._probe_runtime_diagnostics())
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(diagnostics, f, indent=2, ensure_ascii=False)
            ok = True
        except Exception as e:
            detail = str(e)
        try:
            self.diagnostics_export_finished.emit(ok, detail)
        except RuntimeError:
            logger.debug("Diagnostics export result dropped: dialog gone")

    def _on_diagnostics_export_finished(self, ok: bool, detail: str) -> None:
        """UI-thread slot: restore the cursor and report the outcome."""
        QApplication.restoreOverrideCursor()
        if ok:
            QMessageBox.information(
                self,
                I18N.tr("preferences.advanced.export_diagnostics", "Export Diagnostics"),
                I18N.tr("preferences.advanced.diagnostics_saved",
                        "Diagnostics saved to: {path}").format(path=detail)
            )
        else:
            QMessageBox.warning(
                self,
                I18N.tr("preferences.advanced.export_diagnostics", "Export Diagnostics"),
                I18N.tr("preferences.advanced.diagnostics_failed",
                        "Failed to save diagnostics: {error}").format(error=detail)
            )

    def _open_log_directory(self) -> None:
        """Open log directory in file explorer."""
        from src.shared.io.file_io import FileIO
        # Try custom log path, then cache dir, then project root
        log_dir = self._config_manager.get("log_file_path") or ""
        if log_dir:
            log_dir = os.path.dirname(log_dir)
        if not log_dir or not os.path.exists(log_dir):
            log_dir = FileIO.CACHE_DIR
        if not os.path.exists(log_dir):
            log_dir = FileIO.PROJECT_ROOT
        os.makedirs(log_dir, exist_ok=True)
        self._open_in_file_manager(log_dir)

    def _fe_plugin_bridge(self):
        """[R1-15] 访问 file_explorer 的插件系统模块（可能未随宿主可用）。

        import 零副作用（ARCH-6）；插件列表只在 ensure_runtime() 后有内容。"""
        try:
            import file_explorer.main as fe_main
            return fe_main
        except Exception:
            return None

    def _fe_disabled_plugins(self, fe_main) -> list:
        if fe_main is None:
            return []
        try:
            conf = fe_main.load_config() or {}
            return list(conf.get("disabled_plugins", []) or [])
        except Exception:
            return []

    def _refresh_plugins(self) -> None:
        """Refresh plugin list.

        [R1-15] file_explorer 插件（FE/plugins/，plugin.json 注册制）与宿主
        仓库根 plugins/（.tglsl/.tai）是两套体系。此前这里扫仓库根 plugins/
        且 disabled_plugins 读写宿主 config——与 FE 插件系统毫无交集，UI
        实际停用不了任何 FE 插件。现在列表直接读 plugin_manager.plugins，
        停用状态写回 FE 自己的 config.json（见 _apply_settings）。"""
        self._plugin_list.clear()

        fe_main = self._fe_plugin_bridge()
        plugins = list(getattr(fe_main.plugin_manager, "plugins", [])) if fe_main else []
        disabled = set(self._fe_disabled_plugins(fe_main))

        if not plugins:
            item = QListWidgetItem(
                I18N.tr("preferences.plugin_management.no_plugins_found",
                        "No plugins found"))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._plugin_list.addItem(item)
            return

        for plugin in plugins:
            name = plugin.get("name", "?")
            info = plugin.get("info") or {}
            version = info.get("version", "?")
            desc = info.get("description") or ""
            label = f"{name} v{version}"
            if desc:
                label += f" — {desc}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Unchecked if name in disabled else Qt.CheckState.Checked)
            self._plugin_list.addItem(item)

    def _open_plugin_directory(self) -> None:
        """Open plugin directory in file explorer."""
        from src.shared.io.file_io import FileIO
        plugin_dir = self._config_manager.get("plugin_directory") or "plugins"
        if not os.path.isabs(plugin_dir):
            plugin_dir = os.path.join(FileIO.PROJECT_ROOT, plugin_dir)
        os.makedirs(plugin_dir, exist_ok=True)
        self._open_in_file_manager(plugin_dir)

    # ── Utility Methods ──────────────────────────────────────────────

    def _open_in_file_manager(self, path: str) -> None:
        """Open a directory in the system file manager."""
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(
                self,
                I18N.tr("preferences.file_storage.open_directory", "Open Directory"),
                I18N.tr("preferences.file_storage.open_directory_failed",
                        "Failed to open directory: {error}").format(error=str(e))
            )

    def _update_cache_size_label(self) -> None:
        """Recalculate and display total cache size (AUD-03908).

        统计要对两个缓存目录做递归遍历（文件可达数千），不再在 UI 线程
        同步扫盘 —— 后台线程汇总后经 ``cache_size_ready`` 信号回填标签；
        已有扫描进行中时直接复用其结果，不叠加重复扫描。
        """
        if self._cache_scan_in_progress:
            return
        self._cache_scan_in_progress = True
        threading.Thread(
            target=self._cache_size_worker,
            name="prefs-cache-size-scan",
            daemon=True,
        ).start()

    def _cache_size_worker(self) -> None:
        """Worker: total the cache directories off the UI thread."""
        try:
            total_bytes = self._compute_cache_bytes()
        except Exception as exc:  # 统计失败只影响标签刷新，不影响对话框
            logger.debug("Cache size scan failed: %s", exc)
            total_bytes = -1
        try:
            self.cache_size_ready.emit(total_bytes)
        except RuntimeError:
            # 对话框已销毁：结果无处投递，丢弃即可
            logger.debug("Cache size scan result dropped: dialog gone")

    @staticmethod
    def _compute_cache_bytes() -> int:
        """Recursively total the app cache directories (AUD-03908).

        ``_clear_cache`` 会 rmtree 整个子目录，旧的顶层 ``os.listdir``
        统计跳过目录、长期低估实际占用；``os.walk`` 逐文件累加修正。
        """
        from src.shared.io.file_io import FileIO
        total_bytes = 0
        for cache_dir in (FileIO.BAKED_CACHE_DIR, FileIO.CACHE_DIR):
            if not os.path.exists(cache_dir):
                continue
            for root, _dirs, files in os.walk(cache_dir):
                for name in files:
                    try:
                        total_bytes += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        # 文件在扫描间隙被清理/无权限：跳过该文件继续
                        continue
        return total_bytes

    def _on_cache_size_ready(self, total_bytes: int) -> None:
        """UI-thread slot: format the scanned total into the label."""
        self._cache_scan_in_progress = False
        if total_bytes < 0:
            return

        # Format size
        if total_bytes < 1024:
            size_str = f"{total_bytes} B"
        elif total_bytes < 1024 * 1024:
            size_str = f"{total_bytes / 1024:.1f} KB"
        elif total_bytes < 1024 * 1024 * 1024:
            size_str = f"{total_bytes / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{total_bytes / (1024 * 1024 * 1024):.2f} GB"

        label = self._widgets.get('cache_size_label')
        if label:
            label.setText(
                I18N.tr("preferences.file_storage.cache_size_label", "Current cache size:") + " " + size_str)

    # ── Data Loading ───────────────────────────────────────────────

    @staticmethod
    def _coerce_int(value: object, default: int) -> int:
        """[AUD-03907] 外部 config 数值容错转换。

        config.json 属外部数据（手工编辑/旧版本残留/损坏）：非数值类型
        （如 ``"6x"``、列表、dict）直接 ``int()``/``setValue()`` 会在
        ``__init__`` 的装载路径抛 ValueError/TypeError 使对话框构造即崩溃。
        转换失败时回退默认值，由后续 Apply 落回合法配置。
        """
        if isinstance(value, bool):
            # bool 是 int 子类，但 "panel_opacity": true 属损坏数据
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return default
        return default

    def _select_combo_by_data(self, widget_key: str, data_value) -> None:
        """Select a combo box item by its data value."""
        combo = self._widgets.get(widget_key)
        if combo is None:
            return
        for i in range(combo.count()):
            if combo.itemData(i) == data_value:
                combo.setCurrentIndex(i)
                return
        # Default to first item if not found
        if combo.count() > 0:
            combo.setCurrentIndex(0)

    def _load_current_settings(self) -> None:
        """Load current settings from config manager into all UI widgets."""
        cfg = self._config_manager.config

        # General
        locale = cfg.get('locale', 'en_US')
        combo = self._widgets['locale']
        for i in range(combo.count()):
            if combo.itemData(i) == locale:
                combo.setCurrentIndex(i)
                break

        # Work Mode (studio only; widget is None for other launchers)
        wm_combo = self._widgets.get('work_mode')
        if wm_combo is not None:
            work_mode = cfg.get('work_mode', 'all')
            for i in range(wm_combo.count()):
                if wm_combo.itemData(i) == work_mode:
                    wm_combo.setCurrentIndex(i)
                    break

        self._widgets['auto_save_interval'].setValue(
            self._coerce_int(cfg.get('auto_save_interval'), 5))
        self._widgets['max_recent_files'].setValue(
            self._coerce_int(cfg.get('max_recent_files'), 10))
        self._widgets['show_welcome_on_startup'].setChecked(
            cfg.get('show_welcome_on_startup', True)
        )
        self._widgets['confirm_before_reset'].setChecked(cfg.get('confirm_before_reset', True))
        self._widgets['auto_load_last_project'].setChecked(cfg.get('auto_load_last_project', True))
        self._widgets['undo_history_limit'].setValue(
            self._coerce_int(cfg.get('undo_history_limit'), 50))
        self._widgets['check_updates_on_startup'].setChecked(cfg.get('check_updates_on_startup', True))

        # Appearance
        scale = cfg.get('ui_scale', 100)
        scale_combo = self._widgets['ui_scale']
        for i in range(scale_combo.count()):
            if scale_combo.itemData(i) == scale:
                scale_combo.setCurrentIndex(i)
                break

        self._widgets['panel_opacity'].setValue(
            self._coerce_int(cfg.get('panel_opacity'), 100))
        self._widgets['show_tooltips'].setChecked(cfg.get('show_tooltips', True))
        # Accent color
        accent = cfg.get('accent_color', '#ff8800')
        accent_combo = self._widgets['accent_color']
        for i in range(accent_combo.count()):
            if accent_combo.itemData(i) == accent:
                accent_combo.setCurrentIndex(i)
                break

        self._widgets['show_panel_titles'].setChecked(cfg.get('show_panel_titles', True))
        self._widgets['zoom_speed'].setValue(self._coerce_int(cfg.get('zoom_speed'), 100))

        # Performance (app-level)
        self._widgets['gpu_acceleration'].setChecked(cfg.get('gpu_acceleration', True))
        backend = cfg.get('backend') or {}
        graphics_backend = backend.get('graphics', 'auto') if isinstance(backend, dict) else 'auto'
        graphics_combo = self._widgets['timeline_graphics_backend']
        for i in range(graphics_combo.count()):
            if graphics_combo.itemData(i) == graphics_backend:
                graphics_combo.setCurrentIndex(i)
                break

        # macOS VT zero-copy
        vt_mode = cfg.get('video_macos_vt_zero_copy', 'auto')
        vt_combo = self._widgets['video_macos_vt_zero_copy']
        for i in range(vt_combo.count()):
            if vt_combo.itemData(i) == vt_mode:
                vt_combo.setCurrentIndex(i)
                break

        resolution = self._coerce_int(cfg.get('video_playback_resolution'), 1920)
        res_combo = self._widgets['video_playback_resolution']
        for i, (label, value) in enumerate(self.VIDEO_RESOLUTIONS.items()):
            if value == resolution:
                res_combo.setCurrentIndex(i)
                break

        self._widgets['processing_threads'].setValue(
            self._coerce_int(cfg.get('processing_threads'), 0))

        # Pass Fusion Mode
        fusion_mode = cfg.get('pass_fusion_mode', 'conservative')
        fusion_combo = self._widgets['pass_fusion_mode']
        for i in range(fusion_combo.count()):
            if fusion_combo.itemData(i) == fusion_mode:
                fusion_combo.setCurrentIndex(i)
                break

        # Shortcuts
        self._widgets['global_shortcuts'].setChecked(cfg.get('global_shortcuts', False))
        self._widgets['show_shortcut_conflicts'].setChecked(cfg.get('show_shortcut_conflicts', True))

        # File & Storage
        self._widgets['default_open_directory'].setText(cfg.get('default_open_directory', ''))
        self._widgets['auto_backup_interval'].setValue(
            self._coerce_int(cfg.get('auto_backup_interval'), 0))
        self._widgets['backup_directory'].setText(cfg.get('backup_directory', ''))
        self._widgets['max_backup_count'].setValue(
            self._coerce_int(cfg.get('max_backup_count'), 5))
        self._widgets['cache_directory'].setText(cfg.get('cache_directory', ''))
        self._widgets['cache_size_limit_gb'].setValue(
            self._coerce_int(cfg.get('cache_size_limit_gb'), 10))
        self._widgets['cleanup_temp_on_exit'].setChecked(cfg.get('cleanup_temp_on_exit', True))
        self._widgets['temp_file_max_age_days'].setValue(
            self._coerce_int(cfg.get('temp_file_max_age_days'), 7))

        # Render Cache
        self._select_combo_by_data('render_cache_auto_mode', cfg.get('render_cache_auto_mode', 'off'))
        self._widgets['render_cache_auto_threshold'].setValue(
            self._coerce_int(cfg.get('render_cache_auto_threshold'), 6))
        self._select_combo_by_data('render_cache_codec', cfg.get('render_cache_codec', 'topos_proxy'))
        self._select_combo_by_data('proxy_format', cfg.get('proxy_format', 'topos_proxy'))
        self._widgets['render_cache_directory'].setText(cfg.get('render_cache_directory', ''))
        self._select_combo_by_data('render_cache_resolution_mode', cfg.get('render_cache_resolution_mode', 'timeline'))
        self._widgets['render_cache_max_size_gb'].setValue(
            self._coerce_int(cfg.get('render_cache_max_size_gb'), 50))

        # Timeline Defaults — the page is hidden in Image mode, so every
        # widget may be absent; guard each access.
        self._select_combo_by_data('default_timeline_fps', cfg.get('default_timeline_fps', '25'))
        _tw = self._widgets.get('default_timeline_width')
        if _tw is not None:
            _tw.setValue(self._coerce_int(cfg.get('default_timeline_width'), 1920))
        _th = self._widgets.get('default_timeline_height')
        if _th is not None:
            _th.setValue(self._coerce_int(cfg.get('default_timeline_height'), 1080))
        # [BUG-B3-03] 按配置宽高同步预设下拉（无匹配选 "Custom"），
        # 避免加载后下拉停在首项、一触碰即覆盖宽高。
        if _tw is not None and _th is not None:
            self._sync_timeline_res_combo(_tw.value(), _th.value())
        self._select_combo_by_data('default_timeline_sample_rate', cfg.get('default_timeline_sample_rate', 48000))
        self._select_combo_by_data('default_timeline_pixel_aspect_ratio', cfg.get('default_timeline_pixel_aspect_ratio', '1.0'))
        self._select_combo_by_data('default_timeline_field_order', cfg.get('default_timeline_field_order', 'progressive'))
        self._select_combo_by_data('default_timeline_audio_track_type', cfg.get('default_timeline_audio_track_type', 'stereo'))
        _ttc = self._widgets.get('default_timeline_start_timecode')
        if _ttc is not None:
            _ttc.setText(cfg.get('default_timeline_start_timecode', '00:00:00:00'))
        # P10.3 - Subtitle default clip duration (stored as frames)
        sub_dur_w = self._widgets.get('default_subtitle_clip_duration_frames')
        if sub_dur_w is not None:
            sub_dur_w.setValue(
                self._coerce_int(cfg.get('default_subtitle_clip_duration_frames'), 72))

        # Advanced (Logs & Plugins)
        log_level = cfg.get('log_level', 'warning')
        log_level_combo = self._widgets['log_level']
        for i in range(log_level_combo.count()):
            if log_level_combo.itemData(i) == log_level:
                log_level_combo.setCurrentIndex(i)
                break
        self._widgets['log_file_path'].setText(cfg.get('log_file_path', ''))
        self._widgets['enable_file_logging'].setChecked(cfg.get('enable_file_logging', False))
        self._widgets['max_log_file_size_mb'].setValue(
            self._coerce_int(cfg.get('max_log_file_size_mb'), 10))
        self._widgets['show_version_in_title'].setChecked(cfg.get('show_version_in_title', True))
        self._widgets['show_gpu_info'].setChecked(cfg.get('show_gpu_info', False))
        self._widgets['developer_mode'].setChecked(bool(cfg.get('developer_mode', False)))
        self._widgets['plugin_directory'].setText(cfg.get('plugin_directory', 'plugins'))
        self._widgets['auto_scan_plugins'].setChecked(cfg.get('auto_scan_plugins', True))
        self._disabled_plugins = list(cfg.get('disabled_plugins', []))  # Update from config

        # Delegate load to module preference pages
        for page_id, page_obj in self._module_pages.items():
            try:
                page_obj.load(cfg)
            except Exception:
                # [AUD-03906] 此前 except-pass 静默吞掉模块页装载失败，
                # 该模块设置停留在默认值且无从排查。至少记录完整堆栈。
                logger.exception("Module preference page load failed: %s", page_id)

        # Update cache size display
        self._update_cache_size_label()

    # ── Event Handlers ──────────────────────────────────────────────

    def _on_category_changed(self, row: int) -> None:
        """Switch to the selected category page."""
        if 0 <= row < self._pages.count():
            self._pages.setCurrentIndex(row)
            # Refresh cache size when switching to File & Storage page
            page_ids = self._page_registry.get_pages(self._enabled_features)
            if row < len(page_ids) and page_ids[row].page_id == "app.file_storage":
                self._update_cache_size_label()

    def select_page(self, page_id: str) -> None:
        """选择指定模块页面（供面板内的快捷入口使用）。"""
        pages = self._page_registry.get_pages(self._enabled_features)
        for row, descriptor in enumerate(pages):
            if descriptor.page_id == page_id:
                self._category_list.setCurrentRow(row)
                return

    def _apply_settings(self) -> None:
        """Collect all settings and apply to config (without closing)."""
        cfg = self._config_manager

        # General
        cfg.set('locale', self._widgets['locale'].currentData(), auto_save=False)
        # Work Mode (studio only; widget may be None)
        wm_widget = self._widgets.get('work_mode')
        if wm_widget is not None:
            cfg.set('work_mode', wm_widget.currentData(), auto_save=False)
        cfg.set('auto_save_interval', self._widgets['auto_save_interval'].intValue(), auto_save=False)
        cfg.set('max_recent_files', self._widgets['max_recent_files'].intValue(), auto_save=False)
        cfg.set('show_welcome_on_startup', self._widgets['show_welcome_on_startup'].isChecked(), auto_save=False)
        cfg.set('confirm_before_reset', self._widgets['confirm_before_reset'].isChecked(), auto_save=False)
        cfg.set('auto_load_last_project', self._widgets['auto_load_last_project'].isChecked(), auto_save=False)
        cfg.set('undo_history_limit', self._widgets['undo_history_limit'].intValue(), auto_save=False)
        cfg.set('check_updates_on_startup', self._widgets['check_updates_on_startup'].isChecked(), auto_save=False)

        # Appearance
        cfg.set('ui_scale', self._widgets['ui_scale'].currentData(), auto_save=False)
        cfg.set('panel_opacity', self._widgets['panel_opacity'].value(), auto_save=False)
        cfg.set('show_tooltips', self._widgets['show_tooltips'].isChecked(), auto_save=False)
        cfg.set('accent_color', self._widgets['accent_color'].currentData(), auto_save=False)
        cfg.set('show_panel_titles', self._widgets['show_panel_titles'].isChecked(), auto_save=False)
        cfg.set('zoom_speed', self._widgets['zoom_speed'].value(), auto_save=False)

        # Performance (app-level)
        cfg.set('gpu_acceleration', self._widgets['gpu_acceleration'].isChecked(), auto_save=False)
        backend = cfg.get('backend') or {}
        backend = dict(backend) if isinstance(backend, dict) else {}
        backend['graphics'] = self._widgets['timeline_graphics_backend'].currentData()
        cfg.set('backend', backend, auto_save=False)
        cfg.set('video_macos_vt_zero_copy',
                self._widgets['video_macos_vt_zero_copy'].currentData(),
                auto_save=False)
        res_label = self._widgets['video_playback_resolution'].currentText()
        resolution = self.VIDEO_RESOLUTIONS.get(res_label, 1920)
        cfg.set('video_playback_resolution', resolution, auto_save=False)
        cfg.set('processing_threads', self._widgets['processing_threads'].intValue(), auto_save=False)
        cfg.set('pass_fusion_mode', self._widgets['pass_fusion_mode'].currentData(), auto_save=False)

        # Shortcuts
        cfg.set('global_shortcuts', self._widgets['global_shortcuts'].isChecked(), auto_save=False)
        cfg.set('show_shortcut_conflicts', self._widgets['show_shortcut_conflicts'].isChecked(), auto_save=False)

        # File & Storage
        cfg.set('default_open_directory', self._widgets['default_open_directory'].text(), auto_save=False)
        cfg.set('auto_backup_interval', self._widgets['auto_backup_interval'].intValue(), auto_save=False)
        cfg.set('backup_directory', self._widgets['backup_directory'].text(), auto_save=False)
        cfg.set('max_backup_count', self._widgets['max_backup_count'].intValue(), auto_save=False)
        cfg.set('cache_directory', self._widgets['cache_directory'].text(), auto_save=False)
        cfg.set('cache_size_limit_gb', self._widgets['cache_size_limit_gb'].intValue(), auto_save=False)
        cfg.set('cleanup_temp_on_exit', self._widgets['cleanup_temp_on_exit'].isChecked(), auto_save=False)
        cfg.set('temp_file_max_age_days', self._widgets['temp_file_max_age_days'].intValue(), auto_save=False)

        # Render Cache
        cfg.set('render_cache_auto_mode', self._widgets['render_cache_auto_mode'].currentData(), auto_save=False)
        cfg.set('render_cache_auto_threshold', self._widgets['render_cache_auto_threshold'].value(), auto_save=False)
        cfg.set('render_cache_codec', self._widgets['render_cache_codec'].currentData(), auto_save=False)
        cfg.set('proxy_format', self._widgets['proxy_format'].currentData(), auto_save=False)
        cfg.set('render_cache_directory', self._widgets['render_cache_directory'].text(), auto_save=False)
        cfg.set('render_cache_resolution_mode', self._widgets['render_cache_resolution_mode'].currentData(), auto_save=False)
        cfg.set('render_cache_max_size_gb', self._widgets['render_cache_max_size_gb'].intValue(), auto_save=False)

        # Timeline Defaults — the page is hidden in Image mode, so widgets
        # may be absent; read each defensively and skip when missing.
        _fps_w = self._widgets.get('default_timeline_fps')
        if _fps_w is not None:
            cfg.set('default_timeline_fps', _fps_w.currentData(), auto_save=False)
        _tw = self._widgets.get('default_timeline_width')
        if _tw is not None:
            cfg.set('default_timeline_width', _tw.intValue(), auto_save=False)
        _th = self._widgets.get('default_timeline_height')
        if _th is not None:
            cfg.set('default_timeline_height', _th.intValue(), auto_save=False)
        _sr_w = self._widgets.get('default_timeline_sample_rate')
        if _sr_w is not None:
            cfg.set('default_timeline_sample_rate', _sr_w.currentData(), auto_save=False)
        _par_w = self._widgets.get('default_timeline_pixel_aspect_ratio')
        if _par_w is not None:
            cfg.set('default_timeline_pixel_aspect_ratio', _par_w.currentData(), auto_save=False)
        _fo_w = self._widgets.get('default_timeline_field_order')
        if _fo_w is not None:
            cfg.set('default_timeline_field_order', _fo_w.currentData(), auto_save=False)
        _at_w = self._widgets.get('default_timeline_audio_track_type')
        if _at_w is not None:
            cfg.set('default_timeline_audio_track_type', _at_w.currentData(), auto_save=False)
        _ttc = self._widgets.get('default_timeline_start_timecode')
        if _ttc is not None:
            cfg.set('default_timeline_start_timecode', _ttc.text(), auto_save=False)
        # P10.3 - Subtitle default clip duration (stored as frames)
        sub_dur_w = self._widgets.get('default_subtitle_clip_duration_frames')
        if sub_dur_w is not None:
            cfg.set('default_subtitle_clip_duration_frames', int(sub_dur_w.intValue()), auto_save=False)

        # Advanced (Logs & Plugins)
        cfg.set('log_level', self._widgets['log_level'].currentData(), auto_save=False)
        cfg.set('log_file_path', self._widgets['log_file_path'].text(), auto_save=False)
        cfg.set('enable_file_logging', self._widgets['enable_file_logging'].isChecked(), auto_save=False)
        cfg.set('max_log_file_size_mb', self._widgets['max_log_file_size_mb'].intValue(), auto_save=False)
        cfg.set('show_version_in_title', self._widgets['show_version_in_title'].isChecked(), auto_save=False)
        cfg.set('show_gpu_info', self._widgets['show_gpu_info'].isChecked(), auto_save=False)
        cfg.set('developer_mode', self._widgets['developer_mode'].isChecked(), auto_save=False)
        cfg.set('plugin_directory', self._widgets['plugin_directory'].text(), auto_save=False)
        cfg.set('auto_scan_plugins', self._widgets['auto_scan_plugins'].isChecked(), auto_save=False)
        # Collect disabled plugins from check states. [AUD-03900] The list is
        # only populated by an explicit "Refresh Plugins" click; a never
        # refreshed (or placeholder-only) list carries no real data, and
        # writing it back would wipe file_explorer's disabled_plugins and
        # silently re-enable every plugin the user disabled there.
        disabled_plugins: list = []
        has_plugin_entries = False
        for i in range(self._plugin_list.count()):
            item = self._plugin_list.item(i)
            entry = item.data(Qt.ItemDataRole.UserRole)
            if not entry:
                continue  # placeholder rows ("No plugins found") hold no data
            has_plugin_entries = True
            if item.checkState() == Qt.CheckState.Unchecked:
                disabled_plugins.append(entry)
        # [R1-15] 停用列表写回 file_explorer 自己的 config.json（FE 插件
        # 系统运行时读的是它），不再误写宿主 config。
        # [AUD-03900] 仅在列表含真实插件条目（已 Refresh 且 FE 返回了插件）
        # 时才写回；否则跳过，保留 FE 侧现有的停用状态。
        fe_main = self._fe_plugin_bridge()
        if fe_main is not None and has_plugin_entries:
            try:
                fe_conf = fe_main.load_config() or {}
                if self._fe_config_snapshot is None:
                    # [AUD-03903] 本会话首次覆写 FE config：先留快照，
                    # 供 Cancel 时撤销这次直写（fe_main.save_config 直接
                    # 写 FE 自己的 config.json，绕过宿主 config 快照）。
                    self._fe_config_snapshot = dict(fe_conf)
                fe_conf["disabled_plugins"] = disabled_plugins
                fe_main.save_config(fe_conf)
            except Exception:
                logger.exception("Failed to write file_explorer disabled_plugins")
            self._disabled_plugins = list(disabled_plugins)

        # Delegate apply to module preference pages
        module_apply_failures: list[str] = []
        for page_id, page_obj in self._module_pages.items():
            try:
                page_obj.apply(cfg)
            except Exception:
                # [AUD-03906] 模块页 apply 抛错时界面仍提示已保存、
                # save_config 照常落盘——必须记录并警示用户，
                # 否则该模块设置实际未写入却毫无感知。
                logger.exception("Module preference page apply failed: %s", page_id)
                module_apply_failures.append(page_id)
        if module_apply_failures:
            QMessageBox.warning(
                self,
                I18N.tr("preferences.module_apply_failed.title", "Apply Failed"),
                I18N.tr(
                    "preferences.module_apply_failed.message",
                    "Some module settings could not be applied:\n{pages}",
                ).format(pages=", ".join(module_apply_failures)),
            )

        # Write all at once
        cfg.save_config()

    def _on_apply(self) -> None:
        """Apply settings without closing dialog."""
        self._applied = True
        with self._tracking_config_writes():
            self._apply_settings()
        # Refresh VT zero-copy capability cache when video settings change
        try:
            from src.shared.media.macos_vt.capability import clear_capability_cache
            clear_capability_cache()
        except Exception as exc:
            logger.warning("Failed to refresh VT capability cache after apply: %s", exc)

    def _on_save(self) -> None:
        """Apply settings and close dialog."""
        self._apply_settings()
        
        # Check if locale changed -> need restart
        locale = self._widgets['locale'].currentData()
        if locale != self._initial_locale:
            self._needs_restart = True

        # Check if work mode changed -> need restart (feature flags change)
        wm_widget = self._widgets.get('work_mode')
        if wm_widget is not None and wm_widget.currentData() != self._initial_work_mode:
            self._needs_restart = True

        # Developer mode is read once at startup (menu wiring), so a change
        # needs a restart to take effect.
        if self._widgets['developer_mode'].isChecked() != self._initial_developer_mode:
            self._needs_restart = True

        # Show restart prompt before accept (restart_application respawns the
        # app in a fresh process if Yes)
        if self._needs_restart:
            self._show_restart_prompt()
        self.accept()

    def _on_cancel(self) -> None:
        """Restore settings and close dialog."""
        # If Apply was pressed, restore everything it wrote: the host config
        # snapshot, the file_explorer config snapshot and module page side
        # effects (see the helpers below).
        if self._applied:
            self._restore_config_snapshot()
            self._restore_fe_config_snapshot()
            self._rollback_module_pages()
        self._restore_preview_snapshot()
        self.reject()

    def _tracking_config_writes(self):
        """[AUD-03904] Record which config keys Apply touches.

        Install a temporary recording wrapper around the manager's ``set``
        for the duration of ``_apply_settings``. Cancel replays snapshot
        values for exactly these keys — undoing Apply's writes while
        preserving keys other components wrote while the dialog was open
        (recent lists, geometry, ...) which the old whole-dict replacement
        silently wiped.
        """
        from contextlib import contextmanager

        mgr = self._config_manager
        original_set = mgr.set
        applied_keys = self._applied_config_keys

        def _recording_set(key, value, auto_save: bool = True):
            applied_keys.add(key)
            return original_set(key, value, auto_save)

        @contextmanager
        def _tracker():
            mgr.set = _recording_set  # type: ignore[method-assign]
            try:
                yield
            finally:
                try:
                    delattr(mgr, "set")  # 撤掉实例级覆盖，回到类绑定方法
                except AttributeError:
                    mgr.set = original_set  # type: ignore[method-assign]

        return _tracker()

    def _restore_config_snapshot(self) -> None:
        """[AUD-03904] Restore the host config snapshot without the raw
        attribute swap.

        Previously Cancel did ``config_manager.config = dict(snapshot)``,
        replacing the whole dict object outside ConfigManager's
        ``_file_lock`` — a concurrent ``set()``/``save_config()`` could
        lose keys or tear a save, and stale ``_dirty_keys`` replayed against
        the replaced dict. Restore key-by-key through the manager's public
        write path (``set(auto_save=False)`` + one ``save_config()``),
        which never swaps the dict identity and keeps ``_dirty_keys``
        manager-owned, and only for the keys Apply actually touched.
        """
        mgr = self._config_manager
        for key in self._applied_config_keys:
            if key in self._config_snapshot:
                mgr.set(key, self._config_snapshot[key], auto_save=False)
            else:
                # Apply 新建且快照里不存在的键（如模块页新增键）：移除。
                mgr.config.pop(key, None)
        mgr.save_config()

    def _restore_fe_config_snapshot(self) -> None:
        """[AUD-03903] Undo the file_explorer config write done by Apply."""
        if self._fe_config_snapshot is None:
            return
        fe_main = self._fe_plugin_bridge()
        if fe_main is None:
            return
        try:
            fe_main.save_config(dict(self._fe_config_snapshot))
        except Exception:
            logger.exception("Failed to restore file_explorer config on cancel")

    def _rollback_module_pages(self) -> None:
        """[AUD-03903] Undo module preference page side effects on Cancel.

        Module pages' ``apply(cfg)`` may cause module-level side effects
        (cache invalidation, service rebuilds) that the host config
        snapshot cannot undo. Pages may expose an optional ``rollback()``
        hook; it is duck-typed here because the ModularPreferencePage
        protocol and the contributed pages live outside this file.
        """
        for page_id, page_obj in self._module_pages.items():
            rollback = getattr(page_obj, "rollback", None)
            if not callable(rollback):
                continue
            try:
                rollback()
            except Exception:
                logger.exception(
                    "Module preference page rollback failed: %s", page_id)

    def _on_restore_defaults(self) -> None:
        """Restore all settings to default values."""
        reply = QMessageBox.question(
            self,
            I18N.tr("preferences.restore_defaults", "Restore Defaults"),
            I18N.tr("preferences.restore_confirm",
                    "Restore all settings to their default values?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Block preview signals during mass widget updates to prevent flickering
        self._preview_blocked = True

        defaults = self._config_manager.DEFAULT_CONFIG

        # General
        combo = self._widgets['locale']
        for i in range(combo.count()):
            if combo.itemData(i) == defaults['locale']:
                combo.setCurrentIndex(i)
                break
        # Work Mode (studio only; widget may be None)
        wm_combo = self._widgets.get('work_mode')
        if wm_combo is not None:
            wm_default = defaults.get('work_mode', 'all')
            for i in range(wm_combo.count()):
                if wm_combo.itemData(i) == wm_default:
                    wm_combo.setCurrentIndex(i)
                    break
        self._widgets['auto_save_interval'].setValue(defaults['auto_save_interval'])
        self._widgets['max_recent_files'].setValue(defaults['max_recent_files'])
        self._widgets['show_welcome_on_startup'].setChecked(defaults['show_welcome_on_startup'])
        self._widgets['confirm_before_reset'].setChecked(defaults.get('confirm_before_reset', True))
        self._widgets['auto_load_last_project'].setChecked(defaults.get('auto_load_last_project', True))
        self._widgets['undo_history_limit'].setValue(defaults.get('undo_history_limit', 50))
        self._widgets['check_updates_on_startup'].setChecked(defaults.get('check_updates_on_startup', True))

        # Appearance
        scale_combo = self._widgets['ui_scale']
        for i in range(scale_combo.count()):
            if scale_combo.itemData(i) == defaults['ui_scale']:
                scale_combo.setCurrentIndex(i)
                break
        self._widgets['panel_opacity'].setValue(defaults['panel_opacity'])
        self._widgets['show_tooltips'].setChecked(defaults['show_tooltips'])

        accent_combo = self._widgets['accent_color']
        for i in range(accent_combo.count()):
            if accent_combo.itemData(i) == defaults.get('accent_color', '#ff8800'):
                accent_combo.setCurrentIndex(i)
                break
        self._widgets['show_panel_titles'].setChecked(defaults.get('show_panel_titles', True))
        self._widgets['zoom_speed'].setValue(defaults.get('zoom_speed', 100))

        # Performance (app-level)
        self._widgets['gpu_acceleration'].setChecked(defaults['gpu_acceleration'])
        default_backend = defaults.get('backend', {}) or {}
        default_graphics_backend = (
            default_backend.get('graphics', 'auto')
            if isinstance(default_backend, dict) else 'auto'
        )
        graphics_combo = self._widgets['timeline_graphics_backend']
        for i in range(graphics_combo.count()):
            if graphics_combo.itemData(i) == default_graphics_backend:
                graphics_combo.setCurrentIndex(i)
                break
        res_combo = self._widgets['video_playback_resolution']
        for i, (label, value) in enumerate(self.VIDEO_RESOLUTIONS.items()):
            if value == defaults['video_playback_resolution']:
                res_combo.setCurrentIndex(i)
                break
        self._widgets['processing_threads'].setValue(defaults.get('processing_threads', 0))
        # [BUG-B3-04] Restore Defaults 此前漏掉 pass_fusion_mode，用户设为
        # "off"/"aggressive" 后恢复默认不生效；DEFAULT_CONFIG 已补默认键
        # "conservative"，这里补重置。
        self._select_combo_by_data(
            'pass_fusion_mode', defaults.get('pass_fusion_mode', 'conservative'))

        # macOS VT zero-copy
        vt_default = defaults.get('video_macos_vt_zero_copy', 'auto')
        vt_combo = self._widgets['video_macos_vt_zero_copy']
        for i in range(vt_combo.count()):
            if vt_combo.itemData(i) == vt_default:
                vt_combo.setCurrentIndex(i)
                break

        # Shortcuts
        self._widgets['global_shortcuts'].setChecked(defaults.get('global_shortcuts', False))
        self._widgets['show_shortcut_conflicts'].setChecked(defaults.get('show_shortcut_conflicts', True))

        # File & Storage
        self._widgets['default_open_directory'].setText(defaults.get('default_open_directory', ''))
        self._widgets['auto_backup_interval'].setValue(defaults.get('auto_backup_interval', 0))
        self._widgets['backup_directory'].setText(defaults.get('backup_directory', ''))
        self._widgets['max_backup_count'].setValue(defaults.get('max_backup_count', 5))
        self._widgets['cache_directory'].setText(defaults.get('cache_directory', ''))
        self._widgets['cache_size_limit_gb'].setValue(defaults.get('cache_size_limit_gb', 10))
        self._widgets['cleanup_temp_on_exit'].setChecked(defaults.get('cleanup_temp_on_exit', True))
        self._widgets['temp_file_max_age_days'].setValue(defaults.get('temp_file_max_age_days', 7))

        # Render Cache defaults
        self._select_combo_by_data('render_cache_auto_mode', defaults.get('render_cache_auto_mode', 'off'))
        # [BUG-B3-02] Restore Defaults 此前漏掉 smart 模式阈值；DEFAULT_CONFIG
        # 已补默认键 render_cache_auto_threshold=6，这里补重置。
        self._widgets['render_cache_auto_threshold'].setValue(
            int(defaults.get('render_cache_auto_threshold', 6)))
        self._select_combo_by_data('render_cache_codec', defaults.get('render_cache_codec', 'topos_proxy'))
        self._select_combo_by_data('proxy_format', defaults.get('proxy_format', 'topos_proxy'))
        self._widgets['render_cache_directory'].setText(defaults.get('render_cache_directory', ''))
        self._select_combo_by_data('render_cache_resolution_mode', defaults.get('render_cache_resolution_mode', 'timeline'))
        self._widgets['render_cache_max_size_gb'].setValue(defaults.get('render_cache_max_size_gb', 50))

        # Advanced (Logs & Plugins)
        log_level_combo = self._widgets['log_level']
        for i in range(log_level_combo.count()):
            if log_level_combo.itemData(i) == defaults.get('log_level', 'warning'):
                log_level_combo.setCurrentIndex(i)
                break
        self._widgets['log_file_path'].setText(defaults.get('log_file_path', ''))
        self._widgets['enable_file_logging'].setChecked(defaults.get('enable_file_logging', False))
        self._widgets['max_log_file_size_mb'].setValue(defaults.get('max_log_file_size_mb', 10))
        self._widgets['show_version_in_title'].setChecked(defaults.get('show_version_in_title', True))
        self._widgets['show_gpu_info'].setChecked(defaults.get('show_gpu_info', False))
        self._widgets['developer_mode'].setChecked(bool(defaults.get('developer_mode', False)))
        self._widgets['plugin_directory'].setText(defaults.get('plugin_directory', 'plugins'))
        self._widgets['auto_scan_plugins'].setChecked(defaults.get('auto_scan_plugins', True))
        self._disabled_plugins = list(defaults.get('disabled_plugins', []))

        # Timeline Defaults — hidden in Image mode, guard each access.
        self._select_combo_by_data('default_timeline_fps', defaults.get('default_timeline_fps', '25'))
        _tw = self._widgets.get('default_timeline_width')
        if _tw is not None:
            _tw.setValue(defaults.get('default_timeline_width', 1920))
        _th = self._widgets.get('default_timeline_height')
        if _th is not None:
            _th.setValue(defaults.get('default_timeline_height', 1080))
        # [BUG-B3-03] 恢复默认后同步预设下拉（默认 1920x1080 → "1080p"）。
        if _tw is not None and _th is not None:
            self._sync_timeline_res_combo(_tw.value(), _th.value())
        self._select_combo_by_data('default_timeline_sample_rate', defaults.get('default_timeline_sample_rate', 48000))
        self._select_combo_by_data('default_timeline_pixel_aspect_ratio', defaults.get('default_timeline_pixel_aspect_ratio', '1.0'))
        self._select_combo_by_data('default_timeline_field_order', defaults.get('default_timeline_field_order', 'progressive'))
        self._select_combo_by_data('default_timeline_audio_track_type', defaults.get('default_timeline_audio_track_type', 'stereo'))
        _ttc = self._widgets.get('default_timeline_start_timecode')
        if _ttc is not None:
            _ttc.setText(defaults.get('default_timeline_start_timecode', '00:00:00:00'))
        # P10.3 - Subtitle default clip duration
        sub_dur_w = self._widgets.get('default_subtitle_clip_duration_frames')
        if sub_dur_w is not None:
            sub_dur_w.setValue(int(defaults.get('default_subtitle_clip_duration_frames', 72)))

        # Delegate restore_defaults to module preference pages
        module_restore_failures: list[str] = []
        for page_id, page_obj in self._module_pages.items():
            try:
                page_obj.restore_defaults(defaults)
            except Exception:
                # [AUD-03906] 同 apply 路径：恢复默认失败不能伪装成
                # "默认值已恢复"的假象。
                logger.exception(
                    "Module preference page restore_defaults failed: %s", page_id)
                module_restore_failures.append(page_id)
        if module_restore_failures:
            QMessageBox.warning(
                self,
                I18N.tr("preferences.module_restore_failed.title",
                        "Restore Defaults Failed"),
                I18N.tr(
                    "preferences.module_restore_failed.message",
                    "Some module pages could not be restored:\n{pages}",
                ).format(pages=", ".join(module_restore_failures)),
            )

        # Unblock preview signals and emit batch preview for all previewable keys
        self._preview_blocked = False
        self._emit_batch_preview()

    def _show_restart_prompt(self) -> None:
        """Show restart prompt after language change."""
        reply = QMessageBox.question(
            self.parent() if self.parent() else self,
            I18N.tr("dialog.restart_title", "Restart Required"),
            I18N.tr("dialog.restart_message",
                    "The application needs to restart for the language change to take effect.\n\n"
                    "Would you like to restart now?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )
        if reply == QMessageBox.StandardButton.Yes:
            from src.shared.app_restart import restart_application  # [R2-6] 统一重启路径
            restart_application()

    def needs_restart(self) -> bool:
        """Check if the application needs to restart (e.g. language changed)."""
        return self._needs_restart

    # ── Keyboard Events ───────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        """Handle key press events for the dialog."""
        if event.key() == Qt.Key.Key_Escape:
            self._on_cancel()
        else:
            super().keyPressEvent(event)

    # ── Styles ─────────────────────────────────────────────────────

    def _apply_styles(self) -> None:
        """Apply dark theme styles unified with the Export panels (Batch/Quick Export).

        配色与导出面板一致：输入框/下拉框/按钮统一用 #232325 背景 + #151517 边框 +
        6px 圆角（同 ChevronComboBox / _apply_text_input_style / 紧凑按钮）。
        颜色全部引用全局 C 常量，方便全局调整。
        """
        self.setStyleSheet(f"""
            /* === Dialog Background === */
            QDialog {{
                background-color: {C.BG_PANEL};
                color: {C.TEXT_PRIMARY};
            }}

            /* === Title Bar === */
            QWidget#PrefTitleBar {{
                background-color: {C.BG_DARK};
                border-bottom: 1px solid {C.BG_DARKEST};
            }}
            QLabel#PrefTitleLabel {{
                font-size: 16px;
                font-weight: bold;
                color: {C.ORANGE};
                background: transparent;
                border: none;
            }}

            /* === Splitter === */
            QSplitter#PrefSplitter::handle {{
                background-color: {C.BG_DARKEST};
                width: 1px;
            }}

            /* === Category List === */
            QListWidget#PrefCategoryList {{
                background-color: {C.BG_DARK};
                border: none;
                border-right: 1px solid {C.BG_DARKEST};
                outline: none;
                padding: 8px 0px;
                font-size: 13px;
            }}
            QListWidget#PrefCategoryList::item {{
                color: {C.TEXT_LABEL};
                padding: 10px 20px;
                border: none;
                border-left: 3px solid transparent;
            }}
            QListWidget#PrefCategoryList::item:hover {{
                background-color: {C.BG_DARK};
                color: {C.TEXT_BRIGHT};
            }}
            QListWidget#PrefCategoryList::item:selected {{
                background-color: {C.BG_DARK};
                color: {C.ORANGE};
                border-left: 3px solid {C.ORANGE};
            }}

            /* === Pages Container === */
            QStackedWidget#PrefPages {{
                background-color: {C.BG_PANEL};
            }}

            /* === Page Content === */
            QWidget#PrefPageContent {{
                background-color: {C.BG_PANEL};
            }}

            /* === Scroll Area for pages === */
            QScrollArea {{
                background-color: {C.BG_PANEL};
                border: none;
            }}

            /* === Settings Group（与导出面板 section 视觉对齐） === */
            QGroupBox#PrefSettingsGroup {{
                background-color: #232325;
                border: 1px solid #151517;
                border-radius: 6px;
                padding: 12px;
                margin-top: 8px;
                font-size: 14px;
                font-weight: bold;
                color: {C.TEXT_BRIGHT};
            }}
            QGroupBox#PrefSettingsGroup::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 4px 8px;
                color: {C.ORANGE};
            }}

            /* === 统一输入控件（QLineEdit/QSpinBox/QComboBox） === */
            /* 与导出面板的文本框/ChevronComboBox 同一套配色：#232325 bg + #151517 border */
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                background-color: #232325;
                border: 1px solid #151517;
                border-radius: 6px;
                padding: 4px 8px;
                color: {C.TEXT_PRIMARY};
                font-size: 12px;
            }}
            QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
                background-color: #2e2e34;
            }}
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
            QComboBox:focus, QComboBox:on {{
                border: 1px solid {C.ORANGE};
                background-color: #2e2e34;
            }}
            /* QComboBox 下拉箭头 */
            QComboBox::drop-down {{
                border: none;
                width: 22px;
                background: transparent;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {C.TEXT_LABEL};
                margin-right: 6px;
            }}
            QComboBox::down-arrow:hover {{
                border-top-color: {C.ORANGE};
            }}
            /* QSpinBox 上下按钮（与导出面板一致，保留可点按） */
            QSpinBox::up-button, QDoubleSpinBox::up-button,
            QSpinBox::down-button, QDoubleSpinBox::down-button {{
                background-color: #2e2e34;
                border: none;
                width: 16px;
            }}
            QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
            QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
                background-color: {C.BG_BUTTON};
            }}
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-bottom: 5px solid {C.TEXT_LABEL};
            }}
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {C.TEXT_LABEL};
            }}

            /* === QCheckBox（全局主题已处理，这里补 spacing） === */
            QCheckBox {{
                color: {C.TEXT_SECONDARY};
                font-size: 12px;
                spacing: 8px;
                background: transparent;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 1px solid {C.BORDER_MID};
                background: #232325;
            }}
            QCheckBox::indicator:hover {{
                border: 1px solid {C.ORANGE};
            }}
            QCheckBox::indicator:checked {{
                background: {C.ORANGE};
                border: 1px solid {C.ORANGE};
            }}

            /* === Form Labels === */
            QLabel#PrefFormLabel {{
                color: {C.TEXT_SECONDARY};
                font-size: 12px;
                background: transparent;
                border: none;
                padding: 0;
                margin: 0;
            }}

            /* === Note/Description Labels === */
            QLabel#PrefNoteLabel {{
                color: {C.TEXT_HINT};
                font-size: 11px;
                background: transparent;
                border: none;
            }}

            /* === Slider Value Labels === */
            QLabel#PrefSliderValue {{
                color: {C.ORANGE};
                font-size: 12px;
                font-weight: bold;
                background: transparent;
                border: none;
            }}

            /* === 紧凑按钮（Browse / Action） — 与导出面板 Browse/Choose 同款 === */
            QPushButton#PrefBrowseBtn, QPushButton#PrefActionBtn {{
                background-color: #232325;
                border: 1px solid #151517;
                border-radius: 6px;
                padding: 6px 14px;
                color: {C.TEXT_BRIGHT};
                font-size: 12px;
                font-weight: bold;
            }}
            QPushButton#PrefBrowseBtn:hover, QPushButton#PrefActionBtn:hover {{
                background-color: #2e2e34;
                border: 1px solid {C.ORANGE};
                color: #ffffff;
            }}
            QPushButton#PrefBrowseBtn:pressed, QPushButton#PrefActionBtn:pressed {{
                background-color: {C.ORANGE_PRESS};
                border: 1px solid {C.ORANGE_PRESS};
                color: #ffffff;
            }}

            /* === Danger Button (Reset/Clear — 语义红) === */
            QPushButton#PrefDangerBtn {{
                background-color: transparent;
                border: 1px solid #553333;
                border-radius: 4px;
                padding: 8px 16px;
                color: #cc6666;
                font-size: 13px;
            }}
            QPushButton#PrefDangerBtn:hover {{
                background-color: #332222;
                border-color: {C.CHANNEL_R};
                color: {C.CHANNEL_R};
            }}

            /* === Bottom Button Bar === */
            QWidget#PrefBtnBar {{
                background-color: {C.BG_DARK};
                border-top: 1px solid {C.BG_DARKEST};
            }}

            /* === Restore Defaults Button（次级灰按钮） === */
            QPushButton#PrefRestoreBtn {{
                background-color: transparent;
                border: 1px solid {C.BORDER_MID};
                border-radius: 4px;
                padding: 8px 16px;
                color: {C.TEXT_LABEL};
                font-size: 12px;
            }}
            QPushButton#PrefRestoreBtn:hover {{
                border-color: {C.ORANGE};
                color: {C.ORANGE};
            }}

            /* === Cancel Button（次级灰按钮） === */
            QPushButton#PrefCancelBtn {{
                background-color: transparent;
                border: 1px solid {C.BORDER_MID};
                border-radius: 4px;
                padding: 8px 20px;
                color: {C.TEXT_BRIGHT};
                font-size: 13px;
                min-width: 80px;
            }}
            QPushButton#PrefCancelBtn:hover {{
                border-color: {C.ORANGE};
                color: {C.ORANGE};
            }}

            /* === Apply Button（橙色描边次级按钮） === */
            QPushButton#PrefApplyBtn {{
                background-color: rgba(255, 136, 0, 0.15);
                border: 1px solid {C.ORANGE};
                border-radius: 4px;
                padding: 8px 20px;
                color: {C.ORANGE};
                font-size: 13px;
                font-weight: bold;
                min-width: 80px;
            }}
            QPushButton#PrefApplyBtn:hover {{
                background-color: rgba(255, 136, 0, 0.25);
            }}
            QPushButton#PrefApplyBtn:pressed {{
                background-color: rgba(255, 136, 0, 0.35);
            }}

            /* === Save Button（主操作橙色实心，与导出面板 #PrimaryButton 同款） === */
            QPushButton#PrefSaveBtn {{
                background-color: {C.ORANGE_PRESS};
                border: none;
                border-radius: 4px;
                padding: 8px 24px;
                color: #ffffff;
                font-size: 13px;
                font-weight: bold;
                min-width: 80px;
            }}
            QPushButton#PrefSaveBtn:hover {{
                background-color: {C.ORANGE_HOVER};
            }}
            QPushButton#PrefSaveBtn:pressed {{
                background-color: {C.ORANGE};
            }}

            /* === Slider in Preferences — 与 DARK_THEME QSlider 全局规范一致 === */
            QSlider#PrefSlider {{ background: transparent; }}
            QSlider#PrefSlider::groove:horizontal {{
                border: 1px solid #3d3d3d;
                height: 4px;
                background: {C.BG_DARKEST};
                margin: 2px 0;
                border-radius: 2px;
            }}
            QSlider#PrefSlider::sub-page:horizontal {{
                background: transparent;
                border: none;
                height: 4px;
                border-radius: 2px;
            }}
            QSlider#PrefSlider::add-page:horizontal {{
                background: transparent;
                border: none;
                height: 4px;
                border-radius: 2px;
            }}
            QSlider#PrefSlider::handle:horizontal {{
                background: {C.ORANGE};
                border: 1px solid {C.BG_DARK};
                width: 10px;
                height: 10px;
                margin: -4px 0;
                border-radius: 5px;
            }}
            QSlider#PrefSlider::handle:horizontal:hover {{
                background: {C.ORANGE_HOVER};
                border: 1px solid {C.BORDER_DARK};
            }}
            QSlider#PrefSlider::handle:horizontal:pressed {{
                background: {C.ORANGE_PRESS};
            }}

            /* === Plugin List === */
            QListWidget#PrefPluginList {{
                background-color: #232325;
                border: 1px solid #151517;
                border-radius: 6px;
                padding: 4px;
                outline: none;
            }}
            QListWidget#PrefPluginList::item {{
                padding: 6px;
                border-radius: 3px;
                color: {C.TEXT_SECONDARY};
            }}
            QListWidget#PrefPluginList::item:hover {{
                background-color: #2e2e34;
            }}

            /* === Scrollbars === */
            QScrollBar:vertical {{
                border: none;
                background: {C.BG_DARK};
                width: 8px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BG_BUTTON};
                min-height: 20px;
                border-radius: 4px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {C.BG_BUTTON_HOVER};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar:horizontal {{
                border: none;
                background: {C.BG_DARK};
                height: 8px;
                margin: 0px;
            }}
            QScrollBar::handle:horizontal {{
                background: {C.BG_BUTTON};
                min-width: 20px;
                border-radius: 4px;
                margin: 2px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {C.BG_BUTTON_HOVER};
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0px;
            }}
        """)
