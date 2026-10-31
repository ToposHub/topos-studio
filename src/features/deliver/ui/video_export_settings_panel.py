"""Video export settings panel — the "compose a task" area above the queue.

This panel is the upper half of the Deliver workspace. It owns the live
draft of a video task (timeline / range / codec / audio / output) and
exposes two commands the workspace wires to the controller:

* :meth:`build_jobs` — read the draft, optionally expand against the
  checked preset cards, run preflight and return one or more ready
  :class:`VideoExportJob` instances (or raise with a structured preflight
  result).
* :meth:`load_job` — load an existing queued job back into the draft for
  editing.

Layout:

    ┌──────────────────────────────────────────────────────────────┐
    │ Presets                                                       │
    ├─ File name / Location                                        │
    ├─ Video / Audio / File                                        │
    ├──────────────────────────────────────────────────────────────┤
    │ preflight message                         [Add] [Add & Start] │
    └──────────────────────────────────────────────────────────────┘

The settings are deliberately stacked like the Deliver sidebar in DaVinci
Resolve.  The sections use the same collapsible export-section shell as the
existing image batch / Quick Export panels, while the action bar stays pinned
below the scroll area so it is never hidden inside an advanced section.

The panel itself performs **no** export work and holds no queue reference
— it is a pure draft editor. All side effects go through signals so the
controller stays testable.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

from PyQt6.QtCore import QStandardPaths, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from src.features.deliver.core.video_export_job import (
    AudioConfig,
    FilenameUse,
    NamingConfig,
    OverwritePolicy,
    RangeType,
    RenderMode,
    StemTrackConfig,
    SubtitleConfig,
    SubtitleMode,
    VideoConfig,
    VideoExportJob,
)
from src.features.deliver.core.video_export_preflight import (
    PreflightContext,
    PreflightResult,
    run_video_export_preflight,
)
from src.features.deliver.core.video_export_formats import (
    CONTAINER_ORDER,
    allowed_codecs_for_container,
    allowed_containers_for_codec,
)
from src.features.deliver.core.video_export_presets import (
    DEFAULT_VIDEO_PRESETS,
    VideoPreset,
    build_job_from_preset,
    expand_filename_template,
    _container_extension,
)
from src.shared.i18n.i18n import I18N
from src.shared.export.export_panel import ExportSectionWidget
from src.shared.assets.icon_manager import make_link_button
from src.shared.ui.style_sheet import C, apply_combo_dropdown_style
from src.shared.ui.widgets import ChevronComboBox, DragValue

logger = logging.getLogger(__name__)

__all__ = ["VideoExportSettingsPanel"]


# ── Codec catalogue (drives the codec combo + dynamic visibility) ──
#
# Each entry: (codec_value, display_label, default_container, supports_crf,
#              supports_alpha, supports_profile_profiles_list)
# The list is intentionally short and aligned with the presets; the dynamic
# UI rules from the plan are derived from these flags.

_CODEC_CATALOGUE: List[dict] = [
    {"codec": "h264", "label": "H.264", "container": "mp4", "crf": True, "alpha": False,
     "profiles": ["auto", "baseline", "main", "high"]},
    {"codec": "hevc", "label": "HEVC (H.265)", "container": "mp4", "crf": True, "alpha": False,
     "profiles": ["auto", "main", "main10"]},
    {"codec": "gif", "label": "Animated GIF", "container": "gif", "crf": False, "alpha": False,
     "profiles": []},
    {"codec": "prores_422", "label": "ProRes 422", "container": "mov", "crf": False, "alpha": False, "profiles": []},
    {"codec": "prores_422hq", "label": "ProRes 422 HQ", "container": "mov", "crf": False, "alpha": False, "profiles": []},
    {"codec": "prores_4444", "label": "ProRes 4444", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    {"codec": "prores_4444xq", "label": "ProRes 4444 XQ", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    {"codec": "dnxhr_sq", "label": "DNxHR SQ", "container": "mov", "crf": False, "alpha": False, "profiles": []},
    {"codec": "dnxhr_hq", "label": "DNxHR HQ", "container": "mov", "crf": False, "alpha": False, "profiles": []},
    # Topos Video Codec V1 Preview（自研中间片）：六档——Proxy/LT/Standard/HQ
    # 为 profile 3 质量预设（ADR-C011 C-91），4444/4444XQ 为独立帧头（ADR-C023 原名 Pro444/Extreme）
    # profile（ADR-C023，矩阵统一 id=1）；均可选 Alpha（计划 §2.2）；
    # quality 控件关闭（预设自带目标码率；QP 覆盖留待后续 UI）。
    # v1.1 音频轨（container_spec_v1.1）：topos 档支持音频
    # （aac/pcm_s16/pcm_s24 + 2.0/5.1/7.1，见 Audio 节的 topos 门控）；
    # 音频编解码不受选时 preflight 仍提示（video_export_preflight._check_topos_video_only）
    {"codec": "topos_proxy", "label": "Topos 422 Proxy", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    {"codec": "topos_lt", "label": "Topos 422 LT", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    {"codec": "topos", "label": "Topos 422", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    {"codec": "topos_hq", "label": "Topos 422 HQ", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    # R4.6：独立 bitstream profile（帧头 profile 5/6；4:4:4 档）
    {"codec": "topos_4444", "label": "Topos 4444", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    {"codec": "topos_4444xq", "label": "Topos 4444 XQ", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    # v1.8（ADR-C056）：唯一帧间档（微 GOP IP-2）。无 Alpha（§3.7 收缩，
    # alpha=False 隐藏控件）；crf=False = 固定锚 qp（72，ADR-C050）。
    {"codec": "topos_lp", "label": "Topos 422 LP", "container": "mov", "crf": False, "alpha": False, "profiles": []},
    # v1.9（P2 重标定）：旧 Proxy 低码率档独立保留——低带宽远程预览/
    # 网络流转码（非 ProRes 对标档；体积 ≈ ProRes 同名档 42–48%）
    {"codec": "topos_netproxy", "label": "Topos 422 Net Proxy", "container": "mov", "crf": False, "alpha": True, "profiles": []},
    # M4-R8（ADR-C058）：视频 RAW——CFA 相位平面 RAW→RAW 直出（帧内全 I，
    # 无 Alpha；crf=False = 比率档锚定，位深含在比率档 id 里）。profiles
    # 占位非空 → 档位下拉显示 RAW 位深×比率 12 档（单一注册表取名）。
    {"codec": "topos_raw", "label": "Topos RAW", "container": "mov", "crf": False, "alpha": False, "profiles": ["@raw_tiers"]},
    # E20：PNG/TIFF 序列暂无编码器实现（timeline_export 配置层显式拒绝，
    # 唯一序列格式是 topos_image_sequence）。标签就地披露，避免选中后
    # 才在导出期报错。
    {"codec": "png_sequence", "label": "PNG Sequence", "container": "", "crf": False, "alpha": True, "profiles": []},
    {"codec": "tiff_sequence", "label": "TIFF Sequence", "container": "", "crf": False, "alpha": True, "profiles": []},
    # M2-F8：profiles 占位非空 → 显示档位下拉；内容由 _topos_image_tier_choices()
    # 从单一注册表填充（图片整数/浮点档；RAW 随 M3-R3 点亮）。
    {"codec": "topos_image_sequence", "label": "Topos Image Sequence", "container": "", "crf": False, "alpha": True, "profiles": ["@image_tiers"]},
]

_CONTAINER_LABELS = {
    "mp4": "MP4",
    "mov": "QuickTime",
    "mkv": "Matroska",
    "mxf": "MXF",
    "avi": "AVI",
    "webm": "WebM",
    "gif": "GIF",
}

#: AUD-12405: domain errors draft/job construction can raise for an
#: invalid draft (value coercions, template/dict lookups, resolution
#: math). Both the live preflight preview and the Add slots catch exactly
#: this set: the preview surfaces the message in the status line instead
#: of silently clearing it, and Add shows a dialog instead of letting the
#: exception escape the Qt slot (PyQt6 aborts the process on unhandled
#: slot exceptions). Deliberately explicit — no broad ``except``.
_DRAFT_BUILD_ERRORS: Tuple[type, ...] = (
    ValueError,
    TypeError,
    KeyError,
    ArithmeticError,
)


def confirm_lossy_color_delivery(parent, result: Any) -> bool:
    """P1-COLOR-25：队列路径同样要有损导出确认门槛。

    直接导出走 ``DeliveryMatrixDialog.confirm``；入队路径此前只在
    摘要行显示 ⚠ 警告，任务仍直接入队开始有损导出，未闭环
    “用户确认后才允许有损转换”。这里基于 preflight 已产出的
    color_delivery 警告做一次性确认；无有损警告时直接放行。
    """
    if result is None:
        return True
    lossy = [
        issue for issue in result.warnings
        if getattr(issue, "field", "") == "color_delivery"
    ]
    if not lossy:
        return True
    from PyQt6.QtWidgets import QMessageBox
    from src.shared.i18n.i18n import I18N
    shown = lossy[:8]
    lines = "\n".join(f"• {issue.message}" for issue in shown)
    more = "" if len(lossy) <= 8 else f"\n… +{len(lossy) - 8}"
    button = QMessageBox.question(
        parent,
        I18N.tr("export.lossy_delivery_title", "有损导出确认"),
        I18N.tr(
            "export.lossy_delivery_queue_prompt",
            "以下调色操作不会包含在导出的视频中：\n{items}{more}\n\n仍要加入导出队列吗？",
            items=lines, more=more),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return button == QMessageBox.StandardButton.Yes


# R7：deliver codec id → Topos tier id（档位格式域/策略可见性判定）
_TOPOS_CODEC_TIERS = {
    "topos_proxy": "proxy",
    "topos_lt": "lt",
    "topos": "standard",
    "topos_hq": "hq",
    "topos_4444": "4444",
    "topos_4444xq": "4444xq",
    "topos_lp": "lp",   # v1.8（ADR-C056；帧间档，pix_fmt 域 yuv422p10 唯一）
    "topos_netproxy": "netproxy",   # v1.9（P2；网络代理档，yuv422p10 唯一）
    "topos_raw": "raw",   # M4-R8（ADR-C058；RAW 单 tier，pix_fmt 域 topos_cfa*）
}


def _topos_tier_for_codec(codec: object) -> Optional[str]:
    return _TOPOS_CODEC_TIERS.get(str(codec or ""))


def _topos_image_tier_choices() -> List[Tuple[str, str]]:
    """图片档位下拉选择域（label, tier_id）；单一注册表取名（E3）。

    M3-R3：整数 422/444 + 浮点 + RAW 十二档全量（raw 档要求 CFA 源
    直出——packed 合成输入在编码器侧显式拒绝）。
    """
    from src.shared.codec.topos_profiles import available_topos_image_tiers

    return [(t.label, t.tier_id) for t in available_topos_image_tiers()
            if t.family in ("int422", "int444", "float", "raw")]


def _default_topos_image_tier_id() -> str:
    from src.shared.codec.topos_profiles import default_topos_image_tier_id

    return default_topos_image_tier_id("int422")


def _topos_raw_tier_choices() -> List[Tuple[str, str]]:
    """视频 RAW 比率档下拉选择域（label, tier_id）；单一注册表取名（E3）。

    M4-R8：视频 RAW 与图片线共享 TRAW 十二档（位深×比率，raw12-*/raw16-*），
    tier id 同时是 ``codec_profile`` 载体（pix_fmt/qp 由注册表权威换算）。
    """
    from src.shared.codec.topos_profiles import TOPOS_IMAGE_TIERS

    return [(t.label, t.tier_id) for t in TOPOS_IMAGE_TIERS.values()
            if t.family == "raw"]


def _default_topos_raw_tier_id() -> str:
    from src.shared.codec.topos_profiles import default_topos_image_tier_id

    return default_topos_image_tier_id("raw")


def _pix_fmt_display_name(name: str) -> str:
    """pix_fmt → UI 显示名（'yuv444p12le' → 'YUV 4:4:4 12-bit'）。"""
    sub = "4:2:2" if "422" in name else ("4:4:4" if "444" in name else "4:4:4")
    model = "GBR" if name.startswith(("gbr", "topos_gbr")) else "YUV"
    if "p16" in name or "ap16" in name:
        depth = "16"   # 批 4：16-bit planar（视频/图片 16-bit 档）
    elif "p12" in name or "ap12" in name:
        depth = "12"
    else:
        depth = "10"
    label = f"{model} {sub} {depth}-bit"
    if "a8" in name:
        label += " · Alpha 8"
    elif "a10" in name:
        label += " · Alpha 10"
    elif "a12" in name:
        label += " · Alpha 12"
    elif "a16" in name:
        label += " · Alpha 16"
    return label


class VideoExportSettingsPanel(QWidget):
    """Upper Deliver panel: compose one or more video tasks.

    Signals:
        add_to_queue_requested(list): the user clicked "Add to queue". Carries
            a list of :class:`VideoExportJob` ready to enqueue (already
            preflighted). Empty list means preflight blocked.
        add_and_start_requested(list): same payload, but also start the queue
            immediately after enqueueing.
        preflight_updated(object): the live :class:`PreflightResult` for the
            current draft. Emitted whenever the draft changes.
        range_type_changed(str): the selected output range mode.
    """

    add_to_queue_requested = pyqtSignal(list)
    add_and_start_requested = pyqtSignal(list)
    preflight_updated = pyqtSignal(object)
    range_type_changed = pyqtSignal(str)

    DEFAULT_FILENAME_TEMPLATE = "{timeline}"
    DEFAULT_BITRATE_KBPS = 12000.0
    MAX_BITRATE_KBPS = 100_000_000.0
    DEFAULT_CRF = 20.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("VideoDeliverSettingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # Live context (set by the workspace / controller)
        self._project: Any = None
        self._sequence: Any = None
        self._project_id: str = ""
        self._project_revision: int = 0
        self._project_name: str = ""
        self._filename_follows_timeline: bool = True
        self._updating_default_fields: bool = False
        self._syncing_naming_fields: bool = False
        self._aspect_locked: bool = False
        self._aspect_ratio: float = 1920.0 / 1080.0
        self._syncing_resolution_values: bool = False
        self._quality_values: dict[str, float] = {
            "vbr": self.DEFAULT_BITRATE_KBPS,
            "cbr": self.DEFAULT_BITRATE_KBPS,
            "crf": self.DEFAULT_CRF,
        }
        self._last_quality_mode: str = "vbr"

        # Selected preset ids (multi-select for "one timeline → many videos")
        self._selected_preset_ids: List[str] = []
        # When editing an existing job, its id (None = composing new)
        self._editing_job_id: Optional[str] = None

        # Preflight lock to avoid signal recursion while programmatically
        # updating the UI.
        self._building_ui: bool = False
        self._syncing_format_options: bool = False

        self._setup_ui()
        self._setup_connections()
        self._apply_panel_style()
        self._apply_draft_to_preview()
        self._refresh_preflight()

    # =========================================================================
    # UI construction
    # =========================================================================

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # Resolve-style preset strip is the visual header of this inspector.
        outer.addWidget(self._build_preset_bar())
        # Resolve keeps the output identity immediately below the preset
        # strip.  It must not be hidden in the File tab because it is the
        # first thing users need to confirm before adding a render job.
        outer.addWidget(self._build_output_header())
        outer.addWidget(self._build_render_mode_selector())
        # Timeline and range are rendered by the live Deliver timeline panel.
        # Keep the real controls here as the draft owner, then reparent this
        # compact bar into TimelinePanel only while Deliver is active.
        self._deliver_timeline_controls = self._build_timeline_range_controls()
        self._deliver_timeline_controls.hide()

        # Stack the sections vertically inside the Deliver dock.  The old
        # three-column implementation required ~950 px and consequently
        # showed only a narrow slice when the Deliver dock was placed on the
        # right side of the main window.  A vertical settings scroll matches
        # the existing Quick Export / image batch panels and keeps every
        # video control reachable at normal dock widths.
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        settings_scroll.setObjectName("VideoSettingsScroll")

        sections_host = QWidget()
        sections_layout = QVBoxLayout(sections_host)
        sections_layout.setContentsMargins(0, 0, 4, 0)
        sections_layout.setSpacing(10)
        # Resolve-style mode tabs: video and audio/delivery are two related
        # inspectors, not two long sections competing for vertical space.
        self._mode_tabs = QTabBar()
        self._mode_tabs.setObjectName("DeliverModeTabs")
        self._mode_tabs.setExpanding(False)
        self._mode_tabs.setDrawBase(False)
        self._mode_tabs.addTab(I18N.tr("deliver.video.video_settings", "Video"))
        self._mode_tabs.addTab(I18N.tr("deliver.video.audio", "Audio"))
        self._mode_tabs.addTab(I18N.tr("deliver.video.file", "File"))
        self._mode_tabs.setCurrentIndex(0)
        self._mode_tabs.currentChanged.connect(self._on_mode_tab_changed)
        sections_layout.addWidget(self._mode_tabs)

        self._mode_stack = QStackedWidget()
        self._mode_stack.setObjectName("DeliverModeStack")
        self._mode_stack.addWidget(
            self._make_tab_page(self._build_video_section(), "VideoTabPage")
        )
        self._mode_stack.addWidget(self._make_tab_page(self._build_audio_section(), "AudioTabPage"))
        self._mode_stack.addWidget(self._make_tab_page(self._build_file_section(), "FileTabPage"))
        sections_layout.addWidget(self._mode_stack, 1)

        # Forms should read like a real Deliver inspector at both dock and
        # full-page widths.  The previous fixed-size fields floated in the
        # middle of a very wide group box, creating the large empty gutters
        # visible in the page screenshot.
        for widget_type in (QComboBox, QLineEdit, QSpinBox):
            for field in sections_host.findChildren(widget_type):
                field.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Fixed,
                )
                field.setMaximumWidth(360)

        settings_scroll.setWidget(sections_host)
        settings_scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        settings_scroll.viewport().setStyleSheet(
            f"background-color: {C.BG_PANEL};"
        )
        outer.addWidget(settings_scroll, 1)

        # Keep actions outside the scroll area.  This is important when the
        # user collapses Audio & Delivery or works in a short Deliver dock.
        self._preflight_label = QLabel("")
        self._preflight_label.setObjectName("PreflightSummary")
        self._preflight_label.setWordWrap(True)
        self._preflight_label.setStyleSheet("color: #9AA0A6; font-size: 12px;")
        outer.addWidget(self._preflight_label)

        action_bar = QHBoxLayout()
        action_bar.setContentsMargins(0, 0, 0, 0)
        action_bar.setSpacing(8)
        action_bar.addStretch(1)
        self._add_btn = QPushButton(
            I18N.tr("deliver.video.add_to_queue", "Add to Video Queue")
        )
        self._add_btn.setObjectName("PrimaryButton")
        self._add_btn.setMinimumHeight(32)
        self._add_and_start_btn = QPushButton(
            I18N.tr("deliver.video.add_and_start", "Add & Start")
        )
        self._add_and_start_btn.setMinimumHeight(32)
        action_bar.addWidget(self._add_btn)
        action_bar.addWidget(self._add_and_start_btn)
        outer.addLayout(action_bar)

    def _make_tab_page(self, content: QWidget, object_name: str) -> QWidget:
        """Create a compact tab content frame using the panel surface tokens."""
        page = QFrame()
        page.setObjectName(object_name)
        page.setFrameShape(QFrame.Shape.StyledPanel)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(content, 1)
        return page

    def _on_mode_tab_changed(self, index: int) -> None:
        """Switch between the video and audio/delivery inspectors."""
        self._mode_stack.setCurrentIndex(index)

    def _make_collapsible_section(
        self, title: str, content: QWidget, expanded: bool
    ) -> ExportSectionWidget:
        """Wrap a settings group in the shared export collapsible shell."""
        # The QGroupBox is retained as a semantic/layout container, but its
        # title and border are owned by ExportSectionWidget to avoid a nested
        # frame and to match the existing export panels exactly.
        if isinstance(content, QGroupBox):
            content.setTitle("")
        section = ExportSectionWidget(title, header_mode="full")
        section.setObjectName("DeliverCollapsibleSection")
        section.setStyleSheet(
            f"ExportSectionWidget {{ background-color: {C.BG_PANEL}; "
            f"border: 1px solid {C.BORDER_DARK}; border-radius: 4px; }}"
        )
        section.content_layout().addWidget(content)
        if not expanded:
            section.toggle_collapse()
        return section

    def _apply_panel_style(self) -> None:
        """Match the image batch and Quick Export dark/orange visual system."""
        self.setStyleSheet(
            f"""
            QWidget#VideoDeliverSettingsPanel {{
                background-color: {C.BG_PANEL};
                color: {C.TEXT_SECONDARY};
            }}
            ExportSectionWidget#DeliverCollapsibleSection {{
                background-color: {C.BG_PANEL};
                border: 1px solid {C.BORDER_DARK};
                border-radius: 4px;
            }}
            QTabBar#DeliverModeTabs {{
                background-color: {C.BG_PANEL};
                qproperty-drawBase: 0;
                border: none;
            }}
            QTabBar#DeliverModeTabs::tab {{
                background-color: {C.BG_BUTTON};
                color: {C.TEXT_LABEL};
                border: 1px solid {C.BORDER_MID};
                border-bottom: none;
                border-radius: 4px 4px 0 0;
                padding: 5px 10px;
                min-width: 72px;
                font-size: 11px;
                font-weight: 600;
            }}
            QTabBar#DeliverModeTabs::tab:hover:!selected {{
                background-color: {C.BG_BUTTON_HOVER};
                color: {C.TEXT_BRIGHT};
            }}
            QTabBar#DeliverModeTabs::tab:selected {{
                background-color: {C.ORANGE_PRESS};
                border-color: {C.ORANGE_PRESS};
                color: #ffffff;
            }}
            QFrame#VideoTabPage, QFrame#AudioTabPage, QFrame#FileTabPage {{
                background-color: {C.BG_PANEL};
                border: 1px solid {C.BORDER_DARK};
                border-radius: 0 4px 4px 4px;
            }}
            QGroupBox#VideoSettingsSection,
            QGroupBox#AudioDeliverySection,
            QGroupBox#AudioSection,
            QGroupBox#FileSection {{
                background-color: {C.BG_PANEL};
                border: none;
                margin: 0;
                padding: 0;
            }}
            QGroupBox::title {{
                height: 0;
                padding: 0;
                color: transparent;
            }}
            QLineEdit, QSpinBox {{
                min-height: 26px;
                background-color: {C.BG_INPUT};
                color: {C.TEXT_BRIGHT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 2px 6px;
            }}
            QLineEdit:focus, QSpinBox:focus {{
                border-color: {C.ORANGE};
            }}
            QPushButton {{
                background-color: {C.BG_BUTTON};
                color: {C.TEXT_BRIGHT};
                border: 1px solid {C.BORDER_MID};
                border-radius: 3px;
                padding: 5px 10px;
            }}
            QPushButton:hover {{
                background-color: {C.BG_BUTTON_HOVER};
                border-color: {C.ORANGE};
            }}
            QPushButton#PrimaryButton {{
                background-color: {C.ORANGE_PRESS};
                color: white;
                border: none;
                font-weight: 600;
            }}
            QPushButton#PrimaryButton:hover {{
                background-color: {C.ORANGE_HOVER};
            }}
            QScrollArea#VideoSettingsScroll {{
                background-color: {C.BG_PANEL};
                border: none;
            }}
            QLabel {{ color: {C.TEXT_SECONDARY}; }}
            QLabel#VideoSettingsTitle {{
                color: {C.TEXT_BRIGHT};
                font-size: 13px;
                font-weight: 600;
            }}
            """
        )

        # ExportSectionWidget intentionally owns its own compact content
        # stylesheet, and QComboBox uses a Fusion style for reliable macOS
        # popup rendering.  Apply the shared input tokens directly as well so
        # every field keeps the same visible shell inside the collapsible
        # sections, not just the same popup list.
        input_style = f"""
            QLineEdit, QSpinBox {{
                background-color: {C.BG_INPUT};
                color: {C.TEXT_BRIGHT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 3px 7px;
                min-height: 26px;
            }}
            QLineEdit:hover, QSpinBox:hover {{
                background-color: {C.BG_ACTIVE};
                border-color: {C.BORDER_HOVER};
            }}
            QLineEdit:focus, QSpinBox:focus {{
                border-color: {C.ORANGE};
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                width: 16px;
                border: none;
                background: transparent;
            }}
            QCheckBox {{ color: {C.TEXT_SECONDARY}; spacing: 6px; }}
            QCheckBox::indicator {{
                width: 13px;
                height: 13px;
                border: 1px solid {C.BORDER_MID};
                border-radius: 2px;
                background: {C.BG_INPUT};
            }}
            QCheckBox::indicator:checked {{
                background: {C.ORANGE_PRESS};
                border-color: {C.ORANGE};
            }}
        """
        for widget_type in (QLineEdit, QSpinBox):
            for field in self.findChildren(widget_type):
                # DragValue owns a compact inline editor with its own focus
                # border and height.  Applying the regular panel input style
                # here adds padding/min-height and clips that editor inside
                # its 24px value cell.
                if field.objectName() == "DragValueInlineEdit":
                    continue
                field.setStyleSheet(input_style)

    # ── Preset bar ──

    def _build_preset_bar(self) -> QWidget:
        host = QWidget()
        host.setObjectName("VideoPresetBar")
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Horizontal scroll so the bar doesn't compress text on narrow windows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(86)
        scroll.setMinimumHeight(72)

        cards_host = QWidget()
        cards_layout = QHBoxLayout(cards_host)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(8)
        cards_layout.addStretch()

        self._preset_cards: dict[str, _PresetCard] = {}
        for preset in DEFAULT_VIDEO_PRESETS:
            card = _PresetCard(preset)
            card.selection_toggled.connect(self._on_preset_selection_toggled)
            self._preset_cards[preset.preset_id] = card
            cards_layout.addWidget(card)

        # B6: user-defined presets group (persisted under Presets/User/export)
        # followed by the Save-As-Preset action at the end of the strip.
        self._preset_cards_layout = cards_layout
        self._custom_group_label = QLabel(
            I18N.tr("deliver.preset.custom_group", "Custom")
        )
        self._custom_group_label.setStyleSheet(
            f"color: {C.TEXT_DIM}; font-size: 10px; padding: 0 2px;"
        )
        self._custom_group_label.hide()
        cards_layout.addWidget(self._custom_group_label)
        self._custom_card_ids: list[str] = []
        self._save_preset_btn = QPushButton(
            I18N.tr("deliver.preset.save_as", "Save As Preset…")
        )
        self._save_preset_btn.setToolTip(I18N.tr(
            "deliver.preset.save_as_tooltip",
            "把当前设置面板状态保存为自定义预设（Presets/User/export）",
        ))
        self._save_preset_btn.clicked.connect(self._on_save_as_preset)
        cards_layout.addWidget(self._save_preset_btn)
        self._reload_custom_presets()
        cards_layout.addStretch()

        scroll.setWidget(cards_host)
        scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        scroll.viewport().setStyleSheet(
            f"background-color: {C.BG_PANEL}; border: none;"
        )
        cards_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        cards_host.setStyleSheet(
            f"background-color: {C.BG_PANEL}; border: none;"
        )
        layout.addWidget(scroll)
        return host

    def _reload_custom_presets(self) -> None:
        """Rebuild the custom preset cards from disk (B6)."""
        from src.features.deliver.core.custom_export_presets import (
            list_user_presets,
        )

        layout = getattr(self, "_preset_cards_layout", None)
        if layout is None:
            return
        anchor = self._custom_group_label
        for preset_id in list(getattr(self, "_custom_card_ids", [])):
            card = self._preset_cards.pop(preset_id, None)
            if card is not None:
                layout.removeWidget(card)
                card.deleteLater()
        self._custom_card_ids = []

        customs = list_user_presets()
        self._custom_group_label.setVisible(bool(customs))
        for preset in customs:
            card = _PresetCard(preset)
            card.selection_toggled.connect(self._on_preset_selection_toggled)
            card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            card.customContextMenuRequested.connect(
                lambda _pos, pid=preset.preset_id: self._show_custom_preset_menu(pid)
            )
            self._preset_cards[preset.preset_id] = card
            self._custom_card_ids.append(preset.preset_id)
            layout.insertWidget(layout.indexOf(anchor) + 1 + len(self._custom_card_ids), card)

    def _show_custom_preset_menu(self, preset_id: str) -> None:
        """Rename / Delete context menu for a user preset card (B6)."""
        from src.features.deliver.core import custom_export_presets

        card = self._preset_cards.get(preset_id)
        if card is None:
            return
        menu = QMenu(self)
        rename_act = menu.addAction(I18N.tr("deliver.preset.rename", "Rename…"))
        delete_act = menu.addAction(I18N.tr("deliver.preset.delete", "Delete"))
        chosen = menu.exec(card.mapToGlobal(card.rect().center()))
        if chosen is rename_act:
            current = card.preset.fallback_title
            new_name, ok = QInputDialog.getText(
                self,
                I18N.tr("deliver.preset.rename", "Rename…"),
                I18N.tr("deliver.preset.name_label", "预设名称"),
                text=current,
            )
            if ok and new_name.strip():
                try:
                    custom_export_presets.rename_user_preset(
                        preset_id, new_name.strip()
                    )
                except ValueError as exc:
                    QMessageBox.warning(self, I18N.tr(
                        "deliver.preset.save_as", "Save As Preset…"
                    ), str(exc))
                    return
                self._reload_custom_presets()
        elif chosen is delete_act:
            confirm = QMessageBox.question(
                self,
                I18N.tr("deliver.preset.delete", "Delete"),
                I18N.tr(
                    "deliver.preset.delete_confirm",
                    "删除自定义预设 “{name}”？",
                    name=card.preset.fallback_title,
                ),
            )
            if confirm is QMessageBox.StandardButton.Yes:
                custom_export_presets.delete_user_preset(preset_id)
                if preset_id in self._selected_preset_ids:
                    self._selected_preset_ids.remove(preset_id)
                self._reload_custom_presets()

    def _on_save_as_preset(self) -> None:
        """Persist the current panel draft as a reusable preset (B6)."""
        from src.features.deliver.core import custom_export_presets

        name, ok = QInputDialog.getText(
            self,
            I18N.tr("deliver.preset.save_as", "Save As Preset…"),
            I18N.tr("deliver.preset.name_label", "预设名称"),
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        try:
            custom_export_presets.save_user_preset(
                name,
                self._read_video_config_from_draft(),
                self._read_audio_config_from_draft(),
                self._read_subtitle_config_from_draft(),
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                I18N.tr("deliver.preset.save_as", "Save As Preset…"),
                str(exc),
            )
            return
        self._reload_custom_presets()

    def _build_output_header(self) -> QWidget:
        """Build the always-visible filename and location strip."""
        host = QWidget()
        host.setObjectName("DeliverOutputHeader")
        form = QFormLayout(host)
        form.setContentsMargins(0, 2, 0, 2)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._filename_edit = QLineEdit()
        self._filename_edit.setPlaceholderText(
            I18N.tr("deliver.video.filename_placeholder", "Timeline name")
        )
        self._filename_edit.clear()
        form.addRow(self._label("File name"), self._filename_edit)

        out_row = QWidget()
        out_layout = QHBoxLayout(out_row)
        out_layout.setContentsMargins(0, 0, 0, 0)
        out_layout.setSpacing(6)
        self._output_dir_edit = QLineEdit()
        self._output_dir_edit.setText(self._default_output_dir())
        self._output_dir_edit.setPlaceholderText(
            I18N.tr("deliver.video.output_dir_placeholder", "Output directory")
        )
        browse_btn = QPushButton(I18N.tr("deliver.browse", "Browse"))
        browse_btn.clicked.connect(self._browse_output_dir)
        out_layout.addWidget(self._output_dir_edit, 1)
        out_layout.addWidget(browse_btn)
        form.addRow(self._label("Location"), out_row)
        return host

    def _build_render_mode_selector(self) -> QWidget:
        """Build the Resolve-style Single Clip / Individual Clips choice."""
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self._label("Render"))

        self._render_mode_group = QButtonGroup(host)
        self._single_clip_radio = QRadioButton(
            I18N.tr("deliver.render.single_clip", "Single Clip")
        )
        self._individual_clips_radio = QRadioButton(
            I18N.tr("deliver.render.individual_clips", "Individual Clips")
        )
        self._single_clip_radio.setChecked(True)
        self._render_mode_group.addButton(self._single_clip_radio)
        self._render_mode_group.addButton(self._individual_clips_radio)
        row.addWidget(self._single_clip_radio)
        row.addWidget(self._individual_clips_radio)
        row.addStretch(1)
        return host

    # ── Deliver timeline controls ──

    def _build_timeline_range_controls(self) -> QWidget:
        """Build the compact Timeline/Range bar shown above the timeline."""
        bar = QFrame(self)
        bar.setObjectName("DeliverTimelineRangeControls")
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar.setStyleSheet(
            f"QFrame#DeliverTimelineRangeControls {{"
            f"background-color: {C.BG_PANEL}; "
            f"border: none; }}"
        )
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(6)

        # Timeline selection is deliberately not duplicated here.  The
        # Program monitor is the single visible timeline selector; this
        # hidden state combo keeps the existing draft/job API compatible and
        # is synchronised by DeliverWorkspace when Program switches timeline.
        self._timeline_combo = ChevronComboBox(self)
        self._timeline_combo.setObjectName("DeliverTimelineState")
        apply_combo_dropdown_style(self._timeline_combo)
        self._timeline_combo.hide()

        range_label = self._label("Range")
        range_label.setMinimumWidth(0)
        row.addWidget(range_label)
        self._range_combo = ChevronComboBox()
        self._range_combo.setObjectName("DeliverRangeCombo")
        self._range_combo.addItem(I18N.tr("deliver.range_entire", "Entire Timeline"), RangeType.ENTIRE)
        self._range_combo.addItem(I18N.tr("deliver.range_inout", "In / Out"), RangeType.IN_OUT)
        self._range_combo.addItem(I18N.tr("deliver.range_custom", "Custom Frames"), RangeType.CUSTOM)
        self._range_combo.setMinimumWidth(135)
        self._range_combo.setMaximumWidth(220)
        apply_combo_dropdown_style(self._range_combo)
        row.addWidget(self._range_combo, 1)

        # Custom frame inputs stay in the same top bar and only appear when
        # the user chooses Custom Frames.
        self._custom_range_label = self._label("Frames")
        self._custom_range_label.setMinimumWidth(0)
        self._custom_range_label.setVisible(False)
        row.addWidget(self._custom_range_label)
        self._custom_range_widget = QWidget()
        cr_layout = QHBoxLayout(self._custom_range_widget)
        cr_layout.setContentsMargins(0, 0, 0, 0)
        cr_layout.setSpacing(6)
        self._start_frame_spin = QSpinBox()
        self._start_frame_spin.setRange(0, 10_000_000)
        self._start_frame_spin.setValue(0)
        self._end_frame_spin = QSpinBox()
        self._end_frame_spin.setRange(-1, 10_000_000)
        self._end_frame_spin.setValue(-1)
        self._end_frame_spin.setSpecialValueText(I18N.tr("deliver.to_end", "To End"))
        cr_layout.addWidget(QLabel(I18N.tr("node_props.in_label", "In")))
        cr_layout.addWidget(self._start_frame_spin)
        cr_layout.addWidget(QLabel(I18N.tr("node_props.out_label", "Out")))
        cr_layout.addWidget(self._end_frame_spin)
        self._custom_range_widget.setVisible(False)
        row.addWidget(self._custom_range_widget)

        return bar

    # ── Video settings ──

    def _build_video_section(self) -> QWidget:
        group = QGroupBox(I18N.tr("deliver.video.video_settings", "Video"))
        group.setObjectName("VideoSettingsSection")
        group.setMinimumWidth(390)
        form = QFormLayout(group)
        form.setSpacing(8)
        form.setContentsMargins(10, 14, 10, 10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._container_combo = ChevronComboBox()
        for c in CONTAINER_ORDER:
            self._container_combo.addItem(_CONTAINER_LABELS.get(c, c.upper()), c)
        self._container_combo.addItem(
            I18N.tr("deliver.format_image_sequence", "Image Sequence Directory"), ""
        )
        apply_combo_dropdown_style(self._container_combo)
        form.addRow(self._label("Format"), self._container_combo)

        self._codec_combo = ChevronComboBox()
        for entry in _CODEC_CATALOGUE:
            self._codec_combo.addItem(entry["label"], entry["codec"])
        apply_combo_dropdown_style(self._codec_combo)
        form.addRow(self._label("Encoder"), self._codec_combo)

        self._profile_combo = ChevronComboBox()
        apply_combo_dropdown_style(self._profile_combo)
        profile_tooltip = I18N.tr(
            "deliver.video.profile_tooltip",
            "Encoder profile. Auto selects the codec's default compatibility profile.",
        )
        self._profile_combo.setToolTip(profile_tooltip)
        self._profile_label = self._label("Profile")
        self._profile_label.setToolTip(profile_tooltip)

        self._resolution_combo = ChevronComboBox()
        self._resolution_combo.addItem(I18N.tr("deliver.resolution_original", "Timeline Resolution"), None)
        self._resolution_combo.addItem("1920 × 1080", (1920, 1080))
        self._resolution_combo.addItem("3840 × 2160", (3840, 2160))
        self._resolution_combo.addItem("1280 × 720", (1280, 720))
        self._resolution_combo.addItem(I18N.tr("deliver.resolution_custom", "Custom"), "custom")
        apply_combo_dropdown_style(self._resolution_combo)
        form.addRow(self._label("Resolution"), self._resolution_combo)

        # Custom resolution row
        self._custom_res_widget = QWidget()
        cres_layout = QHBoxLayout(self._custom_res_widget)
        cres_layout.setContentsMargins(0, 0, 0, 0)
        cres_layout.setSpacing(6)
        width_label = QLabel(I18N.tr("deliver.w_label", "W"))
        width_label.setStyleSheet("color: #888; font-size: 10px;")
        cres_layout.addWidget(width_label)
        self._width_spin = DragValue("", 16, 16384, 1920.0, decimals=0)
        self._width_spin.setFixedValueWidth(64)
        cres_layout.addWidget(self._width_spin)

        self._aspect_lock_btn = make_link_button(
            initial_linked=False,
            tooltip=I18N.tr(
                "deliver.video.aspect_lock",
                "Lock aspect ratio",
            ),
        )
        self._aspect_lock_btn.toggled.connect(self._on_aspect_lock_toggled)
        cres_layout.addWidget(self._aspect_lock_btn)

        height_label = QLabel(I18N.tr("deliver.h_label", "H"))
        height_label.setStyleSheet("color: #888; font-size: 10px;")
        cres_layout.addWidget(height_label)
        self._height_spin = DragValue("", 16, 16384, 1080.0, decimals=0)
        self._height_spin.setFixedValueWidth(64)
        cres_layout.addWidget(self._height_spin)
        cres_layout.addStretch(1)
        # Resolve keeps the resolved dimensions visible but locks them when
        # the output follows the timeline.  This is more useful than leaving
        # an empty "Size" row in the middle of the inspector.
        self._custom_res_widget.setVisible(True)
        self._custom_res_label = self._label("Size")
        form.addRow(self._custom_res_label, self._custom_res_widget)

        self._fps_combo = ChevronComboBox()
        self._fps_combo.addItem(I18N.tr("deliver.fps_original", "Timeline FPS"), None)
        for fps in (24.0, 25.0, 30.0, 50.0, 60.0):
            self._fps_combo.addItem(f"{fps:g} fps", fps)
        self._fps_combo.addItem(I18N.tr("deliver.fps_custom", "Custom"), "custom")
        apply_combo_dropdown_style(self._fps_combo)
        form.addRow(self._label("Frame rate"), self._fps_combo)

        self._custom_fps_spin = QSpinBox()
        self._custom_fps_spin.setRange(1, 240)
        self._custom_fps_spin.setValue(24)
        self._custom_fps_spin.setVisible(False)
        self._custom_fps_label = self._label("Custom FPS")
        self._custom_fps_label.setVisible(False)
        form.addRow(self._custom_fps_label, self._custom_fps_spin)

        # GIF palette control.  Resolution and frame rate use the same
        # Deliver controls above; this third setting is specific to GIF.
        self._gif_color_count_spin = QSpinBox()
        self._gif_color_count_spin.setRange(2, 256)
        self._gif_color_count_spin.setValue(256)
        self._gif_color_count_spin.setSuffix(
            I18N.tr("deliver.video.gif_colors_suffix", " colors")
        )
        self._gif_color_count_spin.setToolTip(
            I18N.tr(
                "deliver.video.gif_colors_hint",
                "Number of colors in the GIF palette. Fewer colors produce smaller files.",
            )
        )
        self._gif_color_count_label = self._label("GIF colors")
        form.addRow(self._gif_color_count_label, self._gif_color_count_spin)

        # R15/R17（第七轮审计）：图像序列（PNG/TIFF）专属设置——位深、
        # 帧号 padding、首帧号、同名覆盖授权。仅在序列 codec 时可见。
        self._image_bit_depth_combo = ChevronComboBox()
        self._image_bit_depth_combo.addItem("8-bit", 8)
        self._image_bit_depth_combo.addItem("16-bit", 16)
        apply_combo_dropdown_style(self._image_bit_depth_combo)
        self._image_bit_depth_combo.setToolTip(I18N.tr(
            "deliver.video.image_bit_depth_hint",
            "16-bit uses the high-precision render pipeline (real 16-bit "
            "output, not up-scaled 8-bit)."))
        self._image_bit_depth_label = self._label("Bit depth")
        form.addRow(self._image_bit_depth_label, self._image_bit_depth_combo)

        self._image_digits_spin = QSpinBox()
        self._image_digits_spin.setRange(1, 10)
        self._image_digits_spin.setValue(6)
        self._image_digits_spin.setToolTip(I18N.tr(
            "deliver.video.image_digits_hint",
            "Zero padding of frame numbers (name_000000.png)."))
        self._image_digits_label = self._label("Frame # digits")
        form.addRow(self._image_digits_label, self._image_digits_spin)

        self._image_start_spin = QSpinBox()
        self._image_start_spin.setRange(0, 9999999)
        self._image_start_spin.setValue(0)
        self._image_start_spin.setToolTip(I18N.tr(
            "deliver.video.image_start_hint",
            "First frame number of the sequence (e.g. 1001)."))
        self._image_start_label = self._label("Start frame #")
        form.addRow(self._image_start_label, self._image_start_spin)

        self._image_overwrite_check = QCheckBox(I18N.tr(
            "deliver.video.image_overwrite",
            "Overwrite existing sequence"))
        self._image_overwrite_check.setToolTip(I18N.tr(
            "deliver.video.image_overwrite_hint",
            "Off: exporting over an existing sequence of the same name "
            "fails (existing files are never touched). On: frames are "
            "staged and committed on success, stale tail frames removed."))
        self._image_overwrite_label = self._label("Existing files")
        form.addRow(self._image_overwrite_label, self._image_overwrite_check)
        # 初始隐藏——_on_codec_changed 仅在 PNG/TIFF 序列 codec 时显示。
        for w in (self._image_bit_depth_label, self._image_bit_depth_combo,
                  self._image_digits_label, self._image_digits_spin,
                  self._image_start_label, self._image_start_spin,
                  self._image_overwrite_label, self._image_overwrite_check):
            w.setVisible(False)

        # Quality
        self._quality_mode_combo = ChevronComboBox()
        self._quality_mode_combo.addItem(
            I18N.tr("deliver.video.quality_vbr", "VBR"),
            "vbr",
        )
        self._quality_mode_combo.addItem(
            I18N.tr("deliver.video.quality_cbr", "CBR"),
            "cbr",
        )
        self._quality_mode_combo.addItem("CRF", "crf")
        self._quality_mode_combo.setCurrentIndex(0)
        apply_combo_dropdown_style(self._quality_mode_combo)
        self._quality_mode_label = self._label("Quality mode")
        form.addRow(self._quality_mode_label, self._quality_mode_combo)

        self._quality_value_spin = DragValue(
            "",
            1,
            self.MAX_BITRATE_KBPS,
            self.DEFAULT_BITRATE_KBPS,
            decimals=0,
            suffix=" kbps",
        )
        self._quality_value_spin.setFixedValueWidth(100)
        self._quality_label = self._label("Quality")
        self._quality_value_spin.setToolTip(
            I18N.tr(
                "deliver.video.quality_bitrate_hint",
                "Target average video bitrate in kbps; actual bitrate may vary.",
            )
        )
        self._crf_combo = ChevronComboBox()
        crf_options = (
            ("CRF 18 · High quality", 18),
            ("CRF 20 · Blu-ray quality", 20),
            ("CRF 23 · Recommended", 23),
            ("CRF 28 · Smaller file", 28),
            ("CRF 30 · Preview", 30),
        )
        for label, value in crf_options:
            self._crf_combo.addItem(
                I18N.tr(
                    f"deliver.video.crf_{value}",
                    label,
                ),
                value,
            )
        apply_combo_dropdown_style(self._crf_combo)
        self._crf_combo.hide()

        quality_value_host = QWidget()
        quality_value_layout = QHBoxLayout(quality_value_host)
        quality_value_layout.setContentsMargins(0, 0, 0, 0)
        quality_value_layout.addWidget(self._quality_value_spin)
        quality_value_layout.addWidget(self._crf_combo)
        self._estimated_size_label = QLabel()
        self._estimated_size_label.setObjectName("EstimatedFileSize")
        self._estimated_size_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._estimated_size_label.setMinimumWidth(92)
        self._estimated_size_label.setToolTip(
            I18N.tr(
                "deliver.video.estimated_size_hint",
                "Estimated from output duration × video bitrate ÷ 8; audio and container overhead are not included.",
            )
        )
        quality_value_layout.addWidget(self._estimated_size_label)
        self._quality_value_host = quality_value_host
        form.addRow(self._quality_label, quality_value_host)

        # Resolve exposes the encoder profile with the other quality controls,
        # not between the format and resolution fields.
        form.addRow(self._profile_label, self._profile_combo)

        # These legacy fields remain as hidden compatibility state for queued
        # jobs and presets.  The actual TimelineExportConfig derives color
        # metadata and pixel format from the selected encoder; exposing them
        # here created controls that did not affect the real export.
        self._color_space_combo = ChevronComboBox()
        for cs in ("rec709", "rec2020", "p3-d65", "srgb"):
            self._color_space_combo.addItem(cs, cs)
        apply_combo_dropdown_style(self._color_space_combo)

        self._bit_depth_combo = ChevronComboBox()
        self._bit_depth_combo.addItem("8-bit", 8)
        self._bit_depth_combo.addItem("10-bit", 10)
        self._bit_depth_combo.addItem("12-bit", 12)
        apply_combo_dropdown_style(self._bit_depth_combo)

        self._alpha_check = QCheckBox(I18N.tr("deliver.video.alpha", "Alpha channel"))
        self._alpha_label = QLabel("")
        self._alpha_row_visible = False
        form.addRow(self._alpha_label, self._alpha_check)

        # R7（ADR-C020 遗留收口）：Topos 档位 pix_fmt 显式选择器——域来自
        # topos_profiles.tier_pix_fmt_choices（单一真相源，O5），编码器
        # 构造期仍经 topos_encoder 解析表兜底校验。
        self._pix_fmt_combo = ChevronComboBox()
        apply_combo_dropdown_style(self._pix_fmt_combo)
        self._pix_fmt_combo.setToolTip(I18N.tr(
            "deliver.video.topos_pix_fmt_hint",
            "Topos pixel format for this tier. Alpha variants (a8–a16) fix "
            "the alpha bit depth as a format contract; bare formats probe "
            "alpha depth from content."))
        self._pix_fmt_label = self._label("Pixel Format")
        form.addRow(self._pix_fmt_label, self._pix_fmt_combo)

        # R7（ADR-C014 C-114）：Alpha 预算超硬上限策略 UI 授权开关——
        # record=记录交付 / error=拒绝交付 / continue=授权交付（tpcB 记
        # authorized 标志）。
        self._alpha_budget_policy_combo = ChevronComboBox()
        for pol, key, fb in (
            ("record", "deliver.video.alpha_policy_record", "Record & deliver"),
            ("error", "deliver.video.alpha_policy_error", "Fail the export"),
            ("continue", "deliver.video.alpha_policy_continue", "Deliver (authorized)"),
        ):
            self._alpha_budget_policy_combo.addItem(
                I18N.tr(key, fb), pol)
        apply_combo_dropdown_style(self._alpha_budget_policy_combo)
        self._alpha_budget_policy_combo.setToolTip(I18N.tr(
            "deliver.video.alpha_policy_hint",
            "What to do when the Topos alpha stream exceeds the tier's "
            "hard budget cap."))
        self._alpha_budget_policy_label = self._label("Alpha Policy")
        form.addRow(self._alpha_budget_policy_label, self._alpha_budget_policy_combo)

        # Subtitles affect the rendered picture, so keep this beside the
        # video controls rather than hiding it among filename options.
        self._subtitle_combo = ChevronComboBox()
        self._subtitle_combo.addItem(I18N.tr("deliver.subtitle.off", "Off"), SubtitleMode.OFF)
        self._subtitle_combo.addItem(I18N.tr("deliver.subtitle.burn", "Burn in"), SubtitleMode.BURN)
        apply_combo_dropdown_style(self._subtitle_combo)
        form.addRow(self._label("Subtitles"), self._subtitle_combo)

        # Resolve adds handles in Video > Advanced Settings for Individual
        # Clips.  One value deliberately applies to both head and tail.
        self._frame_handles_spin = QSpinBox()
        self._frame_handles_spin.setRange(0, 100_000)
        self._frame_handles_spin.setValue(0)
        self._frame_handles_spin.setSuffix(
            I18N.tr("deliver.individual.frame_handles_suffix", " frames")
        )
        self._frame_handles_spin.setToolTip(
            I18N.tr(
                "deliver.individual.frame_handles_hint",
                "Add this many available source frames before and after every individual clip.",
            )
        )
        self._frame_handles_label = self._label("Frame handles")
        form.addRow(self._frame_handles_label, self._frame_handles_spin)

        # Keep non-visual state attached to the panel without putting
        # unsupported controls in the normal Resolve-style inspector.
        self._color_space_combo.setParent(group)
        self._bit_depth_combo.setParent(group)
        self._color_space_combo.setVisible(False)
        self._bit_depth_combo.setVisible(False)
        # R7：Topos 专属控件默认隐藏（_on_codec_changed 按档位显示）
        self._pix_fmt_combo.setVisible(False)
        self._pix_fmt_label.setVisible(False)
        self._alpha_budget_policy_combo.setVisible(False)
        self._alpha_budget_policy_label.setVisible(False)

        return group

    # ── Audio ──

    def _build_audio_section(self) -> QWidget:
        """Build the audio inspector tab used by the Deliver layout."""
        group = QGroupBox(I18N.tr("deliver.video.audio", "Audio"))
        group.setObjectName("AudioSection")
        group.setMinimumWidth(300)
        form = QFormLayout(group)
        form.setSpacing(8)
        form.setContentsMargins(10, 14, 10, 10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._audio_check = QCheckBox(I18N.tr("deliver.include_audio", "Include audio"))
        self._audio_check.setChecked(True)
        form.addRow("", self._audio_check)

        self._audio_codec_combo = ChevronComboBox()
        for ac in ("aac", "mp3", "pcm_s16le", "pcm_s24le", "flac", "opus"):
            self._audio_codec_combo.addItem(ac, ac)
        apply_combo_dropdown_style(self._audio_codec_combo)
        form.addRow(self._label("Audio codec"), self._audio_codec_combo)

        self._audio_sample_combo = ChevronComboBox()
        # v1.4 全家族采样率（88.2/176.4/192kHz；_sync_audio_sample_choices_
        # for_codec 按容器收敛——避免"可选但导出必败"状态）
        for sr in (44100, 48000, 88200, 96000, 176400, 192000):
            label = f"{sr / 1000:g} kHz"
            self._audio_sample_combo.addItem(label, sr)
        apply_combo_dropdown_style(self._audio_sample_combo)
        form.addRow(self._label("Sample rate"), self._audio_sample_combo)

        self._audio_bitrate_combo = ChevronComboBox()
        for br in (128, 160, 192, 256, 320, 1536, 2304):
            label = f"{br} kbps" if br < 1000 else f"{br / 1000:.1f} Mbps"
            self._audio_bitrate_combo.addItem(label, br)
        apply_combo_dropdown_style(self._audio_bitrate_combo)
        form.addRow(self._label("Audio bitrate"), self._audio_bitrate_combo)

        # v1.1 音频轨（container_spec_v1.1）：声道布局选择——
        # topos 容器 2.0/5.1/7.1 三档；其它 codec 沿用声道数语义
        self._audio_channels_combo = ChevronComboBox()
        for ch, label in ((2, "2.0 Stereo"), (6, "5.1"),
                          (8, "7.1")):
            self._audio_channels_combo.addItem(label, ch)
        apply_combo_dropdown_style(self._audio_channels_combo)
        form.addRow(self._label("Channels"), self._audio_channels_combo)

        # v1.6 M-B8：stems 追加音轨（角色分组容器轨；仅 topos MOV 容器，
        # _on_codec_changed 按 is_topos 门控可见性）。轨 0 恒为主混音，
        # 每个角色组追加一条独立轨（轨名承载角色标签）。
        self._stems_check = QCheckBox(I18N.tr(
            "deliver.audio.stems_enable",
            "Stems 多音轨（角色分组容器轨）"))
        self._stems_check.setChecked(False)
        self._stems_check.setToolTip(I18N.tr(
            "deliver.audio.stems_hint",
            "轨 0 为主混音（兼容视图）；时间线上标注了角色（对白/音乐/…）"
            "的音频轨各占一条独立容器轨，轨名随角色标签写入"
            "（Resolve/Premiere 导入可见）。仅 Topos MOV 容器支持。"))
        self._stems_check.toggled.connect(
            lambda _on: self._on_draft_changed())
        form.addRow("", self._stems_check)

        self._stems_rows_host = QWidget()
        self._stems_rows_layout = QVBoxLayout(self._stems_rows_host)
        self._stems_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._stems_rows_layout.setSpacing(4)
        self._stems_empty_label = QLabel(I18N.tr(
            "deliver.audio.stems_empty",
            "时间线暂无角色标注（音频轨 stem_group 为空）"))
        self._stems_empty_label.setStyleSheet("color: gray;")
        self._stems_rows_layout.addWidget(self._stems_empty_label)
        form.addRow(self._label("Stems 轨"), self._stems_rows_host)
        self._stems_rows: List[dict] = []
        self._refresh_stem_rows()

        return group

    # ── File / delivery ──

    def _build_file_section(self) -> QWidget:
        """Build Resolve-style naming controls for both delivery modes."""
        group = QGroupBox(I18N.tr("deliver.video.file", "File"))
        group.setObjectName("FileSection")
        group.setMinimumWidth(300)
        form = QFormLayout(group)
        form.setSpacing(8)
        form.setContentsMargins(10, 14, 10, 10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        name_use_host = QWidget()
        name_use_layout = QVBoxLayout(name_use_host)
        name_use_layout.setContentsMargins(0, 0, 0, 0)
        name_use_layout.setSpacing(4)
        self._filename_use_group = QButtonGroup(name_use_host)
        self._custom_name_radio = QRadioButton(
            I18N.tr("deliver.file.custom_name", "Custom name")
        )
        self._timeline_or_source_name_radio = QRadioButton()
        self._custom_name_radio.setChecked(True)
        self._filename_use_group.addButton(self._custom_name_radio)
        self._filename_use_group.addButton(self._timeline_or_source_name_radio)
        name_use_layout.addWidget(self._custom_name_radio)
        name_use_layout.addWidget(self._timeline_or_source_name_radio)
        form.addRow(self._label("Filename uses"), name_use_host)

        self._custom_name_edit = QLineEdit()
        self._custom_name_edit.setPlaceholderText(
            I18N.tr("deliver.file.custom_name_placeholder", "Name or {timeline}")
        )
        form.addRow(self._label("Custom name"), self._custom_name_edit)

        self._file_suffix_edit = QLineEdit()
        self._file_suffix_edit.setPlaceholderText(
            I18N.tr("deliver.file.suffix_placeholder", "Optional suffix")
        )
        form.addRow(self._label("File suffix"), self._file_suffix_edit)

        self._unique_filenames_check = QCheckBox(
            I18N.tr("deliver.file.unique_filenames", "Use unique filenames")
        )
        self._unique_filenames_check.setChecked(True)
        self._unique_filenames_check.setToolTip(
            I18N.tr(
                "deliver.file.unique_filenames_hint",
                "Append each clip's track and position so repeated source names cannot overwrite one another.",
            )
        )
        form.addRow("", self._unique_filenames_check)

        self._unique_position_host = QWidget()
        unique_position_layout = QHBoxLayout(self._unique_position_host)
        unique_position_layout.setContentsMargins(0, 0, 0, 0)
        unique_position_layout.setSpacing(10)
        self._unique_position_group = QButtonGroup(self._unique_position_host)
        self._unique_prefix_radio = QRadioButton(
            I18N.tr("deliver.file.unique_prefix", "Prefix")
        )
        self._unique_suffix_radio = QRadioButton(
            I18N.tr("deliver.file.unique_suffix", "Suffix")
        )
        self._unique_suffix_radio.setChecked(True)
        self._unique_position_group.addButton(self._unique_prefix_radio)
        self._unique_position_group.addButton(self._unique_suffix_radio)
        unique_position_layout.addWidget(self._unique_prefix_radio)
        unique_position_layout.addWidget(self._unique_suffix_radio)
        unique_position_layout.addStretch(1)
        form.addRow(self._label("Unique ID"), self._unique_position_host)

        self._unique_digits_spin = QSpinBox()
        self._unique_digits_spin.setRange(2, 10)
        self._unique_digits_spin.setValue(4)
        self._unique_digits_spin.setToolTip(
            I18N.tr(
                "deliver.file.unique_digits_hint",
                "Pad the unique clip number with 2 to 10 digits (for example 0001).",
            )
        )
        self._unique_digits_label = self._label("ID digits")
        form.addRow(self._unique_digits_label, self._unique_digits_spin)

        self._source_frame_count_check = QCheckBox(
            I18N.tr("deliver.file.source_frame_count", "Add source frame count to filename")
        )
        form.addRow("", self._source_frame_count_check)

        # B7: 占位片段放行——preflight 默认 blocking；勾选后降级 warning
        #（占位片段按 SMPTE 彩条渲染进成片）。
        self._allow_placeholder_check = QCheckBox(
            I18N.tr("deliver.video.allow_placeholders", "Allow placeholder clips")
        )
        self._allow_placeholder_check.setToolTip(
            I18N.tr(
                "deliver.video.allow_placeholders_hint",
                "Placeholder clips normally block delivery. Enable to export "
                "anyway; placeholders render as color bars in the output.",
            )
        )
        form.addRow("", self._allow_placeholder_check)

        self._update_naming_controls()

        # Conflict policy
        self._conflict_combo = ChevronComboBox()
        self._conflict_combo.addItem(I18N.tr("deliver.conflict.ask", "Ask"), OverwritePolicy.ASK)
        self._conflict_combo.addItem(I18N.tr("deliver.conflict.overwrite", "Overwrite"), OverwritePolicy.OVERWRITE)
        self._conflict_combo.addItem(I18N.tr("deliver.conflict.auto_number", "Auto-number"), OverwritePolicy.AUTO_NUMBER)
        self._conflict_combo.addItem(I18N.tr("deliver.conflict.skip", "Skip"), OverwritePolicy.SKIP)
        apply_combo_dropdown_style(self._conflict_combo)
        # Kept hidden for queue-schema compatibility.  The current worker
        # does not implement overwrite/skip actions, so exposing this would
        # suggest a behavior that cannot happen at export time.
        self._conflict_combo.setParent(group)
        self._conflict_combo.setVisible(False)

        return group

    # Kept as a small compatibility shim for integrations that used the old
    # private builder while the UI was a two-tab inspector.
    def _build_audio_delivery_section(self) -> QWidget:
        return self._build_audio_section()

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(I18N.tr(f"deliver.video.{text.lower().replace(' ', '_')}", text))
        lbl.setMinimumWidth(88)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return lbl

    # =========================================================================
    # Signal wiring
    # =========================================================================

    def _setup_connections(self) -> None:
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        self._resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)
        self._fps_combo.currentIndexChanged.connect(self._on_fps_changed)
        self._codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        # profile / pix_fmt 选择器联动（HEVC main↔main10 位深档）与 draft
        # 变更（R7 遗留：Topos pix_fmt 此前未联线，选择后不标脏）
        self._profile_combo.currentIndexChanged.connect(
            self._on_profile_combo_changed)
        self._pix_fmt_combo.currentIndexChanged.connect(
            self._on_pix_fmt_combo_changed)
        self._container_combo.currentIndexChanged.connect(self._on_container_changed)
        self._quality_mode_combo.currentIndexChanged.connect(self._on_quality_mode_changed)
        self._crf_combo.currentIndexChanged.connect(self._on_crf_changed)
        self._audio_check.toggled.connect(self._on_audio_toggled)
        self._subtitle_combo.currentIndexChanged.connect(self._on_draft_changed)
        self._render_mode_group.buttonToggled.connect(self._on_render_mode_changed)
        self._filename_use_group.buttonToggled.connect(self._on_filename_use_changed)
        self._unique_filenames_check.toggled.connect(self._on_unique_filenames_changed)
        self._custom_name_edit.textChanged.connect(self._on_custom_name_changed)

        # AUD-12400: aspect-lock linkage. Without these connections the lock
        # button only refreshed ``_aspect_ratio`` and the W/H handlers below
        # were dead code — editing one side never updated the other. The
        # handlers are re-entrancy-guarded by ``_syncing_resolution_values``.
        self._width_spin.valueChanged.connect(self._on_width_changed)
        self._height_spin.valueChanged.connect(self._on_height_changed)

        for w in (
            self._start_frame_spin, self._end_frame_spin,
            self._width_spin, self._height_spin,
            self._custom_fps_spin, self._quality_value_spin,
            self._gif_color_count_spin,
            self._image_digits_spin, self._image_start_spin,
            self._color_space_combo,
            self._bit_depth_combo,
            self._image_bit_depth_combo,
            self._audio_codec_combo, self._audio_sample_combo,
            self._audio_bitrate_combo,
            self._conflict_combo,
        ):
            if isinstance(w, QComboBox):
                w.currentIndexChanged.connect(self._on_draft_changed)
            else:
                w.valueChanged.connect(self._on_draft_changed)  # type: ignore[attr-defined]
        # QCheckBox uses toggled, not valueChanged
        self._alpha_check.toggled.connect(self._on_draft_changed)
        self._image_overwrite_check.toggled.connect(self._on_draft_changed)
        self._alpha_check.toggled.connect(
            lambda _checked: self._refresh_pix_fmt_choices())
        self._file_suffix_edit.textChanged.connect(self._on_draft_changed)
        self._source_frame_count_check.toggled.connect(self._on_draft_changed)
        self._frame_handles_spin.valueChanged.connect(self._on_draft_changed)
        self._unique_digits_spin.valueChanged.connect(self._on_draft_changed)
        self._unique_prefix_radio.toggled.connect(self._on_draft_changed)
        self._unique_suffix_radio.toggled.connect(self._on_draft_changed)
        self._filename_edit.textChanged.connect(self._on_filename_changed)
        self._output_dir_edit.textChanged.connect(self._on_draft_changed)
        self._audio_check.toggled.connect(self._on_draft_changed)

        self._timeline_combo.currentIndexChanged.connect(self._on_timeline_changed)

        self._add_btn.clicked.connect(self._on_add_clicked)
        self._add_and_start_btn.clicked.connect(self._on_add_and_start_clicked)

        # Initialise the format matrix before exposing the panel.  This is
        # what prevents MP4 from ever presenting ProRes/DNxHR as choices.
        self._refresh_codec_options_for_container()
        self._on_codec_changed(self._codec_combo.currentIndex())
        self._on_resolution_changed(self._resolution_combo.currentIndex())
        self._on_fps_changed(self._fps_combo.currentIndex())
        self._on_quality_mode_changed(self._quality_mode_combo.currentIndex())
        self._on_audio_toggled(self._audio_check.isChecked())
        self._update_naming_controls()

    # =========================================================================
    # Public API (used by the workspace / controller)
    # =========================================================================

    def set_project(
        self,
        project: Any,
        project_id: str = "",
        project_revision: int = 0,
        project_name: str = "",
    ) -> None:
        self._project = project
        self._project_id = project_id
        self._project_revision = project_revision
        self._project_name = project_name or ""
        # AUD-12404: a job being edited belongs to the previous project's
        # queue — never carry the editing id across a project switch.
        self._editing_job_id = None
        self._refresh_preflight()

    def set_timelines(self, timelines: List[Tuple[str, str]]) -> None:
        """Populate the timeline dropdown.

        Args:
            timelines: list of (timeline_id, timeline_name) tuples. Pass an
                empty list to show an empty state.
        """
        self._building_ui = True
        try:
            self._timeline_combo.clear()
            if not timelines:
                self._timeline_combo.addItem(I18N.tr("deliver.no_timeline", "No Timeline"), "")
            else:
                for tid, name in timelines:
                    self._timeline_combo.addItem(name, tid)
        finally:
            self._building_ui = False
        self._on_timeline_changed(self._timeline_combo.currentIndex())

    def set_active_timeline(self, timeline_id: str) -> None:
        """Select a timeline by id (no-op if not present)."""
        idx = self._timeline_combo.findData(timeline_id)
        if idx >= 0:
            self._timeline_combo.setCurrentIndex(idx)

    def get_deliver_timeline_controls(self) -> QWidget:
        """Return the Timeline/Range bar owned by the export draft.

        DeliverWorkspace reparents this widget into the live timeline panel
        while the Deliver page is active.  The hidden timeline state and the
        visible range combo remain owned by this settings panel's draft, so
        job creation and the visible controls always read the same values.
        """
        return self._deliver_timeline_controls

    def current_range_type(self) -> str:
        """Return the current output range mode without exposing the widget."""
        return str(self._range_combo.currentData() or RangeType.ENTIRE)

    def set_timeline_sequences(self, sequences: Any) -> None:
        """全部时间线序列（nested 音频 preflight 展开，复验 P1-14）。

        U03（第六轮审计）：只接收完整 TimelineSequence。轻量
        TimelineSequenceRef 缺 ``audio_tracks``，进入 nested resolver 会在
        flatten 时 AttributeError，这里直接过滤。
        """
        raw = dict(sequences) if sequences else {}
        self._timeline_sequences = {
            str(tid): seq
            for tid, seq in raw.items()
            if self._is_full_sequence(seq)
        }

    def set_sequence(self, sequence: Any) -> None:
        """Set the live sequence (used for duration / frame-count display)."""
        self._sequence = sequence
        # R01（第七轮审计）：完整 live 序列必须同时刷新 _timeline_sequences
        # map——_sequence_for_combo_selection 优先读 map，只更新 _sequence
        # 会让 draft/preflight/时长继续用编辑前的旧序列（Deliver 内剪短
        # 后导出仍为 100 帧/4 秒）。
        if sequence is not None and self._is_full_sequence(sequence):
            live_id = str(getattr(sequence, "timeline_id", "") or "")
            if live_id:
                self._timeline_sequences[live_id] = sequence
        # U03：live 序列与 combo 选择保持同向。否则宿主先安装 B 而 combo
        # 仍停在 A 时，用户再点 A 不会触发 currentIndexChanged，draft 永远
        # 切不回 A。
        if sequence is not None:
            seq_id = str(getattr(sequence, "timeline_id", "") or "")
            if seq_id:
                self.set_active_timeline(seq_id)
        self._sync_default_filename()
        self._update_duration_display()
        self._update_timeline_video_values()
        self._update_file_size_estimate()
        # v1.6 M-B8：序列就绪后重建 stems 角色组行（stem_group 标注变化）
        self._refresh_stem_rows()
        # ``set_project`` populates the timeline combo before the controller
        # hands us the full TimelineSequence.  That combo change can produce
        # a temporary "timeline not found" preflight result. Re-run preflight
        # after the real sequence context is installed so the status reflects
        # the current timeline rather than the previous initialization order.
        self._refresh_preflight()

    # ── build / load jobs ──

    def build_jobs(self, preflight: bool = True) -> Tuple[List[VideoExportJob], Optional[PreflightResult]]:
        """Build one job per selected preset (or one from the current draft).

        Returns ``(jobs, result)``. When preflight blocks, ``jobs`` is empty
        and ``result`` carries the issues. Pass ``preflight=False`` to skip
        the preflight step (the controller may want to show its own UI).

        Override semantics (critical for multi-preset §6.2):
        * **No preset selected** — the draft IS the spec; one "custom" job.
        * **Exactly one preset selected** — preset provides codec/resolution/
          quality, draft overrides any field the user has changed.
        * **Multiple presets selected** — each preset keeps its own
          codec/resolution/quality (the draft does NOT override these,
          because the draft only reflects the last-clicked preset). Only
          *shared* fields (range, audio, subtitles, output dir, filename
          template, conflict policy) come from the draft.
        """
        jobs: List[VideoExportJob] = []

        # R01（第七轮审计）：revision 从活动工程对象实时读取——编辑提交
        # 后 revision 递增，沿用 set_project 时的缓存值会让任务记录落后
        # 于实际导出的内容版本。
        live_revision = int(getattr(self._project, "revision", 0) or 0)
        if live_revision and live_revision != self._project_revision:
            self._project_revision = live_revision

        selected_presets = [self._preset_cards[pid].preset for pid in self._selected_preset_ids
                            if pid in self._preset_cards]
        multi_preset = len(selected_presets) > 1

        if not selected_presets:
            # No preset selected → synthesise one straight from the draft so
            # the user's custom codec/resolution/quality are honoured.
            selected_presets = [self._draft_as_preset()]
            multi_preset = False

        timeline_id = self._timeline_combo.currentData() or ""
        timeline_name = self._timeline_combo.currentText()
        range_type = self._range_combo.currentData() or RangeType.ENTIRE
        start_frame = self._start_frame_spin.value() if range_type == RangeType.CUSTOM else 0
        end_frame = self._end_frame_spin.value() if range_type == RangeType.CUSTOM else -1
        output_dir = self._output_dir_edit.text().strip() or self._default_output_dir()
        template = self._filename_edit.text().strip() or self.DEFAULT_FILENAME_TEMPLATE
        overwrite_policy = self._conflict_combo.currentData() or OverwritePolicy.ASK

        for i, preset in enumerate(selected_presets):
            job = build_job_from_preset(
                preset,
                project_id=self._project_id,
                project_name=self._project_name,
                project_revision=self._project_revision,
                timeline_id=timeline_id,
                timeline_name=timeline_name,
                range_type=range_type,
                start_frame=start_frame,
                end_frame=end_frame,
                output_dir=output_dir,
                filename_template=template,
                overwrite_policy=overwrite_policy,
                index=i,
            )
            # Apply draft overrides. In multi-preset mode we only override
            # shared fields (audio/subtitles) — NEVER the per-preset video
            # settings, because the draft only reflects the last-clicked
            # preset and overriding would homogenise all tasks.
            if multi_preset:
                self._apply_shared_overrides(job)
            else:
                self._apply_draft_overrides(job, preset)
            self._refresh_job_output_path(job, output_dir, template, i)
            job.allow_placeholder_clips = self._allow_placeholder_check.isChecked()
            if self._editing_job_id and len(selected_presets) == 1:
                # AUD-12404: only carry the editing id into a single-job
                # build — stamping it onto every job of a multi-preset
                # batch produced duplicate ids that clobbered each other
                # in the id-indexed queue.
                job.job_id = self._editing_job_id
            jobs.append(job)

        if not preflight:
            return jobs, None

        # Preflight every job; accumulate issues. If any job is blocked we
        # return an empty list + a merged result so the caller can show one
        # consolidated error.
        merged = PreflightResult()
        for job in jobs:
            ctx = self._build_preflight_context(job)
            r = run_video_export_preflight(job, ctx)
            for issue in r.issues:
                merged.add(issue)
        if not merged.ok:
            return [], merged
        return jobs, merged

    def load_job(self, job: VideoExportJob) -> None:
        """Load an existing job into the draft for editing."""
        self._building_ui = True
        try:
            self._editing_job_id = job.job_id
            # Source / range
            self._select_combo_by_data(self._timeline_combo, job.timeline_id)
            self._select_combo_by_data(self._range_combo, job.range_type)
            self._allow_placeholder_check.setChecked(
                bool(getattr(job, "allow_placeholder_clips", False)))
            self._start_frame_spin.setValue(job.start_frame)
            self._end_frame_spin.setValue(job.end_frame)

            # Video
            vc = job.video_config
            self._select_combo_by_data(self._container_combo, vc.container)
            self._select_combo_by_data(self._codec_combo, vc.codec)
            # AUD-12401: profile ids are stored lowercase ("high", …) while
            # the combo shows capitalised labels with the raw id as data —
            # setCurrentText is case-sensitive and silently missed, so the
            # profile fell back to "auto". Select by data instead (also
            # fixes Topos tier ids whose labels come from the registry).
            self._select_combo_by_data(self._profile_combo, vc.profile or "")
            if vc.resolution is None:
                self._resolution_combo.setCurrentIndex(0)
            else:
                idx = self._resolution_combo.findData(vc.resolution)
                if idx >= 0:
                    self._resolution_combo.setCurrentIndex(idx)
                else:
                    self._select_combo_by_data(self._resolution_combo, "custom")
                    self._width_spin.setValue(vc.resolution[0])
                    self._height_spin.setValue(vc.resolution[1])
            if vc.fps is None:
                self._fps_combo.setCurrentIndex(0)
            else:
                idx = self._fps_combo.findData(vc.fps)
                if idx >= 0:
                    self._fps_combo.setCurrentIndex(idx)
                else:
                    self._select_combo_by_data(self._fps_combo, "custom")
                    self._custom_fps_spin.setValue(int(vc.fps))
            self._gif_color_count_spin.setValue(
                max(2, min(256, int(getattr(vc, "gif_color_count", 256))))
            )
            # R17：图像序列作业字段回填（位深/帧号/首帧号/覆盖授权）。
            self._select_combo_by_data(
                self._image_bit_depth_combo,
                16 if int(getattr(vc, "bit_depth", 8) or 8) >= 16 else 8)
            self._image_digits_spin.setValue(
                max(1, min(10, int(
                    getattr(vc, "image_sequence_digits", 6) or 6))))
            self._image_start_spin.setValue(
                max(0, int(getattr(vc, "image_sequence_start_index", 0) or 0)))
            self._image_overwrite_check.setChecked(
                bool(getattr(vc, "image_sequence_overwrite", False)))
            self._select_combo_by_data(
                self._quality_mode_combo,
                self._normalize_quality_mode(vc.quality_mode),
            )
            self._set_quality_value_from_config(
                vc.quality_mode,
                vc.quality_value,
            )
            self._select_combo_by_data(self._color_space_combo, vc.color_space)
            self._select_combo_by_data(self._bit_depth_combo, vc.bit_depth)
            self._alpha_check.setChecked(vc.alpha)
            # R7：Topos pix_fmt / Alpha 预算策略回填（_on_codec_changed 已按
            # 档位重建域；域外持久化值回落档位默认）
            self._refresh_pix_fmt_choices()
            if _topos_tier_for_codec(vc.codec) is not None:
                self._select_combo_by_data(
                    self._pix_fmt_combo, vc.pixel_format)
                self._select_combo_by_data(
                    self._alpha_budget_policy_combo,
                    getattr(vc, "alpha_budget_policy", "record"))
            elif str(vc.codec) == "hevc":
                # HEVC 位深档回填；历史队列 profile=main10 语义优先
                # （pixel_format 曾是不可编辑的占位默认值）
                hevc_pf = str(vc.pixel_format or "")
                if str(vc.profile or "").strip().lower() == "main10":
                    hevc_pf = "yuv420p10le"
                self._select_combo_by_data(self._pix_fmt_combo, hevc_pf)
                self._sync_hevc_profile_with_pix_fmt()

            # Audio
            ac = job.audio_config
            self._audio_check.setChecked(ac.include_audio)
            self._select_combo_by_data(self._audio_codec_combo, ac.codec)
            self._select_combo_by_data(self._audio_sample_combo, ac.sample_rate)
            self._select_combo_by_data(self._audio_bitrate_combo, ac.bitrate)

            # Subtitles
            self._select_combo_by_data(self._subtitle_combo, job.subtitle_config.mode)

            # Resolve-style render and filename rules.
            naming = job.naming_config
            self._individual_clips_radio.setChecked(
                naming.render_mode == RenderMode.INDIVIDUAL_CLIPS
            )
            self._single_clip_radio.setChecked(
                naming.render_mode != RenderMode.INDIVIDUAL_CLIPS
            )
            uses_custom = naming.filename_use == FilenameUse.CUSTOM
            self._custom_name_radio.setChecked(uses_custom)
            self._timeline_or_source_name_radio.setChecked(not uses_custom)
            self._custom_name_edit.setText(naming.custom_name)
            self._file_suffix_edit.setText(naming.file_suffix)
            self._unique_filenames_check.setChecked(naming.unique_filenames)
            self._unique_prefix_radio.setChecked(
                naming.unique_filename_position == "prefix"
            )
            self._unique_suffix_radio.setChecked(
                naming.unique_filename_position != "prefix"
            )
            self._source_frame_count_check.setChecked(naming.add_source_frame_count)
            self._unique_digits_spin.setValue(naming.unique_filename_digits)
            self._frame_handles_spin.setValue(naming.frame_handles)
            self._update_naming_controls()

            # Output
            out_dir = os.path.dirname(job.output_path) or self._default_output_dir()
            self._output_dir_edit.setText(out_dir)
            loaded_name = os.path.splitext(os.path.basename(job.output_path))[0]
            self._filename_follows_timeline = not bool(loaded_name)
            self._filename_edit.setText(loaded_name)
            self._select_combo_by_data(self._conflict_combo, job.overwrite_policy)
        finally:
            self._building_ui = False
        self._on_draft_changed()

    def clear_editing(self) -> None:
        """Stop editing an existing job (next Add creates a new one)."""
        self._editing_job_id = None

    # =========================================================================
    # Slots
    # =========================================================================

    def _on_preset_selection_toggled(self, preset_id: str, checked: bool) -> None:
        if checked:
            if preset_id not in self._selected_preset_ids:
                self._selected_preset_ids.append(preset_id)
        else:
            if preset_id in self._selected_preset_ids:
                self._selected_preset_ids.remove(preset_id)
        # Only mirror the preset into the draft controls when at most one
        # preset is selected. In multi-select mode the draft cannot represent
        # every preset at once, so we leave the controls alone — each preset
        # keeps its own codec/resolution/quality at build time.
        if checked and len(self._selected_preset_ids) <= 1:
            # AUD-12402: take the preset from the card (single source of
            # truth). The old DEFAULT_VIDEO_PRESETS lookup never matched
            # user presets (their ids are "user_…" prefixed), so checking a
            # custom preset card left a stale draft in place that then
            # reverse-overrode the preset's settings via
            # _apply_draft_overrides.
            card = self._preset_cards.get(preset_id)
            if card is not None:
                self._apply_preset_to_draft(card.preset)
        self._on_draft_changed()

    def _on_range_changed(self, _idx: int) -> None:
        range_type = self._range_combo.currentData()
        custom = range_type == RangeType.CUSTOM
        self._custom_range_widget.setVisible(custom)
        self._custom_range_label.setVisible(custom)
        self.range_type_changed.emit(str(range_type or RangeType.ENTIRE))
        self._on_draft_changed()

    def _on_render_mode_changed(self, _button: QRadioButton, checked: bool) -> None:
        if checked:
            self._update_naming_controls()
            self._on_draft_changed()

    def _on_filename_use_changed(self, _button: QRadioButton, checked: bool) -> None:
        if checked:
            self._update_naming_controls()
            self._on_draft_changed()

    def _on_unique_filenames_changed(self, _checked: bool) -> None:
        self._update_naming_controls()
        self._on_draft_changed()

    def _on_custom_name_changed(self, text: str) -> None:
        """Keep the File tab's custom name and visible header in sync."""
        if self._syncing_naming_fields:
            return
        self._syncing_naming_fields = True
        try:
            self._filename_edit.setText(text)
        finally:
            self._syncing_naming_fields = False
        self._on_draft_changed()

    def _is_individual_clips_mode(self) -> bool:
        return self._individual_clips_radio.isChecked()

    def _update_naming_controls(self) -> None:
        """Reveal only File settings that apply to the selected render mode."""
        individual = self._is_individual_clips_mode()
        source_label = (
            I18N.tr("deliver.file.source_filename", "Source filename")
            if individual
            else I18N.tr("deliver.file.timeline_name", "Timeline name")
        )
        self._timeline_or_source_name_radio.setText(source_label)
        self._unique_filenames_check.setVisible(individual)
        self._unique_position_host.setVisible(individual)
        self._unique_digits_label.setVisible(individual)
        self._unique_digits_spin.setVisible(individual)
        self._source_frame_count_check.setVisible(individual)
        self._frame_handles_label.setVisible(individual)
        self._frame_handles_spin.setVisible(individual)
        self._custom_name_edit.setEnabled(self._custom_name_radio.isChecked())
        unique_enabled = individual and self._unique_filenames_check.isChecked()
        self._unique_position_host.setEnabled(unique_enabled)
        self._unique_digits_spin.setEnabled(unique_enabled)
        self._source_frame_count_check.setEnabled(individual)
        self._frame_handles_spin.setEnabled(individual)

    def _on_resolution_changed(self, _idx: int) -> None:
        is_custom = self._resolution_combo.currentData() == "custom"
        self._custom_res_widget.setVisible(True)
        self._custom_res_widget.setEnabled(is_custom)
        self._on_draft_changed()

    def _on_fps_changed(self, _idx: int) -> None:
        is_custom = self._fps_combo.currentData() == "custom"
        self._custom_fps_spin.setVisible(is_custom)
        self._custom_fps_label.setVisible(is_custom)
        self._on_draft_changed()

    def _refresh_pix_fmt_choices(self) -> None:
        """按当前 codec 重建 pix_fmt 选择域（Topos 档位 / HEVC 位深档）。

        Topos：域来自 topos_profiles.tier_pix_fmt_choices（单一真相源，
        O5）；当前选择尽量保留，失效时回落首项（= 档位默认色度结构 ×
        默认位深）。HEVC（2026-09-16）：8/10-bit 交付档（yuv420p /
        yuv420p10le），域内选择保留，默认 8-bit 平面直通档。
        """
        combo = self._pix_fmt_combo
        codec = str(self._codec_combo.currentData() or "")
        tier = _topos_tier_for_codec(codec)
        if tier is None and codec != "hevc":
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
            return
        keep = combo.currentData()
        combo.blockSignals(True)  # 重建不触发 draft 变更
        try:
            combo.clear()
            if tier is not None:
                from src.shared.codec.topos_profiles import (
                    tier_pix_fmt_choices,
                )
                choices = tier_pix_fmt_choices(
                    tier, with_alpha=self._alpha_check.isChecked())
                for name in choices:
                    combo.addItem(_pix_fmt_display_name(name), name)
            else:
                combo.addItem(I18N.tr(
                    "deliver.video.hevc_pix_fmt_8bit",
                    "YUV 4:2:0 8-bit"), "yuv420p")
                combo.addItem(I18N.tr(
                    "deliver.video.hevc_pix_fmt_10bit",
                    "YUV 4:2:0 10-bit"), "yuv420p10le")
            idx = combo.findData(keep) if keep else -1
            if idx < 0:
                idx = 0  # 域内首项（= 默认档）
            combo.setCurrentIndex(idx)
        finally:
            combo.blockSignals(False)

    def _on_codec_changed(self, _idx: int) -> None:
        codec = self._codec_combo.currentData()
        entry = next((e for e in _CODEC_CATALOGUE if e["codec"] == codec), None)
        if entry is None:
            return
        # Refresh the profile combo options（blockSignals：重建过程不触发
        # profile→pix_fmt 联动与 draft 变更，末尾统一 _on_draft_changed）
        self._profile_combo.blockSignals(True)
        try:
            self._profile_combo.clear()
            if codec == "topos_image_sequence":
                # M2-F8：图片档位（单一注册表取名）；默认 422 Ultra
                #（= CLI 无 --tier 默认），保留既有选择。
                keep_profile = self._profile_combo.currentData()
                for label, tid in _topos_image_tier_choices():
                    self._profile_combo.addItem(label, tid)
                idx = self._profile_combo.findData(keep_profile)
                if idx < 0:
                    idx = self._profile_combo.findData(
                        _default_topos_image_tier_id())
                if idx >= 0:
                    self._profile_combo.setCurrentIndex(idx)
            elif codec == "topos_raw":
                # M4-R8：视频 RAW 位深×比率 12 档（与图片线共享注册表）；
                # 默认 raw12-4（家族默认档），保留既有选择。
                keep_profile = self._profile_combo.currentData()
                for label, tid in _topos_raw_tier_choices():
                    self._profile_combo.addItem(label, tid)
                idx = self._profile_combo.findData(keep_profile)
                if idx < 0:
                    idx = self._profile_combo.findData(
                        _default_topos_raw_tier_id())
                if idx >= 0:
                    self._profile_combo.setCurrentIndex(idx)
            else:
                for p in entry["profiles"]:
                    self._profile_combo.addItem(p if p == "auto" else p.capitalize(), p)
                if not entry["profiles"]:
                    self._profile_combo.addItem(I18N.tr("deliver.video.profile_default", "(default)"), "")
        finally:
            self._profile_combo.blockSignals(False)
        profile_visible = bool(entry["profiles"])
        self._profile_label.setVisible(profile_visible)
        self._profile_combo.setVisible(profile_visible)
        quality_visible = bool(entry["crf"])
        self._quality_mode_label.setVisible(quality_visible)
        self._quality_mode_combo.setVisible(quality_visible)
        self._quality_label.setVisible(quality_visible)
        self._quality_value_host.setVisible(quality_visible)
        if quality_visible:
            self._on_quality_mode_changed(self._quality_mode_combo.currentIndex())
        else:
            self._quality_value_spin.setVisible(False)
            self._crf_combo.setVisible(False)
        # Alpha is a real Deliver option, but Resolve only presents it for
        # codecs that can carry alpha (e.g. ProRes 4444/4444 XQ).
        alpha_visible = bool(entry["alpha"])
        self._alpha_label.setVisible(alpha_visible)
        self._alpha_check.setVisible(alpha_visible)
        self._alpha_check.setEnabled(alpha_visible)
        if not alpha_visible:
            self._alpha_check.setChecked(False)

        # GIF has no audio track. Preserve the user's audio choice so
        # switching back to a normal video format is reversible.
        is_gif = codec == "gif"
        was_gif = getattr(self, "_is_gif_export", False)
        if is_gif and not was_gif:
            self._audio_before_gif = self._audio_check.isChecked()
        if not is_gif and was_gif:
            self._audio_check.setChecked(getattr(self, "_audio_before_gif", True))
        self._is_gif_export = is_gif
        self._gif_color_count_label.setVisible(is_gif)
        self._gif_color_count_spin.setVisible(is_gif)
        # R19：图像序列同样不承载音频——与 GIF 同语义禁用（编码器
        # supports_audio=False，导出结果会标注 audio_omitted）。
        is_image_seq = codec in ("png_sequence", "tiff_sequence")
        was_image_seq = getattr(self, "_is_image_seq_export", False)
        if is_image_seq and not was_image_seq:
            self._audio_before_image_seq = self._audio_check.isChecked()
        if not is_image_seq and was_image_seq:
            self._audio_check.setChecked(
                getattr(self, "_audio_before_image_seq", True))
        self._is_image_seq_export = is_image_seq
        no_audio = is_gif or is_image_seq
        self._custom_fps_spin.setRange(1, 100 if is_gif else 240)
        self._audio_check.setEnabled(not no_audio)
        if no_audio:
            self._audio_check.setChecked(False)
            self._audio_check.setToolTip(
                "GIF 输出不包含音频轨道" if is_gif else
                "图像序列不承载音频轨道（audio_omitted）")
        else:
            self._audio_check.setToolTip("")
        # R15/R17：图像序列专属行（位深/帧号/首帧号/覆盖授权）。
        for w in (self._image_bit_depth_label, self._image_bit_depth_combo,
                  self._image_digits_label, self._image_digits_spin,
                  self._image_start_label, self._image_start_spin,
                  self._image_overwrite_label, self._image_overwrite_check):
            w.setVisible(is_image_seq)
        # A programmatic load or a legacy job can still arrive with a stale
        # container.  Repair the container only when the current pair is
        # invalid; normal user changes are handled by the filtered combo.
        if entry["container"] and self._container_combo.currentData() not in allowed_containers_for_codec(codec):
            idx = self._container_combo.findData(entry["container"])
            if idx >= 0:
                self._container_combo.setCurrentIndex(idx)
        # Quality mode sensible default
        if not entry["crf"] and self._quality_mode_combo.currentData() == "crf":
            self._quality_mode_combo.setCurrentIndex(0)  # VBR
        # R7：Topos 专属控件——pix_fmt 选择器（档位格式域）+ Alpha 预算策略；
        # HEVC（2026-09-16）复用 pix_fmt 选择器承载 8/10-bit 交付档
        is_topos = _topos_tier_for_codec(codec) is not None
        is_hevc = str(codec) == "hevc"
        self._refresh_pix_fmt_choices()
        # v1.1 音频轨：topos 容器仅 aac/pcm_s16/pcm_s24（PCM 为制作链路
        # 无损档、AAC 为对外交付档）；PCM 档比特率无关 → 禁用
        self._sync_audio_codec_choices_for_codec(is_topos)
        for w in (self._pix_fmt_label, self._pix_fmt_combo,
                  self._alpha_budget_policy_label,
                  self._alpha_budget_policy_combo):
            w.setVisible(is_topos)
        # v1.6 M-B8：stems 追加音轨仅 topos MOV 容器支持——非 topos 档
        # 隐藏并复位（导出侧对不支持编码器也会整体降级，此处先行收敛）
        stems_visible = is_topos
        self._stems_check.setVisible(stems_visible)
        self._stems_rows_host.setVisible(stems_visible)
        if not stems_visible and self._stems_check.isChecked():
            self._stems_check.setChecked(False)
        if is_hevc:
            self._pix_fmt_label.setVisible(True)
            self._pix_fmt_combo.setVisible(True)
            self._pix_fmt_combo.setToolTip(I18N.tr(
                "deliver.video.hevc_pix_fmt_hint",
                "HEVC delivery bit depth. 8-bit keeps ungraded 4:2:0 "
                "sources on the fast planar passthrough path; 10-bit "
                "(Main10) is the wider-range delivery tier."))
            # 位深档与 profile 选择器对齐（main ↔ 8-bit / main10 ↔ 10-bit）
            self._sync_hevc_profile_with_pix_fmt()
        elif is_topos:
            self._pix_fmt_combo.setToolTip(I18N.tr(
                "deliver.video.topos_pix_fmt_hint",
                "Topos pixel format for this tier. Alpha variants (a8–a16) "
                "fix the alpha bit depth as a format contract; bare formats "
                "probe alpha depth from content."))
        self._on_draft_changed()

    _AUDIO_CODECS_HOST = ("aac", "mp3", "pcm_s16le", "pcm_s24le",
                          "flac", "opus")
    # v1.4：三档 PCM 定位——s16 草稿 / s24 标准 / s32 整数工作流 / f32
    # 母带浮点直通（>0dBFS 余量与亚 LSB 精度保留）；AAC 对外交付。
    _AUDIO_CODECS_TOPOS = ("pcm_s16le", "pcm_s24le", "pcm_s32le",
                           "pcm_f32le", "aac")
    #: v1.4 全家族采样率（topos 容器支持域；非 topos 容器不受限）
    _AUDIO_RATES_TOPOS = (44100, 48000, 88200, 96000, 176400, 192000)

    def _sync_audio_codec_choices_for_codec(self, is_topos: bool) -> None:
        """按目标容器约束音频编解码下拉（v1.4：topos PCM 四档 + AAC）。

        切档保留用户选择（可用则不动、不可用回落列表首项——topos 档
        为 pcm_s16le，M-B4 起 PCM 排在 AAC 前）；从 topos 切回
        其它容器时恢复全量列表。PCM 档比特率无关 → 禁用。采样率下拉
        一并按容器收敛（消除"可选但导出必败"状态，plan M-B4 任务 4）。
        """
        choices = self._AUDIO_CODECS_TOPOS if is_topos else self._AUDIO_CODECS_HOST
        current = self._audio_codec_combo.currentData()
        self._audio_codec_combo.blockSignals(True)
        self._audio_codec_combo.clear()
        for ac in choices:
            self._audio_codec_combo.addItem(ac, ac)
        if current in choices:
            idx = self._audio_codec_combo.findData(current)
            self._audio_codec_combo.setCurrentIndex(max(0, idx))
        self._audio_codec_combo.blockSignals(False)
        pcm_selected = str(
            self._audio_codec_combo.currentData() or "").startswith("pcm")
        self._audio_bitrate_combo.setEnabled(not pcm_selected)
        self._sync_audio_sample_choices_for_codec(is_topos)
        if is_topos:
            self._audio_codec_combo.setToolTip(I18N.tr(
                "deliver.video.topos_audio_hint",
                "PCM：s16 草稿 / s24 标准 / s32 整数工作流 / f32 母带级"
                "浮点直通；AAC 用于对外交付。"))
        else:
            self._audio_codec_combo.setToolTip("")

    def _sync_audio_sample_choices_for_codec(self, is_topos: bool) -> None:
        """采样率下拉按容器收敛（v1.4：topos 全家族 44.1→192kHz）。

        非 topos 容器不做约束（宿主编码器能力面）；topos 下采样率若不在
        支持域则回落 48kHz——与 codec 下拉同机制，避免导出期才被 C 层
        fail-fast 拒绝。
        """
        rates = self._AUDIO_RATES_TOPOS if is_topos else None
        current = self._audio_sample_combo.currentData()
        self._audio_sample_combo.blockSignals(True)
        if rates is not None:
            self._audio_sample_combo.clear()
            for sr in rates:
                self._audio_sample_combo.addItem(f"{sr / 1000:g} kHz", sr)
        if current is not None:
            idx = self._audio_sample_combo.findData(current)
            if idx >= 0:
                self._audio_sample_combo.setCurrentIndex(idx)
            elif is_topos:
                idx = self._audio_sample_combo.findData(48000)
                self._audio_sample_combo.setCurrentIndex(max(0, idx))
        self._audio_sample_combo.blockSignals(False)

    def _on_profile_combo_changed(self, _idx: int) -> None:
        """profile 变更 → HEVC 位深档联动 + draft 变更。"""
        if getattr(self, "_syncing_pix_profile", False):
            return
        codec = str(self._codec_combo.currentData() or "")
        if codec == "hevc":
            profile = str(self._profile_combo.currentData() or "")
            if profile in ("main", "main10"):
                self._select_pix_fmt_for_hevc(
                    "yuv420p" if profile == "main" else "yuv420p10le")
        elif codec == "topos_raw":
            # M4-R8：比率档内含位深 → pix_fmt 跟随（注册表权威，二者
            # 不允许漂移；守卫位防与 _on_pix_fmt_combo_changed 回环）。
            from src.shared.codec.topos_profiles import (
                get_topos_image_tier_or_none,
            )
            raw_tier = get_topos_image_tier_or_none(
                str(self._profile_combo.currentData() or ""))
            if raw_tier is not None and raw_tier.family == "raw":
                combo = self._pix_fmt_combo
                idx = combo.findData(raw_tier.pix_fmt)
                if idx >= 0 and combo.currentData() != raw_tier.pix_fmt:
                    self._syncing_pix_profile = True
                    try:
                        combo.setCurrentIndex(idx)
                    finally:
                        self._syncing_pix_profile = False
        self._on_draft_changed()

    def _on_pix_fmt_combo_changed(self, _idx: int) -> None:
        """pix_fmt 变更 → HEVC profile 联动 + draft 变更（补 R7 联线）。"""
        if getattr(self, "_syncing_pix_profile", False):
            return
        codec = str(self._codec_combo.currentData() or "")
        if codec == "hevc":
            self._sync_hevc_profile_with_pix_fmt()
        self._on_draft_changed()

    def _select_pix_fmt_for_hevc(self, name: str) -> None:
        idx = self._pix_fmt_combo.findData(name)
        if idx < 0 or idx == self._pix_fmt_combo.currentIndex():
            return
        self._syncing_pix_profile = True
        try:
            self._pix_fmt_combo.setCurrentIndex(idx)
        finally:
            self._syncing_pix_profile = False

    def _sync_hevc_profile_with_pix_fmt(self) -> None:
        """HEVC profile 选择器与位深档对齐（main ↔ 8-bit / main10 ↔ 10-bit）。"""
        pix = str(self._pix_fmt_combo.currentData() or "")
        target = "main10" if pix == "yuv420p10le" else "main"
        idx = self._profile_combo.findData(target)
        if idx < 0 or idx == self._profile_combo.currentIndex():
            return
        self._syncing_pix_profile = True
        try:
            self._profile_combo.setCurrentIndex(idx)
        finally:
            self._syncing_pix_profile = False

    def _on_container_changed(self, _idx: int) -> None:
        if not self._syncing_format_options:
            self._refresh_codec_options_for_container()
        self._on_draft_changed()

    def _on_quality_mode_changed(self, _idx: int) -> None:
        """Apply the selected quality representation and its unit."""
        mode = self._normalize_quality_mode(self._quality_mode_combo.currentData())
        previous_mode = self._last_quality_mode
        if previous_mode == "crf" and self._crf_combo.currentData() is not None:
            self._quality_values[previous_mode] = float(self._crf_combo.currentData())
        elif previous_mode in self._quality_values:
            self._quality_values[previous_mode] = self._quality_value_spin.value()

        if mode in ("vbr", "cbr"):
            self._quality_value_spin.setVisible(True)
            self._crf_combo.setVisible(False)
            self._quality_value_spin.setRange(1, self.MAX_BITRATE_KBPS)
            self._quality_value_spin.setSuffix(I18N.tr("deliver.kbps_suffix", " kbps"))
            self._quality_value_spin.setValue(
                self._quality_values.get(mode, self.DEFAULT_BITRATE_KBPS)
            )
            self._quality_label.setText(
                I18N.tr("deliver.video.quality_bitrate", "Bitrate")
            )
            self._quality_value_spin.setToolTip(
                I18N.tr(
                    "deliver.video.quality_cbr_hint"
                    if mode == "cbr"
                    else "deliver.video.quality_bitrate_hint",
                    (
                        "Fixed video bitrate in kbps; minimum and maximum rates are constrained."
                        if mode == "cbr"
                        else "Target average video bitrate in kbps; actual bitrate may vary."
                    ),
                )
            )
        elif mode == "crf":
            self._quality_value_spin.setVisible(False)
            self._crf_combo.setVisible(True)
            self._quality_value_spin.setRange(0, 51)
            self._quality_value_spin.setSuffix("")
            crf_value = int(round(self._quality_values.get("crf", self.DEFAULT_CRF)))
            crf_index = self._crf_combo.findData(crf_value)
            if crf_index < 0:
                self._crf_combo.addItem(f"CRF {crf_value} · Custom", crf_value)
                crf_index = self._crf_combo.findData(crf_value)
            self._crf_combo.setCurrentIndex(crf_index)
            self._quality_label.setText(
                I18N.tr("deliver.video.quality_crf", "Quality (CRF)")
            )
            self._quality_value_spin.setToolTip(
                I18N.tr(
                    "deliver.video.quality_crf_hint",
                    "CRF is unitless; lower values mean higher quality.",
                )
            )
        self._last_quality_mode = mode
        self._quality_value_spin.setEnabled(mode in ("vbr", "cbr"))
        self._estimated_size_label.setVisible(mode in ("vbr", "cbr"))
        self._update_file_size_estimate()
        self._on_draft_changed()

    @staticmethod
    def _normalize_quality_mode(quality_mode: Any) -> str:
        """Map quality modes from older saved jobs to the current UI model."""
        mode = str(quality_mode or "vbr").lower()
        if mode in ("auto", "bitrate"):
            return "vbr"
        return mode

    def _on_crf_changed(self, _idx: int) -> None:
        value = self._crf_combo.currentData()
        if value is not None:
            self._quality_values["crf"] = float(value)
        self._on_draft_changed()

    def _refresh_codec_options_for_container(self, preferred_codec: Optional[str] = None) -> None:
        """Filter encoder choices to codecs legal for the selected format."""
        container = self._container_combo.currentData() or ""
        current_codec = preferred_codec or self._codec_combo.currentData()
        allowed = set(allowed_codecs_for_container(container))
        entries = [entry for entry in _CODEC_CATALOGUE if entry["codec"] in allowed]
        if not entries:
            # Sequence codecs are directory outputs and therefore have no
            # normal container choice; keep the combo usable for unknown or
            # legacy values without inventing a codec/container pairing.
            entries = [entry for entry in _CODEC_CATALOGUE if entry["codec"] == current_codec]

        selected_codec = current_codec if current_codec in {e["codec"] for e in entries} else entries[0]["codec"]
        self._syncing_format_options = True
        try:
            self._codec_combo.blockSignals(True)
            self._codec_combo.clear()
            for entry in entries:
                self._codec_combo.addItem(entry["label"], entry["codec"])
            self._codec_combo.setCurrentIndex(self._codec_combo.findData(selected_codec))
        finally:
            self._codec_combo.blockSignals(False)
            self._syncing_format_options = False
        self._on_codec_changed(self._codec_combo.currentIndex())

    def _on_audio_toggled(self, checked: bool) -> None:
        for w in (self._audio_codec_combo, self._audio_sample_combo, self._audio_bitrate_combo):
            w.setEnabled(checked)
        self._on_draft_changed()

    def _on_timeline_changed(self, _idx: int) -> None:
        # U03（第六轮审计）：combo 选择另一时间线时必须把 draft 的序列
        # 上下文一并切换到完整目标序列，否则 job.timeline_id 与
        # settings._sequence 指向不同时间线，preflight/时长/草稿全部失真。
        selected = self._sequence_for_combo_selection()
        if selected is not None and selected is not self._sequence:
            self.set_sequence(selected)
            return
        self._sync_default_filename()
        self._update_duration_display()
        self._on_draft_changed()

    def _on_aspect_lock_toggled(self, locked: bool) -> None:
        self._aspect_locked = bool(locked)
        if self._aspect_locked and self._height_spin.value() > 0:
            self._aspect_ratio = self._width_spin.value() / self._height_spin.value()

    def _on_width_changed(self, value: float) -> None:
        if not self._aspect_locked or self._syncing_resolution_values:
            return
        if self._aspect_ratio <= 0.0:
            return
        self._syncing_resolution_values = True
        try:
            self._height_spin.setValue(round(float(value) / self._aspect_ratio))
        finally:
            self._syncing_resolution_values = False

    def _on_height_changed(self, value: float) -> None:
        if not self._aspect_locked or self._syncing_resolution_values:
            return
        self._syncing_resolution_values = True
        try:
            self._width_spin.setValue(round(float(value) * self._aspect_ratio))
        finally:
            self._syncing_resolution_values = False

    def _on_filename_changed(self, *_args: Any) -> None:
        if not self._building_ui and not self._updating_default_fields:
            self._filename_follows_timeline = False
        if not self._syncing_naming_fields:
            self._syncing_naming_fields = True
            try:
                self._custom_name_edit.setText(self._filename_edit.text())
            finally:
                self._syncing_naming_fields = False
        self._on_draft_changed()

    def _on_draft_changed(self, *_args: Any) -> None:
        if self._building_ui:
            return
        self._update_file_size_estimate()
        self._apply_draft_to_preview()
        self._refresh_preflight()

    def _on_add_clicked(self) -> None:
        built = self._build_jobs_for_add()
        if built is None:
            return
        jobs, result = built
        if not jobs and result is not None:
            self._show_preflight_block(result)
            return
        if not self._confirm_lossy_color_delivery(result):
            return
        self.add_to_queue_requested.emit(jobs)
        # Clear multi-select after enqueue so the cards don't stay lit
        self._reset_preset_selection()
        # AUD-12404: the editing session ends once the job is enqueued —
        # a later Add must compose a new job instead of replacing the old.
        self.clear_editing()

    def _on_add_and_start_clicked(self) -> None:
        built = self._build_jobs_for_add()
        if built is None:
            return
        jobs, result = built
        if not jobs and result is not None:
            self._show_preflight_block(result)
            return
        if not self._confirm_lossy_color_delivery(result):
            return
        self.add_and_start_requested.emit(jobs)
        self._reset_preset_selection()
        # AUD-12404: symmetric to _on_add_clicked — end the edit session.
        self.clear_editing()

    def _build_jobs_for_add(
        self,
    ) -> Optional[Tuple[List[VideoExportJob], Optional[PreflightResult]]]:
        """Build jobs for the Add buttons (AUD-12405).

        Returns ``None`` when draft construction raised a domain error —
        the error has then been reported to the user and nothing should be
        enqueued. The guard keeps the exception out of the Qt slot, where
        PyQt6 would abort the process.
        """
        try:
            return self.build_jobs(preflight=True)
        except _DRAFT_BUILD_ERRORS as e:
            logger.error("Video export draft build failed on Add: %s", e)
            QMessageBox.warning(
                self,
                I18N.tr(
                    "deliver.video.draft_error_title",
                    "Cannot add export task",
                ),
                I18N.tr(
                    "deliver.video.draft_error_body",
                    "The current export settings are invalid: {error}",
                ).format(error=e),
            )
            self._refresh_preflight()
            return None

    def _confirm_lossy_color_delivery(self, result: Any) -> bool:
        return confirm_lossy_color_delivery(self, result)

    # =========================================================================
    # Internals
    # =========================================================================

    def _select_combo_by_data(self, combo: QComboBox, data: Any) -> None:
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _reset_preset_selection(self) -> None:
        for card in self._preset_cards.values():
            card.set_checked(False)
        self._selected_preset_ids.clear()

    def _apply_preset_to_draft(self, preset: VideoPreset) -> None:
        """Mirror a preset's video/audio settings into the draft controls."""
        self._building_ui = True
        try:
            vc = preset.video_config
            # Format owns the valid encoder list, so select it first.  This
            # matters when switching from the default MP4/H.264 draft to a
            # MOV/ProRes preset: the encoder must not remain H.264 merely
            # because it was selected before the format changed.
            self._select_combo_by_data(self._container_combo, vc.container)
            self._refresh_codec_options_for_container(preferred_codec=vc.codec)
            if vc.profile:
                # AUD-12401: select by stored data, not by display label —
                # the label is capitalised ("High") while presets store the
                # lowercase id, and setCurrentText would silently no-op.
                self._select_combo_by_data(self._profile_combo, vc.profile)
            if vc.resolution is None:
                self._resolution_combo.setCurrentIndex(0)
            else:
                idx = self._resolution_combo.findData(vc.resolution)
                if idx >= 0:
                    self._resolution_combo.setCurrentIndex(idx)
                else:
                    self._select_combo_by_data(self._resolution_combo, "custom")
                    self._width_spin.setValue(vc.resolution[0])
                    self._height_spin.setValue(vc.resolution[1])
            if vc.quality_mode:
                self._select_combo_by_data(
                    self._quality_mode_combo,
                    self._normalize_quality_mode(vc.quality_mode),
                )
            if vc.quality_value is not None:
                self._set_quality_value_from_config(
                    vc.quality_mode,
                    vc.quality_value,
                )
            self._select_combo_by_data(self._color_space_combo, vc.color_space)
            self._select_combo_by_data(self._bit_depth_combo, vc.bit_depth)
            # R17：图像序列作业字段回填（预设/队列重开）。
            self._select_combo_by_data(
                self._image_bit_depth_combo,
                16 if int(getattr(vc, "bit_depth", 8) or 8) >= 16 else 8)
            self._image_digits_spin.setValue(
                max(1, min(10, int(
                    getattr(vc, "image_sequence_digits", 6) or 6))))
            self._image_start_spin.setValue(
                max(0, int(getattr(vc, "image_sequence_start_index", 0) or 0)))
            self._image_overwrite_check.setChecked(
                bool(getattr(vc, "image_sequence_overwrite", False)))
            self._alpha_check.setChecked(vc.alpha)

            ac = preset.audio_config
            self._audio_check.setChecked(ac.include_audio)
            self._select_combo_by_data(self._audio_codec_combo, ac.codec)
            self._select_combo_by_data(self._audio_sample_combo, ac.sample_rate)
            self._select_combo_by_data(self._audio_bitrate_combo, ac.bitrate)
        finally:
            self._building_ui = False
        self._on_draft_changed()

    def _draft_as_preset(self) -> VideoPreset:
        """Synthesise a preset from the current draft (no preset selected)."""
        return VideoPreset(
            preset_id="custom",
            title_key="deliver.preset.custom.title",
            fallback_title="Custom",
            subtitle_key="deliver.preset.custom.subtitle",
            fallback_subtitle="Current settings",
            video_config=self._read_video_config_from_draft(),
            audio_config=self._read_audio_config_from_draft(),
            subtitle_config=self._read_subtitle_config_from_draft(),
        )

    def _set_quality_value_from_config(
        self,
        quality_mode: str,
        quality_value: Optional[int],
    ) -> None:
        """Load stored CRF or bps values into the mode-specific UI units."""
        if quality_value is None:
            return
        mode = self._normalize_quality_mode(quality_mode)
        ui_value = (
            float(quality_value) / 1_000.0
            if mode in ("vbr", "cbr")
            else float(quality_value)
        )
        if mode in self._quality_values:
            self._quality_values[mode] = ui_value
        if mode == "crf":
            crf_value = int(round(ui_value))
            index = self._crf_combo.findData(crf_value)
            if index < 0:
                self._crf_combo.addItem(f"CRF {crf_value} · Custom", crf_value)
                index = self._crf_combo.findData(crf_value)
            self._crf_combo.setCurrentIndex(index)
        else:
            self._quality_value_spin.setValue(ui_value)

    def _read_video_config_from_draft(self) -> VideoConfig:
        resolution = self._read_resolution_from_draft()
        fps = self._read_fps_from_draft()
        quality_mode = self._normalize_quality_mode(self._quality_mode_combo.currentData())
        if quality_mode in ("vbr", "cbr"):
            quality_ui_value = self._quality_value_spin.value()
            quality_value = int(round(quality_ui_value * 1_000))
        else:
            quality_value = int(self._crf_combo.currentData() or self.DEFAULT_CRF)
        codec = self._codec_combo.currentData() or "h264"
        is_topos = _topos_tier_for_codec(codec) is not None
        container_data = self._container_combo.currentData()
        return VideoConfig(
            container="mp4" if container_data is None else str(container_data),
            codec=codec,
            profile=self._profile_combo.currentData() or None,
            resolution=resolution,
            fps=fps,
            quality_mode=quality_mode,
            quality_value=quality_value,
            color_space=self._color_space_combo.currentData() or "rec709",
            bit_depth=(
                (self._image_bit_depth_combo.currentData() or 8)
                if codec in ("png_sequence", "tiff_sequence")
                else (self._bit_depth_combo.currentData() or 8)),
            # R17：图像序列作业字段（codec 为 PNG/TIFF 时位深取图像行）。
            image_sequence_digits=self._image_digits_spin.value(),
            image_sequence_start_index=self._image_start_spin.value(),
            image_sequence_overwrite=self._image_overwrite_check.isChecked(),
            alpha=self._alpha_check.isChecked(),
            gif_color_count=self._gif_color_count_spin.value(),
            # R7：Topos 专属控件——pix_fmt 覆盖与 Alpha 预算策略；
            # HEVC 复用 pix_fmt 选择器承载 8/10-bit 交付档
            pixel_format=(str(self._pix_fmt_combo.currentData() or "")
                          if (is_topos or str(codec) == "hevc")
                          else "yuv420p"),
            alpha_budget_policy=(
                str(self._alpha_budget_policy_combo.currentData() or "record")
                if is_topos else "record"),
        )

    def _refresh_stem_rows(self) -> None:
        """按当前序列的 stem_group 标注重建 stems 行（v1.6 M-B8）。

        序列未就绪/无标注 → 仅显示占位提示；每组一行：组名 + 独立
        codec/声道映射（每轨独立 codec/声道/格式，plan §3 M-B8 任务 4）。
        """
        layout = getattr(self, "_stems_rows_layout", None)
        if layout is None:
            return
        # 清空旧行（占位提示除外——按需显隐）
        for row in list(getattr(self, "_stems_rows", []) or []):
            for w in (row["host"],):
                layout.removeWidget(w)
                w.deleteLater()
        self._stems_rows = []

        groups: Tuple[str, ...] = ()
        sequence = getattr(self, "_sequence", None)
        if sequence is not None:
            try:
                from src.features.edit.core.timeline_export import (
                    auto_detect_stem_groups,
                )
                groups = auto_detect_stem_groups(sequence)
            except Exception:  # noqa: BLE001 — 序列不可用时按无标注处理
                groups = ()

        self._stems_empty_label.setVisible(not groups)
        for group in groups:
            host = QWidget()
            row = QHBoxLayout(host)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)

            codec = ChevronComboBox()
            for ac in ("pcm_s24le", "pcm_s16le", "pcm_s32le",
                       "pcm_f32le", "aac"):
                codec.addItem(ac, ac)
            codec.setCurrentIndex(0)
            apply_combo_dropdown_style(codec)
            codec.currentIndexChanged.connect(lambda _i: self._on_draft_changed())

            channels = ChevronComboBox()
            for ch, label in ((1, "1.0 Mono"), (2, "2.0 Stereo"),
                              (6, "5.1"), (8, "7.1")):
                channels.addItem(label, ch)
            channels.setCurrentIndex(1)
            apply_combo_dropdown_style(channels)
            channels.currentIndexChanged.connect(lambda _i: self._on_draft_changed())

            name_label = QLabel(str(group).capitalize())
            name_label.setToolTip(str(group))
            row.addWidget(name_label)
            row.addWidget(codec, 1)
            row.addWidget(channels)
            layout.addWidget(host)
            self._stems_rows.append({
                "group": str(group),
                "host": host,
                "codec": codec,
                "channels": channels,
            })

    def _stems_enabled(self) -> bool:
        """stems 导出是否启用（复选框勾选且有可映射组）。"""
        return bool(
            getattr(self, "_stems_check", None) is not None
            and self._stems_check.isChecked()
            and self._stems_rows
        )

    def _read_stems_from_draft(self) -> Tuple[StemTrackConfig, ...]:
        if not self._stems_enabled():
            return ()
        return tuple(
            StemTrackConfig(
                stem_group=row["group"],
                name=str(row["group"]).capitalize(),
                codec=str(row["codec"].currentData() or "pcm_s24le"),
                channels=int(row["channels"].currentData() or 2),
            )
            for row in self._stems_rows
        )

    def _read_audio_config_from_draft(self) -> AudioConfig:
        return AudioConfig(
            include_audio=self._audio_check.isChecked(),
            codec=self._audio_codec_combo.currentData() or "aac",
            sample_rate=self._audio_sample_combo.currentData() or 48000,
            bitrate=self._audio_bitrate_combo.currentData() or 192,
            channels=int(self._audio_channels_combo.currentData() or 2),
            stems=self._read_stems_from_draft(),
        )

    def _read_subtitle_config_from_draft(self) -> SubtitleConfig:
        return SubtitleConfig(mode=self._subtitle_combo.currentData() or SubtitleMode.OFF)

    def _read_naming_config_from_draft(self) -> NamingConfig:
        individual = self._is_individual_clips_mode()
        if self._custom_name_radio.isChecked():
            filename_use = FilenameUse.CUSTOM
        else:
            filename_use = FilenameUse.SOURCE if individual else FilenameUse.TIMELINE
        return NamingConfig(
            render_mode=(RenderMode.INDIVIDUAL_CLIPS if individual else RenderMode.SINGLE_CLIP),
            filename_use=filename_use,
            custom_name=self._custom_name_edit.text().strip(),
            file_suffix=self._file_suffix_edit.text().strip(),
            unique_filenames=self._unique_filenames_check.isChecked(),
            unique_filename_position=(
                "prefix" if self._unique_prefix_radio.isChecked() else "suffix"
            ),
            unique_filename_digits=self._unique_digits_spin.value(),
            add_source_frame_count=self._source_frame_count_check.isChecked(),
            frame_handles=self._frame_handles_spin.value(),
        )

    def _read_resolution_from_draft(self) -> Optional[Tuple[int, int]]:
        data = self._resolution_combo.currentData()
        if data is None:
            return None
        if data == "custom":
            return (
                int(round(self._width_spin.value())),
                int(round(self._height_spin.value())),
            )
        return data

    def _read_fps_from_draft(self) -> Optional[float]:
        data = self._fps_combo.currentData()
        if data is None:
            return None
        if data == "custom":
            return float(self._custom_fps_spin.value())
        return data

    def _apply_draft_overrides(self, job: VideoExportJob, preset: VideoPreset) -> None:
        """When the user has manually edited draft fields, override the preset.

        We compare key fields: if the user changed resolution / fps / quality
        / container / audio beyond what the preset specifies, the draft wins.
        For the common case (preset selected, nothing edited) this is a no-op.
        """
        draft_vc = self._read_video_config_from_draft()
        # Video fields: draft wins only if it differs from the preset.  This
        # keeps preset cards convenient while allowing the Resolve-style
        # inspector to make a one-off change to format/encoder/profile.
        if draft_vc.container != preset.video_config.container:
            job.video_config.container = draft_vc.container
        if draft_vc.codec != preset.video_config.codec:
            job.video_config.codec = draft_vc.codec
        if draft_vc.profile != preset.video_config.profile:
            job.video_config.profile = draft_vc.profile
        if draft_vc.resolution != preset.video_config.resolution:
            job.video_config.resolution = draft_vc.resolution
        if draft_vc.fps != preset.video_config.fps:
            job.video_config.fps = draft_vc.fps
        if draft_vc.quality_value != preset.video_config.quality_value:
            job.video_config.quality_value = draft_vc.quality_value
        if draft_vc.quality_mode != preset.video_config.quality_mode:
            job.video_config.quality_mode = draft_vc.quality_mode
        if draft_vc.gif_color_count != preset.video_config.gif_color_count:
            job.video_config.gif_color_count = draft_vc.gif_color_count
        if draft_vc.color_space != preset.video_config.color_space:
            job.video_config.color_space = draft_vc.color_space
        if draft_vc.bit_depth != preset.video_config.bit_depth:
            job.video_config.bit_depth = draft_vc.bit_depth
        if draft_vc.alpha != preset.video_config.alpha:
            job.video_config.alpha = draft_vc.alpha
        if draft_vc.pixel_format != preset.video_config.pixel_format:
            job.video_config.pixel_format = draft_vc.pixel_format
        if draft_vc.alpha_budget_policy != preset.video_config.alpha_budget_policy:
            job.video_config.alpha_budget_policy = draft_vc.alpha_budget_policy
        # Audio: always take the draft (audio is shared across presets)
        job.audio_config = self._read_audio_config_from_draft()
        job.subtitle_config = self._read_subtitle_config_from_draft()
        job.naming_config = self._read_naming_config_from_draft()

    def _refresh_job_output_path(
        self,
        job: VideoExportJob,
        output_dir: str,
        template: str,
        index: int,
    ) -> None:
        """Rebuild the extension after a draft format/encoder override."""
        base = expand_filename_template(
            template,
            project_name=self._project_name,
            timeline_name=job.timeline_name,
            preset_id=job.preset_id,
            codec=job.video_config.codec,
            resolution=job.video_config.resolution,
            fps=job.video_config.fps,
            index=index,
        )
        suffix = job.naming_config.file_suffix
        filename = base + (f"_{suffix}" if suffix else "")
        filename += _container_extension(job.video_config.container, job.video_config.codec)
        job.output_path = os.path.join(output_dir, filename) if output_dir else filename
        job.name = filename

    def _apply_shared_overrides(self, job: VideoExportJob) -> None:
        """In multi-preset mode, only shared fields come from the draft.

        Per-preset video settings (codec/resolution/quality/profile/alpha)
        are left untouched — they come straight from the preset. We only
        override audio, subtitles and (optionally) fps when the user has
        chosen a custom one that should apply uniformly.
        """
        job.audio_config = self._read_audio_config_from_draft()
        job.subtitle_config = self._read_subtitle_config_from_draft()
        job.naming_config = self._read_naming_config_from_draft()
        # FPS: the draft may carry a custom fps the user wants for all
        # outputs. Only override when not "timeline fps" (None) so we don't
        # clobber presets that intentionally follow the timeline.
        draft_fps = self._read_fps_from_draft()
        if draft_fps is not None:
            job.video_config.fps = draft_fps

    def _apply_draft_to_preview(self) -> None:
        """Keep the draft path current without adding a redundant preview row."""
        # The File tab shows the editable filename and location directly;
        # the final path is rebuilt when a job snapshot is created.
        return

    def _update_duration_display(self) -> None:
        """Retained as a compatibility hook; duration is no longer shown here."""
        return

    def _output_duration_seconds(self) -> float:
        """Return the active Deliver range duration in seconds."""
        sequence = self._sequence
        if sequence is None:
            return 0.0

        try:
            total_frames = int(getattr(sequence, "duration_frames", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        if total_frames <= 0:
            return 0.0

        range_type = str(self._range_combo.currentData() or RangeType.ENTIRE)
        if range_type == RangeType.IN_OUT:
            start_value = getattr(sequence, "range_start", None)
            end_value = getattr(sequence, "range_end", None)
            start_frame = 0 if start_value is None else int(start_value)
            end_frame = total_frames - 1 if end_value is None else int(end_value)
        elif range_type == RangeType.CUSTOM:
            start_frame = int(self._start_frame_spin.value())
            end_value = int(self._end_frame_spin.value())
            end_frame = total_frames - 1 if end_value < 0 else end_value
        else:
            start_frame = 0
            end_frame = total_frames - 1

        start_frame = max(0, min(total_frames - 1, start_frame))
        end_frame = max(0, min(total_frames - 1, end_frame))
        if end_frame < start_frame:
            return 0.0

        fps_value = getattr(sequence, "fps", 24.0)
        try:
            if hasattr(fps_value, "to_fps"):
                fps_value = fps_value.to_fps()
            fps = float(fps_value)
        except (TypeError, ValueError):
            return 0.0
        frame_count = end_frame - start_frame + 1
        return frame_count / fps if fps > 0.0 else 0.0

    @staticmethod
    def _format_estimated_size(duration_seconds: float, bitrate_kbps: float) -> str:
        """Format a decimal kbps-based size estimate as MB or GB."""
        if duration_seconds <= 0.0 or bitrate_kbps <= 0.0:
            return "--"
        # kbps / 8 gives kB/s; divide by 1000 for decimal MB.
        size_mb = duration_seconds * bitrate_kbps / 8000.0
        if size_mb >= 1000.0:
            return f"{size_mb / 1000.0:.2f} GB"
        if size_mb >= 10.0:
            return f"{size_mb:.1f} MB"
        return f"{size_mb:.2f} MB"

    def _update_file_size_estimate(self) -> None:
        """Refresh the target-bitrate estimate shown beside the bitrate field."""
        if not hasattr(self, "_estimated_size_label"):
            return
        mode = self._normalize_quality_mode(self._quality_mode_combo.currentData())
        if mode not in ("vbr", "cbr"):
            self._estimated_size_label.setVisible(False)
            return

        size = self._format_estimated_size(
            self._output_duration_seconds(),
            self._quality_value_spin.value(),
        )
        prefix = I18N.tr("deliver.video.estimated_size", "Est. size")
        self._estimated_size_label.setText(f"{prefix}: {size}")
        self._estimated_size_label.setVisible(True)

    def _update_timeline_video_values(self) -> None:
        """Show the resolved timeline FPS/size in the locked fields."""
        seq = self._sequence
        if seq is None:
            return
        fps = getattr(seq, "fps", None)
        if fps is not None:
            try:
                self._fps_combo.setItemText(0, f"{float(fps):g} fps")
            except (TypeError, ValueError):
                pass
        if self._resolution_combo.currentData() is None:
            width = getattr(seq, "width", None)
            height = getattr(seq, "height", None)
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                self._width_spin.setValue(width)
                self._height_spin.setValue(height)

    def _browse_output_dir(self) -> None:
        cur = self._output_dir_edit.text() or self._default_output_dir()
        path = QFileDialog.getExistingDirectory(
            self,
            I18N.tr("deliver.video.select_output_dir", "Select Output Directory"),
            cur,
        )
        if path:
            self._output_dir_edit.setText(path)

    @staticmethod
    def _default_output_dir() -> str:
        """Return the platform's standard Desktop folder for new exports."""
        desktop = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DesktopLocation
        )
        return desktop or os.path.expanduser("~/Desktop")

    def _sync_default_filename(self) -> None:
        """Show the active timeline name until the user enters a custom name."""
        if not self._filename_follows_timeline:
            return

        timeline_name = ""
        if getattr(self, "_timeline_combo", None) is not None:
            if self._timeline_combo.currentData():
                timeline_name = self._timeline_combo.currentText().strip()
        if not timeline_name and self._sequence is not None:
            timeline_name = str(getattr(self._sequence, "name", "") or "").strip()

        self._updating_default_fields = True
        try:
            self._filename_edit.setText(timeline_name)
            self._custom_name_edit.setText(timeline_name)
        finally:
            self._updating_default_fields = False

    def _build_preflight_context(self, job: VideoExportJob) -> PreflightContext:
        ctx_sequence = self._sequence_for_combo_selection()
        return PreflightContext(
            project=self._project,
            sequence=ctx_sequence,
            sequence_resolver=self._sequence_resolver(),
            project_id=self._project_id,
            project_revision=self._project_revision,
            timeline_frame_count=self._effective_timeline_frame_count(ctx_sequence),
            edit_feature_available=True,
        )

    def _effective_timeline_frame_count(self, sequence: Any = None) -> Optional[int]:
        """Resolve the sequence length used by preflight range checks.

        U01（第六轮审计）：``TimelineSequence`` 暴露的是 ``duration_frames``；
        旧代码读取模型上不存在的 ``frame_count``，缺省 0 使任何 Custom 范围
        都被误判为“Start frame 越界（0 frames）”而阻断。这里以
        ``duration_frames`` 为权威字段（``frame_count`` 仅作兼容回退），
        两者都不可得时返回 None（跳过越界检查）而不是臆造 0。
        """
        seq = sequence if sequence is not None else self._sequence
        if seq is None:
            return None
        for attr in ("duration_frames", "frame_count"):
            value = getattr(seq, attr, None)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def _sequence_for_combo_selection(self) -> Any:
        """Return the full TimelineSequence for the combo's selected timeline.

        U03（第六轮审计）：timeline combo 只切换显示值不切换序列时，job 的
        ``timeline_id`` 指向 A 而 draft/preflight 仍用当前 Program 序列 B，
        导出拿到错误内容或轻量 Ref。这里按 combo 选择的 id 解析完整序列；
        解析不到（Ref-only 或未加载）时不得用其他序列冒充，返回 None 由
        preflight 显式阻断。
        """
        timeline_id = ""
        combo = getattr(self, "_timeline_combo", None)
        if combo is not None:
            timeline_id = str(combo.currentData() or "")
        if not timeline_id:
            return None
        seq = (getattr(self, "_timeline_sequences", None) or {}).get(timeline_id)
        if self._is_full_sequence(seq):
            return seq
        # Live sequence is only valid when it IS the selected timeline.
        current = self._sequence
        if (
            self._is_full_sequence(current)
            and str(getattr(current, "timeline_id", "") or "") == timeline_id
        ):
            return current
        return None

    @staticmethod
    def _is_full_sequence(candidate: Any) -> bool:
        """True for full TimelineSequence-like objects (not TimelineSequenceRef).

        完整序列必然带 track 元组字段；轻量 Ref 只有元数据，把它当序列交给
        导出器会在 ``audio_tracks`` 上直接 AttributeError（U03 真实失败模式）。
        """
        return candidate is not None and (
            hasattr(candidate, "video_tracks") or hasattr(candidate, "audio_tracks")
        )

    def _sequence_resolver(self):
        """timeline_id → TimelineSequence 解析（nested 音频展开用，P1-14）。

        与真实导出同一数据源（DeliverPage._timeline_sequences →
        create_timeline_exporter(timeline_sequences=...)）。
        """
        seqs = getattr(self, "_timeline_sequences", None) or {}
        if not seqs:
            return None
        return lambda tid: seqs.get(tid)

    def _refresh_preflight(self) -> None:
        # Build a single representative job (first selected preset or custom)
        # to drive the live preflight indicator. We don't preflight every
        # preset on every keystroke — that's done on Add.
        try:
            jobs, _ = self.build_jobs(preflight=False)
        except _DRAFT_BUILD_ERRORS as e:
            # AUD-12405: domain errors are no longer swallowed — show the
            # reason in the status line instead of silently clearing it,
            # so the user understands why the preview went away.
            logger.warning("Video export draft build failed in preflight preview: %s", e)
            self._preflight_label.setText(f"⚠ {e}")
            self._preflight_label.setStyleSheet("color: #E05A5A; font-size: 12px;")
            return
        if not jobs:
            self._preflight_label.setText("")
            return
        ctx = self._build_preflight_context(jobs[0])
        result = run_video_export_preflight(jobs[0], ctx)
        self.preflight_updated.emit(result)
        self._render_preflight_summary(result)

    def _render_preflight_summary(self, result: PreflightResult) -> None:
        parts: List[str] = []
        color = "#9AA0A6"
        if result.blocking:
            color = "#E05A5A"
            parts.append("✕ " + "  ✕ ".join(b.message for b in result.blocking[:2]))
            if len(result.blocking) > 2:
                parts.append(f"… +{len(result.blocking) - 2} more")
        if result.warnings:
            if color == "#9AA0A6":
                color = "#D59A43"
            parts.append("⚠ " + "  ⚠ ".join(w.message for w in result.warnings[:2]))
            if len(result.warnings) > 2:
                parts.append(f"… +{len(result.warnings) - 2} more")
        if not parts:
            color = "#51B56A"
            parts.append("✓ " + I18N.tr("deliver.video.preflight_ok", "Ready to add"))
        self._preflight_label.setText("   ".join(parts))
        self._preflight_label.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _show_preflight_block(self, result: PreflightResult) -> None:
        # The workspace listens to preflight_updated; here we just emit a
        # fresh copy so any open detail view refreshes.
        self.preflight_updated.emit(result)


# ── Preset card widget ──


class _PresetCard(QFrame):
    """A single clickable preset tile on the preset bar."""

    selection_toggled = pyqtSignal(str, bool)  # preset_id, now_checked

    _STYLE_UNSELECTED = """
        QFrame#PresetCard {
            background-color: #20232A;
            border: 1px solid #3A3F48;
            border-radius: 6px;
        }
        QFrame#PresetCard:hover {
            border-color: #D47A2C;
        }
    """
    _STYLE_SELECTED = """
        QFrame#PresetCard {
            background-color: rgba(212, 122, 44, 0.18);
            border: 1px solid #D47A2C;
            border-radius: 6px;
        }
    """

    def __init__(self, preset: VideoPreset) -> None:
        super().__init__()
        self.preset = preset
        self._checked = False
        self.setObjectName("PresetCard")
        self.setMinimumWidth(150)
        self.setMaximumWidth(220)
        self.setMinimumHeight(64)
        self.setMaximumHeight(72)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(self._STYLE_UNSELECTED)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self._title = QLabel(I18N.tr(preset.title_key, preset.fallback_title))
        self._title.setStyleSheet("color: #E6E8EB; font-size: 12px; font-weight: 600; border: none; background: transparent;")
        layout.addWidget(self._title)

        self._subtitle = QLabel(I18N.tr(preset.subtitle_key, preset.fallback_subtitle))
        self._subtitle.setStyleSheet("color: #9AA0A6; font-size: 10px; border: none; background: transparent;")
        layout.addWidget(self._subtitle)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_checked(not self._checked)

    def set_checked(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self.setStyleSheet(self._STYLE_SELECTED if checked else self._STYLE_UNSELECTED)
        self.selection_toggled.emit(self.preset.preset_id, checked)
