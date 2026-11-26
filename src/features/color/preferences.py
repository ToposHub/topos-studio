"""Color module's Color & Processing preference page.

Self-contained preference page for color management and image processing:
- Color settings (color space, bit depth, rendering intent, GPU, ICC, OCIO)
- Image processing (denoise strength, sharpen amount)
- AI processing (model path, tile size, GPU device)

This page is always shown since Color is the core feature.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.shared.color.lut_resource_registry import LutResourceSession
    from src.shared.config.config_manager import ConfigManager
    from src.shared.project.resource_resolver import ResourceEntry

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QFileDialog, QLabel, QMessageBox, QPushButton, QCheckBox, QLineEdit, QProgressDialog,
    QGroupBox, QSpinBox, QDoubleSpinBox,
)
from PyQt6.QtCore import QSignalBlocker, QTimer, Qt
from PyQt6.QtGui import QFont

from src.shared.i18n.i18n import I18N
from src.shared.color.camera_input import CAMERA_INPUTS
from src.shared.color.color_space_descriptor import (
    COLOR_SPACE_PRESETS,
    LEGACY_SPACE,
    OUTPUT_SPACE_KEY,
    TIMELINE_SPACE_KEY,
    project_output_space,
    project_timeline_space,
)
from src.shared.color.desktop_video_color import USE_DISPLAY_ICC
from src.shared.color.hdr_video_signal import (
    HDR_HLG_PEAK_KEY, HDR_INPUT_KEY, HDR_INPUT_SPACES, HDR_MASTER_GAMUT_KEY, HDR_MASTER_PEAK_KEY,
    HDR_MONITOR_KEY, HDR_OUTPUT_SPACES,
    HDR_WHITE_KEY, HDR_WORKING_SPACES,
)
from src.shared.color.monitor_calibration import (
    DESKTOP_CALIBRATION_LUT, EXTERNAL_CALIBRATION_LUT, load_monitor_calibration,
)
from src.shared.color.project_video_signal import (
    ACES_DCI_P3_MONITOR, ACES_DESKTOP_LUT_KEY, ACES_DISPLAY_TARGETS, ACES_EXTERNAL_LUT_KEY,
    ACES_EXTERNAL_MONITOR_KEY, ACES_INPUT_KEY, ACES_INPUT_SPACES, ACES_LINEAR_SPACES,
    ACES_RGC_KEY, ACES_VERSION_KEY, ACES_WORKING_SPACES,
)
from src.shared.color.types import ColorPrimaries, ColorTransfer
from src.shared.color.video_delivery_profiles import SDR_VIDEO_OUTPUTS, WIDE_GAMUT_TIFF_OUTPUTS
from src.shared.feature_flags import get_workspace_work_mode
from src.shared.ui.widgets import ChevronComboBox
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.widgets.pref_form_layout import make_pref_form, make_pref_label, make_pref_page
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
_MONITOR_LUT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aces-monitor-lut")
_CALIBRATION_LUT_KEYS = (DESKTOP_CALIBRATION_LUT, EXTERNAL_CALIBRATION_LUT)


def _wait_for_lut(parent: QWidget, title: str, future: Future[Any]) -> bool:
    progress = QProgressDialog(title, "", 0, 0, parent)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setCancelButton(None)
    poll = QTimer(progress)

    def finish_import() -> None:
        if future.done():
            progress.accept()

    poll.timeout.connect(finish_import)
    poll.start(25)
    accepted = progress.exec()
    poll.stop()
    progress.deleteLater()
    return bool(accepted and future.done())


def _import_monitor_lut(path: str, staging: LutResourceSession) -> ResourceEntry:
    from src.shared.color.aces_monitor_lut import validate_aces_monitor_lut

    validate_aces_monitor_lut(path)
    result = staging.import_files([path])[0]
    if result.entry is None:
        raise ValueError(str(result.error))
    return result.entry


class ColorSpaceControls(QWidget):
    """Compact project color selectors shared by Preferences and Project Settings."""

    _ROWS = {
        TIMELINE_SPACE_KEY: ("timeline_space", "Timeline color space"),
        OUTPUT_SPACE_KEY: ("output_space", "Video output color space"),
    }
    _ACES_ROWS = {
        TIMELINE_SPACE_KEY: ("aces_working_space", "ACES working space"),
        OUTPUT_SPACE_KEY: ("aces_output_transform", "ACES output transform"),
    }
    _OPTIONS = {
        "rec709-g24": ("preset_rec709_g24", "Rec.709 Gamma 2.4"),
        "rec709-bt709": ("preset_rec709_bt709", "Rec.709 (BT.709 OETF)"),
        "srgb": ("preset_srgb", "sRGB"),
        "display-p3": ("preset_display_p3", "Display P3"),
        "p3-d65-g24": ("preset_p3_d65_g24", "P3-D65 Gamma 2.4"),
        "dci-p3-g26": ("preset_dci_p3_g26", "DCI-P3 Gamma 2.6"),
        "rec2020-g24": ("preset_rec2020_g24", "Rec.2020 Gamma 2.4"),
        "rec2020-bt2020": ("preset_rec2020_bt2020", "Rec.2020 (BT.2020 OETF)"),
        "aces2065-1": ("preset_aces2065_1", "ACES2065-1 (AP0 Linear)"),
        "acescg": ("preset_acescg", "ACEScg (AP1 Linear)"),
        "acescct": ("preset_acescct", "ACEScct"),
        "acescc": ("preset_acescc", "ACEScc"),
        "auto": ("aces_auto_input", "Auto (EXR metadata or SDR default)"),
        "linear-rec709": ("preset_linear_rec709", "Linear Rec.709"),
        "rec2100-pq": ("preset_rec2100_pq", "Rec.2100 PQ (ST 2084)"),
        "rec2100-hlg": ("preset_rec2100_hlg", "Rec.2100 HLG"),
        "davinci-wide-gamut-intermediate": ("preset_davinci_intermediate", "DaVinci Wide Gamut / Intermediate"),
        LEGACY_SPACE: ("preset_legacy", "Existing color settings"),
    }
    _ROLE_OPTIONS = {
        TIMELINE_SPACE_KEY: (*SDR_VIDEO_OUTPUTS, *HDR_WORKING_SPACES),
        OUTPUT_SPACE_KEY: (*SDR_VIDEO_OUTPUTS, *WIDE_GAMUT_TIFF_OUTPUTS, *HDR_OUTPUT_SPACES),
    }
    _MODE_DEFAULTS = {
        "topos": ("rec709-g24", "rec709-g24"),
        "aces": ("acescct", "rec709-g24"),
    }
    _PRIMARIES_LABELS = {
        ColorPrimaries.BT709: "Rec.709",
        ColorPrimaries.BT2020: "Rec.2020",
        ColorPrimaries.P3_DISPLAY: "P3-D65",
        ColorPrimaries.P3_DCI: "DCI-P3",
        ColorPrimaries.AP0: "ACES AP0",
        ColorPrimaries.AP1: "ACES AP1",
        ColorPrimaries.DAVINCI_WIDE_GAMUT: "DaVinci Wide Gamut",
    }
    _TRANSFER_LABELS = {
        ColorTransfer.GAMMA24: "Gamma 2.4",
        ColorTransfer.GAMMA26: "Gamma 2.6",
        ColorTransfer.SRGB: "sRGB",
        ColorTransfer.BT709: "BT.709 OETF",
        ColorTransfer.BT2020_10: "BT.2020 OETF",
        ColorTransfer.LINEAR: "Linear",
        ColorTransfer.ACESCC: "ACEScc",
        ColorTransfer.ACESCCT: "ACEScct",
        ColorTransfer.PQ: "PQ (ST 2084)",
        ColorTransfer.HLG: "HLG",
        ColorTransfer.DAVINCI_INTERMEDIATE: "DaVinci Intermediate",
    }

    def __init__(
        self, parent: QWidget | None = None, *, project_monitor_luts: bool = False,
        preference_locale: bool = False,
    ) -> None:
        super().__init__(parent)
        self._tr = I18N.tr_preference if preference_locale else I18N.tr
        self._combos: dict[str, ChevronComboBox] = {}
        self._space_labels: dict[str, QLabel] = {}
        self._parts: dict[str, tuple[QWidget, ChevronComboBox, ChevronComboBox]] = {}
        self._loaded: dict[str, str] = {}
        self._mode_drafts: dict[str, tuple[str, str]] = {}
        self._aces_group = QWidget(self)
        self._aces_combos: dict[str, ChevronComboBox] = {}
        self._monitor_lut_labels: dict[str, QLabel] = {}
        self._monitor_lut_clear_buttons: dict[str, QPushButton] = {}
        self._monitor_lut_refs: dict[str, str] = {}
        self._monitor_lut_folder: TemporaryDirectory[str] | None = None
        self._monitor_lut_staging: LutResourceSession | None = None
        self._hdr_group = QWidget(self)
        self._hdr_combos: dict[str, ChevronComboBox] = {}
        self._form = form = make_pref_form(self)
        self._science = ChevronComboBox(self)
        self._science.setObjectName("color.science")
        science_label = self._tr("preferences.color_management.color_science", "Color science")
        self._science.setAccessibleName(science_label)
        self._science.addItem("TOPOS Color Manager", "topos")
        self._science.addItem("ACES", "aces")
        form.addRow(make_pref_label(science_label), self._science)
        for key, (label_key, label) in self._ROWS.items():
            if key == OUTPUT_SPACE_KEY:
                form.addRow(self._aces_group)
            combo = ChevronComboBox(self)
            combo.setObjectName(key)
            combo.setMinimumWidth(160)
            title = self._tr(f"preferences.color_management.{label_key}", label)
            combo.setAccessibleName(title)
            row = QWidget(self)
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(combo)
            split = QWidget(row)
            split_layout = QHBoxLayout(split)
            split_layout.setContentsMargins(0, 0, 0, 0)
            gamut, gamma = ChevronComboBox(split), ChevronComboBox(split)
            for widget, name, label in ((gamut, "primaries", "Gamut"), (gamma, "transfer", "Gamma")):
                caption = self._tr(f"preferences.color_management.{name}", label)
                widget.setObjectName(f"{key}.{name}")
                widget.setAccessibleName(caption)
                column = QVBoxLayout()
                column.setSpacing(4)
                caption_label = QLabel(caption)
                caption_label.setObjectName("PrefNoteLabel")
                caption_label.setBuddy(widget)
                column.addWidget(caption_label)
                column.addWidget(widget)
                split_layout.addLayout(column, 1)
            row_layout.addWidget(split)
            self._space_labels[key] = make_pref_label(title)
            form.addRow(self._space_labels[key], row)
            self._combos[key] = combo
            self._parts[key] = (split, gamut, gamma)
            combo.currentIndexChanged.connect(lambda _, row_key=key: self._sync_parts(row_key))
            gamut.currentIndexChanged.connect(lambda _, row_key=key: self._select_parts(row_key))
            gamma.currentIndexChanged.connect(lambda _, row_key=key: self._select_parts(row_key))
        self._combine = QCheckBox(self._tr(
            "preferences.color_management.combined_color_space", "Combine gamut and gamma"), self)
        self._combine.setObjectName("color.combine_gamut_gamma")
        self._combine.setChecked(True)
        self._combine.toggled.connect(self._sync_all_parts)
        form.addRow("", self._combine)
        self._wide_delivery_notice = QLabel(self._tr(
            "preferences.color_management.wide_tiff_delivery",
            "This output uses a 16-bit RGB TIFF sequence with an ICC profile."))
        self._wide_delivery_notice.setObjectName("PrefNoteLabel")
        self._wide_delivery_notice.setWordWrap(True)
        form.addRow("", self._wide_delivery_notice)
        aces_form = make_pref_form(self._aces_group, margins=(0, 0, 0, 0))
        for key, title, fallback, items in (
            (ACES_VERSION_KEY, "aces_version", "ACES version", (("1.3", "1.3"), ("2.0", "2.0"))),
            (ACES_INPUT_KEY, "aces_input", "ACES input transform",
             tuple((option, self._option_label(option)) for option in ACES_INPUT_SPACES)),
            (ACES_RGC_KEY, "aces_rgc", "ACES 1.3 Reference Gamut Compression",
             (("off", self._tr("preferences.color_management.aces_off", "Off")),
              ("on", self._tr("preferences.color_management.aces_on", "On")))),
            (ACES_EXTERNAL_MONITOR_KEY, "aces_external_monitor", "ACES external monitor",
             (("rec709-g24", self._option_label("rec709-g24")),
              (ACES_DCI_P3_MONITOR, self._tr("preferences.color_management.aces_dci_monitor",
                                          "Cinema DCI-P3 Gamma 2.6 (48 nit)")))),
        ):
            combo = ChevronComboBox(self._aces_group)
            combo.setObjectName(key)
            label = self._tr(f"preferences.color_management.{title}", fallback)
            combo.setAccessibleName(label)
            for value, caption in items:
                combo.addItem(caption, value)
            aces_form.addRow(make_pref_label(label), combo)
            self._aces_combos[key] = combo
        if project_monitor_luts:
            for key, name, fallback in (
                (ACES_DESKTOP_LUT_KEY, "aces_desktop_lut", "Desktop viewer LUT"),
                (ACES_EXTERNAL_LUT_KEY, "aces_external_lut", "External monitor LUT"),
            ):
                title = self._tr(f"preferences.color_management.{name}", fallback)
                field = QWidget(self._aces_group)
                field.setObjectName(key)
                row = QHBoxLayout(field)
                row.setContentsMargins(0, 0, 0, 0)
                file_label = QLabel(field)
                file_label.setWordWrap(True)
                self._monitor_lut_labels[key] = file_label
                row.addWidget(file_label, 1)
                browse = QPushButton(self._tr("preferences.color_management.aces_lut_load", "Load…"), field)
                clear = QPushButton(self._tr("preferences.color_management.aces_lut_clear", "Clear"), field)
                _apply_compact_button_style(browse)
                _apply_compact_button_style(clear)
                row.addWidget(browse)
                row.addWidget(clear)
                browse.clicked.connect(lambda _, lut_key=key: self._select_monitor_lut(lut_key))
                clear.clicked.connect(lambda _, lut_key=key: self._set_monitor_lut(lut_key, ""))
                self._monitor_lut_clear_buttons[key] = clear
                if key == ACES_DESKTOP_LUT_KEY:
                    tip = self._tr("preferences.color_management.aces_desktop_lut_tip",
                                  "Load a 3D .cube from ACEScct to Rec.709 Gamma 2.4 to replace the desktop transform.")
                else:
                    tip = self._tr("preferences.color_management.aces_external_lut_tip",
                                  "Load a 3D .cube from ACEScct to the selected external monitor signal.")
                field.setToolTip(tip)
                aces_form.addRow(make_pref_label(title, tip), field)
        self._aces_combos[ACES_VERSION_KEY].currentIndexChanged.connect(self._sync_aces_options)
        self._combos[TIMELINE_SPACE_KEY].currentIndexChanged.connect(self._refresh_output_choices)
        self._aces_delivery_notice = QLabel(self._tr(
            "preferences.color_management.aces_exr_delivery",
            "OpenEXR delivery is scene-linear. The viewer uses the ACES Rec.709 output transform."))
        self._aces_delivery_notice.setObjectName("PrefNoteLabel")
        self._aces_delivery_notice.setWordWrap(True)
        form.addRow("", self._aces_delivery_notice)
        hdr_form = make_pref_form(self._hdr_group, margins=(0, 0, 0, 0))
        hdr_inputs = (
            ("auto", self._tr("preferences.color_management.hdr_auto", "Auto (video tags or SDR default)")),
            ("timeline", self._tr("preferences.color_management.hdr_timeline_input", "Same as timeline")),
            *((option, self._option_label(option)) for option in HDR_INPUT_SPACES[2:]),
        )
        for key, name, fallback, choices in (
            (HDR_INPUT_KEY, "hdr_input", "Default HDR input", hdr_inputs),
            (HDR_WHITE_KEY, "hdr_white", "Reference white", (("100", "100 nit"), ("203", "203 nit"))),
            (HDR_HLG_PEAK_KEY, "hdr_hlg_peak", "HLG reference display peak",
             tuple((str(peak), f"{peak} nit") for peak in (1000, 600, 2000, 4000))),
            (HDR_MONITOR_KEY, "hdr_monitor", "HDR desktop preview",
             (("signal", self._tr("preferences.color_management.hdr_signal", "PQ / HLG reference signal")),
              ("rec709", self._tr("preferences.color_management.hdr_rec709", "Rec.709 SDR reference")))),
            (HDR_MASTER_PEAK_KEY, "hdr_master_peak", "PQ mastering display peak",
             tuple((str(peak), f"{peak} nit") for peak in (1000, 600, 2000, 4000, 10000))),
            (HDR_MASTER_GAMUT_KEY, "hdr_master_gamut", "PQ mastering display gamut",
             (("bt2020", "Rec.2020"), ("p3-d65", "P3-D65"))),
        ):
            combo = ChevronComboBox(self._hdr_group)
            combo.setObjectName(key)
            label = self._tr(f"preferences.color_management.{name}", fallback)
            combo.setAccessibleName(label)
            for value, caption in choices:
                combo.addItem(caption, value)
            hdr_form.addRow(make_pref_label(label), combo)
            self._hdr_combos[key] = combo
        form.addRow(self._hdr_group)
        self._hdr_combos[HDR_MONITOR_KEY].setToolTip(self._tr(
            "preferences.color_management.hdr_desktop_signal_tip",
            "Signal codes can be compared to Resolve with viewer ICC disabled. "
            "Rec.709 reference converts to SDR and clips values beyond the display range.",
        ))
        self._science.currentIndexChanged.connect(self._switch_science)
        self.load(None)

    def _option_label(self, option: str) -> str:
        if option in self._OPTIONS:
            key, label = self._OPTIONS[option]
            return self._tr(f"preferences.color_management.{key}", label)
        if option in CAMERA_INPUTS:
            return CAMERA_INPUTS[option][0]
        return str(COLOR_SPACE_PRESETS[option])

    def _sync_aces_options(self) -> None:
        version13 = self._aces_combos[ACES_VERSION_KEY].currentData() == "1.3"
        self._aces_combos[ACES_RGC_KEY].setEnabled(version13)
        external = self._aces_combos[ACES_EXTERNAL_MONITOR_KEY]
        dci_index = external.findData(ACES_DCI_P3_MONITOR)
        dci_available = version13 or bool(self._monitor_lut_refs.get(ACES_EXTERNAL_LUT_KEY))
        external.model().item(dci_index).setEnabled(dci_available)
        if not dci_available and external.currentIndex() == dci_index:
            external.setCurrentIndex(external.findData("rec709-g24"))

    def _set_monitor_lut(self, key: str, ref: str) -> None:
        from src.shared.color.lut_resource_registry import get_lut_resource_session

        self._monitor_lut_refs[key] = ref
        label = self._tr("preferences.color_management.aces_lut_builtin", "Built-in transform")
        if ref:
            entries = get_lut_resource_session().lut_entries()
            if self._monitor_lut_staging is not None:
                entries.extend(self._monitor_lut_staging.lut_entries())
            entry = next((item for item in entries if item.uri == ref), None)
            label = (entry.original_filename if entry is not None else
                     self._tr("preferences.color_management.aces_lut_unavailable", "LUT unavailable"))
        self._monitor_lut_labels[key].setText(label)
        self._monitor_lut_clear_buttons[key].setEnabled(bool(ref))
        self._sync_aces_options()

    def _select_monitor_lut(self, key: str) -> None:
        from src.shared.color.lut_resource_registry import LutResourceSession

        title = self._tr("preferences.color_management.aces_lut_dialog", "Load ACES monitor LUT")
        path, _ = QFileDialog.getOpenFileName(self, title, "", "3D LUT (*.cube)")
        if not path:
            return
        temporary = TemporaryDirectory(prefix="topos-monitor-lut-selection-")
        staging = LutResourceSession(temporary.name)
        future = _MONITOR_LUT_POOL.submit(_import_monitor_lut, path, staging)
        if not _wait_for_lut(self, title, future):
            future.add_done_callback(lambda _: temporary.cleanup())
            return
        try:
            entry = future.result()
            if self._monitor_lut_staging is None:
                self._monitor_lut_folder = TemporaryDirectory(prefix="topos-monitor-lut-draft-")
                self._monitor_lut_staging = LutResourceSession(self._monitor_lut_folder.name)
            self._monitor_lut_staging.adopt_staged_luts(staging, (entry.uri,))
            self._set_monitor_lut(key, entry.uri)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, title, str(exc))
        finally:
            temporary.cleanup()

    def commit_monitor_luts(self) -> None:
        """Adopt only the monitor LUTs kept by the accepted project settings."""
        from src.shared.color.lut_resource_registry import get_lut_resource_session

        staging = self._monitor_lut_staging
        settings = self.settings()
        if staging is not None:
            refs = tuple(ref for key in self._monitor_lut_refs if (ref := settings.get(key))
                         and staging.staged_path(ref) is not None)
            if refs:
                get_lut_resource_session().adopt_staged_luts(staging, refs)

    def discard_monitor_luts(self) -> None:
        if self._monitor_lut_folder is not None:
            self._monitor_lut_folder.cleanup()
        self._monitor_lut_folder = None
        self._monitor_lut_staging = None

    @staticmethod
    def _science_for_timeline(timeline: str) -> str:
        return "aces" if timeline in ACES_WORKING_SPACES else "topos"

    def _switch_science(self) -> None:
        timeline = self._combos[TIMELINE_SPACE_KEY].currentData()
        output = self._combos[OUTPUT_SPACE_KEY].currentData()
        previous = self._science_for_timeline(timeline)
        self._mode_drafts[previous] = (timeline, output)
        mode = self._science.currentData()
        self._load_spaces(*self._mode_drafts.get(mode, self._MODE_DEFAULTS[mode]))

    def _load_spaces(self, timeline: str, output: str) -> None:
        mode = self._science_for_timeline(timeline)
        aces = mode == "aces"
        with QSignalBlocker(self._science):
            self._science.setCurrentIndex(self._science.findData(mode))
        for key, (label_key, label) in (self._ACES_ROWS if aces else self._ROWS).items():
            text = self._tr(f"preferences.color_management.{label_key}", label)
            self._space_labels[key].setText(text)
            self._combos[key].setAccessibleName(text)
        self._form.setRowVisible(self._aces_group, aces)
        self._form.setRowVisible(self._combine, not aces)

        options = ACES_WORKING_SPACES if aces else self._ROLE_OPTIONS[TIMELINE_SPACE_KEY]
        combo = self._combos[TIMELINE_SPACE_KEY]
        with QSignalBlocker(combo):
            combo.clear()
            for option in options if timeline in options else (*options, timeline):
                combo.addItem(self._option_label(option), option)
            combo.setCurrentIndex(combo.findData(timeline))
        self._refresh_output_choices(selection=output)
        self._sync_parts(TIMELINE_SPACE_KEY)

    def _refresh_output_choices(self, *, selection: str | None = None) -> None:
        combo = self._combos[OUTPUT_SPACE_KEY]
        if selection is None:
            selection = combo.currentData()
        active = self._combos[TIMELINE_SPACE_KEY].currentData() in ACES_WORKING_SPACES
        options = (*ACES_DISPLAY_TARGETS, *ACES_LINEAR_SPACES) if active else self._ROLE_OPTIONS[OUTPUT_SPACE_KEY]
        if not active and selection is not None and selection not in (*options, *ACES_LINEAR_SPACES):
            options = (*options, selection)
        with QSignalBlocker(combo):
            combo.clear()
            for option in options:
                combo.addItem(self._option_label(option), option)
            combo.setCurrentIndex(max(0, combo.findData(selection)))
        self._sync_parts(OUTPUT_SPACE_KEY)

    def _sync_hdr_group(self) -> None:
        timeline = self._combos[TIMELINE_SPACE_KEY].currentData()
        output = self._combos[OUTPUT_SPACE_KEY].currentData()
        self._hdr_group.setVisible(timeline in HDR_WORKING_SPACES or output in HDR_OUTPUT_SPACES)
        monitor = self._hdr_combos.get(HDR_MONITOR_KEY)
        if monitor is not None:
            monitor.setEnabled(output in HDR_OUTPUT_SPACES)
            for key in (HDR_MASTER_PEAK_KEY, HDR_MASTER_GAMUT_KEY):
                self._hdr_combos[key].setEnabled(output == "rec2100-pq")

    def load(
        self, settings: Mapping[str, Any] | None,
        config_manager: ConfigManager | Mapping[str, Any] | None = None,
    ) -> None:
        """Load effective project choices without emitting change signals."""
        self._mode_drafts.clear()
        self._load_spaces(project_timeline_space(settings, config_manager),
                          project_output_space(settings, config_manager))
        for key, combo in (self._aces_combos | self._hdr_combos).items():
            value = (settings or {}).get(key, config_manager.get(key) if config_manager else None)
            with QSignalBlocker(combo):
                if key in (HDR_WHITE_KEY, HDR_HLG_PEAK_KEY, HDR_MASTER_PEAK_KEY) and value is not None:
                    value = str(value)
                    if combo.findData(value) == -1:
                        combo.addItem(f"{value} nit", value)
                combo.setCurrentIndex(max(0, combo.findData(value)))
        self._monitor_lut_refs = {
            key: value if isinstance(value := (settings or {}).get(key), str) else ""
            for key in self._monitor_lut_labels
        }
        for key, value in self._monitor_lut_refs.items():
            self._set_monitor_lut(key, value)
        self._sync_aces_options()
        self._sync_all_parts()
        self._loaded = self.settings()

    def _sync_all_parts(self) -> None:
        for key in self._combos:
            self._sync_parts(key)

    def _sync_parts(self, key: str) -> None:
        combo = self._combos[key]
        selection = combo.currentData()
        split, gamut, gamma = self._parts[key]
        options = tuple(combo.itemData(index) for index in range(combo.count())
                        if combo.itemData(index) in COLOR_SPACE_PRESETS)
        choices = tuple(COLOR_SPACE_PRESETS[option] for option in options
                        if COLOR_SPACE_PRESETS[option].primaries in self._PRIMARIES_LABELS
                        and COLOR_SPACE_PRESETS[option].transfer in self._TRANSFER_LABELS)
        show_split = (self._science.currentData() == "topos" and not self._combine.isChecked() and selection in options
                      and COLOR_SPACE_PRESETS[selection] in choices)
        combo.setVisible(not show_split)
        split.setVisible(show_split)
        if key == OUTPUT_SPACE_KEY:
            self._sync_hdr_group()
            self._form.setRowVisible(self._wide_delivery_notice, selection in WIDE_GAMUT_TIFF_OUTPUTS)
            if hasattr(self, "_aces_delivery_notice"):
                self._form.setRowVisible(self._aces_delivery_notice, selection in ACES_LINEAR_SPACES)
        if not show_split:
            return
        space = COLOR_SPACE_PRESETS[selection]
        with QSignalBlocker(gamut), QSignalBlocker(gamma):
            gamut.clear()
            for primaries in dict.fromkeys(choice.primaries for choice in choices):
                gamut.addItem(self._PRIMARIES_LABELS[primaries], primaries)
            gamut.setCurrentIndex(gamut.findData(space.primaries))
            gamma.clear()
            for choice in choices:
                if choice.primaries == space.primaries:
                    gamma.addItem(self._TRANSFER_LABELS[choice.transfer], choice.transfer)
            gamma.setCurrentIndex(gamma.findData(space.transfer))

    def _select_parts(self, key: str) -> None:
        _, gamut, gamma = self._parts[key]
        combo = self._combos[key]
        options = [combo.itemData(index) for index in range(combo.count())
                   if combo.itemData(index) in COLOR_SPACE_PRESETS
                   and COLOR_SPACE_PRESETS[combo.itemData(index)].primaries == gamut.currentData()]
        if not options:
            return
        selection = next((option for option in options
                          if COLOR_SPACE_PRESETS[option].transfer == gamma.currentData()), options[0])
        combo.setCurrentIndex(combo.findData(selection))

    def settings(self) -> dict[str, str]:
        """Return the color space IDs selected in this widget."""
        values = {key: combo.currentData() for key, combo in self._combos.items()}
        if values[TIMELINE_SPACE_KEY] in ACES_WORKING_SPACES:
            values.update({key: combo.currentData() for key, combo in self._aces_combos.items()})
            values.update(self._monitor_lut_refs)
        if values[TIMELINE_SPACE_KEY] in HDR_WORKING_SPACES or values[OUTPUT_SPACE_KEY] in HDR_OUTPUT_SPACES:
            values.update({key: combo.currentData() for key, combo in self._hdr_combos.items()})
        return values

    def changes(self) -> dict[str, str]:
        """Return the selections edited since the most recent load."""
        return {key: value for key, value in self.settings().items() if value != self._loaded.get(key)}


class _TangentSensitivitySpinBox(QDoubleSpinBox):
    """Display normalized sensitivity without a redundant trailing .0."""

    def textFromValue(self, value: float) -> str:
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.1f}"


class ColorManagementPreferencePage:
    """Project color defaults, display calibration and image profiles."""

    page_id = "color.management"

    def __init__(self) -> None:
        self._widgets: dict[str, QWidget] = {}
        self._color_space_controls: ColorSpaceControls | None = None

    def create_widget(self, parent: QWidget | None) -> QWidget:
        scroll, layout = make_pref_page(parent)

        if get_workspace_work_mode(getattr(parent, "_config_manager", None)) == "video":
            spaces_group = QGroupBox(I18N.tr_preference(
                "preferences.color_management.project_color_defaults", "Project Color Defaults"))
            spaces_group.setObjectName("PrefSettingsGroup")
            spaces_layout = QVBoxLayout(spaces_group)
            spaces_layout.setContentsMargins(0, 0, 0, 0)
            self._color_space_controls = ColorSpaceControls(spaces_group, preference_locale=True)
            spaces_layout.addWidget(self._color_space_controls)
            if sys.platform == "darwin":
                display_icc = QCheckBox(I18N.tr_preference(
                    "preferences.color_management.use_display_icc",
                    "Use macOS display ICC for video",
                ))
                display_icc.setToolTip(I18N.tr_preference(
                    "preferences.color_management.use_display_icc_tip",
                    "Match the “Use Mac Display Color Profile for Viewers” setting in DaVinci Resolve.",
                ))
                self._widgets[USE_DISPLAY_ICC] = display_icc
                self._color_space_controls._form.addRow("", display_icc)
            hdr_metadata = QCheckBox(I18N.tr_preference(
                "preferences.color_management.hdr_export_metadata",
                "Include static metadata in HDR10 exports",
            ))
            hdr_metadata.setToolTip(I18N.tr_preference(
                "preferences.color_management.hdr_export_metadata_help",
                "Default for new projects using HDR10 HEVC/PQ export.",
            ))
            self._widgets["color.output.hdr_metadata"] = hdr_metadata
            self._color_space_controls._form.addRow("", hdr_metadata)
            layout.addWidget(spaces_group)
            layout.addWidget(self._create_monitor_calibration_group())
        else:
            self._color_space_controls = None

        # ── Group 1: Color Settings ──
        color_group = QGroupBox(
            I18N.tr_preference("preferences.color_management.section_title", "Color Settings")
        )
        color_group.setObjectName("PrefSettingsGroup")
        color_form = make_pref_form(color_group)

        # Reference profile for warnings on imported still images.
        cs_combo = ChevronComboBox()
        cs_combo.setMinimumWidth(120)
        for cs in ["sRGB", "Adobe RGB", "ProPhoto RGB", "Rec.709", "DCI-P3"]:
            cs_combo.addItem(cs)
        self._widgets['default_color_space'] = cs_combo
        cs_label = make_pref_label(I18N.tr_preference("preferences.color_management.image_warning_profile",
                                            "Image profile warning reference:"))
        color_form.addRow(cs_label, cs_combo)

        # Default bit depth
        bd_combo = ChevronComboBox()
        bd_combo.setMinimumWidth(120)
        for depth, label in (
            ("8-bit", I18N.tr_preference("preferences.color_management.depth_8", "8-bit")),
            ("16-bit", I18N.tr_preference("preferences.color_management.depth_16", "16-bit")),
            ("32-bit float", I18N.tr_preference("preferences.color_management.depth_float", "32-bit float")),
        ):
            bd_combo.addItem(label, depth)
        self._widgets['default_bit_depth'] = bd_combo
        bd_label = make_pref_label(I18N.tr_preference("preferences.color_management.default_bit_depth",
                                            "Default Bit Depth:"))
        color_form.addRow(bd_label, bd_combo)

        # Rendering Intent
        intent_combo = ChevronComboBox()
        intent_combo.setMinimumWidth(120)
        for value, label in [
            ("perceptual", I18N.tr_preference("preferences.color_management.intent_perceptual",
                                    "Perceptual")),
            ("relative", I18N.tr_preference("preferences.color_management.intent_relative",
                                  "Relative Colorimetric")),
            ("absolute", I18N.tr_preference("preferences.color_management.intent_absolute",
                                  "Absolute Colorimetric")),
            ("saturation", I18N.tr_preference("preferences.color_management.intent_saturation",
                                    "Saturation")),
        ]:
            intent_combo.addItem(label, value)
        self._widgets['rendering_intent'] = intent_combo
        intent_label = make_pref_label(I18N.tr_preference("preferences.color_management.rendering_intent",
                                                "Rendering Intent:"))
        color_form.addRow(intent_label, intent_combo)

        # GPU color processing
        gpu_color = QCheckBox(
            I18N.tr_preference("preferences.color_management.gpu_color_processing",
                    "Use GPU for color processing")
        )
        self._widgets['gpu_color_processing'] = gpu_color
        color_form.addRow("", gpu_color)

        # Show color space warnings
        show_warnings = QCheckBox(
            I18N.tr_preference("preferences.color_management.show_warnings",
                    "Show color space warnings")
        )
        self._widgets['show_color_space_warnings'] = show_warnings
        color_form.addRow("", show_warnings)

        # Auto-assign ICC
        auto_icc = QCheckBox(
            I18N.tr_preference("preferences.color_management.auto_assign_icc",
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
            I18N.tr_preference("preferences.ocio_placeholder", "config.ocio"))
        _apply_text_input_style(ocio_edit)
        ocio_btn = QPushButton(I18N.tr_preference("preferences.export.browse", "Browse"))
        ocio_btn.setObjectName("PrefBrowseBtn")
        ocio_btn.setFixedWidth(80)
        _apply_compact_button_style(ocio_btn)
        ocio_btn.clicked.connect(
            lambda: _browse_path(ocio_edit, I18N.tr_preference("preferences.paths.select_ocio", "Select OCIO Config"), is_file=True)
        )
        ocio_layout.addWidget(ocio_edit, 1)
        ocio_layout.addWidget(ocio_btn)
        self._widgets['ocio_config_path'] = ocio_edit
        ocio_label = make_pref_label(I18N.tr_preference("preferences.color_management.ocio_config",
                                              "OCIO Config File:"))
        color_form.addRow(ocio_label, ocio_widget)

        layout.addWidget(color_group)
        layout.addStretch()
        return scroll

    def load(self, config: dict) -> None:
        if not self._widgets:
            return

        # Color Settings
        cs = config.get('default_color_space', 'sRGB')
        cs_combo = self._widgets['default_color_space']
        idx = cs_combo.findText(cs)
        if idx >= 0:
            cs_combo.setCurrentIndex(idx)

        bd = config.get('default_bit_depth', '8-bit')
        bd_combo = self._widgets['default_bit_depth']
        idx = bd_combo.findData(bd)
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
        if self._color_space_controls is not None:
            self._color_space_controls.load(None, config)
        if USE_DISPLAY_ICC in self._widgets:
            self._widgets[USE_DISPLAY_ICC].setChecked(config.get(USE_DISPLAY_ICC, False) is True)
        hdr_metadata = self._widgets.get("color.output.hdr_metadata")
        if hdr_metadata is not None:
            hdr_metadata.setChecked(config.get("color.output.hdr_metadata", True) not in (False, "off"))
        for key in _CALIBRATION_LUT_KEYS:
            if key in self._widgets:
                self._widgets[key].setText(config.get(key, "") or "")

    def apply(self, config_manager: ConfigManager) -> None:
        if not self._widgets:
            return

        if USE_DISPLAY_ICC in self._widgets:
            config_manager.set(USE_DISPLAY_ICC, self._widgets[USE_DISPLAY_ICC].isChecked(),
                               auto_save=False)
        hdr_metadata = self._widgets.get("color.output.hdr_metadata")
        if hdr_metadata is not None:
            config_manager.set("color.output.hdr_metadata", hdr_metadata.isChecked(), auto_save=False)
        for key in _CALIBRATION_LUT_KEYS:
            if key in self._widgets:
                config_manager.set(key, self._widgets[key].text(), auto_save=False)
        # Color Settings
        config_manager.set('default_color_space',
                           self._widgets['default_color_space'].currentText(),
                           auto_save=False)
        config_manager.set('default_bit_depth',
                           self._widgets['default_bit_depth'].currentData(),
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
        if self._color_space_controls is not None:
            for key, value in self._color_space_controls.settings().items():
                config_manager.set(key, value, auto_save=False)

    def restore_defaults(self, defaults: dict) -> None:
        self.load(defaults)

    def _create_monitor_calibration_group(self) -> QGroupBox:
        group = QGroupBox(I18N.tr_preference("preferences.color_management.calibration_section", "Monitor calibration"))
        group.setObjectName("PrefSettingsGroup")
        form = make_pref_form(group)
        tip = I18N.tr_preference(
            "preferences.color_management.calibration_tip",
            "Use a calibration LUT measured for this display and its output signal. The LUT applies after its display transform.",
        )
        for key, label in (
            (DESKTOP_CALIBRATION_LUT, I18N.tr_preference("preferences.color_management.desktop_calibration_lut",
                                             "Built-in monitor calibration LUT")),
            (EXTERNAL_CALIBRATION_LUT, I18N.tr_preference("preferences.color_management.external_calibration_lut",
                                              "External card monitor calibration LUT")),
        ):
            field = QWidget(group)
            row = QHBoxLayout(field)
            row.setContentsMargins(0, 0, 0, 0)
            path = QLineEdit(field)
            path.setObjectName(key)
            path.setAccessibleName(label)
            path.setReadOnly(True)
            path.setPlaceholderText(I18N.tr_preference("preferences.color_management.calibration_none", "No LUT selected"))
            path.textChanged.connect(path.setToolTip)
            _apply_text_input_style(path)
            self._widgets[key] = path
            browse = QPushButton(I18N.tr_preference("preferences.color_management.aces_lut_load", "Load…"), field)
            clear = QPushButton(I18N.tr_preference("preferences.color_management.aces_lut_clear", "Clear"), field)
            _apply_compact_button_style(browse)
            _apply_compact_button_style(clear)
            browse.clicked.connect(lambda _, editor=path: self._select_calibration_lut(editor))
            clear.clicked.connect(path.clear)
            clear.setEnabled(False)
            path.textChanged.connect(lambda text, button=clear: button.setEnabled(bool(text)))
            row.addWidget(path, 1)
            row.addWidget(browse)
            row.addWidget(clear)
            form.addRow(make_pref_label(label, tip), field)
        return group

    @staticmethod
    def _select_calibration_lut(path: QLineEdit) -> None:
        title = I18N.tr_preference("preferences.color_management.calibration_load", "Load monitor calibration LUT")
        selected, _ = QFileDialog.getOpenFileName(path, title, path.text(), "LUT (*.cube *.3dl)")
        if not selected:
            return
        future = _MONITOR_LUT_POOL.submit(load_monitor_calibration, selected)
        if not _wait_for_lut(path, title, future):
            return
        try:
            future.result()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(path, title, str(exc))
            return
        path.setText(selected)


class ControlSurfacePreferencePage:
    """Tangent hardware selection and trackball calibration."""

    page_id = "color.control_surface"

    def __init__(self) -> None:
        self._widgets: dict[str, QWidget] = {}

    def create_widget(self, parent: QWidget | None) -> QWidget:
        scroll, layout = make_pref_page(parent)

        # ── Group: Tangent control surface ──
        tangent_group = QGroupBox(
            I18N.tr_preference("preferences.tangent.section_title", "Device Connection")
        )
        tangent_group.setObjectName("PrefSettingsGroup")
        tangent_form = make_pref_form(tangent_group)

        tangent_combo = ChevronComboBox()
        tangent_combo.setMinimumWidth(180)
        tangent_combo.addItem(
            I18N.tr_preference("preferences.tangent.disabled", "Disabled"),
            CONTROL_SURFACE_DISABLED,
        )
        tangent_combo.addItem(
            I18N.tr_preference("preferences.tangent.auto", "Auto Detect"),
            CONTROL_SURFACE_AUTO,
        )
        tangent_combo.addItem(
            I18N.tr_preference("preferences.tangent.ripple", "Tangent Ripple"),
            CONTROL_SURFACE_RIPPLE,
        )
        tangent_combo.addItem(
            I18N.tr_preference("preferences.tangent.element", "Tangent Element"),
            CONTROL_SURFACE_ELEMENT,
        )
        self._widgets[CONTROL_SURFACE_CONFIG_KEY] = tangent_combo
        tangent_form.addRow(
            make_pref_label(
                I18N.tr_preference("preferences.tangent.surface_label", "Control Surface:")
            ),
            tangent_combo,
        )

        tangent_note = QLabel(
            I18N.tr_preference(
                "preferences.tangent.note",
                "Uses the native Tangent Hub connection. Element is treated as one four-panel set.",
            )
        )
        tangent_note.setObjectName("PrefNoteLabel")
        tangent_note.setWordWrap(True)
        tangent_form.addRow("", tangent_note)

        layout.addWidget(tangent_group)

        calibration_group = QGroupBox(
            I18N.tr_preference("preferences.tangent.calibration_title", "Color Wheels and Trackerballs")
        )
        calibration_group.setObjectName("PrefSettingsGroup")
        calibration_form = make_pref_form(calibration_group)

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
        calibration_form.addRow(
            make_pref_label(
                I18N.tr_preference("preferences.tangent.rotation", "Color Wheel Rotation:")
            ),
            rotation_spin,
        )

        direction_check = QCheckBox(
            I18N.tr_preference("preferences.tangent.invert_direction", "Invert hue direction")
        )
        self._widgets[HUE_DIRECTION_INVERTED_CONFIG_KEY] = direction_check
        calibration_form.addRow("", direction_check)

        smoothing_spin = QSpinBox()
        smoothing_spin.setRange(0, 100)
        smoothing_spin.setSingleStep(5)
        smoothing_spin.setSuffix(" %")
        smoothing_spin.setMinimumWidth(110)
        self._widgets[TRACKERBALL_SMOOTHING_CONFIG_KEY] = smoothing_spin
        calibration_form.addRow(
            make_pref_label(
                I18N.tr_preference(
                    "preferences.tangent.smoothing",
                    "Trackerball Smoothing:",
                )
            ),
            smoothing_spin,
        )

        wheel_labels = {
            "lift": I18N.tr_preference("preferences.tangent.lift", "Lift"),
            "gamma": I18N.tr_preference("preferences.tangent.gamma", "Gamma"),
            "gain": I18N.tr_preference("preferences.tangent.gain", "Gain"),
        }
        component_labels = {
            "hue": I18N.tr_preference("preferences.tangent.axis_x", "X Axis"),
            "saturation": I18N.tr_preference("preferences.tangent.axis_y", "Y Axis"),
            "brightness": I18N.tr_preference("preferences.tangent.brightness", "Brightness"),
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
            calibration_form.addRow(
                make_pref_label(
                    f"{wheel_labels[wheel]} {component_labels[component]}:"
                ),
                spin,
            )

        sensitivity_note = QLabel(
            I18N.tr_preference(
                "preferences.tangent.sensitivity_note",
                "X/Y UI 100% = internal 1000% base maximum (10x native Tangent increment). Slow rolls use a finer gain; fast rolls can rise smoothly to 20x; brightness keeps the native 100% scale.",
            )
        )
        sensitivity_note.setObjectName("PrefNoteLabel")
        sensitivity_note.setWordWrap(True)
        calibration_form.addRow("", sensitivity_note)
        smoothing_note = QLabel(
            I18N.tr_preference(
                "preferences.tangent.smoothing_note",
                "0% = raw response and is the default for straight diagonal movement. Higher values smooth noisy hardware but add response lag.",
            )
        )
        smoothing_note.setObjectName("PrefNoteLabel")
        smoothing_note.setWordWrap(True)
        calibration_form.addRow("", smoothing_note)
        layout.addWidget(calibration_group)
        layout.addStretch()
        return scroll

    def load(self, config: dict) -> None:
        if not self._widgets:
            return

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
            _config_int(
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

    def apply(self, config_manager: ConfigManager) -> None:
        if not self._widgets:
            return

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

    def restore_defaults(self, defaults: dict) -> None:
        self.load(defaults)

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

class ColorProcessingPreferencePage:
    """Image-processing defaults and AI image-processing settings."""

    page_id = "color.processing"

    def __init__(self) -> None:
        self._widgets: dict[str, QWidget] = {}

    def create_widget(self, parent: QWidget | None) -> QWidget:
        scroll, layout = make_pref_page(parent)

        # Default processing parameters
        proc_group = QGroupBox(
            I18N.tr_preference("preferences.image_processing.section_default_title",
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
            make_pref_label(I18N.tr_preference("preferences.image_processing.denoise_strength",
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
            make_pref_label(I18N.tr_preference("preferences.image_processing.sharpen_amount",
                                     "Default sharpening amount:")),
            sharp_widget
        )

        layout.addWidget(proc_group)

        # AI image processing
        ai_group = QGroupBox(
            I18N.tr_preference("preferences.image_processing.section_ai_title", "AI Image Processing")
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
        ai_path_edit.setPlaceholderText(I18N.tr_preference(
            "preferences.ai_model_dir_placeholder", "Select AI Model Directory"))
        _apply_text_input_style(ai_path_edit)
        ai_path_btn = QPushButton(I18N.tr_preference("preferences.export.browse", "Browse"))
        ai_path_btn.setObjectName("PrefBrowseBtn")
        ai_path_btn.setFixedWidth(80)
        _apply_compact_button_style(ai_path_btn)
        ai_path_btn.clicked.connect(
            lambda: _browse_path(ai_path_edit, I18N.tr_preference("preferences.paths.select_ai_models", "Select AI Model Directory"))
        )
        ai_path_layout.addWidget(ai_path_edit, 1)
        ai_path_layout.addWidget(ai_path_btn)
        self._widgets['ai_model_path'] = ai_path_edit
        ai_form.addRow(
            make_pref_label(I18N.tr_preference("preferences.image_processing.ai_model_path",
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
            make_pref_label(I18N.tr_preference("preferences.image_processing.ai_tile_size",
                                     "AI Tile Size:")),
            ai_tile
        )

        # AI GPU device
        ai_device = ChevronComboBox()
        ai_device.setMinimumWidth(120)
        ai_device.addItem(
            I18N.tr_preference("preferences.image_processing.ai_device_auto", "Auto"), "auto"
        )
        ai_device.addItem(
            I18N.tr_preference("preferences.image_processing.ai_device_cpu", "CPU Only"), "cpu"
        )
        ai_device.addItem(
            I18N.tr_preference("preferences.image_processing.ai_device_cuda",
                    "CUDA (NVIDIA)"), "cuda"
        )
        ai_device.addItem(
            I18N.tr_preference("preferences.image_processing.ai_device_coreml",
                    "CoreML (Apple)"), "coreml"
        )
        self._widgets['ai_gpu_device'] = ai_device
        ai_form.addRow(
            make_pref_label(I18N.tr_preference("preferences.image_processing.ai_gpu_device",
                                     "AI GPU Device:")),
            ai_device
        )

        layout.addWidget(ai_group)
        layout.addStretch()
        return scroll

    def load(self, config: dict) -> None:
        if not self._widgets:
            return

        # Image Processing
        self._widgets['default_denoise_strength'].setValue(
            _config_int(config.get('default_denoise_strength', 50), 50,
                             'default_denoise_strength')
        )
        self._widgets['default_sharpen_amount'].setValue(
            _config_int(config.get('default_sharpen_amount', 0), 0,
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
        if not self._widgets:
            return

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
        self.load(defaults)


def _config_int(value: object, fallback: int, key: str = "") -> int:
    """AUD-24612: 外部配置值防御式 int 转换（语义同 _config_float）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "Invalid numeric value %r for %s; using default %r",
            value, key or "<preference>", fallback)
        return fallback


def _browse_path(line_edit: QLineEdit, title: str, is_file: bool = False) -> None:
    """Open directory or file picker for path input."""
    current = line_edit.text()
    if is_file:
        path, _ = QFileDialog.getOpenFileName(
            line_edit.window(), title, current if current else "",
            I18N.tr_preference("preferences.paths.all_files", "All Files (*)")
        )
    else:
        path = QFileDialog.getExistingDirectory(
            line_edit.window(), title, current if current else ""
        )
    if path:
        line_edit.setText(path)
