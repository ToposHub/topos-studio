"""
Topos compact audio channel strip.

单个音频轨道的混音器控件，包含：
- 音轨标签
- 静音/独奏/监听按钮
- 音量推子（dB）
- 平衡控制（立体声）
- 电平表
- 效果栈按钮

说明：本控件的 track_id 是字符串（与 EditTrack.track_id 的 UUID 字符串一致），
Master 条用 "-1" 作为保留 id。UI 内部用 dB / balance（-50..50），
而数据模型 EditTrack 用线性增益 volume(0.0-2.0) / pan(-1.0..1.0)，
两者通过下方模块级换算函数转换。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from functools import partial

import logging
import math
import uuid
from typing import TYPE_CHECKING, Optional, Any
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QToolButton, QSpinBox, QDoubleSpinBox,
    QMenu, QInputDialog, QColorDialog, QPushButton,
    QListWidget, QListWidgetItem, QDialog, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QSize
from PyQt6.QtGui import QColor, QAction, QIcon, QPainter, QPixmap, QResizeEvent

from src.shared.ui.audio_mixer.audio_fader import AudioFader
from src.shared.ui.audio_mixer.audio_level_meter import AudioLevelMeter, MeterMode, TickLabelsMode, iec_scale, iec_inverse
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C, MIXER_SPIN_STYLE
from src.shared.track_colors import resolve_track_color

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
    from src.features.mixer.effects.production_dialog import ProductionEffectDialog

logger = logging.getLogger(__name__)


# W26: 效果器目录（effect_type → 列表显示名）。用于内嵌列表 + 添加菜单。
EFFECT_SHORT_NAMES = {
    "strip_eq": "Parametric EQ",
    "limiter": "Limiter",
    "compressor": "Compressor",
    "expander": "Exp/Gate",
    "reverb": "Reverb",
    "delay": "Delay",
    "saturation": "Saturation",
    "imager": "Imager",
    "noise_gate": "Exp/Gate",  # CW-A1: 旧类型沿用 Exp/Gate 编辑器
}

def _effect_default_params(effect_type: str) -> dict:
    """效果器默认参数工厂（延迟 import 避免 timeline_v2 前向引用）。"""
    from src.features.edit.core.timeline_v2 import (
        StripEQParams, LimiterParams, CompressorParams, ReverbParams,
        DelayParams, SaturationParams, ImagerParams, ExpanderParams,
        VoiceChangerParams, SpectralManglerParams,
        _ensure_effect_param_registry,
    )
    if effect_type == "voice_changer":
        return VoiceChangerParams.default().to_dict()
    elif effect_type == "spectral_mangler":
        return SpectralManglerParams.default().to_dict()
    elif effect_type == "strip_eq":
        return StripEQParams.default().to_dict()
    elif effect_type == "limiter":
        return LimiterParams.default().to_dict()
    elif effect_type == "compressor":
        return CompressorParams.default().to_dict()
    elif effect_type == "reverb":
        return ReverbParams.default().to_dict()
    elif effect_type == "delay":
        return DelayParams.default().to_dict()
    elif effect_type == "saturation":
        return SaturationParams.default().to_dict()
    elif effect_type == "imager":
        return ImagerParams.default().to_dict()
    elif effect_type in ("expander", "noise_gate"):
        # CW-A1: Expander/Gate 接替 Noise Gate（旧类型键经 from_dict 无损转 Gate）
        params = ExpanderParams.default().to_dict()
        if effect_type == "noise_gate":
            params["mode"] = "Gate"
        return params
    # P1（2026-11-19 计划第三阶段）：参数注册表统一兜底（新 FX 与后续
    # 扩展无需逐款加分支；DSP DEFAULTS 载体的 default().to_dict()）
    try:
        registry = _ensure_effect_param_registry()
        cls = registry.get(effect_type)
        if cls is not None:
            return cls.default().to_dict()
    except Exception:  # noqa: BLE001 — 注册表不可用回空（DSP 默认兜底）
        pass
    return {}


def _effect_display_name(effect_type: str) -> str:
    """效果器完整显示名（用于菜单/对话框标题）。"""
    names = {
        "dynamics": I18N.tr("audio.fx.dynamics", "Dynamics"),
        "strip_eq": I18N.tr("audio.fx.parametric_eq", "Parametric EQ"),
        "limiter": I18N.tr("audio.fx.limiter", "Limiter"),
        "compressor": I18N.tr("audio.fx.compressor", "Compressor"),
        "expander": I18N.tr("audio.fx.expander_gate", "Expander / Gate"),
        "reverb": I18N.tr("audio.fx.acoustic_reverb", "Acoustic Reverb"),
        "delay": I18N.tr("audio.fx.stereo_delay", "Stereo Delay"),
        "saturation": I18N.tr("audio.fx.saturation", "Saturation"),
        "imager": I18N.tr("audio.fx.imager", "Imager"),
        "noise_gate": I18N.tr("audio.fx.expander_gate", "Expander / Gate"),
        # NF-02：两款新复合 FX（短名 + 完整 tooltip；紧凑轨宽不变）
        "voice_changer": I18N.tr("audio.fx.voice_changer", "Voice"),
        "spectral_mangler": I18N.tr("audio.fx.spectral_mangler", "Mangler"),
        # P1（2026-11-19 计划第三阶段 #1-#7）：新 FX 完整名
        "de_esser": I18N.tr("audio.fx.name.de_esser", "De-Esser"),
        "true_peak_limiter": I18N.tr("audio.fx.name.true_peak_limiter",
                                     "True Peak Limiter"),
        "dynamic_eq": I18N.tr("audio.fx.name.dynamic_eq", "Dynamic EQ"),
        "bus_comp": I18N.tr("audio.fx.name.bus_comp", "Bus Compressor"),
        "multiband_comp": I18N.tr("audio.fx.name.multiband_comp",
                                  "Multiband Compressor"),
        "convolution_reverb": I18N.tr("audio.fx.name.convolution_reverb",
                                      "Convolution Reverb"),
        "transient_designer": I18N.tr("audio.fx.name.transient_designer",
                                      "Transient Designer"),
        "tremolo_auto_pan": I18N.tr("audio.fx.name.tremolo_auto_pan",
                                    "Tremolo / Auto Pan"),
        "chorus_flanger": I18N.tr("audio.fx.name.chorus_flanger",
                                  "Chorus / Flanger"),
        "phaser": I18N.tr("audio.fx.name.phaser", "Phaser"),
    }
    if effect_type in names:
        return names[effect_type]
    # Newly registered processors use their catalog label in the rack too.
    # Falling back to the internal ID made Exciter/Subharmonic look different
    # from the same entries in Add Effect and the audio-effects browser.
    from src.features.edit.core.effect_registry import get_effect_definition

    definition = get_effect_definition(effect_type)
    fallback = definition.name if definition is not None else effect_type
    return I18N.tr(f"audio.fx.name.{effect_type}", fallback)


# P1（2026-11-19 计划第三阶段 #1-#7）：通用表驱动参数对话框覆盖的类型
# （specs 定义在 fx_rack_dialogs._FX_FIELD_SPECS；shared 层字面量保持解耦）
_FX_PARAM_EFFECT_TYPES = frozenset({
    "de_esser", "true_peak_limiter", "dynamic_eq", "bus_comp",
    "multiband_comp", "convolution_reverb", "transient_designer",
    "tremolo_auto_pan", "chorus_flanger", "phaser",
})
_DYNAMICS_STAGES = {"eg": "expander", "comp": "compressor", "lim": "limiter"}
_SPECTRUM_EFFECT_TYPES = frozenset({"strip_eq", "dynamic_eq", "saturation"})
_ChainEdit = Callable[["EffectChainParams | None"], "EffectChainParams | None"]


def _update_track_effect(
    instance: TrackEffectInstance, params: dict, enabled: bool | None = None,
) -> TrackEffectInstance:
    updated = replace(instance, params=params, enabled=instance.enabled if enabled is None else enabled)
    # Dataclass creation interprets 0 as a new instance. Loaded, unversioned
    # effects must retain their original DSP behavior when edited.
    if instance.algorithm_version == 0:
        object.__setattr__(updated, "algorithm_version", 0)
    return updated


# 中性值
NEUTRAL_VOLUME = 0.0
NEUTRAL_BALANCE = 0

# Master 条的保留 track_id
MASTER_TRACK_ID = "-1"

# dB / 线性增益 / balance / pan 的量程边界
# 达芬奇同款：推子 [-60, +6]，0 dB 为 unity（默认位置）。
# +6 dB 上限与数据层 EditTrack.volume / master_volume 的硬约束 2.0 完全一致，
# 使推子顶端对应的线性增益（2.0）能完整传到引擎并持久化，避免视觉/引擎/落库三者不一致。
VOLUME_DB_MIN = -60.0
VOLUME_DB_MAX = 6.0
VOLUME_LINEAR_MIN = 0.0
VOLUME_LINEAR_MAX = 2.0
BALANCE_MIN = -50
BALANCE_MAX = 50


# ==================== 单位换算（模块级）====================

def linear_to_db(v: float) -> float:
    """线性增益(0.0-2.0) → dB，结果 clamp 到 [VOLUME_DB_MIN, VOLUME_DB_MAX]。"""
    v = max(float(v), 1e-6)
    db = 20.0 * math.log10(v)
    return max(VOLUME_DB_MIN, min(VOLUME_DB_MAX, db))


# MX-13: 推子底端契约——VOLUME_DB_MIN 是**硬零端点**（严格静音，-∞ 语义），
# 连续 dB 有效区为 (VOLUME_DB_MIN, VOLUME_DB_MAX]。旧工程的中间线性值映射
# 冻结不变（10^(dB/20)）；只有底端从 0.001 改为严格 0.0——volume=0 在
# 保存/重开/undo 后保持严格 0（模型无 clamp，引擎线性乘 0 = 完全静音）。
def db_to_linear(db: float) -> float:
    """dB → 线性增益。

    - db <= VOLUME_DB_MIN（推子底端/数值输入下限）→ 0.0（硬零端点）
    - (VOLUME_DB_MIN, VOLUME_DB_MAX] → 10^(dB/20)（冻结的连续映射）
    - 结果 clamp 到 [VOLUME_LINEAR_MIN, VOLUME_LINEAR_MAX]。
    """
    db = float(db)
    if db <= VOLUME_DB_MIN:
        return 0.0
    v = 10.0 ** (db / 20.0)
    return max(VOLUME_LINEAR_MIN, min(VOLUME_LINEAR_MAX, v))


def pan_to_balance(pan: float) -> int:
    """声像(-1.0..1.0) → balance 整数([BALANCE_MIN, BALANCE_MAX])。"""
    bal = int(round(max(-1.0, min(1.0, float(pan))) * 50.0))
    return max(BALANCE_MIN, min(BALANCE_MAX, bal))


def balance_to_pan(bal: int) -> float:
    """balance 整数 → 声像(-1.0..1.0)。"""
    bal = max(BALANCE_MIN, min(BALANCE_MAX, int(bal)))
    return bal / 50.0


class TrackMixerWidget(QWidget):
    """单个音频轨道混音器"""

    # Effects 面板拖出的效果 MIME（与 EditEffectsPanel/TimelineView 同名；
    # shared 层不 import features，用字面量保持解耦）。
    _EFFECT_DROP_MIME = "application/x-topos-effect"
    # 插入链可接受的 effect_id（与「添加效果器」菜单及对话框能力一致；
    # 视频效果/转场在此给出 no-drop 反馈）。
    _INSERT_EFFECT_TYPES = frozenset({
        "dynamics", "strip_eq", "reverb", "delay", "saturation", "imager",
        "limiter", "compressor", "expander", "voice_changer",
        "spectral_mangler",
        # P1（2026-11-19 计划第三阶段 #1-#7）：新 FX
        "de_esser", "true_peak_limiter", "dynamic_eq", "bus_comp",
        "multiband_comp", "convolution_reverb", "transient_designer",
        "tremolo_auto_pan", "chorus_flanger", "phaser",
        # P2（2026-11-21 计划 FX-15/FX-16）：激励器 / 次谐波
        "exciter", "subharmonic",
    })

    # 信号（track_id 统一为 str，与 EditTrack.track_id UUID 一致）
    mute_toggled = pyqtSignal(str, bool)      # track_id, muted
    solo_toggled = pyqtSignal(str, bool)      # track_id, solo
    record_arm_toggled = pyqtSignal(str, bool)  # track_id, armed（R 按钮=开始/停止录音开关；arm 待命分层为 MX-18 待办）
    volume_changed = pyqtSignal(str, float)   # track_id, db_value（松手时最终提交）
    balance_changed = pyqtSignal(str, int)    # track_id, balance_value（松手时最终提交）
    # 拖动期间的纯视觉预览（实时听感 + 视觉联动，不进命令栈）
    volume_preview = pyqtSignal(str, float)   # track_id, db_value
    balance_preview = pyqtSignal(str, int)    # track_id, balance_value
    show_effects = pyqtSignal(str)            # track_id
    # W8: Strip EQ 信号（StripEQParams | None）
    strip_eq_preview = pyqtSignal(str, object)   # track_id, StripEQParams|None（实时听感）
    strip_eq_committed = pyqtSignal(str, object)  # track_id, StripEQParams|None（命令栈）
    # W26: FX 机架效果器链信号（EffectChainParams | None）
    track_effects_preview = pyqtSignal(str, object)   # track_id, EffectChainParams|None（实时听感）
    track_effects_committed = pyqtSignal(str, object)  # track_id, EffectChainParams|None（命令栈）
    # W16: 重命名 / 颜色
    name_changed = pyqtSignal(str, str)           # track_id, new_name
    color_changed = pyqtSignal(str, object)       # track_id, color(str|None)
    # W15: 隐藏 strip 请求（上抛给 panel）
    hide_requested = pyqtSignal(str)              # track_id
    # W22: 打开 Automation Lane 请求
    show_automation = pyqtSignal(str)             # track_id
    # AM-08 阶段 4.2: 打开 Routing 对话框请求（上抛给 panel）
    show_routing = pyqtSignal(str)                # track_id
    # CW-A1: 空间声像器信号。payload {"pan": -1..1, "panner": dict}——
    # pan 走 SetTrackPanCommand，panner 走 SetTrackPannerCommand（宿主聚合 Macro）
    panner_preview = pyqtSignal(str, object)    # track_id, payload（拖动期实时听感）
    panner_committed = pyqtSignal(str, object)  # track_id, payload（命令栈）
    # CW-A1: 频谱仪门控（任一 EQ 对话框打开 → True；全部关闭 → False）
    spectrum_metering_changed = pyqtSignal(bool)
    mixer_menu_requested = pyqtSignal(object)  # QMenu: host appends mixer view options

    def __init__(
        self,
        track_id: str,
        track_name: str,
        track_tag: str = "",
        parent: Optional[QWidget] = None,
        is_master: bool = False
    ) -> None:
        super().__init__(parent)
        # 接受 Effects 面板拖来的音频效果（dragEnter 按 MIME+类型过滤）。
        self.setAcceptDrops(True)
        # MX-09: 引擎采样率缓存（set_engine_sample_rate 更新；对话框构造注入）
        self._engine_sample_rate: float = 48000.0
        self._effect_processing_layout = "stereo"
        self._track_id = track_id
        self._track_name = track_name
        self._track_tag = track_tag
        self._is_master = is_master
        self._is_muted = False
        self._is_solo = False
        self._is_armed = False  # Record Arm 状态（占位，录音路由待实现）
        self._volume_db = NEUTRAL_VOLUME
        self._balance = NEUTRAL_BALANCE
        # W8: Strip EQ 当前参数（None=禁用）+ 对话框实例引用
        self._strip_eq: Optional[Any] = None  # StripEQParams | None
        self._eq_dialog: ProductionEffectDialog | None = None
        self._effect_chain: EffectChainParams | None = None
        self._effect_previews: dict[QDialog, _ChainEdit] = {}
        self._limiter_dialogs: dict[str, ProductionEffectDialog] = {}
        self._dynamics_dialog: ProductionEffectDialog | None = None
        self._sidechain_sources: tuple[tuple[str, str], ...] = ()
        # CW-A1: 空间声像器当前状态（pan float + panner dict）+ 对话框实例引用
        self._pan: float = 0.0
        self._panner: dict = {}
        self._panner_dialog: ProductionEffectDialog | None = None
        self._panner_preview_active = False
        # W15: 窄/宽布局模式
        self._is_narrow: bool = False
        self._inserts_visible = True
        # W16: 轨道颜色（hex 字符串，None=默认）
        self._track_color: Optional[str] = None
        # 推子拖动状态（可变闭包，参照 properties 面板三段式节流模式）
        self._volume_dragging = {"active": False}
        self._balance_dragging = {"active": False}

        self._init_ui()
        self._setup_connections()

    # ========== UI 构建 ==========

    def _init_ui(self) -> None:
        self.setContentsMargins(0, 0, 0, 0)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. 达芬奇 Header 区域 (Top Tag + Status Badges + Track Name)
        header_widget = QWidget(self)
        header_widget.setObjectName("mixer_header")
        self._header_widget = header_widget
        header_layout = QVBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 2)
        header_layout.setSpacing(1)

        # 顶部 Track Tag (A1 / Master / Bus1)
        self._track_tag_label = QLabel()
        self._track_tag_label.setFixedHeight(26)
        self._track_tag_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # Track identity spans the full strip so its rule meets the side boundaries.
        tag_text = self._track_tag or ("Master" if self._is_master else "A1")
        self._track_tag_label.setText(tag_text)
        header_layout.addWidget(self._track_tag_label)

        # FX / DY / EQ shortcuts retain their existing effect-chain semantics.
        badge_layout = QHBoxLayout()
        badge_layout.setContentsMargins(0, 0, 0, 0)
        badge_layout.setSpacing(1)

        self._fx_badge = QToolButton()
        self._fx_badge.setText(I18N.tr("audio.mixer.fx_badge", "FX"))
        self._dy_badge = QToolButton()
        self._dy_badge.setText(I18N.tr("audio.mixer.dy_badge", "DY"))
        self._eq_badge = QToolButton()
        self._eq_badge.setText(I18N.tr("audio.mixer.eq_badge", "EQ"))

        for badge in (self._fx_badge, self._dy_badge, self._eq_badge):
            badge.setFixedHeight(18)
            badge.setCursor(Qt.CursorShape.PointingHandCursor)
            badge_layout.addWidget(badge)

        # FX 胶囊：一键全开/全关列表里的效果器。
        # 青色=有启用的效果器；点击：若当前有启用的→全部旁路，否则→全部启用。
        self._fx_badge.clicked.connect(self._on_fx_clicked)
        self._fx_badge.setToolTip(I18N.tr("audio.mixer.fx_rack", "Toggle all FX on/off"))
        # DY 胶囊：点击打开 Dynamics 子面板（Compressor + Noise Gate + Limiter）
        self._dy_badge.clicked.connect(self._open_dynamics_panel)
        self._dy_badge.setToolTip(I18N.tr("audio.mixer.dynamics", "Open Dynamics panel"))
        # EQ 胶囊点击打开 Graphic EQ 单例对话框（快捷入口）。
        # FX 列表里也可添加多个 strip_eq 实例（_open_strip_eq_editor），与此单例入口独立。
        self._eq_badge.clicked.connect(self._open_eq_from_badge)
        self._eq_badge.setToolTip(I18N.tr("audio.mixer.effects", "Open Graphic EQ"))

        self._update_badges_style()
        header_layout.addLayout(badge_layout)

        # One add entry below the rack, independent of list fullness/scroll position.
        self._effects_container = QWidget()
        self._effects_container.setObjectName("mixer_effects")
        effects_layout = QVBoxLayout(self._effects_container)
        effects_layout.setContentsMargins(3, 3, 3, 3)
        effects_layout.setSpacing(2)
        self._effect_list = QListWidget()
        self._effect_list.setObjectName("mixer_inserts")
        self._effect_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._effect_list.setIconSize(QSize(6, 6))
        self._effect_list.setUniformItemSizes(True)
        self._effect_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        # 长效果器名直接遮挡（clip），不触发横向滚动条；垂直滚动条仍按需出现
        self._effect_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._effect_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._effect_list.customContextMenuRequested.connect(self._on_effect_list_context_menu)
        self._effect_list.itemClicked.connect(self._on_effect_item_clicked)
        self._effect_list.itemActivated.connect(self._on_effect_item_clicked)
        for keys, action_name in ((["Space"], "bypass"), (["Delete", "Backspace"], "remove")):
            action = QAction(self._effect_list)
            action.setShortcuts(keys)
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(lambda _checked=False, name=action_name: self._on_effect_key_action(name))
            self._effect_list.addAction(action)
        self._effect_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {C.BG_INPUT}; border: 1px solid {C.PANEL_DIVIDER};
                border-radius: 0; font-size: 10px; color: {C.TEXT_SECONDARY}; outline: none;
            }}
            QListWidget::item {{ padding: 1px 2px; border-radius: 0; }}
            QListWidget::item:selected {{ background-color: {C.BG_BUTTON}; color: {C.TEXT_PRIMARY}; }}
            QListWidget::item:hover {{ background-color: {C.BG_ACTIVE}; }}
            QScrollBar:vertical {{ width: 4px; background: transparent; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {C.BORDER_MID}; border-radius: 2px; min-height: 12px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        effects_layout.addWidget(self._effect_list)
        self._add_effect_btn = QToolButton()
        self._add_effect_btn.setText("+")
        self._add_effect_btn.setFixedHeight(18)
        self._add_effect_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._add_effect_btn.setAccessibleName(I18N.tr("audio.mixer.add_effect", "Add effect"))
        self._add_effect_btn.setToolTip(I18N.tr("audio.mixer.add_effect", "Add effect"))
        self._add_effect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_effect_btn.setStyleSheet(f"""
            QToolButton {{ color: {C.TEXT_LABEL}; background: {C.BG_INPUT};
                border: 1px solid {C.PANEL_DIVIDER}; border-radius: 0; padding: 0; }}
            QToolButton:hover, QToolButton:focus {{ color: {C.TEXT_PRIMARY}; background: {C.BG_ACTIVE}; }}
        """)
        self._add_effect_btn.clicked.connect(self._show_add_effect_menu)
        effects_layout.addWidget(self._add_effect_btn)
        self._refresh_effect_list()

        # 轨道名称标签（如 "音频 1"）
        self._track_label = QLabel()
        self._track_label.setFixedHeight(16)
        self._track_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._update_track_label()
        for label in (self._track_label, self._track_tag_label):
            label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            label.customContextMenuRequested.connect(self._on_label_context_menu)
        if not self._is_master:
            # 右键菜单和双击重命名同时挂到 _track_label 和 _track_tag_label，
            # 确保窄模式下 _track_label 隐藏后，用户仍可通过 tag 标签（始终可见）
            # 访问重命名/设色/隐藏/automation 菜单。
            self._track_label.mouseDoubleClickEvent = self._on_label_double_clicked
            self._track_tag_label.mouseDoubleClickEvent = self._on_label_double_clicked
        header_layout.insertWidget(1, self._track_label)

        main_layout.addWidget(header_widget)
        main_layout.addWidget(self._effects_container)

        # 2. 按钮行 (M / S / R / FX)
        self._button_section = QWidget()
        self._button_section.setObjectName("mixer_buttons")
        button_layout = QHBoxLayout(self._button_section)
        button_layout.setSpacing(1)
        button_layout.setContentsMargins(3, 4, 3, 4)
        button_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        # 静音按钮 (M)
        self._mute_btn = QToolButton(self)
        self._mute_btn.setCheckable(True)
        self._mute_btn.setFixedSize(20, 20)
        self._mute_btn.setToolTip(I18N.tr("audio.mixer.mute", "Mute/Unmute track"))
        self._mute_btn.setText(I18N.tr("audio.mixer.mute_short", "M"))
        self._mute_btn.setObjectName("mute_btn")
        button_layout.addWidget(self._mute_btn)

        if not self._is_master:
            # 独奏按钮 (S)
            self._solo_btn = QToolButton(self)
            self._solo_btn.setCheckable(True)
            self._solo_btn.setFixedSize(20, 20)
            self._solo_btn.setToolTip(I18N.tr("audio.mixer.solo", "Solo mode"))
            self._solo_btn.setText(I18N.tr("audio.mixer.solo_short", "S"))
            self._solo_btn.setObjectName("solo_btn")
            button_layout.addWidget(self._solo_btn)

            # 监听按钮 (R)
            self._monitor_btn = QToolButton(self)
            self._monitor_btn.setCheckable(True)
            self._monitor_btn.setFixedSize(20, 20)
            # AM-11: 录音路由未实现前禁用 R 按钮，避免"可点击但伪装成功"的虚假入口。
            # tooltip 明确标注状态；功能实现后把 RECORD_ARM_IMPLEMENTED 置 True 即恢复可用。
            from src.shared.ui.audio_mixer.mixer_feature_flags import RECORD_ARM_IMPLEMENTED
            if RECORD_ARM_IMPLEMENTED:
                self._monitor_btn.setToolTip(I18N.tr(
                    "audio.mixer.record_arm",
                    "Record Arm",
                ))
            else:
                self._monitor_btn.setEnabled(False)
                self._monitor_btn.setToolTip(I18N.tr(
                    "audio.mixer.record_arm_not_implemented",
                    "Record Arm（录音功能尚未实现，敬请期待）",
                ))
            self._monitor_btn.setText(I18N.tr("audio.mixer.record_short", "R"))
            self._monitor_btn.setObjectName("monitor_btn")
            button_layout.addWidget(self._monitor_btn)

        # 注：原 FX 按钮已移除，EQ 功能迁移到 header 的 EQ 胶囊（_eq_badge）。
        # Center the compact controls beneath the wider, readable rack header.
        main_layout.addWidget(self._button_section)

        # 平衡控制（仅立体声）
        self._balance_widget = self._create_balance_widget()
        main_layout.addWidget(self._balance_widget)

        # 电平表 + 推子
        self._meter_section = QWidget()
        self._meter_section.setObjectName("mixer_meter")
        meter_fader_layout = QHBoxLayout(self._meter_section)
        # 电平表贴 strip 左缘（左内边距 0），与 Master 保持一致。
        # 右侧留 1px 内边距给推子刻度。
        meter_fader_layout.setContentsMargins(2, 4, 2, 4)
        meter_fader_layout.setSpacing(3)
        meter_fader_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._meter = AudioLevelMeter(
            self,
            Qt.Orientation.Vertical,
            # 电平表左侧不显示 dB 刻度，由右侧推子刻度提供参考（与 Master 一致）。
            TickLabelsMode.HIDE,
            show_clipping=True,
            channels=2,
            max_db=0.0  # 0 dBFS 顶格：满量程=0dB，>0dB 靠 clip 指示器报红。
            # 与电平表内部 set_audio_values 的 clamp [-100, 0] 一致，消除顶部死区。
        )
        from src.shared.ui.audio_mixer.audio_level_meter import LevelStyle
        self._meter._level_style = LevelStyle.GRADIENT
        self._meter._draw_block_lines = False
        meter_fader_layout.addWidget(self._meter, 0)

        # 音量推子
        # 0 dB = unity，对应 slider 8000（新 IEC 对齐映射下）。
        self._volume_fader = AudioFader(
            Qt.Orientation.Vertical,
            self,
            narrow_knob=False,
            neutral_position=8000
        )
        self._volume_fader.setRange(0, 10000)
        self._volume_fader.setValue(8000)
        # 注：不再 setFixedWidth(28) —— 紧接的 set_tick_positions 会按
        # KNOB_WIDTH + max_label_width + margin 重算固定宽度（audio_fader.py:95-97），
        # 此处设置会被覆盖，保留只会误导维护。

        # 设置 dB 刻度和标签（达芬奇同款 0dBFS 顶格刻度）
        # 负值刻度密集（专业混音常用拐点 -6/-12/-20/-30），正值 +3/+6 给增益裕量参考。
        gain_values = [
            -60.0, -50.0, -40.0, -30.0, -20.0, -12.0, -6.0, 0.0, 3.0, 6.0
        ]
        self._volume_fader.set_tick_positions(gain_values)
        self._volume_fader.set_label_formatter(lambda v: f"{int(v)}" if v <= 0 else f"+{int(v)}")
        self._volume_fader.set_value_to_slider_function(lambda db: self._db_to_slider(db))

        meter_fader_layout.addWidget(self._volume_fader, 0)
        # Keep meter and fader together; center the pair without stretching its gap.
        main_layout.addWidget(self._meter_section, 1)

        # 音量显示
        self._gain_section = QWidget()
        self._gain_section.setObjectName("mixer_gain")
        vol_display_layout = QHBoxLayout(self._gain_section)
        vol_display_layout.setContentsMargins(0, 1, 0, 1)
        vol_display_layout.setSpacing(1)

        self._volume_spin = QDoubleSpinBox(self)
        self._volume_spin.setRange(VOLUME_DB_MIN, VOLUME_DB_MAX)
        self._volume_spin.setDecimals(1)
        self._volume_spin.setSingleStep(0.5)
        self._volume_spin.setValue(0.0)
        self._volume_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._volume_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._volume_spin.setFixedWidth(46)
        self._volume_spin.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._volume_spin.setKeyboardTracking(False)
        self._volume_spin.setToolTip(I18N.tr("audio.mixer.gain_tip", "Channel gain (dB) · double-click the fader to reset"))
        self._volume_fader.setAccessibleName(I18N.tr("audio.mixer.gain", "Channel gain"))
        vol_display_layout.addStretch()
        vol_display_layout.addWidget(self._volume_spin)

        self._db_label = QLabel(I18N.tr("audio.mixer.db_unit", "dB"))
        self._db_label.setStyleSheet("background: transparent; border: none; color: #66666e; font-size: 9px;")
        self._db_label.setFixedWidth(14)
        vol_display_layout.addWidget(self._db_label)
        vol_display_layout.addStretch()

        main_layout.addWidget(self._gain_section)

        self._update_size_constraints()
        self._apply_styles()

    def _create_balance_widget(self) -> QWidget:
        """创建平衡控制"""
        widget = QWidget()
        widget.setObjectName("mixer_balance")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(1)

        # 平衡推子（水平窄滑块）
        self._balance_fader = AudioFader(
            Qt.Orientation.Horizontal,
            self,
            narrow_knob=True,
            neutral_position=0
        )
        self._balance_fader.setRange(-50, 50)
        self._balance_fader.setValue(0)
        self._balance_fader.setFixedHeight(12)
        layout.addWidget(self._balance_fader)

        # L/C/R 标签
        label_layout = QHBoxLayout()
        label_layout.setContentsMargins(0, 0, 0, 0)
        label_layout.setSpacing(1)

        # L/R 标签字号略小于标签栏（8px），避免与 10px tag 文字视觉冲突、过于突出。
        l_label = QLabel(I18N.tr("audio.mixer.left_short", "L"))
        l_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        l_label.setStyleSheet("color: #88888f; font-size: 8px;")

        self._balance_spin = QSpinBox(self)
        self._balance_spin.setRange(-50, 50)
        self._balance_spin.setValue(0)
        self._balance_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._balance_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._balance_spin.setFixedWidth(40)
        self._balance_spin.setFixedHeight(17)
        self._balance_spin.setKeyboardTracking(False)
        self._balance_spin.setToolTip(I18N.tr("audio.mixer.balance_tip", "Stereo balance · 0 is center"))

        r_label = QLabel(I18N.tr("audio.mixer.right_short", "R"))
        r_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        r_label.setStyleSheet("color: #88888f; font-size: 8px;")

        label_layout.addWidget(l_label)
        label_layout.addWidget(self._balance_spin)
        label_layout.addWidget(r_label)

        # CW-A1: ⤢ 展开按钮 → 空间声像器对话框（立体声域：spread/rotate/
        # divergence/boom 真实可听；F/B、高度、制式持久化待多声道引擎）
        self._panner_expand_btn = QPushButton("⤢")
        self._panner_expand_btn.setFixedSize(18, 16)
        self._panner_expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._panner_expand_btn.setToolTip(
            I18N.tr("audio.fx.open_panner_tip", "打开空间声像器 (Spatial Panner)"))
        self._panner_expand_btn.clicked.connect(self._open_panner_dialog)
        label_layout.addWidget(self._panner_expand_btn)
        layout.addLayout(label_layout)

        if self._is_master:
            # The project and playback graph have pan/panner state for tracks
            # and buses only. Master edits otherwise appear to apply locally
            # without reaching either commands or audio processing.
            widget.setEnabled(False)
            widget.setToolTip(I18N.tr(
                "audio.mixer.panner_track_bus_only",
                "Spatial panning is available on audio tracks and buses.",
            ))

        return widget

    def _apply_styles(self) -> None:
        """Neutral shared surfaces, bounded by full-width black functional rules."""
        self.setStyleSheet(f"""
            TrackMixerWidget {{ background-color: {C.BG_PANEL}; }}
            QWidget#mixer_header, QWidget#mixer_effects, QWidget#mixer_buttons,
            QWidget#mixer_balance, QWidget#mixer_meter {{
                background-color: {C.BG_PANEL}; border: none;
                border-bottom: 1px solid {C.PANEL_DIVIDER};
            }}
            QWidget#mixer_gain {{ background-color: {C.BG_DARK}; border: none; }}
        """)
        self._update_panner_indicator()
        for name, accent in (("_mute_btn", "#dfa359"), ("_solo_btn", "#72d6b1"),
                             ("_monitor_btn", "#ed7b82")):
            button = getattr(self, name, None)
            if button is not None:
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setStyleSheet(f"""
                    QToolButton {{ background: {C.BG_ACTIVE}; color: {C.TEXT_SECONDARY};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                        font-size: 10px; font-weight: 600; padding: 0; }}
                    QToolButton:hover, QToolButton:focus {{ border-color: {C.BORDER_HOVER}; color: {C.TEXT_PRIMARY}; }}
                    QToolButton:checked {{ color: {C.BG_DEEPEST}; background: {accent}; border-color: {accent}; }}
                    QToolButton:disabled {{ color: {C.TEXT_HINT}; border-color: {C.BORDER_DARK}; }}
                """)
        # 注：_effects_btn 已移除，EQ 入口改由 header 的 _eq_badge 胶囊承担。

        # 轨道标签样式
        self._update_track_label_style()

        # SpinBox 样式（使用集中定义）
        self._volume_spin.setStyleSheet(MIXER_SPIN_STYLE)
        self._balance_spin.setStyleSheet(MIXER_SPIN_STYLE)

    def _update_size_constraints(self) -> None:
        """更新尺寸约束（统一 Master 与轨道 strip 宽度）。

        只用确定性数值（电表固定宽 + 推子固定宽 + 按钮行固定宽），
        不依赖 sizeHint（布局前不可靠），确保 Master 和轨道 strip 算出完全相同的值。
        """
        margins = self.contentsMargins()
        h_margins = margins.left() + margins.right()

        # 电表 + 推子（两者都有 setFixedWidth，maximumWidth 可靠）
        fader_w = self._volume_fader.maximumWidth()
        if fader_w <= 0:
            fader_w = self._volume_fader.width()
        # 2px side insets and 3px spacing keep the scale inside its section.
        meter_fader_w = self._meter.maximumWidth() + 3 + fader_w + 4

        btn_row_w = 20 * 3 + 2 * 1 + 2 * 3

        content_width = max(meter_fader_w, btn_row_w, 88)
        strip_width = content_width + h_margins
        self.setFixedWidth(strip_width)

    def set_narrow(self, narrow: bool) -> None:
        """W15: 切换窄/宽布局。

        窄布局：隐藏标签文字（只显示 tag）、隐藏 balance/spinbox、推子用 narrow knob
        宽布局：完整显示所有控件
        """
        self._is_narrow = narrow
        # 推子 knob 样式
        if hasattr(self, '_volume_fader'):
            self._volume_fader.set_narrow_knob(narrow)
        # 标签：窄模式只显示 tag
        self._update_track_label()
        # balance widget 在窄模式下隐藏
        if hasattr(self, '_balance_widget'):
            self._balance_widget.setVisible(not narrow)
        # 调整宽度（窄模式用 setFixedWidth 同时设 min/max，避免 min>max 的非法约束）
        if narrow:
            self.setFixedWidth(70)
        else:
            self._update_size_constraints()
        self._refresh_effect_list()
        self.update()

    def set_inserts_visible(self, visible: bool) -> None:
        """Show or hide only the rack, preserving its effects and open editors."""
        self._inserts_visible = visible
        self._effects_container.setVisible(visible)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_track_label"):
            self._update_track_label()

    def _setup_connections(self) -> None:
        """设置信号连接（三段式节流：拖动期发 preview，松手发 changed）"""
        self._mute_btn.toggled.connect(lambda checked: self._on_mute_toggled(checked))
        if hasattr(self, '_solo_btn'):
            self._solo_btn.toggled.connect(lambda checked: self._on_solo_toggled(checked))
        if hasattr(self, '_monitor_btn'):
            self._monitor_btn.toggled.connect(lambda checked: self._on_record_arm_toggled(checked))
        # 注：_effects_btn.clicked 连接已移除，EQ 入口在 _init_ui 中连接到 _eq_badge。

        # 音量推子：三段式节流
        #   slider_pressed/released 是 AudioFader 自定义信号（mousePress/Release 时 emit）
        #   拖动期 valueChanged → 只发 volume_preview（实时听感+视觉，不进命令栈）
        #   松手 slider_released → 发 volume_changed（最终值，触发命令提交一次）
        #   非拖动（spinbox 输入、外部回填）valueChanged → 直接发 volume_changed
        self._volume_fader.slider_pressed.connect(self._on_volume_pressed)
        self._volume_fader.slider_released.connect(self._on_volume_released)
        self._volume_fader.valueChanged.connect(self._on_volume_changed)

        # 音量 SpinBox（键盘输入，无拖动概念，直接提交）
        self._volume_spin.valueChanged.connect(self._on_volume_spin_changed)

        # 平衡推子：同样三段式
        if hasattr(self, '_balance_fader'):
            self._balance_fader.slider_pressed.connect(self._on_balance_pressed)
            self._balance_fader.slider_released.connect(self._on_balance_released)
            self._balance_fader.valueChanged.connect(self._on_balance_changed)
            # AUD-37901: 不再把 valueChanged 直连 _balance_spin.setValue ——
            # 直连会让拖动期 spinbox 的 valueChanged 触发
            # _on_balance_spin_changed（commit 出口），balance_changed
            # （commit 信号）在拖动期泄露进命令栈且松手重复提交。
            # spinbox 视觉同步改在 _on_balance_changed 内 blockSignals 完成
            # （与音量侧 _on_volume_changed 的既有范式一致）。

        # 平衡 SpinBox（键盘输入，直接提交）
        if hasattr(self, '_balance_spin'):
            self._balance_spin.valueChanged.connect(self._on_balance_spin_changed)

    # ========== 状态更新 ==========

    def set_track_name(self, name: str) -> None:
        self._track_name = name
        self._update_track_label()

    def set_track_color(self, color: Optional[str]) -> None:
        """W16: 设置 strip 颜色（hex 字符串如 '#ff5722'，None=默认）。"""
        self._track_color = color
        self._update_track_label_style()

    def set_mute(self, mute: bool) -> None:
        self._is_muted = mute
        self._mute_btn.blockSignals(True)
        self._mute_btn.setChecked(mute)
        self._mute_btn.blockSignals(False)
        self._update_controls_enabled()
        self._update_track_label_style()

    def is_mute(self) -> bool:
        return self._is_muted

    def set_solo(self, solo: bool) -> None:
        if hasattr(self, '_solo_btn'):
            self._is_solo = solo
            self._solo_btn.blockSignals(True)
            self._solo_btn.setChecked(solo)
            self._solo_btn.blockSignals(False)
            self._update_track_label_style()

    def set_armed(self, armed: bool) -> None:
        """外部静默回填 Record Arm 状态（panel 独占 arm 联动用）。"""
        if hasattr(self, '_monitor_btn'):
            self._is_armed = armed
            self._monitor_btn.blockSignals(True)
            self._monitor_btn.setChecked(armed)
            self._monitor_btn.blockSignals(False)
            self._update_track_label_style()

    # ========== 外部静默回填（用于 timeline 变化后同步显示，避免信号回环）==========

    def set_volume_linear(self, vol: float) -> None:
        """用线性增益(0.0-2.0)静默回填推子+spinbox，不发任何信号。

        供 controller 在 timeline 变化后把 EditTrack.volume 同步到 UI。
        """
        self._set_volume_db(linear_to_db(vol))

    def _set_volume_db(self, db: float) -> None:
        slider_val = self._db_to_slider(db)
        self._volume_db = db
        self._volume_fader.blockSignals(True)
        self._volume_fader.setValue(slider_val)
        self._volume_fader.blockSignals(False)
        self._volume_spin.blockSignals(True)
        self._volume_spin.setValue(round(db, 1))
        self._volume_spin.blockSignals(False)

    def set_pan_linear(self, pan: float) -> None:
        """用声像(-1.0..1.0)静默回填 balance 推子+spinbox，不发任何信号。"""
        bal = pan_to_balance(pan)
        self._balance = bal
        if hasattr(self, '_balance_fader'):
            self._balance_fader.blockSignals(True)
            self._balance_fader.setValue(bal)
            self._balance_fader.blockSignals(False)
        if hasattr(self, '_balance_spin'):
            self._balance_spin.blockSignals(True)
            self._balance_spin.setValue(bal)
            self._balance_spin.blockSignals(False)

    def get_volume_linear(self) -> float:
        """返回当前推子对应的线性增益(0.0-2.0)。"""
        return db_to_linear(self._volume_db)

    def get_pan_linear(self) -> float:
        """返回当前 balance 对应的声像(-1.0..1.0)。"""
        return balance_to_pan(self._balance)

    def set_strip_eq(self, params: Any) -> None:
        """外部静默回填 Strip EQ 参数（legacy 字段，仅用于 badge 高亮）。

        注意：不再回填已打开的 EQ 对话框。EQ 的真相源是 effect_chain 里的
        strip_eq 实例（panel="eq"），对话框状态由用户操作驱动。legacy 的
        track.strip_eq 字段可能与新 chain 不同步，回填会导致用户拖拽的值被覆盖。

        Args:
            params: StripEQParams | None
        """
        self._strip_eq = params
        if hasattr(self, '_eq_badge'):
            self._update_badges_style()

    def _open_eq_from_badge(self) -> None:
        """Open the channel's own EQ without creating an insert until Apply."""
        from src.features.mixer.effects.production_dialog import ProductionEffectDialog
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, StripEQParams, TrackEffectInstance,
        )
        if self._eq_dialog is not None and self._eq_dialog.isVisible():
            self._eq_dialog.raise_()
            self._eq_dialog.activateWindow()
            return
        effects = self._effect_chain.effects if self._effect_chain else ()
        existing = next(
            (instance for instance in effects
             if instance.effect_type == "strip_eq" and instance.panel == "eq"), None,
        )
        initial = existing.params if existing else StripEQParams.default().to_dict()
        draft = existing or TrackEffectInstance("strip_eq", initial, panel="eq")
        dialog = ProductionEffectDialog(
            "strip_eq", self._track_name, initial, self.window(),
            enabled=existing.enabled if existing else True,
        )
        dialog._instance_id = existing.instance_id if existing else ""
        self._eq_dialog = dialog
        self._configure_eq_plot(dialog, draft.algorithm_version)

        def edit(params: dict, commit: bool) -> None:
            if self._eq_dialog is not dialog:
                return
            power = dialog.power_override
            can_create = not dialog._instance_id

            def change(chain: EffectChainParams | None) -> EffectChainParams | None:
                current = chain.effects if chain else ()
                if any(instance.instance_id == draft.instance_id for instance in current):
                    return self._chain_with_insert_edit(chain, draft, params, power)
                if not can_create or any(instance.panel == "eq" for instance in current):
                    return chain
                order = max((instance.order for instance in current), default=-1) + 1
                inserted = _update_track_effect(draft, params, power)
                return EffectChainParams((*current, replace(inserted, order=order)))

            if commit and can_create:
                dialog._instance_id = draft.instance_id
            self._apply_effect_window_edit(dialog, change, commit=commit)

        dialog.preview.connect(lambda params: edit(params, False))
        dialog.committed.connect(lambda params: edit(params, True))
        self._show_effect_dialog(dialog)
        self._update_spectrum_gating()

    def _on_eq_preview_from_badge(self, eq_params: Any) -> None:
        """EQ 对话框拖动期预览：本地更新/惰性创建 chain 实例 + preview 信号。

        首次拖动即创建实例（panel="eq"），保证第一下调整就实时可听；
        命令栈仍只在 committed 信号写入（松手/预设/开关）。
        """
        chain = self._rebuild_chain_with_eq(eq_params, create=True)
        if chain is None:
            return
        self._effect_chain = chain
        self._strip_eq = eq_params
        self._update_badges_style()
        self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _rebuild_chain_with_eq(self, eq_params: Any, create: bool = False) -> Any:
        """把 EQ 胶囊实例替换为给定参数（create=True 时惰性新建）。"""
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        effects = list(self._effect_chain.effects) if self._effect_chain is not None else []
        new_params = eq_params.to_dict() if eq_params is not None else None
        idx = next(
            (i for i, e in enumerate(effects)
             if e.effect_type == "strip_eq" and getattr(e, "panel", "fx") == "eq"),
            -1,
        )
        if idx >= 0:
            old = effects[idx]
            effects[idx] = replace(
                old, params=new_params or old.params,
                enabled=bool(getattr(eq_params, 'enabled', old.enabled)),
            )
        elif create:
            new_order = max((e.order for e in effects), default=-1) + 1
            effects.append(TrackEffectInstance(
                effect_type="strip_eq", params=new_params or {},
                enabled=bool(getattr(eq_params, 'enabled', True)),
                order=new_order, panel="eq",
            ))
        else:
            return None
        return EffectChainParams(effects=tuple(effects))

    def _on_eq_committed_from_badge(self, track_id: str, eq_params: Any) -> None:
        """EQ 对话框提交：更新 chain 中 EQ 胶囊管理的 strip_eq 实例 + emit。

        R06: 外部已移除 panel="eq" 实例（且窗口未被回填关闭的竞态窗口内）
        的迟到提交不再复活该实例——只允许"链里已有实例的更新"与
        "对话框绑定实例 id 的首次创建"两种路径。
        """
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        effects = list(self._effect_chain.effects) if self._effect_chain is not None else []
        new_params = eq_params.to_dict() if eq_params is not None else None
        # 只匹配 EQ 胶囊自己的实例（panel="eq"），不碰 FX 列表里的 strip_eq
        idx = next(
            (i for i, e in enumerate(effects)
             if e.effect_type == "strip_eq" and getattr(e, "panel", "fx") == "eq"),
            -1,
        )
        if idx >= 0:
            old = effects[idx]
            if new_params is not None:
                effects[idx] = replace(
                    old, params=new_params,
                    enabled=bool(getattr(eq_params, 'enabled', old.enabled)),
                )
                # 同步对话框的 instance_id（首次提交新建实例后回填）
                if self._eq_dialog is not None and not getattr(self._eq_dialog, "_instance_id", ""):
                    self._eq_dialog._instance_id = old.instance_id
            else:
                # None 表示禁用 → 从 chain 移除 EQ
                effects.pop(idx)
        elif new_params is not None:
            # R06 守卫：链里已无 EQ 实例时，仅当对话框绑定过实例 id
            # （=它创建过该实例，实例随后被外部移除）才拒绝迟到提交，
            # 避免旧窗把已删除的效果重新写回链。
            bound_id = getattr(self._eq_dialog, "_instance_id", "") \
                if self._eq_dialog is not None else ""
            if bound_id and not self._instance_in_chain(bound_id):
                logger.info(
                    "R06: EQ 窗口迟到提交被拒绝（实例 %s 已被外部移除，"
                    "track=%s）", bound_id, self._track_id)
                self._update_badges_style()
                return
            # 创建新 strip_eq 实例（追加到链尾，EQ 胶囊管理）
            new_order = max((e.order for e in effects), default=-1) + 1
            effects.append(TrackEffectInstance(
                effect_type="strip_eq", params=new_params,
                enabled=bool(getattr(eq_params, 'enabled', True)),
                order=new_order, panel="eq",
            ))
        self._effect_chain = EffectChainParams(effects=tuple(effects)) if effects else None
        # 同步旧 strip_eq 字段（兼容 set_strip_eq 路径 + badges_style）
        self._strip_eq = eq_params
        self._update_badges_style()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    def _open_dynamics_panel(self) -> None:
        """Open the new Dynamics panel over the existing three-stage channel chain."""
        from src.features.mixer.effects.production_dialog import ProductionEffectDialog

        if self._dynamics_dialog is not None and self._dynamics_dialog.isVisible():
            self._dynamics_dialog.raise_()
            self._dynamics_dialog.activateWindow()
            return
        dialog = ProductionEffectDialog(
            "dynamics", self._track_name, self._dynamics_params(self._effect_chain),
            self.window(), sidechain_sources=self._sidechain_sources,
        )
        self._dynamics_dialog = dialog
        draft_ids = {key: str(uuid.uuid4()) for key in _DYNAMICS_STAGES}

        def edit(params: dict, commit: bool) -> None:
            if self._dynamics_dialog is dialog:
                baseline = self._dynamics_params(self._effect_chain)
                self._apply_effect_window_edit(
                    dialog, lambda chain: self._chain_with_dynamics_edit(chain, baseline, params, draft_ids),
                    commit=commit,
                )

        dialog.preview.connect(lambda params: edit(params, False))
        dialog.committed.connect(lambda params: edit(params, True))
        self._show_effect_dialog(dialog)

    @staticmethod
    def _dynamics_params(chain: EffectChainParams | None) -> dict:
        """Read group bypass and the three independent stage settings."""
        effects = chain.effects if chain else ()
        stages = {instance.effect_type: instance for instance in reversed(effects)
                  if instance.panel == "dynamics"}
        if "expander" not in stages and "noise_gate" in stages:
            stages["expander"] = stages["noise_gate"]
        state = {"enabled": any(instance.enabled for instance in stages.values())}
        for key, kind in _DYNAMICS_STAGES.items():
            instance = stages.get(kind)
            params = _effect_default_params(instance.effect_type if instance else kind)
            if instance:
                params.update(instance.params)
            state[key] = params
        state["makeup"] = state["comp"]["makeup_gain"]
        return state

    @staticmethod
    def _chain_with_dynamics_edit(
        chain: EffectChainParams | None, baseline: dict, updated: dict, draft_ids: dict[str, str],
    ) -> EffectChainParams | None:
        from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance

        effects = list(chain.effects) if chain else []
        master_changed = baseline["enabled"] != updated["enabled"]
        groups = {instance.effect_type: index for index, instance in reversed(list(enumerate(effects)))
                  if instance.panel == "dynamics"}
        if "expander" not in groups and "noise_gate" in groups:
            groups["expander"] = groups["noise_gate"]
        for key, kind in _DYNAMICS_STAGES.items():
            params = {**baseline[key], **updated[key]}
            if key == "comp":
                params["makeup_gain"] = updated["makeup"]
            index = groups.get(kind)
            if index is not None:
                current = effects[index]
                changes = {param: value for param, value in params.items()
                           if baseline[key].get(param) != value}
                power = updated["enabled"] if master_changed else None
                if changes or power is not None:
                    effects[index] = _update_track_effect(current, {**current.params, **changes}, power)
            elif master_changed or baseline[key] != params:
                if kind == "compressor":
                    params.setdefault("makeup_post", True)
                order = max((instance.order for instance in effects), default=-1) + 1
                effects.append(TrackEffectInstance(
                    kind, params, enabled=updated["enabled"], order=order,
                    instance_id=draft_ids[key], panel="dynamics",
                ))
        return EffectChainParams(tuple(effects)) if effects else None

    def _apply_effect_window_edit(
        self, dialog: QDialog, change: _ChainEdit, *, commit: bool,
    ) -> None:
        if not commit:
            self._effect_previews[dialog] = change
            self._send_effect_preview()
            return
        had_preview = self._effect_previews.pop(dialog, None) is not None
        updated = change(self._effect_chain)
        if updated != self._effect_chain:
            self._effect_chain = updated
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, updated)
        if had_preview or self._effect_previews:
            self._send_effect_preview()

    def _send_effect_preview(self) -> None:
        chain = self._effect_chain
        for change in self._effect_previews.values():
            chain = change(chain)
        self.track_effects_preview.emit(self._track_id, chain)

    def _show_effect_dialog(self, dialog: QDialog) -> None:
        """关闭时释放效果窗，并在 deferred delete 前清理注册引用。"""
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(self._on_effect_dialog_finished)
        dialog.show()

    def _on_effect_dialog_finished(self, _result: int) -> None:
        """只移除本次关闭的窗口，避免旧窗延迟销毁清掉新窗引用。"""
        dialog = self.sender()
        if self._effect_previews.pop(dialog, None) is not None:
            self._send_effect_preview()
        if dialog is self._panner_dialog:
            self._rollback_panner_preview()
        for attr in ("_eq_dialog", "_dynamics_dialog", "_panner_dialog"):
            if getattr(self, attr, None) is dialog:
                setattr(self, attr, None)
        for attr in self._EFFECT_DIALOG_ATTRS:
            dialogs = getattr(self, attr, None)
            if dialogs:
                for instance_id, registered in list(dialogs.items()):
                    if registered is dialog:
                        dialogs.pop(instance_id, None)
        self._safe_spectrum_gating()

    def set_effect_chain(self, chain: EffectChainParams | None) -> None:
        """Refresh the rack and reconcile open settings after a model edit."""
        previous = self._effect_chain.effects if self._effect_chain else ()
        self._effect_chain = chain
        self._refresh_effect_list()
        current = chain.effects if chain else ()
        dynamics = self._dynamics_dialog
        if dynamics is not None:
            old_group = tuple(entry for entry in previous if entry.panel == "dynamics")
            new_group = tuple(entry for entry in current if entry.panel == "dynamics")
            if old_group != new_group:
                if old_group and not new_group:
                    dynamics.close()
                else:
                    self._effect_previews.pop(dynamics, None)
                    dynamics.set_current_params(self._dynamics_params(chain))

        eq = self._eq_dialog
        if eq is not None:
            bound_id = getattr(eq, "_instance_id", "")
            entry = next((item for item in current if item.effect_type == "strip_eq"
                          and item.panel == "eq"), None)
            if (bound_id and (entry is None or entry.instance_id != bound_id)
                    or not bound_id and entry is not None):
                eq.close()
            elif entry is not None and entry not in previous:
                self._backfill_effect_window(eq, entry)

        live = {entry.instance_id: entry for entry in current}
        before = {entry.instance_id: entry for entry in previous}
        for attr in self._EFFECT_DIALOG_ATTRS:
            default_kind = self._EFFECT_DIALOG_TYPES.get(attr)
            for instance_id, dialog in list(getattr(self, attr, {}).items()):
                entry = live.get(instance_id)
                kind = getattr(dialog, "fx_effect_type", default_kind)
                if entry is None or entry.effect_type != kind:
                    self._close_dialogs_for_instance(instance_id)
                elif entry != before.get(instance_id):
                    self._backfill_effect_window(dialog, entry)

    def _backfill_effect_window(self, dialog: QDialog, instance: TrackEffectInstance) -> None:
        self._effect_previews.pop(dialog, None)
        params = instance.params or _effect_default_params(instance.effect_type)
        if hasattr(dialog, "fx_effect_type"):
            if dialog.fx_effect_type == "strip_eq" and dialog._effect_version != instance.algorithm_version:
                self._set_eq_response_calculator(dialog, instance.algorithm_version)
            dialog.set_current_params(params, enabled=instance.enabled)
        else:
            dialog.set_current_params(params)

    def _show_eq_insert_in_rack(self) -> bool:
        return False

    def _refresh_effect_list(self) -> None:
        """根据 self._effect_chain 刷新内嵌效果器列表 UI。

        只显示 panel="fx" 的实例（FX 列表多实例，由用户右键添加）。
        DY 胶囊（panel="dynamics"）和 EQ 胶囊（panel="eq"）管理的实例不在此显示，
        它们是独立的快捷单例入口，与 FX 列表互不干扰。
        """
        selected = self._effect_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected is not None else None
        scroll_position = self._effect_list.verticalScrollBar().value()
        self._effect_list.blockSignals(True)
        self._effect_list.clear()
        effects = self._effect_chain.effects if self._effect_chain else ()
        # CW-A1: Dynamics 单元（panel="dynamics" 三实例）在列表里显示为
        # 单一 "Dynamics" 行——Da Vinci Fairlight 语义，整体加入/整体移除，
        # 点击打开完整 Dynamics 面板。
        dyn_insts = [e for e in effects if getattr(e, "panel", "fx") == "dynamics"]
        if dyn_insts:
            group_enabled = any(bool(e.enabled) for e in dyn_insts)
            dyn_item = QListWidgetItem(I18N.tr("audio.fx.dynamics", "Dynamics"))
            dyn_item.setData(Qt.ItemDataRole.UserRole, dyn_insts[0].instance_id)
            dyn_item.setData(Qt.ItemDataRole.UserRole + 1, "dynamics")
            dyn_item.setData(Qt.ItemDataRole.UserRole + 2, group_enabled)
            if not group_enabled:
                dyn_item.setForeground(QColor("#666670"))
            self._effect_list.addItem(dyn_item)
        for inst in effects:
            # EQ 胶囊管理的实例（panel="eq"）不在 FX 列表显示；
            # dynamics 三实例已聚合为上面的单一行。
            panel = getattr(inst, "panel", "fx")
            if panel != "fx" and not (panel == "eq" and self._show_eq_insert_in_rack()):
                continue
            short = EFFECT_SHORT_NAMES.get(inst.effect_type) or _effect_display_name(inst.effect_type)
            if inst.effect_type == "vst3_hosted":
                short = str(inst.params.get("plugin_name") or short)
            p_enabled = inst.enabled and bool(inst.params.get("enabled", True))
            label = short  # 旁路不加方括号，仅用灰色区分
            item = QListWidgetItem(label)
            # 存 instance_id 到 data，供点击/右键定位
            item.setData(Qt.ItemDataRole.UserRole, inst.instance_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, inst.effect_type)
            item.setData(Qt.ItemDataRole.UserRole + 2, p_enabled)
            if not p_enabled:
                # 旁路项灰色
                item.setForeground(QColor("#666670"))
            self._effect_list.addItem(item)
        self._effect_list.blockSignals(False)
        # All strips share the same rack height, including Master and empty racks.
        # Extra inserts scroll inside the rack instead of shortening one fader.
        for row in range(self._effect_list.count()):
            item = self._effect_list.item(row)
            item.setSizeHint(QSize(0, 20))
            iid = item.data(Qt.ItemDataRole.UserRole)
            if iid is not None:
                active = bool(item.data(Qt.ItemDataRole.UserRole + 2))
                item.setIcon(self._effect_status_icon(active))
                effect_type = item.data(Qt.ItemDataRole.UserRole + 1)
                state = I18N.tr("audio.mixer.active", "Active") if active else I18N.tr("audio.mixer.bypassed", "Bypassed")
                name = item.text() if effect_type == "vst3_hosted" else _effect_display_name(effect_type)
                item.setToolTip(f"{name} · {state}\n" + I18N.tr(
                    "audio.mixer.insert_tip", "Click / Enter: edit · Space: bypass · Delete: remove · Right-click: more"))
            if iid == selected_id:
                self._effect_list.setCurrentItem(item)
        self._effect_list.setFixedHeight(42 if self._is_narrow else 62)
        self._effect_list.verticalScrollBar().setValue(scroll_position)
        # 胶囊高亮同样依赖 params.enabled，与列表显示同源，一并刷新保持同步
        if hasattr(self, '_fx_badge'):
            self._update_badges_style()

    def _effect_status_icon(self, active: bool) -> QIcon:
        """Cache small status dots instead of constructing widgets for every insert."""
        if not hasattr(self, "_effect_status_icons"):
            self._effect_status_icons: dict[bool, QIcon] = {}
        if active not in self._effect_status_icons:
            pixmap = QPixmap(6, 6)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#61cfc5" if active else "#65707d"))
            painter.drawEllipse(0, 0, 6, 6)
            painter.end()
            self._effect_status_icons[active] = QIcon(pixmap)
        return self._effect_status_icons[active]

    def _on_effect_key_action(self, action: str) -> None:
        """Route focused rack shortcuts through the existing command signals."""
        item = self._effect_list.currentItem()
        if item is None or self._effect_chain is None:
            return
        iid = item.data(Qt.ItemDataRole.UserRole)
        inst = next((e for e in self._effect_chain.effects if e.instance_id == iid), None)
        if inst is not None and inst.panel == "dynamics":
            if action == "bypass":
                self._effect_bypass_dynamics()
            elif action == "remove":
                self._effect_remove_dynamics()
            return
        if inst is not None:
            if action == "bypass":
                self._effect_bypass(inst)
            elif action == "remove":
                self._effect_remove(inst)

    def _on_fx_clicked(self) -> None:
        """FX 胶囊点击：一键全开/全关 FX 列表里的效果器（panel="fx"）。

        只影响 FX 列表的实例，DY 胶囊（panel="dynamics"）和 EQ 胶囊（panel="eq"）
        管理的实例完全不碰。
        若当前 FX 列表有任意启用的效果器 → 全部旁路；否则全部启用。
        空列表时不做任何事（由列表内的 "+" 占位项或右键添加）。
        """
        if self._effect_chain is None or not self._effect_chain.effects:
            return
        effects = self._effect_chain.effects

        # 只看 FX 列表管理的实例（panel="fx"）
        fx_effects = [e for e in effects if getattr(e, "panel", "fx") == "fx"]
        if not fx_effects:
            return
        any_enabled = any(e.enabled and bool(e.params.get("enabled", True)) for e in fx_effects)
        target_enabled = not any_enabled  # 有启用的→全关；全关→全开

        from src.features.edit.core.timeline_v2 import EffectChainParams
        # 只改 panel="fx" 的实例，其余（dynamics/eq）原样保留（含 panel 字段）
        changed_ids = set()
        new_effects = []
        for e in effects:
            if getattr(e, "panel", "fx") == "fx":
                new_effects.append(_update_track_effect(
                    e, {**e.params, "enabled": target_enabled}, True if target_enabled else None,
                ))
                changed_ids.add(e.instance_id)
            else:
                new_effects.append(e)  # DY/EQ 实例原样保留
        self._effect_chain = EffectChainParams(effects=tuple(new_effects))
        # 只同步 FX 列表实例对应的对话框开关（不动 DY/EQ 对话框）
        for inst_id in changed_ids:
            self._sync_dialog_toggle(inst_id, target_enabled)
        self._refresh_effect_list()
        self._update_badges_style()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    def _on_effect_item_clicked(self, item: QListWidgetItem) -> None:
        """Open the selected instance or the channel's composite Dynamics unit."""
        instance_id = item.data(Qt.ItemDataRole.UserRole)
        effects = self._effect_chain.effects if self._effect_chain else ()
        instance = next((entry for entry in effects if entry.instance_id == instance_id), None)
        if instance is None:
            return
        if instance.panel == "dynamics":
            self._open_dynamics_panel()
        elif instance.panel == "eq":
            self._open_eq_from_badge()
        elif instance.effect_type == "vst3_hosted":
            self._edit_vst3_insert(instance)
        elif instance.effect_type in self._INSERT_EFFECT_TYPES or instance.effect_type == "noise_gate":
            self._open_insert_effect(instance)

    def _on_effect_list_context_menu(self, pos) -> None:
        """W26: 统一右键菜单。

        无论点在项上还是空白，都显示同一个菜单：
        - 项命中：项操作（旁路/上移/下移/删除）+ 分隔符
        - 永远有：按类别分组的添加效果器子菜单

        这样即使列表填满（无空白），也能添加新效果器。
        """
        item = self._effect_list.itemAt(pos)
        menu = QMenu(self)

        if item is not None:
            # 点在项上：项操作
            instance_id = item.data(Qt.ItemDataRole.UserRole)
            entries = self._effect_chain.effects if self._effect_chain else ()
            inst = next((entry for entry in entries if entry.instance_id == instance_id), None)
            if inst is not None and inst.panel == "dynamics":
                # Dynamics 单元行：整体旁路/删除（三实例一个整体）
                dyn = self._dynamics_instances()
                group_on = any(bool(e.enabled) for e in dyn)
                bypass_dyn = menu.addAction(
                    I18N.tr("audio.fx.bypass", "Bypass") if group_on
                    else I18N.tr("audio.fx.enable", "Enable"))
                bypass_dyn.triggered.connect(
                    lambda checked=False: self._effect_bypass_dynamics())
                menu.addSeparator()
                del_dyn = menu.addAction("✕ " + I18N.tr("audio.fx.delete", "Delete"))
                del_dyn.triggered.connect(
                    lambda checked=False: self._effect_remove_dynamics())
                menu.addSeparator()
            elif inst is not None:
                idx = self._effect_chain.effects.index(inst)
                # 旁路（文字与列表显示统一读 params.enabled）
                p_enabled = inst.enabled and bool(inst.params.get("enabled", True))
                bypass_action = menu.addAction(I18N.tr("audio.fx.bypass", "Bypass") if p_enabled
                                               else I18N.tr("audio.fx.enable", "Enable"))
                bypass_action.triggered.connect(lambda checked=False, i=inst: self._effect_bypass(i))
                menu.addSeparator()
                # 上移
                up_action = menu.addAction("▲ " + I18N.tr("audio.fx.move_up", "Move Up"))
                up_action.setEnabled(idx > 0)
                up_action.triggered.connect(lambda checked=False, i=inst: self._effect_move(i, -1))
                # 下移
                down_action = menu.addAction("▼ " + I18N.tr("audio.fx.move_down", "Move Down"))
                down_action.setEnabled(idx < len(self._effect_chain.effects) - 1)
                down_action.triggered.connect(lambda checked=False, i=inst: self._effect_move(i, 1))
                menu.addSeparator()
                # 删除
                del_action = menu.addAction("✕ " + I18N.tr("audio.fx.delete", "Delete"))
                del_action.triggered.connect(lambda checked=False, i=inst: self._effect_remove(i))
                menu.addSeparator()

        # 始终提供插入效果菜单，与通道条下方的添加按钮共用。
        add_menu = menu.addMenu("+ " + I18N.tr("audio.fx.add", "Add Effect"))
        self._populate_add_effect_menu(add_menu)

        self.mixer_menu_requested.emit(menu)
        menu.exec(self._effect_list.mapToGlobal(pos))
        menu.deleteLater()

    def _populate_add_effect_menu(self, menu: QMenu) -> None:
        """Use the audio browser's categories for the channel's add commands."""
        from src.features.mixer.effects.browser_catalog import audio_effect_groups, audio_effect_name

        # The add menu offers the complete Dynamics unit. Its components can also be dragged in.
        effect_types = (self._INSERT_EFFECT_TYPES - {"compressor", "limiter", "expander"}) | {"spatial_panner"}
        for title, _, effects in audio_effect_groups(include_track_effects=True, effect_types=effect_types):
            category = menu.addMenu(title)
            for effect in effects:
                effect_type = effect.effect_type
                action = category.addAction(audio_effect_name(effect))
                action.setData(effect_type)
                if effect_type == "spatial_panner":
                    action.setEnabled(not self._is_master)
                    if self._is_master:
                        action.setToolTip(I18N.tr(
                            "audio.mixer.panner_track_bus_only",
                            "Spatial panning is available on audio tracks and buses.",
                        ))
                    action.triggered.connect(self._add_spatial_panner)
                else:
                    action.triggered.connect(lambda checked=False, et=effect_type: self._effect_add(et))
        hosted = menu.addAction(I18N.tr("audio.fx.vst3.add", "Add VST3 plug-in…"))
        hosted.setData("vst3_hosted")
        if self._effect_processing_layout != "stereo":
            reason = I18N.tr("audio.fx.vst3.stereo_only", "Requires a stereo channel")
            hosted.setEnabled(False)
            hosted.setToolTip(reason)
            hosted.setText(f"{hosted.text()} · {reason}")
        hosted.triggered.connect(lambda _checked=False: self._edit_vst3_insert())

    def _show_add_effect_menu(self) -> None:
        """Open Add Effect from the permanent button below the rack."""
        menu = QMenu(self)
        self._populate_add_effect_menu(menu)
        button = self._add_effect_btn
        pos = button.mapToGlobal(QPoint(0, button.height()))
        menu.exec(pos)
        menu.deleteLater()

    # ─── 效果器链操作（add/remove/move/bypass）───

    def _dynamics_instances(self) -> list:
        """当前链中 Dynamics 单元的全部实例（panel="dynamics"）。"""
        if self._effect_chain is None:
            return []
        return [
            e for e in self._effect_chain.effects
            if getattr(e, "panel", "fx") == "dynamics"
        ]

    def _effect_bypass_dynamics(self) -> None:
        """整体旁路/启用 Dynamics 单元（三实例同步）。"""
        dyn = self._dynamics_instances()
        if not dyn:
            return
        target_enabled = not any(bool(e.enabled) for e in dyn)
        from src.features.edit.core.timeline_v2 import EffectChainParams
        effects = tuple(
            _update_track_effect(e, e.params, target_enabled)
            if getattr(e, "panel", "fx") == "dynamics" else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if self._dynamics_dialog is not None:
            self._effect_previews.pop(self._dynamics_dialog, None)
            self._dynamics_dialog.set_current_params(self._dynamics_params(self._effect_chain))
        self._refresh_effect_list()
        self._update_badges_style()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    def _effect_remove_dynamics(self) -> None:
        """整体移除 Dynamics 单元（关闭面板 + 删除三实例）。"""
        if self._dynamics_dialog is not None:
            try:
                self._dynamics_dialog.close()
            except RuntimeError:
                pass
            self._dynamics_dialog = None
        if self._effect_chain is None:
            return
        effects = tuple(
            e for e in self._effect_chain.effects
            if getattr(e, "panel", "fx") != "dynamics"
        )
        self._effect_chain = EffectChainParams(effects=effects) if effects else None
        self._refresh_effect_list()
        self._update_badges_style()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    def _add_spatial_panner(self) -> None:
        """添加空间声像器（轨级固定，Da Vinci Fairlight 语义）。

        以默认参数启用本轨声像器并提交命令栈（panner_committed →
        controller 聚合 MacroCommand 持久化），随后打开完整声像器对话框
        供微调。已启用时不重复提交，直接打开面板。
        """
        if self._is_master:
            return
        from src.features.edit.core.timeline_v2 import SpatialPannerParams
        already = bool(self._panner) and bool(self._panner.get("enabled", True))
        if not already:
            payload = {
                "pan": float(self._pan),
                "panner": SpatialPannerParams.default().to_dict(),
            }
            # 复用统一提交路径：本地记录 + panner_committed → 命令栈
            self._on_panner_edited(payload, commit=True)
        self._update_panner_indicator()
        self._open_panner_dialog()

    def _update_panner_indicator(self) -> None:
        """声像器启用状态在 strip 上的可见指示（⤢ 按钮高亮）。"""
        btn = getattr(self, "_panner_expand_btn", None)
        if btn is None:
            return
        active = bool(self._panner) and bool(self._panner.get("enabled", True))
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {C.BG_INPUT};
                color: {'#28D8F0' if active else C.TEXT_DIM};
                border: 1px solid {'#28D8F0' if active else C.BORDER};
                border-radius: 4px;
                padding: 0px 2px;
                font-size: 10px;
            }}
            QPushButton:hover {{ color: {C.TEXT_PRIMARY}; border-color: {C.BORDER_HOVER}; }}
        """)

    def _add_dynamics_unit(self) -> None:
        """添加完整 Dynamics 单元（Da Vinci Fairlight 语义）。

        Expander/Gate、Compressor、Limiter 三个实例作为一个整体加入
        （panel="dynamics"，FX 列表不单独显示），随后打开完整 Dynamics
        面板。已存在任一 dynamics 实例时不重复添加，直接打开面板。
        """
        from src.features.edit.core.timeline_v2 import (
            TrackEffectInstance, EffectChainParams,
        )
        existing = list(self._effect_chain.effects) if self._effect_chain else []
        has_dynamics = any(
            getattr(e, "panel", "fx") == "dynamics"
            and e.effect_type in ("expander", "noise_gate", "compressor", "limiter")
            for e in existing
        )
        if not has_dynamics:
            new_order = max((e.order for e in existing), default=-1) + 1
            for et in ("expander", "compressor", "limiter"):
                existing.append(TrackEffectInstance(
                    effect_type=et,
                    params=_effect_default_params(et),
                    enabled=True,
                    order=new_order,
                    panel="dynamics",
                ))
            self._effect_chain = EffectChainParams(effects=tuple(existing))
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        self._refresh_effect_list()
        self._update_badges_style()
        self._open_dynamics_panel()

    def _edit_vst3_insert(self, instance: TrackEffectInstance | None = None) -> None:
        from src.features.edit.core.hosted_vst3 import EFFECT_TYPE, HostedVst3Params
        from src.features.mixer.ui.vst3_insert_dialog import Vst3InsertDialog

        if instance is None and self._effect_processing_layout != "stereo":
            return
        try:
            initial = HostedVst3Params.from_dict(instance.params) if instance else None
        except ValueError as exc:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, I18N.tr("audio.fx.vst3.title", "VST3 plug-in"), str(exc))
            return
        original_chain = self._effect_chain
        dialog = Vst3InsertDialog(self._engine_sample_rate, self, initial)
        dialog.exec()
        result = dialog.result_params
        dialog.deleteLater()
        if result is None or original_chain != self._effect_chain:
            return
        if instance is None and self._effect_processing_layout == "stereo":
            self._effect_add(EFFECT_TYPE, result.to_dict())
        elif instance is not None and result != initial:
            self._on_fx_param_edited(instance, {**instance.params, **result.to_dict()})

    def set_effect_processing_layout(self, layout: str) -> None:
        """Keep rack choices aligned with the channel's actual processing domain."""
        self._effect_processing_layout = str(layout or "stereo")

    def _effect_add(self, effect_type: str, params: dict | None = None) -> None:
        """添加一个效果器到链尾（支持多实例，同类型可重复添加）。

        effect_type == "dynamics" 时一次性加入完整 Dynamics 单元
        （expander + compressor + limiter，panel="dynamics"）并打开面板。
        """
        if effect_type == "dynamics":
            self._add_dynamics_unit()
            return
        existing = self._effect_chain.effects if self._effect_chain else ()
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_order = max((e.order for e in existing), default=-1) + 1
        inst = TrackEffectInstance(
            effect_type=effect_type,
            params=_effect_default_params(effect_type) if params is None else params,
            enabled=True,
            order=new_order,
            panel="fx",  # FX 列表管理的实例（与 DY 胶囊的 panel="dynamics" 区分）
        )
        effects = existing + (inst,)
        self._effect_chain = EffectChainParams(effects=effects)
        self._refresh_effect_list()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    # ─── 效果拖放（Effects 面板 → 插入链插槽）───

    def _dropped_effect_id(self, event: Any) -> Optional[str]:
        """读取拖放载荷里的 effect_id；不支持的作用域返回 None。"""
        mime = event.mimeData()
        if mime is None or not mime.hasFormat(self._EFFECT_DROP_MIME):
            return None
        effect_id = str(mime.data(self._EFFECT_DROP_MIME).data(), "utf-8").strip()
        return effect_id if effect_id in self._INSERT_EFFECT_TYPES else None

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802
        if self._dropped_effect_id(event) is not None:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: Any) -> None:  # noqa: N802
        if self._dropped_effect_id(event) is not None:
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: Any) -> None:  # noqa: N802
        effect_id = self._dropped_effect_id(event)
        if effect_id is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._effect_add(effect_id)

    def _effect_remove(self, inst: Any) -> None:
        """删除一个效果器。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 关闭该实例打开中的编辑对话框 —— 否则对话框仍指向已删除
        # 的实例，后续编辑被静默丢弃（instance_id 不再命中 chain 中任何条目），
        # 且每次编辑都向命令栈发出无变化链的重复提交。
        self._close_dialogs_for_instance(inst.instance_id)
        from src.features.edit.core.timeline_v2 import EffectChainParams
        effects = tuple(e for e in self._effect_chain.effects if e.instance_id != inst.instance_id)
        # 重新编号 order
        effects = tuple(
            replace(e, order=i) for i, e in enumerate(effects)
        )
        self._effect_chain = EffectChainParams(effects=effects) if effects else None
        self._refresh_effect_list()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    # FX 列表 8 类效果器对话框的实例字典属性名（AUD-37902 关闭/守卫共用）
    _EFFECT_DIALOG_ATTRS = (
        '_limiter_dialogs', '_compressor_dialogs', '_reverb_dialogs',
        '_delay_dialogs', '_saturation_dialogs', '_imager_dialogs',
        '_expander_dialogs', '_strip_eq_dialogs',
        '_voice_changer_dialogs', '_spectral_mangler_dialogs',
        '_fx_param_dialogs',
    )

    # R06: 对话框字典属性 → 效果类型（外部链变化时按 id+类型回填/关闭）
    _EFFECT_DIALOG_TYPES = {
        '_limiter_dialogs': 'limiter',
        '_compressor_dialogs': 'compressor',
        '_reverb_dialogs': 'reverb',
        '_delay_dialogs': 'delay',
        '_saturation_dialogs': 'saturation',
        '_imager_dialogs': 'imager',
        '_expander_dialogs': 'expander',
        '_strip_eq_dialogs': 'strip_eq',
        '_voice_changer_dialogs': 'voice_changer',
        '_spectral_mangler_dialogs': 'spectral_mangler',
    }

    def _close_dialogs_for_instance(self, instance_id: str) -> None:
        """AUD-37902: 关闭并移除某效果器实例的编辑对话框。

        删除实例时调用：对话框随后发射的 preview/committed 会被
        _on_*_edited 的实例存在性守卫拦下，用户也不会继续编辑一个
        已不存在的效果器（编辑静默失效）。
        """
        for attr in self._EFFECT_DIALOG_ATTRS:
            dialogs = getattr(self, attr, None)
            if not dialogs:
                continue
            dlg = dialogs.pop(instance_id, None)
            if dlg is not None:
                if self._effect_previews.pop(dlg, None) is not None:
                    self._send_effect_preview()
                dlg.deleteLater()
        self._safe_spectrum_gating()

    def close_all_dialogs(self) -> None:
        """MX-12: 关闭本 strip 的全部效果器对话框（轨删除/工程切换时调用）。

        清理单例（EQ/Dynamics/Panner）与多实例对话框字典，断开引用——
        否则轨删除后对话框仍 parent 在主窗口上成为孤儿窗，继续接收
        已删轨的迟到编辑并泄漏订阅。
        """
        if self._effect_previews:
            self._effect_previews.clear()
            self._send_effect_preview()
        self._rollback_panner_preview()
        for attr in ("_eq_dialog", "_dynamics_dialog", "_panner_dialog"):
            dlg = getattr(self, attr, None)
            if dlg is not None:
                setattr(self, attr, None)
                try:
                    dlg.deleteLater()
                except RuntimeError:
                    pass  # C++ 对象已销毁
        for attr in self._EFFECT_DIALOG_ATTRS:
            dialogs = getattr(self, attr, None)
            if not dialogs:
                continue
            for dlg in list(dialogs.values()):
                try:
                    dlg.deleteLater()
                except RuntimeError:
                    pass
            dialogs.clear()
        # 频谱/LUFS 门控随对话框关闭而解除
        try:
            self._update_spectrum_gating()
        except (RuntimeError, AttributeError):
            pass

    def set_engine_sample_rate(self, sample_rate: float) -> None:
        """MX-09: 引擎采样率下发（记忆 + 推送到已打开的 EQ 曲线/频谱轴）。"""
        self._engine_sample_rate = float(sample_rate)
        for dialog in self._iter_eq_dialogs():
            graph = getattr(dialog, "eq_graph", None)
            if graph is not None and hasattr(graph, "set_sample_rate"):
                graph.set_sample_rate(self._engine_sample_rate)
            elif hasattr(dialog, "update_sample_rate"):
                dialog.update_sample_rate(self._engine_sample_rate)

    def _configure_eq_plot(self, dialog: ProductionEffectDialog, algorithm_version: int) -> None:
        dialog.eq_graph = dialog.panel.eq_graph
        self._set_eq_response_calculator(dialog, algorithm_version)
        dialog.eq_graph.set_sample_rate(self._engine_sample_rate)
        dialog.panel.update_spectrum([], [])
        dialog.panel.update_output_meter(-100.0)

    @staticmethod
    def _set_eq_response_calculator(dialog: ProductionEffectDialog, algorithm_version: int) -> None:
        from src.features.edit.core.audio_dsp import strip_eq_band_response_db

        dialog._effect_version = algorithm_version
        dialog.eq_graph.set_response_calculator(
            partial(strip_eq_band_response_db, algorithm_version=algorithm_version)
        )

    def _instance_in_chain(self, instance_id: str) -> bool:
        """AUD-37902: instance_id 是否仍存在于当前效果器链（删除后的编辑守卫）。"""
        if self._effect_chain is None:
            return False
        return any(e.instance_id == instance_id for e in self._effect_chain.effects)

    def _effect_move(self, inst: Any, delta: int) -> None:
        """上移/下移一个效果器（调整 order）。"""
        if self._effect_chain is None:
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        effects = list(self._effect_chain.effects)
        idx = next((i for i, e in enumerate(effects) if e.instance_id == inst.instance_id), -1)
        if idx < 0:
            return
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(effects):
            return
        effects[idx], effects[new_idx] = effects[new_idx], effects[idx]
        effects = [
            replace(e, order=i) for i, e in enumerate(effects)
        ]
        self._effect_chain = EffectChainParams(effects=tuple(effects))
        self._refresh_effect_list()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    def _effect_bypass(self, inst: Any) -> None:
        """切换某个效果器的旁路状态。

        统一读写 params.enabled（与效果器对话框开关、FX 列表 [name] 显示三者同源）。
        不再翻转 TrackEffectInstance.enabled（chain-level bypass 字段保留但 FX 列表不再用它驱动显示）。
        """
        if self._effect_chain is None:
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        cur_enabled = inst.enabled and bool(inst.params.get("enabled", True))
        new_enabled = not cur_enabled
        # 仅修改命中实例的 params.enabled，其余字段（含 chain-level enabled）保持不变
        effects = []
        for e in self._effect_chain.effects:
            if e.instance_id == inst.instance_id:
                new_params = dict(e.params) if e.params else {}
                new_params["enabled"] = new_enabled
                effects.append(_update_track_effect(e, new_params, True if new_enabled else None))
            else:
                effects.append(e)
        self._effect_chain = EffectChainParams(effects=tuple(effects))
        self._refresh_effect_list()
        # 若该实例的对话框正打开，同步其开关（AUD-37904: setChecked 实际会发
        # toggled，_sync_dialog_toggle 内部已 blockSignals 防回环 emit）
        self._sync_dialog_toggle(inst.instance_id, new_enabled)
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    def _sync_dialog_toggle(self, instance_id: str, enabled: bool) -> None:
        """把 params.enabled 的变化同步到已打开的效果器对话框开关。

        遍历 8 类 FX 列表效果器对话框字典（按 instance_id 索引，含 strip_eq），
        找到匹配项后调用 setChecked。AUD-37904: Qt 语义上程序化 setChecked
        在状态变化时会发射 toggled（注释旧断言与 Qt 相反），未加信号锁时
        对话框处理器被触发，造成级联重复 preview/commit —— 这里显式
        blockSignals 阻断伪信号。
        EQ 胶囊（_eq_dialog 单例）/ Dynamics 胶囊（_dynamics_dialog）不在此同步范围内
        （它们由独立的 badge 入口管理）。

        注：strip_eq 对话框的开关属性名为 _enable_check（历史命名），其余为 _enable_toggle，
        这里两者都兼容。
        """
        effects = self._effect_chain.effects if self._effect_chain else ()
        instance = next((entry for entry in effects if entry.instance_id == instance_id), None)
        if instance is not None and self._eq_dialog is not None and getattr(
            self._eq_dialog, "_instance_id", "",
        ) == instance_id:
            self._backfill_effect_window(self._eq_dialog, instance)
        for attr in self._EFFECT_DIALOG_ATTRS:
            dialogs = getattr(self, attr, None)
            if not dialogs:
                continue
            dlg = dialogs.get(instance_id)
            if dlg is not None and dlg.isVisible():
                if instance is not None and hasattr(dlg, "fx_effect_type"):
                    self._backfill_effect_window(dlg, instance)
                    continue
                # strip_eq 用 _enable_check，其余用 _enable_toggle
                toggle = getattr(dlg, '_enable_toggle', None) or getattr(dlg, '_enable_check', None)
                if toggle is not None:
                    # AUD-37904: setChecked 会发射 toggled（Qt 实际语义），
                    # 必须加信号锁阻断对话框处理器的级联 preview/commit。
                    toggle.blockSignals(True)
                    toggle.setChecked(enabled)
                    toggle.blockSignals(False)

    # ─── 效果器参数编辑器（Limiter / Compressor / Reverb / Delay / Saturation / Imager / Noise Gate / Strip EQ 对话框）───
    # 注：EQ 胶囊（_eq_dialog 单例）由 _open_eq_from_badge 独立打开；
    # FX 列表里的 strip_eq 实例（多实例）由 _open_strip_eq_editor 打开。

    def _open_insert_effect(self, instance: TrackEffectInstance) -> None:
        from src.features.mixer.effects.production_dialog import ProductionEffectDialog

        effects = self._effect_chain.effects if self._effect_chain else ()
        current = next((entry for entry in effects if entry.instance_id == instance.instance_id), None)
        if current is None or current.effect_type != instance.effect_type:
            return
        instance = current
        effect_type = "expander" if instance.effect_type == "noise_gate" else instance.effect_type
        attr = next((name for name, kind in self._EFFECT_DIALOG_TYPES.items() if kind == effect_type),
                    "_fx_param_dialogs")
        dialogs = getattr(self, attr, None)
        if dialogs is None:
            dialogs = {}
            setattr(self, attr, dialogs)
        dialog = dialogs.get(instance.instance_id)
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return

        dialog = ProductionEffectDialog(
            instance.effect_type, self._track_name,
            instance.params or _effect_default_params(instance.effect_type),
            self.window(), enabled=instance.enabled, sidechain_sources=self._sidechain_sources,
        )
        dialog._instance_id = instance.instance_id
        dialogs[instance.instance_id] = dialog
        if instance.effect_type == "strip_eq":
            self._configure_eq_plot(dialog, instance.algorithm_version)

        def edit(params: dict, commit: bool) -> None:
            if dialogs.get(instance.instance_id) is dialog:
                power = dialog.power_override
                self._apply_effect_window_edit(
                    dialog, lambda chain: self._chain_with_insert_edit(chain, instance, params, power),
                    commit=commit,
                )

        dialog.preview.connect(lambda params: edit(params, False))
        dialog.committed.connect(lambda params: edit(params, True))
        self._show_effect_dialog(dialog)
        self._update_spectrum_gating()

    @staticmethod
    def _chain_with_insert_edit(
        chain: EffectChainParams | None, instance: TrackEffectInstance,
        params: dict, power: bool | None,
    ) -> EffectChainParams | None:
        from src.features.edit.core.timeline_v2 import EffectChainParams

        if chain is None:
            return None
        edited = tuple(
            _update_track_effect(entry, {**entry.params, **params}, power)
            if entry.instance_id == instance.instance_id and entry.effect_type == instance.effect_type
            else entry
            for entry in chain.effects
        )
        return EffectChainParams(edited)

    def _open_limiter_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_limiter_edited(self, inst: Any, limiter_params: Any, commit: bool = True) -> None:
        """Limiter 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = limiter_params.to_dict()
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），需刷新列表的 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_compressor_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_compressor_edited(self, inst: Any, comp_params: Any, commit: bool = True) -> None:
        """Compressor 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = comp_params.to_dict()
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_reverb_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_reverb_edited(self, inst: Any, reverb_params: Any, commit: bool = True) -> None:
        """Reverb 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = reverb_params.to_dict()
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_voice_changer_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _open_spectral_mangler_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_generic_fx_edited(self, inst: Any, effect_type: str,
                              params: dict, commit: bool = True) -> None:
        """NF 两款复合 FX 的统一提交路径（实例存在性守卫同其他类型）。"""
        if not self._instance_in_chain(inst.instance_id):
            return
        effects = list(self._effect_chain.effects) if self._effect_chain else []
        for i, e in enumerate(effects):
            if e.instance_id == inst.instance_id and e.effect_type == effect_type:
                effects[i] = replace(e, params=dict(params))
                break
        from src.features.edit.core.timeline_v2 import EffectChainParams
        self._effect_chain = EffectChainParams(effects=tuple(effects)) if effects else None
        self._update_badges_style()
        if commit:
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_delay_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_delay_edited(self, inst: Any, delay_params: Any, commit: bool = True) -> None:
        """Delay 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = delay_params.to_dict()
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_fx_param_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_fx_param_edited(self, inst: Any, params: dict,
                            commit: bool = True) -> None:
        """通用 FX 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除——丢弃编辑（与专属对话框同一守卫）。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = dict(params)
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_saturation_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_saturation_edited(self, inst: Any, sat_params: Any, commit: bool = True) -> None:
        """Saturation 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = sat_params.to_dict()
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_imager_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_imager_edited(self, inst: Any, imager_params: Any, commit: bool = True) -> None:
        """Imager 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = imager_params.to_dict()
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_expander_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_expander_edited(self, inst: Any, exp_params: Any, commit: bool = True) -> None:
        """Expander/Gate 对话框提交/预览：更新机架中对应条目（保留原类型键）。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = exp_params.to_dict()
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_strip_eq_editor(self, inst: TrackEffectInstance) -> None:
        self._open_insert_effect(inst)

    def _on_eq_instance_closed(self, instance_id: str) -> None:
        """FX 列表 EQ 对话框关闭（hide）：清理字典引用并重算频谱门控。"""
        dlgs = getattr(self, '_strip_eq_dialogs', None)
        if dlgs is not None:
            dlgs.pop(instance_id, None)
        self._safe_spectrum_gating()

    def _on_strip_eq_edited(self, inst: Any, eq_params: Any, commit: bool = True) -> None:
        """FX 列表 strip_eq 实例提交：按 instance_id 更新 chain 中对应实例。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import EffectChainParams
        new_params = eq_params.to_dict()
        effects = tuple(
            replace(e, params=new_params) if e.instance_id == inst.instance_id else e
            for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def update_audio_level(self, levels: list[float]) -> None:
        """更新电平表"""
        self._meter.set_audio_values(levels)

    @property
    def display_meter_mode(self) -> MeterMode:
        return self._meter.meter_mode

    def reset_meter(self) -> None:
        """重置电平表"""
        self._meter.reset()
        self._set_speaker_peaks(None)

    # ─── CW-A1: 空间声像器 ───────────────────────────────────

    def _open_panner_dialog(self) -> None:
        """Edit the channel's pan and spatial panner in the new panel."""
        if self._is_master:
            return
        from src.features.mixer.effects.production_dialog import ProductionEffectDialog

        if self._panner_dialog is not None and self._panner_dialog.isVisible():
            self._panner_dialog.raise_()
            self._panner_dialog.activateWindow()
            return
        dialog = ProductionEffectDialog(
            "spatial_panner", self._track_name, {"pan": self._pan, "panner": self._panner},
            self.window(),
        )
        self._panner_dialog = dialog

        def preview(payload: dict) -> None:
            if self._panner_dialog is dialog:
                self._panner_preview_active = True
                self._sync_balance_from_pan(payload["pan"])
                self.panner_preview.emit(self._track_id, payload)

        def commit(payload: dict) -> None:
            if self._panner_dialog is dialog:
                self._panner_preview_active = False
                self._on_panner_edited(payload, commit=True)

        dialog.preview.connect(preview)
        dialog.committed.connect(commit)
        self._show_effect_dialog(dialog)

    def _rollback_panner_preview(self) -> None:
        if self._panner_preview_active:
            self._panner_preview_active = False
            self._sync_balance_from_pan()
            self.panner_preview.emit(self._track_id, {
                "pan": self._pan, "panner": self._panner, "_preview_rollback": True,
            })

    def _on_panner_edited(self, payload: dict, commit: bool) -> None:
        """声像器对话框提交/预览：本地记录 + 上抛信号。"""
        self._pan = float(payload.get("pan", 0.0))
        self._panner = dict(payload.get("panner") or {})
        self._balance = pan_to_balance(self._pan)
        # balance 推子视觉同步（不发信号）+ ⤢ 启用指示
        self._sync_balance_from_pan()
        self._update_panner_indicator()
        if commit:
            self.panner_committed.emit(self._track_id, payload)
        else:
            self.panner_preview.emit(self._track_id, payload)

    def _sync_balance_from_pan(self, pan: float | None = None) -> None:
        """声像器 lr 变化 → balance 推子/spinbox 静默同步（不发信号）。"""
        balance = pan_to_balance(self._pan if pan is None else pan)
        if hasattr(self, '_balance_fader') and self._balance_fader is not None:
            self._balance_fader.blockSignals(True)
            self._balance_fader.setValue(balance)
            self._balance_fader.blockSignals(False)
        if hasattr(self, '_balance_spin') and self._balance_spin is not None:
            self._balance_spin.blockSignals(True)
            self._balance_spin.setValue(balance)
            self._balance_spin.blockSignals(False)

    def set_pan_and_panner(self, pan: float, panner: Optional[dict]) -> None:
        """外部静默回填 pan + 空间声像器（timeline 变化后同步，blockSignals）。"""
        pan = max(-1.0, min(1.0, float(pan)))
        panner = dict(panner) if panner else {}
        changed = self._pan != pan or self._panner != panner
        self._pan, self._panner = pan, panner
        if not changed and self._panner_preview_active:
            return
        self.set_pan_linear(self._pan)
        self._update_panner_indicator()
        if changed and self._panner_dialog is not None:
            self._panner_preview_active = False
            self._panner_dialog.set_current_params({"pan": self._pan, "panner": self._panner})

    # ─── CW-A1: 遥测分发（引擎 33Hz payload → 已打开对话框）────

    def _set_speaker_peaks(self, speakers: dict | None) -> None:
        meter = getattr(self, "studio_meter", self._meter)
        if speakers:
            if getattr(self, "_speaker_meter_tip", None) is None:
                self._speaker_meter_tip = meter.toolTip()
            meter.setToolTip(" · ".join(f"{name} {db:.1f} dBFS" for name, db in speakers.items()))
        elif getattr(self, "_speaker_meter_tip", None) is not None:
            meter.setToolTip(self._speaker_meter_tip)
            self._speaker_meter_tip = None

    def update_track_telemetry(self, payload: dict) -> None:
        """Send measured values to the open panels, keyed by effect instance."""
        silent = bool(payload.get("silent"))
        self._set_speaker_peaks(payload.get("speakers"))
        reduction = payload.get("gr") or {}
        detectors = payload.get("sc") or {}
        taps = payload.get("taps") or {}
        for row in range(self._effect_list.count()):
            item = self._effect_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole + 1) != "vst3_hosted":
                continue
            tap = taps.get(item.data(Qt.ItemDataRole.UserRole))
            if tap is None and not silent:
                continue
            detail = (tap or {}).get("analysis") or {}
            error = detail.get("error")
            if error:
                state = I18N.tr("audio.fx.vst3.host_muted", "Host muted this insert")
                item.setForeground(QColor("#F09666"))
                item.setToolTip(f"{item.text()} · {state}\n{error}")
                continue
            active = bool(item.data(Qt.ItemDataRole.UserRole + 2))
            item.setForeground(QColor(C.TEXT_PRIMARY if active else "#666670"))
            state = I18N.tr("audio.mixer.active", "Active") if active else I18N.tr("audio.mixer.bypassed", "Bypassed")
            if detail.get("pending"):
                state = I18N.tr("audio.fx.vst3.checking", "Checking plug-in in a separate process…")
            item.setToolTip(f"{item.text()} · {state}")
        spectrum = payload.get("spectrum") or {}
        freqs = [] if silent else spectrum.get("freqs", [])
        db = [] if silent else spectrum.get("db", [])
        pre_db = -100.0 if silent else payload.get("pre", -100.0)
        peak = payload.get("peak") or []
        out_db = -100.0 if silent else max(peak, default=-100.0)

        def tap_level(instance_id: str, key: str, fallback: float) -> float:
            tap = taps.get(instance_id)
            if silent or (tap and tap.get("bypassed")):
                return -100.0
            return float(tap.get(key, fallback)) if tap else fallback

        for dialog in self._iter_eq_dialogs():
            panel = getattr(dialog, "panel", dialog)
            instance_tap = taps.get(getattr(dialog, "_instance_id", "")) or {}
            measured = instance_tap.get("spectrum") or {}
            if silent or instance_tap.get("bypassed"):
                panel.update_spectrum([], [])
            elif measured:
                panel.update_spectrum(measured.get("freqs", []), measured.get("output_db", []))
            elif not instance_tap and spectrum:
                # The dedicated channel-strip EQ has a track tap rather than
                # an insert-chain ID. Insert windows wait for their own tap.
                panel.update_spectrum(freqs, db)
            if silent and hasattr(panel, "reset_peak_hold"):
                panel.reset_peak_hold()
            panel.update_output_meter(tap_level(getattr(dialog, "_instance_id", ""), "out_db", out_db))
        if self._dynamics_dialog is not None and self._dynamics_dialog.isVisible():
            ids = self._dynamics_instance_ids()
            eg = ids.get("expander", "")
            comp = ids.get("compressor", "")
            lim = ids.get("limiter", "")
            panel = getattr(self._dynamics_dialog, "panel", self._dynamics_dialog)
            panel.update_meters(
                input_db=tap_level(eg, "in_db", pre_db),
                output_db=tap_level(lim, "out_db", out_db),
                gr_eg_db=reduction.get(eg, 0.0),
                gr_comp_db=reduction.get(comp, 0.0),
                gr_lim_db=reduction.get(lim, 0.0),
                sidechain_db=detectors.get(comp, -100.0),
            )

        for kind, instance_id, dialog in self._iter_insert_dialogs():
            if kind == "strip_eq":
                continue
            tap = taps.get(instance_id) or {}
            analysis = {} if silent or tap.get("bypassed") else tap.get("analysis") or {}
            # Legacy reverb and delay meters present track output before their tap is ready.
            fallback = out_db if kind in ("reverb", "delay") else -100.0
            input_level = tap_level(instance_id, "in_db", -100.0)
            output_level = tap_level(instance_id, "out_db", fallback)
            gr = 0.0 if silent or tap.get("bypassed") else reduction.get(instance_id, 0.0)
            detector = -100.0 if silent or tap.get("bypassed") else detectors.get(instance_id, -100.0)
            panel = getattr(dialog, "panel", dialog)

            if kind == "convolution_reverb" and hasattr(dialog, "update_resource_status"):
                dialog.update_resource_status(tap.get("analysis") or {})
            if kind in ("compressor", "expander", "noise_gate", "limiter", "dynamics"):
                if hasattr(panel, "update_meters"):
                    panel.update_meters(
                        input_db=input_level, output_db=output_level, sidechain_db=detector,
                        gr_comp_db=gr if kind in ("compressor", "dynamics") else 0.0,
                        gr_eg_db=gr if kind in ("expander", "noise_gate") else 0.0,
                        gr_lim_db=gr if kind == "limiter" else 0.0,
                    )
                elif kind == "compressor" and hasattr(dialog, "set_sidechain_level"):
                    dialog.set_sidechain_level(detector)
            elif kind in ("reverb", "saturation"):
                panel.update_output_meter(output_level)
                if kind == "saturation" and (silent or spectrum):
                    panel.update_spectrum(freqs, db)
            elif kind == "delay":
                panel.update_output_meters(output_level, output_level)
            elif kind == "voice_changer":
                confidence = max(0.0, min(100.0, float(analysis.get("confidence", 0.0)) * 100.0))
                # Spectra are optional in the engine payload. Keep a floor sample
                # for the vocoder bars while no measured spectrum is available.
                panel.update_analysis(
                    analysis.get("spectrum_hz", []),
                    analysis.get("input_spectrum_db", []),
                    analysis.get("output_spectrum_db", [-100.0]),
                    float(analysis.get("f0_in_hz") or 0.0),
                    float(analysis.get("f0_out_hz") or 0.0),
                    clarity_pct=confidence,
                )
                panel.update_meters(
                    input_level, output_level,
                    formant_activity=100.0 if analysis.get("formant_active") else 0.0,
                    pitch_activity=100.0 if analysis.get("pitch_active") else 0.0,
                    vocoder_activity=100.0 if analysis.get("vocoder_active") else 0.0,
                    clarity_pct=confidence,
                )
            elif kind == "spectral_mangler":
                panel.update_telemetry(
                    input_level, output_level,
                    wave_samples=analysis.get("waveform", []), grains=analysis.get("grains", []),
                )
            elif kind == "de_esser":
                panel.update_meters(detector, -abs(analysis.get("gain_reduction_db", gr)), output_level)
            elif kind == "dynamic_eq":
                if silent or spectrum:
                    panel.update_spectrum(freqs, db, sc_db=[])
                offsets = [0.0] * 6
                offsets[3] = analysis.get("dynamic_gain_db", 0.0)
                panel.update_dynamic_meters(input_level, output_level, offsets)
            elif kind == "bus_comp":
                panel.update_meters(input_level, gr, output_level)
            elif kind == "multiband_comp":
                band_gr = [analysis.get(f"band_{index}_gr_db", 0.0) for index in range(4)]
                panel.update_meters(input_level, band_gr, output_level)
            elif kind in ("chorus_flanger", "phaser", "transient_designer", "true_peak_limiter"):
                panel.meter_input.setValue(input_level)
                if kind == "true_peak_limiter":
                    panel.meter_gr.setValue(analysis.get("gain_reduction_db", gr))
                else:
                    panel.meter_output.setValue(output_level)

    def _iter_insert_dialogs(self) -> Iterator[tuple[str, str, QDialog]]:
        for attr in self._EFFECT_DIALOG_ATTRS:
            for instance_id, dialog in list(getattr(self, attr, {}).items()):
                kind = getattr(dialog, "fx_effect_type", self._EFFECT_DIALOG_TYPES.get(attr))
                if kind and (not hasattr(dialog, "isVisible") or dialog.isVisible()):
                    yield kind, instance_id, dialog

    def _iter_eq_dialogs(self) -> list:
        """当前可见的 EQ 对话框（胶囊单例 + FX 列表多实例）。"""
        dlgs = []
        if self._eq_dialog is not None:
            try:
                if self._eq_dialog.isVisible():
                    dlgs.append(self._eq_dialog)
            except RuntimeError:
                # C++ 对象已销毁而引用滞留（destroyed 未清属性）
                self._eq_dialog = None
        for dlg in list(getattr(self, '_strip_eq_dialogs', {}).values()):
            try:
                if dlg.isVisible():
                    dlgs.append(dlg)
            except RuntimeError:
                continue
        return dlgs

    def _dynamics_instance_ids(self) -> dict:
        """DY 面板管理的三个实例 id（按 effect_type）。"""
        iids: dict = {}
        if self._effect_chain is None:
            return iids
        for e in self._effect_chain.effects:
            if getattr(e, "panel", "fx") != "dynamics":
                continue
            if e.effect_type in ("expander", "compressor", "limiter", "noise_gate"):
                iids.setdefault(e.effect_type, e.instance_id)
        if "expander" not in iids and "noise_gate" in iids:
            iids["expander"] = iids["noise_gate"]
        return iids

    def has_spectrum_consumers(self) -> bool:
        return bool(self._iter_eq_dialogs()) or any(
            effect_type in _SPECTRUM_EFFECT_TYPES
            for effect_type, _instance_id, _dialog in self._iter_insert_dialogs()
        )

    def _update_spectrum_gating(self) -> None:
        """Enable the existing track FFT tap while a consumer is visible."""
        self.spectrum_metering_changed.emit(self.has_spectrum_consumers())

    def _safe_spectrum_gating(self, *_args) -> None:
        """destroyed/finished 信号回调：宿主 widget 可能已销毁（应用关闭期）。

        接受任意位置参数（destroyed 传 QObject、finished 传 int）。
        """
        try:
            self._update_spectrum_gating()
        except RuntimeError:
            pass

    def set_sidechain_sources(self, sources: list[tuple[str, str]]) -> None:
        """下发侧链源列表 [(track_id, name), ...] 给已打开的动态对话框。"""
        # 排除本轨（自键控无意义，引擎侧已忽略同轨引用）
        filtered = tuple((tid, name) for tid, name in sources if tid != self._track_id)
        if filtered == self._sidechain_sources:
            return
        self._sidechain_sources = filtered
        if self._dynamics_dialog is not None:
            try:
                self._dynamics_dialog.set_sidechain_sources(filtered)
            except RuntimeError:
                self._dynamics_dialog = None
        for kind, _instance_id, dlg in self._iter_insert_dialogs():
            if kind not in ("compressor", "dynamics"):
                continue
            try:
                dlg.set_sidechain_sources(filtered)
            except RuntimeError:
                continue

    # ========== 事件处理 ==========

    def _on_mute_toggled(self, checked: bool) -> None:
        self._is_muted = checked
        self._update_controls_enabled()
        self._update_track_label_style()
        self.mute_toggled.emit(self._track_id, checked)

    def _on_solo_toggled(self, checked: bool) -> None:
        self._is_solo = checked
        self._update_track_label_style()
        self.solo_toggled.emit(self._track_id, checked)

    def _on_record_arm_toggled(self, checked: bool) -> None:
        """R 按钮（Record Arm）切换。占位：仅视觉状态，录音路由待实现。"""
        self._is_armed = checked
        self._update_track_label_style()
        self.record_arm_toggled.emit(self._track_id, checked)

    # ---- 音量推子：三段式节流 ----

    def _on_volume_pressed(self) -> None:
        """推子按下：进入拖动状态。"""
        self._volume_dragging["active"] = True

    def _on_volume_changed(self, value: int) -> None:
        """推子值变化。

        拖动期间：只同步 spinbox 视觉 + 发 volume_preview（实时听感/视觉）。
        非拖动（spinbox 输入触发的联动、键盘）：发 volume_changed（直接提交）。
        外部 set_volume_linear 回填已 blockSignals，不会进入这里。
        """
        db_val = self._slider_to_db(value)
        self._volume_db = db_val
        # 同步 spinbox 视觉
        self._volume_spin.blockSignals(True)
        self._volume_spin.setValue(round(db_val, 1))
        self._volume_spin.blockSignals(False)

        if self._volume_dragging["active"]:
            # 拖动期：纯预览，不进命令栈
            self.volume_preview.emit(self._track_id, db_val)
            # B4: 拖动期在鼠标旁显示实时 dB 数值（专业混音器约定），
            # 用户视线在推子上时无需扭头看 spinbox。
            from PyQt6.QtGui import QCursor
            from PyQt6.QtWidgets import QToolTip
            tip_text = f"{db_val:+.1f} dB"
            QToolTip.showText(QCursor.pos(), tip_text, self._volume_fader)
        else:
            # 非拖动（键盘/spinbox 联动）：直接提交
            self.volume_changed.emit(self._track_id, db_val)

    def _on_volume_spin_changed(self, value: float) -> None:
        """spinbox 键盘输入：同步推子视觉，并提交（spinbox 是离散输入，无拖动概念）。"""
        slider_val = self._db_to_slider(float(value))
        self._volume_fader.blockSignals(True)
        self._volume_fader.setValue(slider_val)
        self._volume_fader.blockSignals(False)
        self._volume_db = float(value)
        self.volume_changed.emit(self._track_id, float(value))

    def _on_volume_released(self) -> None:
        """推子释放：提交最终值（一次命令）。"""
        if not self._volume_dragging["active"]:
            return
        self._volume_dragging["active"] = False
        # B4: 松手后隐藏拖动期 tooltip
        from PyQt6.QtWidgets import QToolTip
        QToolTip.hideText()
        self.volume_changed.emit(self._track_id, self._volume_db)

    # ---- 平衡推子：三段式节流 ----

    def _on_balance_pressed(self) -> None:
        self._balance_dragging["active"] = True

    def _on_balance_changed(self, value: int) -> None:
        self._balance = value
        # AUD-37901: 拖动期只同步 spinbox 视觉（blockSignals），不让
        # _balance_spin.valueChanged 触发 _on_balance_spin_changed 的
        # commit 出口 —— 与 _on_volume_changed 的范式一致。
        if hasattr(self, '_balance_spin'):
            self._balance_spin.blockSignals(True)
            self._balance_spin.setValue(value)
            self._balance_spin.blockSignals(False)
        if self._balance_dragging["active"]:
            self.balance_preview.emit(self._track_id, value)
        else:
            self.balance_changed.emit(self._track_id, value)

    def _on_balance_spin_changed(self, value: int) -> None:
        self._balance = value
        self._balance_fader.blockSignals(True)
        self._balance_fader.setValue(value)
        self._balance_fader.blockSignals(False)
        self.balance_changed.emit(self._track_id, value)

    def _on_balance_released(self) -> None:
        if not self._balance_dragging["active"]:
            return
        self._balance_dragging["active"] = False
        self.balance_changed.emit(self._track_id, self._balance)

    # ========== 私有方法 ==========

    # ========== W16: 标签右键菜单 / 双击重命名 ==========

    def _on_label_context_menu(self, pos: QPoint) -> None:
        """Track actions and the host mixer preferences, including on Master."""
        menu = QMenu(self)
        if self._is_master:
            self.mixer_menu_requested.emit(menu)
            source = self.sender()
            label = source if isinstance(source, QLabel) else self._track_tag_label
            menu.exec(label.mapToGlobal(pos))
            return
        act_rename = QAction(I18N.tr("audio.mixer.rename", "Rename..."), self)
        act_color = QAction(I18N.tr("audio.mixer.set_color", "Set Color..."), self)
        act_clear_color = QAction(I18N.tr("audio.mixer.clear_color", "Clear Color"), self)
        act_automation = QAction(I18N.tr("audio.mixer.automation", "Automation..."), self)
        act_routing = QAction(I18N.tr("audio.mixer.routing", "Routing..."), self)
        act_hide = QAction(I18N.tr("audio.mixer.hide_strip", "Hide Strip"), self)
        menu.addAction(act_rename)
        menu.addAction(act_color)
        menu.addAction(act_clear_color)
        menu.addSeparator()
        menu.addAction(act_automation)
        menu.addAction(act_routing)
        menu.addSeparator()
        menu.addAction(act_hide)

        # 连接动作
        act_rename.triggered.connect(lambda: self._do_rename())
        act_color.triggered.connect(lambda: self._do_set_color())
        act_clear_color.triggered.connect(lambda: self.color_changed.emit(self._track_id, None))
        act_automation.triggered.connect(lambda: self.show_automation.emit(self._track_id))
        act_routing.triggered.connect(lambda: self.show_routing.emit(self._track_id))
        # hide_strip 由 panel 处理（通过信号上抛）
        act_hide.triggered.connect(lambda: self.hide_requested.emit(self._track_id))

        self.mixer_menu_requested.emit(menu)
        # AUD-37905: pos 属于发射信号的标签自身（_track_label 或
        # _track_tag_label）的坐标系 —— 用 sender() 映射，右键 tag 标签时
        # 菜单不再偏移到 track_label 的原点。
        source = self.sender()
        if isinstance(source, QLabel):
            menu.exec(source.mapToGlobal(pos))
        else:
            menu.exec(self._track_label.mapToGlobal(pos))

    def _on_label_double_clicked(self, event) -> None:
        """双击标签：内联重命名。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._do_rename()
        else:
            # 调用 QLabel 默认行为
            QLabel.mouseDoubleClickEvent(self._track_label, event)

    def _do_rename(self) -> None:
        """弹出输入框重命名轨道。"""
        new_name, ok = QInputDialog.getText(
            self,
            I18N.tr("audio.mixer.rename_title", "Rename Track"),
            I18N.tr("audio.mixer.rename_prompt", "New name:"),
            text=self._track_name,
        )
        if ok and new_name and new_name != self._track_name:
            self.name_changed.emit(self._track_id, new_name)

    def _do_set_color(self) -> None:
        """弹出颜色对话框选色。"""
        initial = QColor(resolve_track_color(self._track_id, self._track_color))
        color = QColorDialog.getColor(initial, self, I18N.tr("audio.mixer.color_title", "Track Color"))
        if color.isValid():
            self.color_changed.emit(self._track_id, color.name())

    def _update_badges_style(self) -> None:
        """更新 FX / DY / EQ 状态胶囊样式（QToolButton）。

        统一规则：青色=该类下有「启用的」效果器，灰色=全部旁路或没有。
        - EQ 胶囊：chain 中存在启用的 strip_eq 实例 → 青
        - DY 胶囊：chain 中存在启用的、DY 管理的（panel="dynamics"）
                   compressor/noise_gate/limiter 任一 → 青
        - FX 胶囊：chain 中存在任意启用的、FX 列表管理的（panel="fx"）效果器 → 青；
                   点击 FX 一键全开/全关
        """
        chain_effects = self._effect_chain.effects if self._effect_chain is not None else ()

        def _enabled(e: Any) -> bool:
            return bool(e.enabled) and (
                bool(e.params.get("enabled", True)) if e.params else True)

        def _panel(e: Any) -> str:
            return getattr(e, "panel", "fx")

        eq_active = any(
            e.effect_type == "strip_eq" and _enabled(e) and _panel(e) == "eq"
            for e in chain_effects
        )
        dy_active = any(
            e.effect_type in ("expander", "compressor", "noise_gate", "limiter")
            and _enabled(e) and _panel(e) == "dynamics"
            for e in chain_effects
        )
        fx_active = any(_enabled(e) and _panel(e) == "fx" for e in chain_effects)

        # AM-01: EQ 依赖 scipy 后端。后端不可用时即便用户启用 EQ 也是静默旁路，
        # 此时 badge 不能显示成"已激活"青色——改用降级告警色，告知用户 EQ 未生效。
        # AUD-37903: 探测默认值改为 fail-closed（False）—— 此前默认 True，
        # capabilities 模块/探测函数抛 ImportError 时 eq_backend_ok 残留 True，
        # SciPy 缺失的降级警示（橙色 badge）永不出现。探测异常时按"不可用"处理
        # 并记日志；eq_backend_ok 在 except 分支显式置 False。
        eq_backend_ok = False
        try:
            from src.features.edit.core.audio_capabilities import (
                is_scipy_available_for_dsp,
            )
            eq_backend_ok = bool(is_scipy_available_for_dsp())
        except ImportError:
            logger.warning(
                "AUD-37903: audio_capabilities 导入失败，EQ 后端按不可用处理"
                "（降级警示）（track=%s）",
                self._track_id,
            )
            eq_backend_ok = False
        except AttributeError:
            # capabilities 模块存在但访问器缺失/签名变化 —— 同样 fail-closed
            logger.warning(
                "AUD-37903: is_scipy_available_for_dsp 访问失败，EQ 后端按不可用"
                "处理（降级警示）（track=%s）",
                self._track_id,
            )
            eq_backend_ok = False
        eq_degraded = eq_active and not eq_backend_ok

        inactive_style = f"""
            QToolButton {{
                font-size: 9px;
                font-weight: bold;
                background-color: {C.BG_INPUT};
                color: {C.TEXT_DIM};
                border-radius: 1px;
                border: 1px solid {C.PANEL_DIVIDER};
                padding: 0px;
            }}
            QToolButton:disabled {{
                background-color: {C.BG_DARK};
                color: {C.TEXT_HINT};
            }}
            QToolButton:hover:enabled {{
                background-color: {C.BG_ACTIVE};
            }}
        """
        cyan_active_style = """
            QToolButton {
                font-size: 9px;
                font-weight: bold;
                background-color: #1b3b41;
                color: #00d2c1;
                border-radius: 3px;
                border: 1px solid #32646c;
                padding: 0px;
            }
            QToolButton:hover {
                background-color: #28535b;
            }
        """
        # AM-01: 后端不可用时的降级告警色（橙色 + 虚线边框），
        # 区别于青色"已生效"与灰色"未启用"。
        degraded_warning_style = """
            QToolButton {
                font-size: 9px;
                font-weight: bold;
                background-color: #664400;
                color: #ffcc66;
                border-radius: 2px;
                border: 1px dashed #ffaa00;
                padding: 0px;
            }
            QToolButton:hover {
                background-color: #774f00;
            }
        """

        if hasattr(self, '_fx_badge'):
            self._fx_badge.setStyleSheet(cyan_active_style if fx_active else inactive_style)
        if hasattr(self, '_dy_badge'):
            self._dy_badge.setStyleSheet(cyan_active_style if dy_active else inactive_style)
        if hasattr(self, '_eq_badge'):
            if eq_degraded:
                self._eq_badge.setStyleSheet(degraded_warning_style)
                self._eq_badge.setToolTip(I18N.tr(
                    "audio.mixer.eq_backend_degraded_tip",
                    "EQ 已启用但 DSP 后端（SciPy）不可用，当前为静默旁路。\n"
                    "请安装 scipy 以使 EQ 生效。",
                ))
            else:
                self._eq_badge.setStyleSheet(cyan_active_style if eq_active else inactive_style)
                # 清除降级 tooltip，避免状态切换后残留
                self._eq_badge.setToolTip(I18N.tr("audio.mixer.open_parametric_eq", "Open parametric EQ"))

    def _update_track_label(self) -> None:
        """更新轨道名标签，避免与顶部 tag 重复显示。

        标准布局始终保留一行轨名高度，空名/Master 不重复文字。
        长名称省略并保留完整 tooltip；紧凑模式统一隐藏该行。
        """
        tag = self._track_tag or ("Master" if self._is_master else "")
        name = self._track_name or ""
        narrow = getattr(self, '_is_narrow', False)
        # tag 已表达身份（Master / A1）或 name 与 tag 重复时，隐藏下方标签。
        full_name = name if name and name != tag else ""
        self._track_tag_label.setToolTip(f"{tag}\n{name}" if name and name != tag else tag)
        self._track_label.setToolTip(full_name)
        self._track_label.setVisible(not narrow)
        available = max(20, self.width() - self.contentsMargins().left()
                        - self.contentsMargins().right() - 6)
        self._track_label.setText(self._track_label.fontMetrics().elidedText(
            full_name, Qt.TextElideMode.ElideRight, available))

    def _update_track_label_style(self) -> None:
        """更新标签样式以反映状态（达芬奇风格无框高亮文本）"""
        if self._is_armed:
            color_code = "#c62828"
        elif self._is_muted:
            color_code = "#ef6c00"
        elif self._is_solo:
            color_code = "#388e3c"
        else:
            color_code = getattr(self, '_track_color', None) or ("#e5b532" if not self._is_master else "#ffffff")

        style = (
            f"background-color: transparent; "
            f"color: {color_code}; "
            f"font-size: 10px; font-weight: bold; "
            f"padding: 1px; margin: 0;"
        )
        self._track_label.setStyleSheet(style)
        accent = "#43c9c0" if self._is_master else resolve_track_color(self._track_id, self._track_color)
        self._track_tag_label.setStyleSheet(f"""
            QLabel {{ background: {C.BG_DARK}; color: {C.TEXT_PRIMARY}; font-size: 10px;
                font-weight: 600; border: none; border-top: 2px solid {accent};
                border-bottom: 1px solid {C.PANEL_DIVIDER};
                padding: 4px 2px; min-height: 12px; }}
        """)

    def _update_controls_enabled(self) -> None:
        """更新控件启用状态"""
        # Mute gates sound, while controls remain editable for preparing the mix.
        self._volume_fader.setEnabled(True)
        self._volume_spin.setEnabled(True)
        self._meter.setEnabled(not self._is_muted)
        self._db_label.setEnabled(True)

        if hasattr(self, '_balance_fader'):
            self._balance_fader.setEnabled(True)
        if hasattr(self, '_balance_spin'):
            self._balance_spin.setEnabled(True)

    def _db_to_slider(self, db: float) -> int:
        """dB 值转换为滑块位置（0-10000）。

        达芬奇同款 IEC 对齐标度：推子 0dB 横线与电平表 0dB 横线（满量程顶部）对齐。
        - 正 dB 区间 (0, +6] → [8000, 10000]，独立线性映射到顶部 20% 行程（增益裕量区）。
        - 负 dB 区间 [VOLUME_DB_MIN, 0] → [0, 8000]，走 IEC 标度并归一化到
          [iec_scale(MIN), iec_scale(0)]，使 -60dB 落在 slider=0（无底部死区），
          同时推子刻度与电平表刻度保持良好近似对齐。

        关键锚点（已脚本验证误差 0.000 dB）：
          +6 dB → 10000（顶部，推子上限）
           0 dB → 8000（unity，对应电平表顶部）
          -6 dB → ~6800
         -12 dB → ~5600
         -60 dB → 0（底部）
        """
        db = max(VOLUME_DB_MIN, min(VOLUME_DB_MAX, float(db)))

        if db >= 0.0:
            # [0, +6] → [8000, 10000]，独立线性映射。
            pct = db / VOLUME_DB_MAX  # 0..1
            return int(round(8000 + pct * 2000))

        # 负 dB：走 IEC 标度并归一化，消除底部死区。
        # iec_scale(VOLUME_DB_MIN) 是推子底端的 IEC 标度值（如 -60 → 0.025），
        # 把 [iec_scale(MIN), iec_scale(0)] = [0.025, 1.0] 线性拉伸到 [0, 1]。
        s_raw = iec_scale(db)                       # 0dB→1.0, -60→0.025
        s_min = iec_scale(VOLUME_DB_MIN)            # 0.025
        s_norm = (s_raw - s_min) / (1.0 - s_min)    # 0dB→1.0, -60→0.0
        return int(round(s_norm * 8000))

    def _slider_to_db(self, value: int) -> float:
        """滑块位置转换为 dB 值（_db_to_slider 的逆函数）。"""
        v = max(0, min(10000, int(value)))

        if v >= 8000:
            # [8000, 10000] → [0, +6]
            pct = (v - 8000) / 2000.0
            return VOLUME_DB_MAX * pct

        # [0, 8000) → 归一化 IEC 逆映射 → [VOLUME_DB_MIN, 0)
        # 先把 slider 归一值反归一化回真实 IEC 标度，再走 iec_inverse。
        s_min = iec_scale(VOLUME_DB_MIN)            # 0.025
        s_norm = v / 8000.0                         # [0, 1)
        s_raw = s_min + s_norm * (1.0 - s_min)      # 反归一化回 [0.025, 1.0)
        db = iec_inverse(s_raw)
        return max(VOLUME_DB_MIN, min(0.0, db))
