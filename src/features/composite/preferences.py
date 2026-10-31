"""Composite module preference page.

Composite-specific settings. Currently exposes the node
shake-to-disconnect sensitivity.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QSlider, QComboBox, QSpinBox, QDoubleSpinBox,
)
from src.shared.ui.widgets import ChevronComboBox
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.widgets.pref_form_layout import make_pref_form, make_pref_label

# Config key + default for the shake-to-disconnect sensitivity slider.
# Range 0-100: higher = easier to disconnect a node by shaking it;
# lower = harder to trigger and ignores slow movements.
SHAKE_SENSITIVITY_KEY = "composite.shake_disconnect_sensitivity"
SHAKE_SENSITIVITY_DEFAULT = 50

# Colour-management config keys (stage 2). Working space is fixed to
# scene-linear for now but is exposed so future stages (ACEScg) can add
# options without changing the schema. Display space controls the viewer's
# default ODT (linear→sRGB encode on/off). Color engine selects the
# transform backend: "ocio" (factory default, COLOR_ENGINE_DEFAULT below;
# requires PyOpenColorIO) or "builtin" (fast sRGB↔linear math).
# AUD-7800: 原注释声称 "builtin (default)"，与实际默认 "ocio" 矛盾。
WORKING_SPACE_KEY = "composite.working_space"
WORKING_SPACE_DEFAULT = "linear"
DISPLAY_SPACE_KEY = "composite.display_space"
DISPLAY_SPACE_DEFAULT = "srgb"
COLOR_ENGINE_KEY = "composite.color_engine"
EXPORT_PARALLEL_FRAMES_KEY = "composite.export_parallel_frames"
EXPORT_PARALLEL_FRAMES_DEFAULT = 2

# A6 node disk cache: capacity ceiling (GB) for the shared DiskCacheStore.
# Path override is config-only (advanced); empty = ~/.topos_cache/node_cache.
NODE_CACHE_CAPACITY_KEY = "composite.node_cache_capacity_gb"
# Node disk-cache payload format: 'auto' prefers native TOPOS .toos (TPIM
# GBR+alpha 12-bit) and falls back to lossless npz for HDR payloads or a
# missing native lib; 'toos'/'npz' pin the choice (npz still used for HDR).
# The TOPOS_NODE_CACHE_FORMAT env var overrides this key (CI/diag escape).
NODE_CACHE_FORMAT_KEY = "composite.node_cache_format"
NODE_CACHE_FORMAT_DEFAULT = "auto"
NODE_CACHE_CAPACITY_DEFAULT = 20.0
COLOR_ENGINE_DEFAULT = "ocio"  # matches DEFAULT_CONFIG factory default


class CompositePreferencePage:
    """Composite preference page.

    Contributes the node shake-to-disconnect sensitivity control under the
    Composite category. Registered by CompositeModule via
    register_preference_pages(); load/apply/restore_defaults are delegated to
    by PreferencesDialog alongside the app-level pages.
    """

    page_id = "composite.settings"

    def __init__(self) -> None:
        self._widgets: dict[str, QWidget] = {}

    def create_widget(self, parent: "QWidget") -> QWidget:
        from PyQt6.QtGui import QFont
        from src.shared.i18n.i18n import I18N

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(24, 20, 24, 20)

        group = QGroupBox(I18N.tr("preferences.composite.group_title", "Node Graph"))
        group.setObjectName("PrefSettingsGroup")
        form = make_pref_form(group)

        # ── Shake-to-disconnect sensitivity ──────────────────────────
        tooltip = I18N.tr(
            "preferences.composite.shake_sensitivity_tooltip",
            "How easily shaking a node disconnects it. Lower values are harder "
            "to trigger and ignore slow movements.",
        )

        sens_row = QWidget()
        sens_row.setStyleSheet("background: transparent;")
        sens_layout = QHBoxLayout(sens_row)
        sens_layout.setContentsMargins(0, 0, 0, 0)
        sens_layout.setSpacing(8)

        sens_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        sens_slider.setRange(0, 100)
        sens_slider.setObjectName("PrefSlider")
        sens_slider.setToolTip(tooltip)

        sens_value = QLabel(str(SHAKE_SENSITIVITY_DEFAULT))
        sens_value.setObjectName("PrefSliderValue")
        sens_value.setFixedWidth(36)
        mono = QFont("Menlo", 11)
        if not mono.exactMatch():
            mono = QFont("Courier", 11)
        sens_value.setFont(mono)
        sens_slider.valueChanged.connect(lambda v: sens_value.setText(str(v)))

        sens_layout.addWidget(sens_slider, 1)
        sens_layout.addWidget(sens_value)

        sens_label = make_pref_label(I18N.tr(
            "preferences.composite.shake_sensitivity",
            "Shake-to-disconnect sensitivity"), tooltip)
        form.addRow(sens_label, sens_row)

        # ── Export parallel frames (A3) ──────────────────────────────
        from PyQt6.QtWidgets import QSpinBox
        par_tooltip = I18N.tr(
            "preferences.composite.export_parallel_frames_tooltip",
            "Number of frames rendered simultaneously during exports. "
            "Each worker holds its own GPU context (more VRAM use); "
            "output is identical to a serial render.")
        par_spin = QSpinBox()
        par_spin.setRange(1, 4)
        par_spin.setValue(EXPORT_PARALLEL_FRAMES_DEFAULT)
        par_spin.setToolTip(par_tooltip)
        par_label = make_pref_label(I18N.tr(
            "preferences.composite.export_parallel_frames",
            "Export Parallel Frames"), par_tooltip)
        form.addRow(par_label, par_spin)

        hint = QLabel(I18N.tr(
            "preferences.composite.shake_sensitivity_hint",
            "Lower this value if nodes disconnect too easily while being moved."))
        hint.setObjectName("PrefNoteLabel")
        hint.setWordWrap(True)
        layout.addWidget(group)
        layout.addWidget(hint)

        # ── Colour Management ────────────────────────────────────────
        cm_group = QGroupBox(I18N.tr(
            "preferences.composite.color_management_group",
            "Colour Management"))
        cm_group.setObjectName("PrefSettingsGroup")
        cm_form = make_pref_form(cm_group)

        # Working space (currently only scene-linear; future: ACEScg).
        ws_tooltip = I18N.tr(
            "preferences.composite.working_space_tooltip",
            "The colour space used for all internal compositing operations. "
            "Scene-linear gives physically correct blending.")
        ws_combo = ChevronComboBox()
        ws_combo.addItem("Scene-Linear", "linear")
        ws_combo.setToolTip(ws_tooltip)
        ws_label = make_pref_label(I18N.tr(
            "preferences.composite.working_space", "Working Space"), ws_tooltip)
        cm_form.addRow(ws_label, ws_combo)

        # Display space (controls the viewer's default ODT).
        ds_tooltip = I18N.tr(
            "preferences.composite.display_space_tooltip",
            "How scene-linear data is shown on screen by default. sRGB applies "
            "a linear→sRGB encode; Linear shows raw values.")
        ds_combo = ChevronComboBox()
        ds_combo.addItem("sRGB", "srgb")
        ds_combo.addItem("Linear", "linear")
        ds_combo.setToolTip(ds_tooltip)
        ds_label = make_pref_label(I18N.tr(
            "preferences.composite.display_space", "Display Space"), ds_tooltip)
        cm_form.addRow(ds_label, ds_combo)

        # Color engine (built-in vs OCIO).
        from src.shared.color.color_transform_provider import is_ocio_available
        # AUD-7800: 文案默认值与实际 factory default "ocio" 对齐。
        ce_tooltip = I18N.tr(
            "preferences.composite.color_engine_tooltip",
            "OCIO (factory default) uses OpenColorIO for arbitrary colour "
            "transforms (requires PyOpenColorIO installed). Built-in uses "
            "fast sRGB↔linear math.")
        ce_combo = ChevronComboBox()
        ce_combo.addItem("Built-in (sRGB/Linear)", "builtin")
        ce_combo.addItem("OCIO (OpenColorIO)", "ocio")
        if not is_ocio_available():
            # Disable the OCIO option when PyOpenColorIO isn't installed.
            # QComboBox.addItem(text, data) 返回 None（只有带 QIcon 的重载返回 index），
            # 所以直接用已知索引 1（OCIO 是第二项）。
            model = ce_combo.model()
            item = model.item(1)
            if item is not None:
                item.setEnabled(False)
                item.setToolTip(
                    I18N.tr(
                        "preferences.composite.ocio_unavailable_tooltip",
                        "Install PyOpenColorIO (pip install opencolorio) to enable."))
        ce_combo.setToolTip(ce_tooltip)
        ce_label = make_pref_label(I18N.tr(
            "preferences.composite.color_engine", "Color Engine"), ce_tooltip)
        cm_form.addRow(ce_label, ce_combo)

        cm_hint = QLabel(I18N.tr(
            "preferences.composite.color_management_hint",
            "Built-in is the fast default for sRGB/Linear workflows; its "
            "transforms are verified identical to OCIO for the common "
            "spaces. With OCIO selected, exports run through OpenColorIO "
            "and the viewer ODT uses an OCIO-baked lookup — same engine as "
            "export. ACEScg sources are colour-managed with the same "
            "matrix as the bundled config's acescg space. A real ACES "
            "Studio Config can be set via ocio_config_path; missing OCIO "
            "falls back to built-in everywhere — behaviour never breaks."))
        cm_hint.setObjectName("PrefNoteLabel")
        cm_hint.setWordWrap(True)
        layout.addWidget(cm_group)
        layout.addWidget(cm_hint)

        # ── Node disk cache capacity (A6) ────────────────────────────
        from PyQt6.QtWidgets import QDoubleSpinBox
        nc_tooltip = I18N.tr(
            "preferences.composite.node_cache_capacity_tooltip",
            "Total size ceiling for the node result disk cache "
            "(Render-in-Place and cache_to_disk nodes). Oldest entries "
            "are evicted first.")
        nc_spin = QDoubleSpinBox()
        nc_spin.setRange(0.5, 2000.0)
        nc_spin.setDecimals(1)
        nc_spin.setSuffix(I18N.tr("preferences.composite.gb_suffix", " GB"))
        nc_spin.setValue(NODE_CACHE_CAPACITY_DEFAULT)
        nc_spin.setToolTip(nc_tooltip)
        nc_label = make_pref_label(I18N.tr(
            "preferences.composite.node_cache_capacity",
            "Node disk cache size"), nc_tooltip)
        form.addRow(nc_label, nc_spin)

        # ── Node disk cache payload format（2026-09-19 审计收口）──────
        # 格式此前只有 TOPOS_NODE_CACHE_FORMAT 环境变量入口，偏好面板
        # 无设置项。auto = topos .toos 优先 + 可诊断 npz 回退（缺 native
        # 库 / HDR 载荷超 12-bit 定点域）。
        nf_tooltip = I18N.tr(
            "preferences.composite.node_cache_format_tooltip",
            "Payload codec for the node result disk cache. Auto prefers "
            "the native Topos .toos image (12-bit GBR+alpha) and falls "
            "back to lossless npz for HDR results or when the native "
            "library is unavailable.")
        nf_combo = ChevronComboBox()
        nf_combo.addItem("Auto (Topos .toos + npz fallback)", "auto")
        nf_combo.addItem("Topos .toos only", "toos")
        nf_combo.addItem("npz (lossless float16)", "npz")
        nf_combo.setToolTip(nf_tooltip)
        nf_label = make_pref_label(I18N.tr(
            "preferences.composite.node_cache_format",
            "Node cache format"), nf_tooltip)
        form.addRow(nf_label, nf_combo)

        layout.addStretch()

        self._widgets[SHAKE_SENSITIVITY_KEY] = sens_slider
        self._widgets[NODE_CACHE_CAPACITY_KEY] = nc_spin
        self._widgets[NODE_CACHE_FORMAT_KEY] = nf_combo
        self._widgets[EXPORT_PARALLEL_FRAMES_KEY] = par_spin
        self._widgets[WORKING_SPACE_KEY] = ws_combo
        self._widgets[DISPLAY_SPACE_KEY] = ds_combo
        self._widgets[COLOR_ENGINE_KEY] = ce_combo
        return widget

    def load(self, config: Any) -> None:
        """Load the sensitivity + colour-management settings from config."""
        slider = self._widgets.get(SHAKE_SENSITIVITY_KEY)
        if isinstance(slider, QSlider):
            val = config.get(SHAKE_SENSITIVITY_KEY, SHAKE_SENSITIVITY_DEFAULT)
            try:
                slider.setValue(int(val))
            except (TypeError, ValueError):
                slider.setValue(SHAKE_SENSITIVITY_DEFAULT)

        par_spin = self._widgets.get(EXPORT_PARALLEL_FRAMES_KEY)
        if isinstance(par_spin, QSpinBox):
            par_val = config.get(
                EXPORT_PARALLEL_FRAMES_KEY, EXPORT_PARALLEL_FRAMES_DEFAULT)
            try:
                par_spin.setValue(int(par_val))
            except (TypeError, ValueError):
                par_spin.setValue(EXPORT_PARALLEL_FRAMES_DEFAULT)

        nc_spin = self._widgets.get(NODE_CACHE_CAPACITY_KEY)
        if isinstance(nc_spin, QDoubleSpinBox):
            nc_val = config.get(
                NODE_CACHE_CAPACITY_KEY, NODE_CACHE_CAPACITY_DEFAULT)
            try:
                nc_spin.setValue(float(nc_val))
            except (TypeError, ValueError):
                nc_spin.setValue(NODE_CACHE_CAPACITY_DEFAULT)

        nf_combo = self._widgets.get(NODE_CACHE_FORMAT_KEY)
        if isinstance(nf_combo, QComboBox):
            nf_val = config.get(
                NODE_CACHE_FORMAT_KEY, NODE_CACHE_FORMAT_DEFAULT)
            self._set_combo_by_data(
                nf_combo, nf_val, NODE_CACHE_FORMAT_DEFAULT)

        ws_combo = self._widgets.get(WORKING_SPACE_KEY)
        if isinstance(ws_combo, QComboBox):
            ws_val = config.get(WORKING_SPACE_KEY, WORKING_SPACE_DEFAULT)
            self._set_combo_by_data(ws_combo, ws_val, WORKING_SPACE_DEFAULT)

        ds_combo = self._widgets.get(DISPLAY_SPACE_KEY)
        if isinstance(ds_combo, QComboBox):
            ds_val = config.get(DISPLAY_SPACE_KEY, DISPLAY_SPACE_DEFAULT)
            self._set_combo_by_data(ds_combo, ds_val, DISPLAY_SPACE_DEFAULT)

        ce_combo = self._widgets.get(COLOR_ENGINE_KEY)
        if isinstance(ce_combo, QComboBox):
            ce_val = config.get(COLOR_ENGINE_KEY, COLOR_ENGINE_DEFAULT)
            self._set_combo_by_data(ce_combo, ce_val, COLOR_ENGINE_DEFAULT)

    @staticmethod
    def _set_combo_by_data(combo: QComboBox, value: Any, default: Any) -> None:
        """Select the combo item whose userData matches ``value``."""
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return
        # Fallback to default.
        for i in range(combo.count()):
            if combo.itemData(i) == default:
                combo.setCurrentIndex(i)
                return

    def apply(self, config_manager: Any) -> None:
        """Write the settings to the ConfigManager (saved by the dialog)."""
        slider = self._widgets.get(SHAKE_SENSITIVITY_KEY)
        if isinstance(slider, QSlider):
            config_manager.set(SHAKE_SENSITIVITY_KEY, slider.value(), auto_save=False)

        ws_combo = self._widgets.get(WORKING_SPACE_KEY)
        if isinstance(ws_combo, QComboBox):
            config_manager.set(WORKING_SPACE_KEY,
                               ws_combo.currentData(), auto_save=False)

        ds_combo = self._widgets.get(DISPLAY_SPACE_KEY)
        if isinstance(ds_combo, QComboBox):
            config_manager.set(DISPLAY_SPACE_KEY,
                               ds_combo.currentData(), auto_save=False)

        ce_combo = self._widgets.get(COLOR_ENGINE_KEY)
        if isinstance(ce_combo, QComboBox):
            config_manager.set(COLOR_ENGINE_KEY,
                               ce_combo.currentData(), auto_save=False)

        par_spin = self._widgets.get(EXPORT_PARALLEL_FRAMES_KEY)
        if isinstance(par_spin, QSpinBox):
            config_manager.set(EXPORT_PARALLEL_FRAMES_KEY,
                               par_spin.value(), auto_save=False)

        nc_spin = self._widgets.get(NODE_CACHE_CAPACITY_KEY)
        if isinstance(nc_spin, QDoubleSpinBox):
            config_manager.set(NODE_CACHE_CAPACITY_KEY,
                               nc_spin.value(), auto_save=False)

        nf_combo = self._widgets.get(NODE_CACHE_FORMAT_KEY)
        if isinstance(nf_combo, QComboBox):
            config_manager.set(NODE_CACHE_FORMAT_KEY,
                               nf_combo.currentData(), auto_save=False)

    def restore_defaults(self, defaults: dict) -> None:
        """Reset all settings to their defaults."""
        slider = self._widgets.get(SHAKE_SENSITIVITY_KEY)
        if isinstance(slider, QSlider):
            slider.setValue(defaults.get(SHAKE_SENSITIVITY_KEY, SHAKE_SENSITIVITY_DEFAULT))

        par_spin = self._widgets.get(EXPORT_PARALLEL_FRAMES_KEY)
        if isinstance(par_spin, QSpinBox):
            par_spin.setValue(int(defaults.get(
                EXPORT_PARALLEL_FRAMES_KEY, EXPORT_PARALLEL_FRAMES_DEFAULT)))

        nc_spin = self._widgets.get(NODE_CACHE_CAPACITY_KEY)
        if isinstance(nc_spin, QDoubleSpinBox):
            nc_spin.setValue(float(defaults.get(
                NODE_CACHE_CAPACITY_KEY, NODE_CACHE_CAPACITY_DEFAULT)))

        nf_combo = self._widgets.get(NODE_CACHE_FORMAT_KEY)
        if isinstance(nf_combo, QComboBox):
            self._set_combo_by_data(
                nf_combo,
                defaults.get(NODE_CACHE_FORMAT_KEY, NODE_CACHE_FORMAT_DEFAULT),
                NODE_CACHE_FORMAT_DEFAULT)

        ws_combo = self._widgets.get(WORKING_SPACE_KEY)
        if isinstance(ws_combo, QComboBox):
            self._set_combo_by_data(
                ws_combo, defaults.get(WORKING_SPACE_KEY, WORKING_SPACE_DEFAULT),
                WORKING_SPACE_DEFAULT)

        ds_combo = self._widgets.get(DISPLAY_SPACE_KEY)
        if isinstance(ds_combo, QComboBox):
            self._set_combo_by_data(
                ds_combo, defaults.get(DISPLAY_SPACE_KEY, DISPLAY_SPACE_DEFAULT),
                DISPLAY_SPACE_DEFAULT)

        ce_combo = self._widgets.get(COLOR_ENGINE_KEY)
        if isinstance(ce_combo, QComboBox):
            self._set_combo_by_data(
                ce_combo, defaults.get(COLOR_ENGINE_KEY, COLOR_ENGINE_DEFAULT),
                COLOR_ENGINE_DEFAULT)
