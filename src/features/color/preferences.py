"""Color module's Color & Processing preference page.

Self-contained preference page for color management and image processing:
- Color settings (color space, bit depth, rendering intent, GPU, ICC, OCIO)
- Image processing (denoise strength, sharpen amount)
- AI processing (model path, tile size, GPU device)

This page is always shown since Color is the core feature.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.shared.config.config_manager import ConfigManager

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QCheckBox, QLineEdit,
    QScrollArea, QGroupBox, QSpinBox, QDoubleSpinBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets import ChevronComboBox
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.widgets.pref_form_layout import make_pref_form, make_pref_label
from src.shared.export.export_panel import (
    _apply_text_input_style,
    _apply_compact_button_style,
)
from src.shared.control_surfaces.tangent.settings import (
    COLOR_WHEEL_ROTATION_CONFIG_KEY,
    CONTROL_SURFACE_AUTO,
    CONTROL_SURFACE_CONFIG_KEY,
    CONTROL_SURFACE_DISABLED,
    CONTROL_SURFACE_ELEMENT,
    CONTROL_SURFACE_RIPPLE,
    DEFAULT_COLOR_WHEEL_ROTATION_DEGREES,
    DEFAULT_CONTROL_SURFACE,
    DEFAULT_TRACKERBALL_SMOOTHING,
    DEFAULT_TANGENT_SENSITIVITY,
    HUE_DIRECTION_INVERTED_CONFIG_KEY,
    TRACKERBALL_SMOOTHING_CONFIG_KEY,
    TANGENT_SENSITIVITY_CONFIG_KEYS,
    TANGENT_SENSITIVITY_DISPLAY_SCALE,
    TANGENT_SENSITIVITY_MAX,
    TANGENT_SENSITIVITY_MIN,
    TANGENT_TRACKERBALL_SENSITIVITY_CONFIG_KEYS,
    tangent_sensitivity_from_display,
    tangent_sensitivity_to_display,
    normalize_control_surface,
)

logger = logging.getLogger(__name__)


class _TangentSensitivitySpinBox(QDoubleSpinBox):
    """Display normalized sensitivity without a redundant trailing .0."""

    def textFromValue(self, value: float) -> str:
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.1f}"


class ColorProcessingPreferencePage:
    """Color module's Color & Processing preference page.

    Manages widgets: default_color_space, default_bit_depth,
    rendering_intent, gpu_color_processing, show_color_space_warnings,
    auto_assign_icc, ocio_config_path, default_denoise_strength,
    default_sharpen_amount, ai_model_path, ai_tile_size, ai_gpu_device,
    tangent.control_surface, tangent.color_wheel_rotation_degrees,
    tangent.hue_direction_inverted, tangent.trackerball_smoothing, and the
    nine per-wheel/per-axis Tangent sensitivity values.
    """

    page_id = "color.processing"

    # AUD-24611: color.{scope}.{kind}_{field} 缺键时的运行时默认，与
    # descriptor_from_config 的 smart-default 回退（descriptor_for_source_kind）
    # 一致：timeline → REC709_VIDEO（BT.709 / BT.709 / limited），
    # image → SRGB_IMAGE（BT.709 / sRGB / full）。load() 按运行时默认预选
    # 缺失键，apply() 写回的就是引擎当前生效的值——旧实现缺键时组合框
    # 停在首项 "limited"，点确定即把 image 范围静默覆盖为 limited。
    _CMS_RUNTIME_DEFAULTS: dict[tuple[str, str], str] = {
        ("timeline", "primaries"): "bt709",
        ("timeline", "transfer"): "bt709",
        ("timeline", "range"): "limited",
        ("image", "primaries"): "bt709",
        ("image", "transfer"): "iec61966_2_1",
        ("image", "range"): "full",
    }

    # 12 个 CMS 键（load/apply/restore_defaults 三处共用同一顺序）。
    _CMS_CONFIG_KEYS: tuple[str, ...] = (
        "color.preview.timeline_primaries",
        "color.preview.timeline_transfer",
        "color.preview.timeline_range",
        "color.preview.image_primaries",
        "color.preview.image_transfer",
        "color.preview.image_range",
        "color.output.timeline_primaries",
        "color.output.timeline_transfer",
        "color.output.timeline_range",
        "color.output.image_primaries",
        "color.output.image_transfer",
        "color.output.image_range",
    )

    def __init__(self):
        self._widgets: dict[str, QWidget] = {}
        self._dialog_ref: Any = None  # Weak reference to PreferencesDialog for _browse_path

    # ── Widget Creation ─────────────────────────────────────────────

    def create_widget(self, parent: QWidget) -> QWidget:
        """Create the Color & Processing preference page UI."""
        self._dialog_ref = parent

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("PrefPageContent")
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # ── Group 1: Color Settings ──
        color_group = QGroupBox(
            I18N.tr("preferences.color_management.section_title", "Color Settings")
        )
        color_group.setObjectName("PrefSettingsGroup")
        color_form = make_pref_form(color_group)

        # Default color space
        cs_combo = ChevronComboBox()
        cs_combo.setMinimumWidth(120)
        for cs in ["sRGB", "Adobe RGB", "ProPhoto RGB", "Rec.709", "DCI-P3"]:
            cs_combo.addItem(cs)
        self._widgets['default_color_space'] = cs_combo
        cs_label = make_pref_label(I18N.tr("preferences.color_management.default_color_space",
                                            "Default Color Space:"))
        color_form.addRow(cs_label, cs_combo)

        # Default bit depth
        bd_combo = ChevronComboBox()
        bd_combo.setMinimumWidth(120)
        for bd in ["8-bit", "16-bit", "32-bit float"]:
            bd_combo.addItem(bd)
        self._widgets['default_bit_depth'] = bd_combo
        bd_label = make_pref_label(I18N.tr("preferences.color_management.default_bit_depth",
                                            "Default Bit Depth:"))
        color_form.addRow(bd_label, bd_combo)

        # Rendering Intent
        intent_combo = ChevronComboBox()
        intent_combo.setMinimumWidth(120)
        for value, label in [
            ("perceptual", I18N.tr("preferences.color_management.intent_perceptual",
                                    "Perceptual")),
            ("relative", I18N.tr("preferences.color_management.intent_relative",
                                  "Relative Colorimetric")),
            ("absolute", I18N.tr("preferences.color_management.intent_absolute",
                                  "Absolute Colorimetric")),
            ("saturation", I18N.tr("preferences.color_management.intent_saturation",
                                    "Saturation")),
        ]:
            intent_combo.addItem(label, value)
        self._widgets['rendering_intent'] = intent_combo
        intent_label = make_pref_label(I18N.tr("preferences.color_management.rendering_intent",
                                                "Rendering Intent:"))
        color_form.addRow(intent_label, intent_combo)

        # GPU color processing
        gpu_color = QCheckBox(
            I18N.tr("preferences.color_management.gpu_color_processing",
                    "Use GPU for color processing")
        )
        self._widgets['gpu_color_processing'] = gpu_color
        color_form.addRow("", gpu_color)

        # Show color space warnings
        show_warnings = QCheckBox(
            I18N.tr("preferences.color_management.show_warnings",
                    "Show color space warnings")
        )
        self._widgets['show_color_space_warnings'] = show_warnings
        color_form.addRow("", show_warnings)

        # Auto-assign ICC
        auto_icc = QCheckBox(
            I18N.tr("preferences.color_management.auto_assign_icc",
                    "Auto-assign ICC profile on import")
        )
        self._widgets['auto_assign_icc'] = auto_icc
        color_form.addRow("", auto_icc)

        # OCIO Config Path
        ocio_widget = QWidget()
        ocio_layout = QHBoxLayout(ocio_widget)
        ocio_layout.setContentsMargins(0, 0, 0, 0)
        ocio_layout.setSpacing(6)
        ocio_edit = QLineEdit()
        ocio_edit.setObjectName("PrefPathEdit")
        ocio_edit.setPlaceholderText(
            I18N.tr("preferences.ocio_placeholder", "config.ocio"))
        _apply_text_input_style(ocio_edit)
        ocio_btn = QPushButton(I18N.tr("preferences.export.browse", "Browse"))
        ocio_btn.setObjectName("PrefBrowseBtn")
        ocio_btn.setFixedWidth(80)
        _apply_compact_button_style(ocio_btn)
        ocio_btn.clicked.connect(
            lambda: self._browse_path(ocio_edit, "Select OCIO Config", is_file=True)
        )
        ocio_layout.addWidget(ocio_edit, 1)
        ocio_layout.addWidget(ocio_btn)
        self._widgets['ocio_config_path'] = ocio_edit
        ocio_label = make_pref_label(I18N.tr("preferences.color_management.ocio_config",
                                              "OCIO Config File:"))
        color_form.addRow(ocio_label, ocio_widget)

        layout.addWidget(color_group)

        # ── Group: Tangent control surface ──
        tangent_group = QGroupBox(
            I18N.tr("preferences.tangent.section_title", "Control Surface")
        )
        tangent_group.setObjectName("PrefSettingsGroup")
        tangent_form = make_pref_form(tangent_group)

        tangent_combo = ChevronComboBox()
        tangent_combo.setMinimumWidth(180)
        tangent_combo.addItem(
            I18N.tr("preferences.tangent.disabled", "Disabled"),
            CONTROL_SURFACE_DISABLED,
        )
        tangent_combo.addItem(
            I18N.tr("preferences.tangent.auto", "Auto Detect"),
            CONTROL_SURFACE_AUTO,
        )
        tangent_combo.addItem(
            I18N.tr("preferences.tangent.ripple", "Tangent Ripple"),
            CONTROL_SURFACE_RIPPLE,
        )
        tangent_combo.addItem(
            I18N.tr("preferences.tangent.element", "Tangent Element"),
            CONTROL_SURFACE_ELEMENT,
        )
        self._widgets[CONTROL_SURFACE_CONFIG_KEY] = tangent_combo
        tangent_form.addRow(
            make_pref_label(
                I18N.tr("preferences.tangent.surface_label", "Control Surface:")
            ),
            tangent_combo,
        )

        tangent_note = QLabel(
            I18N.tr(
                "preferences.tangent.note",
                "Uses the native Tangent Hub connection. Element is treated as one four-panel set.",
            )
        )
        tangent_note.setObjectName("PrefNoteLabel")
        tangent_note.setWordWrap(True)
        tangent_form.addRow("", tangent_note)

        # Tangent calibration follows the same idea as DaVinci Resolve's
        # control-panel preferences: rotation/direction are calibration
        # controls, while every wheel and component has an independent
        # sensitivity percentage. Trackerball X/Y values set maximum speed;
        # slow motion automatically keeps a lower, finer gain.
        rotation_spin = QDoubleSpinBox()
        rotation_spin.setRange(0.0, 359.9)
        rotation_spin.setDecimals(1)
        rotation_spin.setSingleStep(1.0)
        rotation_spin.setSuffix("°")
        rotation_spin.setMinimumWidth(110)
        self._widgets[COLOR_WHEEL_ROTATION_CONFIG_KEY] = rotation_spin
        tangent_form.addRow(
            make_pref_label(
                I18N.tr("preferences.tangent.rotation", "Color Wheel Rotation:")
            ),
            rotation_spin,
        )

        direction_check = QCheckBox(
            I18N.tr("preferences.tangent.invert_direction", "Invert hue direction")
        )
        self._widgets[HUE_DIRECTION_INVERTED_CONFIG_KEY] = direction_check
        tangent_form.addRow("", direction_check)

        smoothing_spin = QSpinBox()
        smoothing_spin.setRange(0, 100)
        smoothing_spin.setSingleStep(5)
        smoothing_spin.setSuffix(" %")
        smoothing_spin.setMinimumWidth(110)
        self._widgets[TRACKERBALL_SMOOTHING_CONFIG_KEY] = smoothing_spin
        tangent_form.addRow(
            make_pref_label(
                I18N.tr(
                    "preferences.tangent.smoothing",
                    "Trackerball Smoothing:",
                )
            ),
            smoothing_spin,
        )

        wheel_labels = {
            "lift": I18N.tr("preferences.tangent.lift", "Lift"),
            "gamma": I18N.tr("preferences.tangent.gamma", "Gamma"),
            "gain": I18N.tr("preferences.tangent.gain", "Gain"),
        }
        component_labels = {
            "hue": I18N.tr("preferences.tangent.axis_x", "X Axis"),
            "saturation": I18N.tr("preferences.tangent.axis_y", "Y Axis"),
            "brightness": I18N.tr("preferences.tangent.brightness", "Brightness"),
        }
        for key in TANGENT_SENSITIVITY_CONFIG_KEYS:
            _, setting = key.split(".", 1)
            wheel, component, _ = setting.split("_", 2)
            # Config stores the real percentage (1000% for the default), but
            # the preference page uses a compact 1:10 display scale (100%) for
            # trackerball X/Y. Brightness keeps the native 100%=100% scale.
            is_trackerball = key in TANGENT_TRACKERBALL_SENSITIVITY_CONFIG_KEYS
            spin = _TangentSensitivitySpinBox()
            if is_trackerball:
                spin.setRange(
                    TANGENT_SENSITIVITY_MIN / TANGENT_SENSITIVITY_DISPLAY_SCALE,
                    TANGENT_SENSITIVITY_MAX / TANGENT_SENSITIVITY_DISPLAY_SCALE,
                )
                spin.setDecimals(1)
                spin.setSingleStep(1.0)
            else:
                spin.setRange(TANGENT_SENSITIVITY_MIN, TANGENT_SENSITIVITY_MAX)
                spin.setDecimals(0)
                spin.setSingleStep(5.0)
            spin.setSuffix(" %")
            spin.setMinimumWidth(110)
            self._widgets[key] = spin
            tangent_form.addRow(
                make_pref_label(
                    f"{wheel_labels[wheel]} {component_labels[component]}:"
                ),
                spin,
            )

        sensitivity_note = QLabel(
            I18N.tr(
                "preferences.tangent.sensitivity_note",
                "X/Y UI 100% = internal 1000% base maximum (10x native Tangent increment). Slow rolls use a finer gain; fast rolls can rise smoothly to 20x; brightness keeps the native 100% scale.",
            )
        )
        sensitivity_note.setObjectName("PrefNoteLabel")
        sensitivity_note.setWordWrap(True)
        tangent_form.addRow("", sensitivity_note)
        smoothing_note = QLabel(
            I18N.tr(
                "preferences.tangent.smoothing_note",
                "0% = raw response and is the default for straight diagonal movement. Higher values smooth noisy hardware but add response lag.",
            )
        )
        smoothing_note.setObjectName("PrefNoteLabel")
        smoothing_note.setWordWrap(True)
        tangent_form.addRow("", smoothing_note)
        layout.addWidget(tangent_group)

        # ── Group: Color Space Presets (preview & output, video vs image) ──
        # Separate gamut+gamma for timeline (video) and image (still) sources.
        # MVP: primaries is BT.709 only (sRGB and Rec.709 share it); users
        # choose the transfer function (gamma curve). Stage 2 adds primaries.
        cms_group = QGroupBox(
            I18N.tr("preferences.color_management.presets_section",
                    "Color Space Presets (Preview & Output)")
        )
        cms_group.setObjectName("PrefSettingsGroup")
        cms_form = make_pref_form(cms_group)

        # Transfer-function options (the gamma curve). Primaries fixed to
        # BT.709 for MVP; this combo is reused for all 4 (preview/output ×
        # timeline/image) selectors. Values are ColorTransfer enum values.
        def _make_transfer_combo(key: str) -> ChevronComboBox:
            combo = ChevronComboBox()
            combo.setMinimumWidth(120)
            combo.addItem(I18N.tr("preferences.color_management.tf_srgb", "sRGB (~gamma 2.2)"), "iec61966_2_1")
            combo.addItem(I18N.tr("preferences.color_management.tf_rec709", "Rec.709 (~gamma 2.4)"), "bt709")
            combo.addItem(I18N.tr("preferences.color_management.tf_linear", "Linear (no encode)"), "linear")
            combo.addItem(I18N.tr("preferences.color_management.tf_pq", "PQ / SMPTE ST 2084 (HDR10)"), "smpte2084")
            combo.addItem(I18N.tr("preferences.color_management.tf_hlg", "HLG / ARIB STD-B67 (HDR broadcast)"), "arib-std-b67")
            self._widgets[key] = combo
            return combo

        # P0-COLOR-04：色域与范围显式暴露。运行时 descriptor_from_config
        # 早已支持 color.{scope}.{kind}_primaries/_range 键，但 UI 从未提供
        # 入口，HDR/宽色域只能依赖隐含默认值。键名与运行时读取一致。
        def _make_primaries_combo(key: str) -> ChevronComboBox:
            combo = ChevronComboBox()
            combo.setMinimumWidth(120)
            combo.addItem(I18N.tr("preferences.color_management.prim_bt709",
                                  "BT.709 (sRGB/HD)"), "bt709")
            combo.addItem(I18N.tr("preferences.color_management.prim_p3",
                                  "Display P3 (wide gamut)"), "smpte432")
            combo.addItem(I18N.tr("preferences.color_management.prim_bt2020",
                                  "BT.2020 (UHD/HDR)"), "bt2020")
            self._widgets[key] = combo
            return combo

        def _make_range_combo(key: str) -> ChevronComboBox:
            combo = ChevronComboBox()
            combo.setMinimumWidth(120)
            combo.addItem(I18N.tr("preferences.color_management.range_limited",
                                  "Limited (video 16-235)"), "limited")
            combo.addItem(I18N.tr("preferences.color_management.range_full",
                                  "Full (PC 0-255)"), "full")
            self._widgets[key] = combo
            return combo

        def _add_cms_row(scope: str, kind: str, kind_label: str) -> None:
            """一组三联选择器：色域 + 传输函数 + 范围（键名与运行时一致）。"""
            cms_form.addRow(
                make_pref_label(I18N.tr(
                    f"preferences.color_management.{scope}_{kind}_primaries",
                    f"{'Preview' if scope == 'preview' else 'Output'} "
                    f"Primaries ({kind_label}):")),
                _make_primaries_combo(f"color.{scope}.{kind}_primaries"),
            )
            cms_form.addRow(
                make_pref_label(I18N.tr(
                    f"preferences.color_management.{scope}_{kind}_transfer",
                    f"{'Preview' if scope == 'preview' else 'Output'} "
                    f"Transfer ({kind_label}):")),
                _make_transfer_combo(
                    f"color.{scope}.{kind}_transfer",
                ),
            )
            cms_form.addRow(
                make_pref_label(I18N.tr(
                    f"preferences.color_management.{scope}_{kind}_range",
                    f"{'Preview' if scope == 'preview' else 'Output'} "
                    f"Range ({kind_label}):")),
                _make_range_combo(f"color.{scope}.{kind}_range"),
            )

        _add_cms_row("preview", "timeline", "Timeline/Video")
        _add_cms_row("preview", "image", "Image")
        _add_cms_row("output", "timeline", "Timeline/Video")
        _add_cms_row("output", "image", "Image")

        # Smart-default toggle: auto-pick video→Rec.709, image→sRGB.
        smart_default = QCheckBox(
            I18N.tr("preferences.color_management.smart_default",
                    "Smart default (timeline→Rec.709, image→sRGB)")
        )
        smart_default.setChecked(True)
        self._widgets['color.smart_default'] = smart_default
        cms_form.addRow(make_pref_label(""), smart_default)

        layout.addWidget(cms_group)

        # ── Group 2: Image Processing ──
        proc_group = QGroupBox(
            I18N.tr("preferences.image_processing.section_default_title",
                    "Image Processing")
        )
        proc_group.setObjectName("PrefSettingsGroup")
        proc_form = make_pref_form(proc_group)

        mono_font = QFont("Menlo", 11)
        if not mono_font.exactMatch():
            mono_font = QFont("Courier", 11)

        # Default denoise strength
        denoise_widget = QWidget()
        denoise_widget.setStyleSheet("background: transparent;")
        denoise_layout = QHBoxLayout(denoise_widget)
        denoise_layout.setContentsMargins(0, 0, 0, 0)
        denoise_layout.setSpacing(8)
        denoise_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        denoise_slider.setRange(0, 100)
        denoise_slider.setObjectName("PrefSlider")
        denoise_value = QLabel("50")
        denoise_value.setObjectName("PrefSliderValue")
        denoise_value.setFixedWidth(32)
        denoise_value.setFont(mono_font)
        denoise_slider.valueChanged.connect(lambda v: denoise_value.setText(str(v)))
        denoise_layout.addWidget(denoise_slider, 1)
        denoise_layout.addWidget(denoise_value)
        self._widgets['default_denoise_strength'] = denoise_slider
        proc_form.addRow(
            make_pref_label(I18N.tr("preferences.image_processing.denoise_strength",
                                     "Default denoise strength:")),
            denoise_widget
        )

        # Default sharpen amount
        sharp_widget = QWidget()
        sharp_widget.setStyleSheet("background: transparent;")
        sharp_layout = QHBoxLayout(sharp_widget)
        sharp_layout.setContentsMargins(0, 0, 0, 0)
        sharp_layout.setSpacing(8)
        sharp_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        sharp_slider.setRange(0, 100)
        sharp_slider.setObjectName("PrefSlider")
        sharp_value = QLabel("0")
        sharp_value.setObjectName("PrefSliderValue")
        sharp_value.setFixedWidth(32)
        sharp_value.setFont(mono_font)
        sharp_slider.valueChanged.connect(lambda v: sharp_value.setText(str(v)))
        sharp_layout.addWidget(sharp_slider, 1)
        sharp_layout.addWidget(sharp_value)
        self._widgets['default_sharpen_amount'] = sharp_slider
        proc_form.addRow(
            make_pref_label(I18N.tr("preferences.image_processing.sharpen_amount",
                                     "Default sharpening amount:")),
            sharp_widget
        )

        layout.addWidget(proc_group)

        # ── Group 3: AI Processing ──
        ai_group = QGroupBox(
            I18N.tr("preferences.image_processing.section_ai_title", "AI Processing")
        )
        ai_group.setObjectName("PrefSettingsGroup")
        ai_form = make_pref_form(ai_group)

        # AI model path
        ai_path_widget = QWidget()
        ai_path_layout = QHBoxLayout(ai_path_widget)
        ai_path_layout.setContentsMargins(0, 0, 0, 0)
        ai_path_layout.setSpacing(6)
        ai_path_edit = QLineEdit()
        ai_path_edit.setObjectName("PrefPathEdit")
        ai_path_edit.setPlaceholderText(I18N.tr(
            "preferences.ai_model_dir_placeholder", "Select AI Model Directory"))
        _apply_text_input_style(ai_path_edit)
        ai_path_btn = QPushButton(I18N.tr("preferences.export.browse", "Browse"))
        ai_path_btn.setObjectName("PrefBrowseBtn")
        ai_path_btn.setFixedWidth(80)
        _apply_compact_button_style(ai_path_btn)
        ai_path_btn.clicked.connect(
            lambda: self._browse_path(ai_path_edit, "Select AI Model Directory")
        )
        ai_path_layout.addWidget(ai_path_edit, 1)
        ai_path_layout.addWidget(ai_path_btn)
        self._widgets['ai_model_path'] = ai_path_edit
        ai_form.addRow(
            make_pref_label(I18N.tr("preferences.image_processing.ai_model_path",
                                     "AI Model Path:")),
            ai_path_widget
        )

        # AI tile size
        ai_tile = ChevronComboBox()
        ai_tile.setMinimumWidth(120)
        for size in [256, 512, 1024]:
            ai_tile.addItem(f"{size}x{size}", size)
        self._widgets['ai_tile_size'] = ai_tile
        ai_form.addRow(
            make_pref_label(I18N.tr("preferences.image_processing.ai_tile_size",
                                     "AI Tile Size:")),
            ai_tile
        )

        # AI GPU device
        ai_device = ChevronComboBox()
        ai_device.setMinimumWidth(120)
        ai_device.addItem(
            I18N.tr("preferences.image_processing.ai_device_auto", "Auto"), "auto"
        )
        ai_device.addItem(
            I18N.tr("preferences.image_processing.ai_device_cpu", "CPU Only"), "cpu"
        )
        ai_device.addItem(
            I18N.tr("preferences.image_processing.ai_device_cuda",
                    "CUDA (NVIDIA)"), "cuda"
        )
        ai_device.addItem(
            I18N.tr("preferences.image_processing.ai_device_coreml",
                    "CoreML (Apple)"), "coreml"
        )
        self._widgets['ai_gpu_device'] = ai_device
        ai_form.addRow(
            make_pref_label(I18N.tr("preferences.image_processing.ai_gpu_device",
                                     "AI GPU Device:")),
            ai_device
        )

        layout.addWidget(ai_group)
        layout.addStretch()
        return scroll

    # ── Load / Apply / Defaults ──────────────────────────────────────

    @staticmethod
    def _set_combo_data(combo: ChevronComboBox, value: object) -> None:
        """Select the combo item whose itemData matches ``value``.

        Falls back silently (leaves current selection) if no match — callers
        pre-populate sensible defaults so this is safe.
        """
        if value is None:
            return
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    @classmethod
    def _cms_value(cls, key: str, value: object) -> object:
        """CMS 键取值：缺键（None）回退运行时默认（AUD-24611）。

        未匹配的键原样返回 None，由 _set_combo_data 保持当前选择。
        """
        if value is not None:
            return value
        parts = key.split(".")  # color.{scope}.{kind}_{field}
        if len(parts) == 3 and parts[0] == "color":
            kind, _, field = parts[2].rpartition("_")
            return cls._CMS_RUNTIME_DEFAULTS.get((kind, field))
        return None

    @staticmethod
    def _config_float(value: object, fallback: float, key: str = "") -> float:
        """AUD-24612: 外部配置值防御式 float 转换。

        config.json 是用户可编辑文件，坏值（"abc"/null/列表）不得中断
        load()——上层对话框对 page.load(cfg) 是静默吞错，半途中断会让
        其后所有控件停留在构建默认值且无任何提示。
        """
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning(
                "Invalid numeric value %r for %s; using default %r",
                value, key or "<color preference>", fallback)
            return fallback

    @staticmethod
    def _config_int(value: object, fallback: int, key: str = "") -> int:
        """AUD-24612: 外部配置值防御式 int 转换（语义同 _config_float）。"""
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning(
                "Invalid numeric value %r for %s; using default %r",
                value, key or "<color preference>", fallback)
            return fallback

    def load(self, config: dict) -> None:
        """Load color & processing settings from config into widgets."""
        if 'default_color_space' not in self._widgets:
            return

        # Color Settings
        cs = config.get('default_color_space', 'sRGB')
        cs_combo = self._widgets['default_color_space']
        idx = cs_combo.findText(cs)
        if idx >= 0:
            cs_combo.setCurrentIndex(idx)

        bd = config.get('default_bit_depth', '8-bit')
        bd_combo = self._widgets['default_bit_depth']
        idx = bd_combo.findText(bd)
        if idx >= 0:
            bd_combo.setCurrentIndex(idx)

        intent = config.get('rendering_intent', 'relative')
        intent_combo = self._widgets['rendering_intent']
        for i in range(intent_combo.count()):
            if intent_combo.itemData(i) == intent:
                intent_combo.setCurrentIndex(i)
                break

        self._widgets['gpu_color_processing'].setChecked(
            config.get('gpu_color_processing', True)
        )
        self._widgets['show_color_space_warnings'].setChecked(
            config.get('show_color_space_warnings', True)
        )
        self._widgets['auto_assign_icc'].setChecked(
            config.get('auto_assign_icc', True)
        )
        self._widgets['ocio_config_path'].setText(
            config.get('ocio_config_path', '')
        )
        self._set_combo_data(
            self._widgets[CONTROL_SURFACE_CONFIG_KEY],
            normalize_control_surface(
                config.get(CONTROL_SURFACE_CONFIG_KEY, DEFAULT_CONTROL_SURFACE)
            ),
        )
        self._widgets[COLOR_WHEEL_ROTATION_CONFIG_KEY].setValue(
            self._config_float(
                config.get(
                    COLOR_WHEEL_ROTATION_CONFIG_KEY,
                    DEFAULT_COLOR_WHEEL_ROTATION_DEGREES,
                ),
                DEFAULT_COLOR_WHEEL_ROTATION_DEGREES,
                COLOR_WHEEL_ROTATION_CONFIG_KEY,
            )
        )
        self._widgets[HUE_DIRECTION_INVERTED_CONFIG_KEY].setChecked(
            bool(config.get(HUE_DIRECTION_INVERTED_CONFIG_KEY, False))
        )
        self._widgets[TRACKERBALL_SMOOTHING_CONFIG_KEY].setValue(
            self._config_int(
                config.get(
                    TRACKERBALL_SMOOTHING_CONFIG_KEY,
                    DEFAULT_TRACKERBALL_SMOOTHING,
                ),
                DEFAULT_TRACKERBALL_SMOOTHING,
                TRACKERBALL_SMOOTHING_CONFIG_KEY,
            )
        )
        for key in TANGENT_SENSITIVITY_CONFIG_KEYS:
            value = config.get(key, DEFAULT_TANGENT_SENSITIVITY[key])
            self._widgets[key].setValue(
                tangent_sensitivity_to_display(value)
                if key in TANGENT_TRACKERBALL_SENSITIVITY_CONFIG_KEYS
                else self._config_float(
                    value, DEFAULT_TANGENT_SENSITIVITY[key], key)
            )

        # Color Space Presets (preview/output transfer functions)
        for key in self._CMS_CONFIG_KEYS:
            # AUD-24611: 缺键按运行时默认预选（image→full 等），apply()
            # 写回值与引擎当前行为一致，不再静默固化为组合框首项。
            self._set_combo_data(
                self._widgets[key], self._cms_value(key, config.get(key)))
        self._widgets['color.smart_default'].setChecked(
            config.get('color.smart_default', True)
        )

        # Image Processing
        self._widgets['default_denoise_strength'].setValue(
            self._config_int(config.get('default_denoise_strength', 50), 50,
                             'default_denoise_strength')
        )
        self._widgets['default_sharpen_amount'].setValue(
            self._config_int(config.get('default_sharpen_amount', 0), 0,
                             'default_sharpen_amount')
        )

        # AI Processing
        self._widgets['ai_model_path'].setText(
            config.get('ai_model_path', '')
        )
        ai_tile = config.get('ai_tile_size', 512)
        ai_tile_combo = self._widgets['ai_tile_size']
        for i in range(ai_tile_combo.count()):
            if ai_tile_combo.itemData(i) == ai_tile:
                ai_tile_combo.setCurrentIndex(i)
                break
        ai_device = config.get('ai_gpu_device', 'auto')
        ai_device_combo = self._widgets['ai_gpu_device']
        for i in range(ai_device_combo.count()):
            if ai_device_combo.itemData(i) == ai_device:
                ai_device_combo.setCurrentIndex(i)
                break

    def apply(self, config_manager: ConfigManager) -> None:
        """Read widget values and write to config manager."""
        if 'default_color_space' not in self._widgets:
            return

        # Color Settings
        config_manager.set('default_color_space',
                           self._widgets['default_color_space'].currentText(),
                           auto_save=False)
        config_manager.set('default_bit_depth',
                           self._widgets['default_bit_depth'].currentText(),
                           auto_save=False)
        config_manager.set('rendering_intent',
                           self._widgets['rendering_intent'].currentData(),
                           auto_save=False)
        config_manager.set('gpu_color_processing',
                           self._widgets['gpu_color_processing'].isChecked(),
                           auto_save=False)
        config_manager.set('show_color_space_warnings',
                           self._widgets['show_color_space_warnings'].isChecked(),
                           auto_save=False)
        config_manager.set('auto_assign_icc',
                           self._widgets['auto_assign_icc'].isChecked(),
                           auto_save=False)
        config_manager.set('ocio_config_path',
                           self._widgets['ocio_config_path'].text(),
                           auto_save=False)
        config_manager.set(
            CONTROL_SURFACE_CONFIG_KEY,
            normalize_control_surface(
                self._widgets[CONTROL_SURFACE_CONFIG_KEY].currentData()
            ),
            auto_save=False,
        )
        config_manager.set(
            COLOR_WHEEL_ROTATION_CONFIG_KEY,
            self._widgets[COLOR_WHEEL_ROTATION_CONFIG_KEY].value(),
            auto_save=False,
        )
        config_manager.set(
            HUE_DIRECTION_INVERTED_CONFIG_KEY,
            self._widgets[HUE_DIRECTION_INVERTED_CONFIG_KEY].isChecked(),
            auto_save=False,
        )
        config_manager.set(
            TRACKERBALL_SMOOTHING_CONFIG_KEY,
            self._widgets[TRACKERBALL_SMOOTHING_CONFIG_KEY].value(),
            auto_save=False,
        )
        for key in TANGENT_SENSITIVITY_CONFIG_KEYS:
            value = self._widgets[key].value()
            config_manager.set(
                key,
                tangent_sensitivity_from_display(value)
                if key in TANGENT_TRACKERBALL_SENSITIVITY_CONFIG_KEYS
                else int(round(value)),
                auto_save=False,
            )

        # Color Space Presets (preview/output transfer functions)
        for key in self._CMS_CONFIG_KEYS:
            config_manager.set(key, self._widgets[key].currentData(), auto_save=False)
        config_manager.set('color.smart_default',
                           self._widgets['color.smart_default'].isChecked(),
                           auto_save=False)

        # Image Processing
        config_manager.set('default_denoise_strength',
                           self._widgets['default_denoise_strength'].value(),
                           auto_save=False)
        config_manager.set('default_sharpen_amount',
                           self._widgets['default_sharpen_amount'].value(),
                           auto_save=False)
        config_manager.set('ai_model_path',
                           self._widgets['ai_model_path'].text(),
                           auto_save=False)
        config_manager.set('ai_tile_size',
                           self._widgets['ai_tile_size'].currentData(),
                           auto_save=False)
        config_manager.set('ai_gpu_device',
                           self._widgets['ai_gpu_device'].currentData(),
                           auto_save=False)

    def restore_defaults(self, defaults: dict) -> None:
        """Reset color & processing widgets to default values."""
        if 'default_color_space' not in self._widgets:
            return

        # Color Settings
        cs_combo = self._widgets['default_color_space']
        idx = cs_combo.findText(defaults['default_color_space'])
        if idx >= 0:
            cs_combo.setCurrentIndex(idx)

        bd_combo = self._widgets['default_bit_depth']
        idx = bd_combo.findText(defaults['default_bit_depth'])
        if idx >= 0:
            bd_combo.setCurrentIndex(idx)

        intent_combo = self._widgets['rendering_intent']
        for i in range(intent_combo.count()):
            if intent_combo.itemData(i) == defaults.get('rendering_intent', 'relative'):
                intent_combo.setCurrentIndex(i)
                break

        self._widgets['gpu_color_processing'].setChecked(
            defaults['gpu_color_processing']
        )
        self._widgets['show_color_space_warnings'].setChecked(
            defaults['show_color_space_warnings']
        )
        self._widgets['auto_assign_icc'].setChecked(
            defaults.get('auto_assign_icc', True)
        )
        self._widgets['ocio_config_path'].setText(
            defaults.get('ocio_config_path', '')
        )
        self._set_combo_data(
            self._widgets[CONTROL_SURFACE_CONFIG_KEY],
            normalize_control_surface(
                defaults.get(CONTROL_SURFACE_CONFIG_KEY, DEFAULT_CONTROL_SURFACE)
            ),
        )
        self._widgets[COLOR_WHEEL_ROTATION_CONFIG_KEY].setValue(
            float(defaults.get(
                COLOR_WHEEL_ROTATION_CONFIG_KEY,
                DEFAULT_COLOR_WHEEL_ROTATION_DEGREES,
            ))
        )
        self._widgets[HUE_DIRECTION_INVERTED_CONFIG_KEY].setChecked(
            bool(defaults.get(HUE_DIRECTION_INVERTED_CONFIG_KEY, False))
        )
        self._widgets[TRACKERBALL_SMOOTHING_CONFIG_KEY].setValue(
            int(defaults.get(
                TRACKERBALL_SMOOTHING_CONFIG_KEY,
                DEFAULT_TRACKERBALL_SMOOTHING,
            ))
        )
        for key in TANGENT_SENSITIVITY_CONFIG_KEYS:
            value = defaults.get(key, DEFAULT_TANGENT_SENSITIVITY[key])
            self._widgets[key].setValue(
                tangent_sensitivity_to_display(value)
                if key in TANGENT_TRACKERBALL_SENSITIVITY_CONFIG_KEYS
                else float(value)
            )

        # Color Space Presets
        for key in self._CMS_CONFIG_KEYS:
            # AUD-24611: DEFAULT_CONFIG 未覆盖的 range 键同样按运行时默认
            # 预选，"恢复默认"后组合框与引擎行为一致。
            self._set_combo_data(
                self._widgets[key], self._cms_value(key, defaults.get(key)))
        self._widgets['color.smart_default'].setChecked(
            defaults.get('color.smart_default', True)
        )

        # Image Processing
        self._widgets['default_denoise_strength'].setValue(
            defaults.get('default_denoise_strength', 50)
        )
        self._widgets['default_sharpen_amount'].setValue(
            defaults.get('default_sharpen_amount', 0)
        )

        # AI Processing
        self._widgets['ai_model_path'].setText(
            defaults.get('ai_model_path', '')
        )
        ai_tile_combo = self._widgets['ai_tile_size']
        for i in range(ai_tile_combo.count()):
            if ai_tile_combo.itemData(i) == defaults.get('ai_tile_size', 512):
                ai_tile_combo.setCurrentIndex(i)
                break
        ai_device_combo = self._widgets['ai_gpu_device']
        for i in range(ai_device_combo.count()):
            if ai_device_combo.itemData(i) == defaults.get('ai_gpu_device', 'auto'):
                ai_device_combo.setCurrentIndex(i)
                break

    # ── Helpers ──────────────────────────────────────────────────────

    def _browse_path(self, line_edit: QLineEdit, title: str,
                     is_file: bool = False) -> None:
        """Open directory or file picker for path input."""
        from PyQt6.QtWidgets import QFileDialog
        current = line_edit.text()
        if is_file:
            path, _ = QFileDialog.getOpenFileName(
                self._dialog_ref, title, current if current else "",
                "All Files (*)"
            )
        else:
            path = QFileDialog.getExistingDirectory(
                self._dialog_ref, title, current if current else ""
            )
        if path:
            line_edit.setText(path)
