"""Edit module's Video Playback preference page.

Self-contained preference page for edit-specific video playback settings:
- Video pipeline mode (auto/v3/v2)
- v2 fallback behavior
- Frame dropping, queue depth
- YUV GPU upload
- Scene Cut Detection defaults (P9)

This page is only shown when the Edit feature is enabled.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QCheckBox, QLineEdit,
    QScrollArea, QGroupBox,
)
from PyQt6.QtCore import Qt

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets import ChevronComboBox
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.widgets.pref_drag_value import PrefDragValue
from src.shared.ui.widgets.pref_form_layout import make_pref_form, make_pref_label

logger = logging.getLogger(__name__)

# 数值键的合法范围（与控件构造参数保持一致；外部 config 值在此范围内归一）。
_SCENE_CUT_SENSITIVITY_RANGE = (0.35, 0.75)
_SCENE_CUT_MIN_SCENE_FRAMES_RANGE = (2, 120)
_VIDEO_PREVIEW_MAX_QUEUE_RANGE = (2, 32)


def _safe_float(value: Any, default: float, low: float, high: float, key: str) -> float:
    """外部 config 浮点键的类型/范围归一（AUD-19305）。

    非法类型或越界时回退/收敛并记 warning，保证 load() 不会在半途抛异常
    （否则宿主 ``except Exception: pass`` 会把后半段控件留在创建时默认值，
    Apply 时被整体写回，静默覆盖用户设置）。
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        logger.warning("config key %r is not a float (%r); using %s", key, value, default)
        return float(default)
    if not (low <= result <= high):
        clamped = min(max(result, low), high)
        logger.warning(
            "config key %r out of range [%s, %s]: %s -> %s",
            key, low, high, result, clamped,
        )
        return clamped
    return result


def _safe_int(value: Any, default: int, low: int, high: int, key: str) -> int:
    """外部 config 整数键的类型/范围归一（AUD-19305，同 _safe_float）。"""
    try:
        result = int(value)
    except (TypeError, ValueError):
        logger.warning("config key %r is not an int (%r); using %s", key, value, default)
        return int(default)
    if not (low <= result <= high):
        clamped = min(max(result, low), high)
        logger.warning(
            "config key %r out of range [%s, %s]: %s -> %s",
            key, low, high, result, clamped,
        )
        return clamped
    return result


def _safe_bool(value: Any, default: bool, key: str) -> bool:
    """外部 config 布尔键归一：仅接受真布尔，其余按默认值处理并记 warning。"""
    if isinstance(value, bool):
        return value
    logger.warning("config key %r is not a bool (%r); using %s", key, value, default)
    return default


class EditPreferencePage:
    """Edit module's Video Playback preference page.

    Manages widgets: video_pipeline_mode, video_pipeline_allow_v2_fallback,
    video_preview_drop_frames, video_preview_max_queue, video_use_yuv_gpu_upload,
    scene_cut_sensitivity, scene_cut_use_proxy, scene_cut_min_scene_frames.

    This page is shown as part of the Performance page (as a Video Playback
    group) or as a standalone page depending on the configuration.
    """

    page_id = "edit.video_playback"

    def __init__(self):
        self._widgets: dict[str, QWidget] = {}

    # ── Widget Creation ─────────────────────────────────────────────

    def create_widget(self, parent: QWidget) -> QWidget:
        """Create the Video Playback preference page UI."""
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

        # Video Playback group
        video_group = QGroupBox(
            I18N.tr("preferences.video_playback.section_title", "Video Playback")
        )
        video_group.setObjectName("PrefSettingsGroup")
        video_form = make_pref_form(video_group)

        # Video Playback Engine - single dropdown replacing dual switches
        playback_engine_combo = ChevronComboBox()
        playback_engine_combo.setMinimumWidth(180)
        playback_engine_combo.addItem(
            I18N.tr("preferences.video_playback.engine_auto",
                    "Auto (Recommended)"),
            "auto"
        )
        playback_engine_combo.addItem(
            I18N.tr("preferences.video_playback.engine_v3",
                    "v3 Realtime Engine"),
            "v3"
        )
        playback_engine_combo.addItem(
            I18N.tr("preferences.video_playback.engine_v2",
                    "v2 Compatibility"),
            "v2"
        )
        self._widgets['video_pipeline_mode'] = playback_engine_combo
        engine_label = make_pref_label(
            I18N.tr("preferences.video_playback.engine",
                    "Video Playback Engine:")
        )
        video_form.addRow(engine_label, playback_engine_combo)

        # Allow v2 fallback
        allow_fallback = QCheckBox(
            I18N.tr("preferences.video_playback.allow_fallback",
                    "Allow automatic fallback to v2 if v3 fails")
        )
        self._widgets['video_pipeline_allow_v2_fallback'] = allow_fallback
        video_form.addRow("", allow_fallback)

        # Engine description
        engine_desc = QLabel(
            I18N.tr("preferences.video_playback.engine_desc",
                     "Auto: Prioritizes v3 realtime engine, falls back to v2 on failure. "
                     "v3: Forces realtime engine, no fallback. "
                     "v2: Uses legacy compatibility path for debugging.")
        )
        engine_desc.setObjectName("PrefNoteLabel")
        engine_desc.setWordWrap(True)
        video_form.addRow("", engine_desc)

        # 预览关滤波（P6 v2.1 后续，2026-09-22）：跳过解码端输出去块
        no_deblock = QCheckBox(
            I18N.tr("preferences.video_pipeline.no_deblock",
                    "Disable deblock filtering in preview (faster, "
                    "may show 8x8 blocks on low tiers)")
        )
        no_deblock.setToolTip(I18N.tr(
            "preferences.video_pipeline.no_deblock_tooltip",
            "Skips the decoder-side deblock filter for timeline preview. "
            "Decoding gets 1.2-2x faster on low tiers but 8x8 block edges "
            "become visible again. Export and delivery always keep the filter.",
        ))
        self._widgets['video_preview_no_deblock'] = no_deblock
        video_form.addRow("", no_deblock)

        # Drop Frames
        drop_frames = QCheckBox(
            I18N.tr("preferences.video_pipeline.drop_frames",
                    "Drop frames when preview queue is full")
        )
        self._widgets['video_preview_drop_frames'] = drop_frames
        video_form.addRow("", drop_frames)

        # Max Queue Size
        queue_spin = PrefDragValue(min_v=2, max_v=32, default=4)
        queue_spin.setSingleStep(2)
        self._widgets['video_preview_max_queue'] = queue_spin
        queue_label = make_pref_label(
            I18N.tr("preferences.video_pipeline.max_queue",
                    "Preview queue depth:")
        )
        video_form.addRow(queue_label, queue_spin)

        # YUV GPU Upload
        yuv_upload = QCheckBox(
            I18N.tr("preferences.video_pipeline.yuv_gpu_upload",
                    "Use GPU for YUV color conversion (recommended)")
        )
        self._widgets['video_use_yuv_gpu_upload'] = yuv_upload
        video_form.addRow("", yuv_upload)

        layout.addWidget(video_group)

        # External monitoring group
        monitor_group = QGroupBox(
            I18N.tr("preferences.decklink.section_title", "External Video Monitoring")
        )
        monitor_group.setObjectName("PrefSettingsGroup")
        monitor_form = make_pref_form(monitor_group)

        decklink_enabled = QCheckBox(
            I18N.tr(
                "preferences.decklink.enabled",
                "Enable Blackmagic DeckLink program monitor output",
            )
        )
        self._widgets['decklink_monitor_enabled'] = decklink_enabled
        monitor_form.addRow("", decklink_enabled)

        decklink_backend = ChevronComboBox()
        decklink_backend.addItem("Native SDK bridge", "native_sdk")
        decklink_backend.addItem("ffmpeg DeckLink", "ffmpeg")
        self._widgets['decklink_monitor_backend'] = decklink_backend
        monitor_form.addRow(
            make_pref_label(I18N.tr("preferences.decklink.backend", "Output backend:")),
            decklink_backend,
        )

        decklink_device = QLineEdit()
        decklink_device.setPlaceholderText(I18N.tr(
            "preferences.decklink.auto_device_placeholder",
            "Auto-select when only one DeckLink device is installed"))
        self._widgets['decklink_monitor_device'] = decklink_device
        monitor_form.addRow(
            make_pref_label(I18N.tr("preferences.decklink.device", "DeckLink device:")),
            decklink_device,
        )

        decklink_ffmpeg = QLineEdit()
        decklink_ffmpeg.setPlaceholderText(I18N.tr(
            "preferences.decklink.ffmpeg_path_placeholder",
            "/usr/local/bin/ffmpeg"))
        self._widgets['decklink_monitor_ffmpeg_path'] = decklink_ffmpeg
        monitor_form.addRow(
            make_pref_label(I18N.tr("preferences.decklink.ffmpeg_path", "ffmpeg path:")),
            decklink_ffmpeg,
        )

        decklink_desc = QLabel(
            I18N.tr(
                "preferences.decklink.desc",
                "First-stage monitoring outputs fixed 1080i50 YUV video. The ffmpeg "
                "backend requires an ffmpeg build compiled with DeckLink support; "
                "standard Homebrew ffmpeg often does not include it. The Native SDK "
                "backend uses the bundled topos_decklink bridge module and can "
                "auto-select a single installed DeckLink device.",
            )
        )
        decklink_desc.setObjectName("PrefNoteLabel")
        decklink_desc.setWordWrap(True)
        monitor_form.addRow("", decklink_desc)

        layout.addWidget(monitor_group)

        # P9-6: Scene Cut Detection group
        scene_cut_group = QGroupBox(
            I18N.tr("preferences.scene_cut.section_title", "Scene Cut Detection")
        )
        scene_cut_group.setObjectName("PrefSettingsGroup")
        scene_cut_form = make_pref_form(scene_cut_group)

        # Sensitivity slider (0.35 - 0.75, default 0.55)
        sens_widget = QWidget()
        sens_widget.setStyleSheet("background: transparent;")
        sens_layout = QHBoxLayout(sens_widget)
        sens_layout.setContentsMargins(0, 0, 0, 0)
        sens_layout.setSpacing(8)
        sensitivity_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        sensitivity_slider.setRange(35, 75)  # Scale 0.35-0.75 to 35-75
        sensitivity_slider.setValue(55)
        sensitivity_slider.setMinimumWidth(180)
        sensitivity_slider.setObjectName("PrefSlider")
        sens_layout.addWidget(sensitivity_slider, 1)
        self._widgets['scene_cut_sensitivity'] = sensitivity_slider

        # Label for sensitivity
        sensitivity_label = make_pref_label(
            I18N.tr("preferences.scene_cut.sensitivity",
                    "Default sensitivity:")
        )
        scene_cut_form.addRow(sensitivity_label, sens_widget)

        # Sensitivity description
        sensitivity_desc = QLabel(
            I18N.tr("preferences.scene_cut.sensitivity_desc",
                    "Lower values = more sensitive (more candidates). "
                    "Higher values = conservative (fewer false positives).")
        )
        sensitivity_desc.setObjectName("PrefNoteLabel")
        sensitivity_desc.setWordWrap(True)
        scene_cut_form.addRow("", sensitivity_desc)

        # Use proxy for detection
        use_proxy = QCheckBox(
            I18N.tr("preferences.scene_cut.use_proxy",
                    "Use proxy media for detection (faster)")
        )
        use_proxy.setChecked(True)
        self._widgets['scene_cut_use_proxy'] = use_proxy
        scene_cut_form.addRow("", use_proxy)

        # Min scene frames
        min_frames_spin = PrefDragValue(min_v=2, max_v=120, default=8)
        self._widgets['scene_cut_min_scene_frames'] = min_frames_spin
        min_frames_label = make_pref_label(
            I18N.tr("preferences.scene_cut.min_scene_frames",
                    "Minimum frames between cuts:")
        )
        scene_cut_form.addRow(min_frames_label, min_frames_spin)

        layout.addWidget(scene_cut_group)

        layout.addStretch()
        return scroll

    # ── Load / Apply / Defaults ──────────────────────────────────────

    def load(self, config: dict) -> None:
        """Load video playback settings from config into widgets."""
        if 'video_pipeline_mode' not in self._widgets:
            return

        pipeline_mode = config.get('video_pipeline_mode', 'auto')
        mode_combo = self._widgets['video_pipeline_mode']
        for i in range(mode_combo.count()):
            if mode_combo.itemData(i) == pipeline_mode:
                mode_combo.setCurrentIndex(i)
                break

        self._widgets['video_pipeline_allow_v2_fallback'].setChecked(
            _safe_bool(config.get('video_pipeline_allow_v2_fallback'), True,
                       'video_pipeline_allow_v2_fallback')
        )
        self._widgets['video_preview_drop_frames'].setChecked(
            _safe_bool(config.get('video_preview_drop_frames'), True,
                       'video_preview_drop_frames')
        )
        self._widgets['video_preview_max_queue'].setValue(
            _safe_int(config.get('video_preview_max_queue'), 3,
                      *_VIDEO_PREVIEW_MAX_QUEUE_RANGE, key='video_preview_max_queue')
        )
        self._widgets['video_preview_no_deblock'].setChecked(
            config.get('video_preview_no_deblock', False)
        )
        self._widgets['video_use_yuv_gpu_upload'].setChecked(
            _safe_bool(config.get('video_use_yuv_gpu_upload'), True,
                       'video_use_yuv_gpu_upload')
        )
        self._widgets['decklink_monitor_enabled'].setChecked(
            _safe_bool(config.get('decklink_monitor_enabled'), False,
                       'decklink_monitor_enabled')
        )
        backend = config.get('decklink_monitor_backend', 'native_sdk')
        backend_combo = self._widgets['decklink_monitor_backend']
        for i in range(backend_combo.count()):
            if backend_combo.itemData(i) == backend:
                backend_combo.setCurrentIndex(i)
                break
        self._widgets['decklink_monitor_device'].setText(
            str(config.get('decklink_monitor_device', '') or '')
        )
        self._widgets['decklink_monitor_ffmpeg_path'].setText(
            str(config.get('decklink_monitor_ffmpeg_path', 'ffmpeg') or 'ffmpeg')
        )

        # P9-6: Load scene cut settings
        sensitivity = _safe_float(
            config.get('scene_cut_sensitivity'), 0.55,
            *_SCENE_CUT_SENSITIVITY_RANGE, key='scene_cut_sensitivity',
        )
        # Scale 0.35-0.75 to 35-75
        sensitivity_slider_value = int(sensitivity * 100)
        self._widgets['scene_cut_sensitivity'].setValue(sensitivity_slider_value)

        self._widgets['scene_cut_use_proxy'].setChecked(
            _safe_bool(config.get('scene_cut_use_proxy'), True,
                       'scene_cut_use_proxy')
        )
        self._widgets['scene_cut_min_scene_frames'].setValue(
            _safe_int(config.get('scene_cut_min_scene_frames'), 8,
                      *_SCENE_CUT_MIN_SCENE_FRAMES_RANGE,
                      key='scene_cut_min_scene_frames')
        )

    def apply(self, config_manager: Any) -> None:
        """Read widget values and write to config manager."""
        if 'video_pipeline_mode' not in self._widgets:
            return

        config_manager.set('video_pipeline_mode',
                           self._widgets['video_pipeline_mode'].currentData(),
                           auto_save=False)
        config_manager.set('video_pipeline_allow_v2_fallback',
                           self._widgets['video_pipeline_allow_v2_fallback'].isChecked(),
                           auto_save=False)
        config_manager.set('video_preview_drop_frames',
                           self._widgets['video_preview_drop_frames'].isChecked(),
                           auto_save=False)
        config_manager.set('video_preview_max_queue',
                           self._widgets['video_preview_max_queue'].intValue(),
                           auto_save=False)
        config_manager.set('video_preview_no_deblock',
                           self._widgets['video_preview_no_deblock'].isChecked(),
                           auto_save=False)
        config_manager.set('video_use_yuv_gpu_upload',
                           self._widgets['video_use_yuv_gpu_upload'].isChecked(),
                           auto_save=False)
        config_manager.set('decklink_monitor_enabled',
                           self._widgets['decklink_monitor_enabled'].isChecked(),
                           auto_save=False)
        config_manager.set('decklink_monitor_backend',
                           self._widgets['decklink_monitor_backend'].currentData(),
                           auto_save=False)
        config_manager.set('decklink_monitor_device',
                           self._widgets['decklink_monitor_device'].text().strip(),
                           auto_save=False)
        config_manager.set('decklink_monitor_ffmpeg_path',
                           self._widgets['decklink_monitor_ffmpeg_path'].text().strip() or 'ffmpeg',
                           auto_save=False)
        config_manager.set('decklink_monitor_width', 1920, auto_save=False)
        config_manager.set('decklink_monitor_height', 1080, auto_save=False)
        config_manager.set('decklink_monitor_fps', 25.0, auto_save=False)
        config_manager.set('decklink_monitor_mode', '1080i50', auto_save=False)
        config_manager.set('decklink_monitor_format_code', 'Hi50', auto_save=False)
        config_manager.set('decklink_monitor_connection', 'hdmi', auto_save=False)
        config_manager.set('decklink_monitor_native_pixel_format', 'yuv', auto_save=False)

        # P9-6: Apply scene cut settings
        sensitivity_slider_value = self._widgets['scene_cut_sensitivity'].value()
        sensitivity = sensitivity_slider_value / 100.0
        config_manager.set('scene_cut_sensitivity', sensitivity, auto_save=False)
        config_manager.set('scene_cut_use_proxy',
                           self._widgets['scene_cut_use_proxy'].isChecked(),
                           auto_save=False)
        config_manager.set('scene_cut_min_scene_frames',
                           self._widgets['scene_cut_min_scene_frames'].intValue(),
                           auto_save=False)

    def restore_defaults(self, defaults: dict) -> None:
        """Reset video playback widgets to default values."""
        if 'video_pipeline_mode' not in self._widgets:
            return

        pipeline_mode = defaults.get('video_pipeline_mode', 'auto')
        mode_combo = self._widgets['video_pipeline_mode']
        for i in range(mode_combo.count()):
            if mode_combo.itemData(i) == pipeline_mode:
                mode_combo.setCurrentIndex(i)
                break

        self._widgets['video_pipeline_allow_v2_fallback'].setChecked(
            _safe_bool(defaults.get('video_pipeline_allow_v2_fallback'), True,
                       'video_pipeline_allow_v2_fallback')
        )
        self._widgets['video_preview_drop_frames'].setChecked(
            _safe_bool(defaults.get('video_preview_drop_frames'), True,
                       'video_preview_drop_frames')
        )
        self._widgets['video_preview_max_queue'].setValue(
            _safe_int(defaults.get('video_preview_max_queue'), 3,
                      *_VIDEO_PREVIEW_MAX_QUEUE_RANGE, key='video_preview_max_queue')
        )
        self._widgets['video_preview_no_deblock'].setChecked(
            defaults.get('video_preview_no_deblock', False)
        )
        self._widgets['video_use_yuv_gpu_upload'].setChecked(
            _safe_bool(defaults.get('video_use_yuv_gpu_upload'), True,
                       'video_use_yuv_gpu_upload')
        )
        self._widgets['decklink_monitor_enabled'].setChecked(
            _safe_bool(defaults.get('decklink_monitor_enabled'), False,
                       'decklink_monitor_enabled')
        )
        backend = defaults.get('decklink_monitor_backend', 'native_sdk')
        backend_combo = self._widgets['decklink_monitor_backend']
        for i in range(backend_combo.count()):
            if backend_combo.itemData(i) == backend:
                backend_combo.setCurrentIndex(i)
                break
        self._widgets['decklink_monitor_device'].setText(
            str(defaults.get('decklink_monitor_device', '') or '')
        )
        self._widgets['decklink_monitor_ffmpeg_path'].setText(
            str(defaults.get('decklink_monitor_ffmpeg_path', 'ffmpeg') or 'ffmpeg')
        )

        # P9-6: Restore scene cut defaults
        sensitivity = _safe_float(
            defaults.get('scene_cut_sensitivity'), 0.55,
            *_SCENE_CUT_SENSITIVITY_RANGE, key='scene_cut_sensitivity',
        )
        sensitivity_slider_value = int(sensitivity * 100)
        self._widgets['scene_cut_sensitivity'].setValue(sensitivity_slider_value)

        self._widgets['scene_cut_use_proxy'].setChecked(
            _safe_bool(defaults.get('scene_cut_use_proxy'), True,
                       'scene_cut_use_proxy')
        )
        self._widgets['scene_cut_min_scene_frames'].setValue(
            _safe_int(defaults.get('scene_cut_min_scene_frames'), 8,
                      *_SCENE_CUT_MIN_SCENE_FRAMES_RANGE,
                      key='scene_cut_min_scene_frames')
        )


def get_scene_cut_settings_from_config(config: dict) -> dict:
    """
    Extract scene cut settings from config dictionary.

    Args:
        config: Configuration dictionary.

    Returns:
        Dictionary with scene cut settings suitable for SceneCutSettings.
    """
    return {
        'sensitivity': _safe_float(
            config.get('scene_cut_sensitivity'), 0.55,
            *_SCENE_CUT_SENSITIVITY_RANGE, key='scene_cut_sensitivity',
        ),
        'use_proxy': _safe_bool(
            config.get('scene_cut_use_proxy'), True, 'scene_cut_use_proxy'
        ),
        'min_scene_frames': _safe_int(
            config.get('scene_cut_min_scene_frames'), 8,
            *_SCENE_CUT_MIN_SCENE_FRAMES_RANGE, key='scene_cut_min_scene_frames',
        ),
    }
