"""
Track Mixer Widget - 复刻 Kdenlive MixerWidget

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

import logging
import math
from typing import Optional, Any
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QToolButton, QSpinBox, QDoubleSpinBox,
    QMenu, QInputDialog, QColorDialog,
    QListWidget, QListWidgetItem,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint
from PyQt6.QtGui import QColor, QAction

from src.shared.ui.audio_mixer.audio_fader import AudioFader
from src.shared.ui.audio_mixer.audio_level_meter import AudioLevelMeter, TickLabelsMode, iec_scale, iec_inverse
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import MIXER_TRACK_BTN_STYLE, MIXER_SPIN_STYLE

logger = logging.getLogger(__name__)


# W26: 效果器目录（effect_type → 列表显示名）。用于内嵌列表 + 添加菜单。
EFFECT_SHORT_NAMES = {
    "strip_eq": "Graphic EQ",
    "limiter": "Limiter",
    "compressor": "Compressor",
    "reverb": "Reverb",
    "delay": "Delay",
    "saturation": "Saturation",
    "imager": "Imager",
    "noise_gate": "Noise Gate",
}

# 空列表时插入的 "+" 占位项的 UserRole 标记值（用于区分真实效果器实例）
_ADD_PLACEHOLDER = "__add__"


def _effect_default_params(effect_type: str) -> dict:
    """效果器默认参数工厂（延迟 import 避免 timeline_v2 前向引用）。"""
    from src.features.edit.core.timeline_v2 import (
        StripEQParams, LimiterParams, CompressorParams, ReverbParams,
        DelayParams, SaturationParams, ImagerParams, NoiseGateParams,
    )
    if effect_type == "strip_eq":
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
    elif effect_type == "noise_gate":
        return NoiseGateParams.default().to_dict()
    return {}


def _effect_display_name(effect_type: str) -> str:
    """效果器完整显示名（用于菜单/对话框标题）。"""
    names = {
        "strip_eq": I18N.tr("audio.fx.strip_eq", "Graphic EQ"),
        "limiter": I18N.tr("audio.fx.limiter", "Limiter"),
        "compressor": I18N.tr("audio.fx.compressor", "Compressor"),
        "reverb": I18N.tr("audio.fx.reverb", "Reverb"),
        "delay": I18N.tr("audio.fx.delay", "Delay"),
        "saturation": I18N.tr("audio.fx.saturation", "Saturation"),
        "imager": I18N.tr("audio.fx.imager", "Imager"),
        "noise_gate": I18N.tr("audio.fx.noise_gate", "Noise Gate"),
    }
    return names.get(effect_type, effect_type)


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


def db_to_linear(db: float) -> float:
    """dB → 线性增益，结果 clamp 到 [VOLUME_LINEAR_MIN, VOLUME_LINEAR_MAX]。"""
    v = 10.0 ** (float(db) / 20.0)
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

    # 信号（track_id 统一为 str，与 EditTrack.track_id UUID 一致）
    mute_toggled = pyqtSignal(str, bool)      # track_id, muted
    solo_toggled = pyqtSignal(str, bool)      # track_id, solo
    record_arm_toggled = pyqtSignal(str, bool)  # track_id, armed（Record Arm 占位，录音路由待实现）
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

    def __init__(
        self,
        track_id: str,
        track_name: str,
        track_tag: str = "",
        parent: Optional[QWidget] = None,
        is_master: bool = False
    ) -> None:
        super().__init__(parent)
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
        self._eq_dialog: Optional[Any] = None  # StripEQDialog | None
        # W26: FX 机架效果器链（None=未使用）+ Limiter 对话框实例引用
        self._effect_chain: Optional[Any] = None  # EffectChainParams | None
        self._limiter_dialogs: dict = {}  # {instance_id: LimiterDialog}
        self._dynamics_dialog: Optional[Any] = None  # DynamicsDialog | None（DY 胶囊单例）
        # W15: 窄/宽布局模式
        self._is_narrow: bool = False
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
        header_layout = QVBoxLayout(header_widget)
        header_layout.setContentsMargins(1, 2, 1, 1)
        header_layout.setSpacing(1)

        # 顶部 Track Tag (A1 / Master / Bus1)
        self._track_tag_label = QLabel()
        self._track_tag_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # 轨道编号 / Master 标题行：
        # 用 stylesheet 的 min-height + 较大 padding 让 Qt 按内容自然撑开，
        # 而非 setFixedHeight 强制框高 —— 后者在 border-top + padding 同时存在时
        # 会让 AlignVCenter 的基线偏低，视觉上文字下半部分像被裁切。
        tag_text = self._track_tag or ("Master" if self._is_master else "A1")
        self._track_tag_label.setText(tag_text)
        tag_color = "#00d2c1" if self._is_master else "#388e3c"
        self._track_tag_label.setStyleSheet(f"""
            QLabel {{
                background-color: #1a1a1e;
                color: #e0e0e0;
                font-size: 10px;
                font-weight: bold;
                border-top: 2px solid {tag_color};
                padding: 5px 2px;
                min-height: 14px;
            }}
        """)
        header_layout.addWidget(self._track_tag_label)

        # 状态 Badge 行 (FX / DY / EQ) —— 可点击胶囊按钮
        # FX/DY 暂为占位（禁用，未来接入效果器/动态处理器时启用）；
        # EQ 承担原 FX 按钮的功能（点击打开 Strip EQ 对话框）。
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
            badge.setFixedHeight(14)
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

        # W26: 内嵌效果器列表（始终可见）。
        # 空列表时在内部居中显示 "+" 占位项（点击弹 Add Effect 菜单，添加后消失）。
        # 右键空白=添加效果器，右键项=删除/旁路/移动，点击项=打开编辑。
        # 高度自适应：由 _refresh_effect_list 按项数动态计算（空列表给一行高度）。
        self._effect_list = QListWidget()
        # 长效果器名直接遮挡（clip），不触发横向滚动条；垂直滚动条仍按需出现
        self._effect_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._effect_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._effect_list.customContextMenuRequested.connect(self._on_effect_list_context_menu)
        self._effect_list.itemClicked.connect(self._on_effect_item_clicked)
        self._effect_list.setStyleSheet("""
            QListWidget {
                background-color: #16161a;
                border: 1px solid #2a2a2e;
                border-radius: 2px;
                font-size: 10px;
                color: #c0c0c8;
                outline: none;
            }
            QListWidget::item {
                padding: 2px 4px;
                border-radius: 2px;
            }
            QListWidget::item:selected {
                background-color: #2a4a48;
                color: #00d2c1;
            }
        """)
        header_layout.addWidget(self._effect_list)
        # 初始刷新一次，设置空列表的自适应高度 + 占位 "+" 项
        self._refresh_effect_list()

        # 轨道名称标签（如 "音频 1"）
        self._track_label = QLabel()
        self._track_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._update_track_label()
        if not self._is_master:
            # 右键菜单和双击重命名同时挂到 _track_label 和 _track_tag_label，
            # 确保窄模式下 _track_label 隐藏后，用户仍可通过 tag 标签（始终可见）
            # 访问重命名/设色/隐藏/automation 菜单。
            self._track_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._track_label.customContextMenuRequested.connect(self._on_label_context_menu)
            self._track_label.mouseDoubleClickEvent = self._on_label_double_clicked
            self._track_tag_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._track_tag_label.customContextMenuRequested.connect(self._on_label_context_menu)
            self._track_tag_label.mouseDoubleClickEvent = self._on_label_double_clicked
        header_layout.addWidget(self._track_label)

        main_layout.addWidget(header_widget)

        # 2. 按钮行 (M / S / R / FX)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(1)
        button_layout.setContentsMargins(1, 1, 1, 1)

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
        # AlignLeft：QVBoxLayout 默认水平居中子布局，Master 只有 1 个按钮时会居中，
        # 与 meter+fader（左对齐）错位。强制左对齐让按钮行与 meter+fader 对齐。
        main_layout.addLayout(button_layout)
        main_layout.setAlignment(button_layout, Qt.AlignmentFlag.AlignLeft)

        # 平衡控制（仅立体声）
        self._balance_widget = self._create_balance_widget()
        main_layout.addWidget(self._balance_widget)

        # 电平表 + 推子
        meter_fader_layout = QHBoxLayout()
        # 电平表贴 strip 左缘（左内边距 0），与 Master 保持一致。
        # 右侧留 1px 内边距给推子刻度。
        meter_fader_layout.setContentsMargins(0, 0, 1, 0)
        meter_fader_layout.setSpacing(3)
        meter_fader_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

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
        self._volume_fader.set_label_formatter(lambda v: f"{abs(int(v))}" if v <= 0 else f"+{int(v)}")
        self._volume_fader.set_value_to_slider_function(lambda db: self._db_to_slider(db))

        meter_fader_layout.addWidget(self._volume_fader, 0)
        # 整个 meter+fader 块在 main_layout(QVBoxLayout) 里靠左对齐 ——
        # 不给 stretch 让 QVBoxLayout 水平拉伸此 layout，否则 meter/fader
        # 会因 spacing 被拉开、推子偏右被裁切。Master 宽度刚好没暴露此问题。
        main_layout.addLayout(meter_fader_layout, 1)
        main_layout.setAlignment(meter_fader_layout, Qt.AlignmentFlag.AlignLeft)

        # 音量显示
        vol_display_layout = QHBoxLayout()
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
        vol_display_layout.addStretch()
        vol_display_layout.addWidget(self._volume_spin)

        self._db_label = QLabel(I18N.tr("audio.mixer.db_unit", "dB"))
        self._db_label.setStyleSheet("color: #66666e; font-size: 9px;")
        self._db_label.setFixedWidth(14)
        vol_display_layout.addWidget(self._db_label)
        vol_display_layout.addStretch()

        main_layout.addLayout(vol_display_layout)

        self._update_size_constraints()
        self._apply_styles()

    def _create_balance_widget(self) -> QWidget:
        """创建平衡控制"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
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

        r_label = QLabel(I18N.tr("audio.mixer.right_short", "R"))
        r_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        r_label.setStyleSheet("color: #88888f; font-size: 8px;")

        label_layout.addWidget(l_label)
        label_layout.addWidget(self._balance_spin)
        label_layout.addWidget(r_label)
        layout.addLayout(label_layout)

        return widget

    def _apply_styles(self) -> None:
        """应用样式"""
        # 使用集中定义的按钮样式
        self._mute_btn.setStyleSheet(MIXER_TRACK_BTN_STYLE)
        if hasattr(self, '_solo_btn'):
            self._solo_btn.setStyleSheet(MIXER_TRACK_BTN_STYLE)
        if hasattr(self, '_monitor_btn'):
            self._monitor_btn.setStyleSheet(MIXER_TRACK_BTN_STYLE)
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
        # 1px 左右内边距 + 3px 列间距，确保电平表的数值与推子刻度互不覆盖。
        meter_fader_w = self._meter.maximumWidth() + 3 + fader_w + 2

        # 紧凑 strip：3 个 20px 按钮 + 2×1px spacing + 2×1px layout margins = 64px。
        btn_row_w = 20 * 3 + 2 * 1 + 2 * 1

        content_width = max(meter_fader_w, btn_row_w, 70)
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
        self.update()

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
        db = linear_to_db(vol)
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
        """EQ 胶囊点击：打开 Graphic EQ 单例对话框（从 chain 找/创建首个 strip_eq 实例）。

        这是 EQ 的快捷单例入口；FX 列表里的 strip_eq 实例（多实例）由
        _open_strip_eq_editor 单独打开，两者共享同一个 effect_chain 存储。
        """
        from src.shared.ui.audio_mixer.strip_eq_dialog import StripEQDialog
        from src.features.edit.core.timeline_v2 import (
            StripEQParams,
        )
        # 若已打开则聚焦
        if self._eq_dialog is not None and self._eq_dialog.isVisible():
            self._eq_dialog.raise_()
            self._eq_dialog.activateWindow()
            return
        # 从 chain 找 EQ 胶囊管理的 strip_eq 实例（panel="eq"）。
        # FX 列表里用户右键添加的 strip_eq（panel="fx"）不归 EQ 胶囊管，互不干扰。
        effects = list(self._effect_chain.effects) if self._effect_chain is not None else []
        eq_inst = next(
            (e for e in effects
             if e.effect_type == "strip_eq" and getattr(e, "panel", "fx") == "eq"),
            None,
        )
        eq_params = StripEQParams.from_dict(eq_inst.params) if (eq_inst and eq_inst.params) else StripEQParams.default()
        self._eq_dialog = StripEQDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            initial_params=eq_params,
            parent=self.window(),
        )
        # EQ 提交：更新 chain 中 strip_eq 实例（找不到则创建）
        self._eq_dialog.strip_eq_committed.connect(self._on_eq_committed_from_badge)
        self._eq_dialog.show()

    def _on_eq_committed_from_badge(self, track_id: str, eq_params: Any) -> None:
        """EQ 对话框提交：更新 chain 中 EQ 胶囊管理的 strip_eq 实例 + emit。"""
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
                effects[idx] = TrackEffectInstance(
                    effect_type="strip_eq", params=new_params,
                    enabled=bool(getattr(eq_params, 'enabled', old.enabled)),
                    order=old.order, instance_id=old.instance_id,
                    panel="eq",
                )
            else:
                # None 表示禁用 → 从 chain 移除 EQ
                effects.pop(idx)
        elif new_params is not None:
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
        """DY 胶囊点击：打开 Dynamics 子面板（Compressor + Noise Gate + Limiter）。"""
        from src.shared.ui.audio_mixer.dynamics_dialog import DynamicsDialog
        if self._dynamics_dialog is not None and self._dynamics_dialog.isVisible():
            self._dynamics_dialog.raise_()
            self._dynamics_dialog.activateWindow()
            return
        self._dynamics_dialog = DynamicsDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            chain=self._effect_chain,
            parent=self.window(),
        )
        self._dynamics_dialog.dynamics_preview.connect(
            lambda tid, c: self._on_dynamics_edited(c, commit=False)
        )
        self._dynamics_dialog.dynamics_committed.connect(
            lambda tid, c: self._on_dynamics_edited(c, commit=True)
        )
        self._dynamics_dialog.show()

    def _on_dynamics_edited(self, chain: Any, commit: bool) -> None:
        """Dynamics 面板提交/预览：更新本地 chain + emit 信号。"""
        self._effect_chain = chain
        self._update_badges_style()
        if commit:
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def set_effect_chain(self, chain: Any) -> None:
        """W26: 外部静默回填 FX 机架效果器链（timeline 变化后同步显示）。

        Args:
            chain: EffectChainParams | None
        """
        self._effect_chain = chain
        # 同步内嵌列表显示（blockSignals 防回环）
        self._refresh_effect_list()
        # 同步 DY/Dynamics 面板（若已打开）
        if self._dynamics_dialog is not None:
            # AUD-37903: 不再用宽泛 except Exception: pass 吞错 —— 只捕获
            # 回填路径实际可能抛出的属性/键/类型错误，并记日志留痕。
            try:
                self._dynamics_dialog.update_chain(chain)
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.warning(
                    "AUD-37903: dynamics 面板回填 chain 失败（track=%s）",
                    self._track_id,
                    exc_info=True,
                )
        # 同步 EQ 胶囊对话框（若已打开）——从 chain 里 panel="eq" 的 strip_eq 实例回填
        if self._eq_dialog is not None and chain is not None:
            try:
                from src.features.edit.core.timeline_v2 import StripEQParams
                eq_inst = next(
                    (e for e in chain.effects
                     if e.effect_type == "strip_eq" and getattr(e, "panel", "fx") == "eq"),
                    None,
                )
                if eq_inst is not None and eq_inst.params:
                    self._eq_dialog._current = StripEQParams.from_dict(eq_inst.params)
                    self._eq_dialog._load_params_to_ui()
            except (AttributeError, KeyError, TypeError, ValueError):
                # AUD-37903: 同上 —— 具体异常类型 + 日志，不再静默吞掉
                logger.warning(
                    "AUD-37903: EQ 胶囊对话框回填失败（track=%s）",
                    self._track_id,
                    exc_info=True,
                )
        # 同步胶囊高亮（EQ/DY 激活状态依赖 chain）
        if hasattr(self, '_eq_badge'):
            self._update_badges_style()

    def _refresh_effect_list(self) -> None:
        """根据 self._effect_chain 刷新内嵌效果器列表 UI。

        只显示 panel="fx" 的实例（FX 列表多实例，由用户右键添加）。
        DY 胶囊（panel="dynamics"）和 EQ 胶囊（panel="eq"）管理的实例不在此显示，
        它们是独立的快捷单例入口，与 FX 列表互不干扰。
        """
        self._effect_list.blockSignals(True)
        self._effect_list.clear()
        effects = self._effect_chain.effects if self._effect_chain else ()
        for inst in effects:
            # 胶囊管理的实例（panel="dynamics"/"eq"）不在 FX 列表显示
            if getattr(inst, "panel", "fx") != "fx":
                continue
            short = EFFECT_SHORT_NAMES.get(inst.effect_type, inst.effect_type)
            # 旁路状态读 params.enabled（与效果器对话框开关、右键 Bypass 三者统一的真相源）
            p_enabled = bool(inst.params.get("enabled", True)) if inst.params else True
            label = short  # 旁路不加方括号，仅用灰色区分
            item = QListWidgetItem(label)
            # 存 instance_id 到 data，供点击/右键定位
            item.setData(Qt.ItemDataRole.UserRole, inst.instance_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, inst.effect_type)
            if not p_enabled:
                # 旁路项灰色
                item.setForeground(QColor("#666670"))
            self._effect_list.addItem(item)
        self._effect_list.blockSignals(False)
        # 高度自适应：按项数动态调整（单行高度 × 项数 + 框架边距）。
        # 空列表插入居中的 "+" 占位项（点击弹 Add Effect 菜单），给一行最小高度。
        count = self._effect_list.count()
        if count == 0:
            # 空列表：插入 "+" 占位项，居中显示，点击触发 Add Effect 菜单
            add_item = QListWidgetItem("+")
            add_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            add_item.setData(Qt.ItemDataRole.UserRole, _ADD_PLACEHOLDER)
            add_item.setForeground(QColor("#88888f"))
            self._effect_list.addItem(add_item)
            self._effect_list.setFixedHeight(24)
        else:
            row_h = self._effect_list.sizeHintForRow(0)
            # 2× frameWidth（边框）+ 项数 × 行高；多留一行高度方便右键空白
            frame = self._effect_list.frameWidth() * 2
            self._effect_list.setFixedHeight(frame + row_h * count + 4)
        # 胶囊高亮同样依赖 params.enabled，与列表显示同源，一并刷新保持同步
        if hasattr(self, '_fx_badge'):
            self._update_badges_style()

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

        def _is_enabled(e: Any) -> bool:
            return bool(e.params.get("enabled", True)) if e.params else True

        # 只看 FX 列表管理的实例（panel="fx"）
        fx_effects = [e for e in effects if getattr(e, "panel", "fx") == "fx"]
        if not fx_effects:
            return
        any_enabled = any(_is_enabled(e) for e in fx_effects)
        target_enabled = not any_enabled  # 有启用的→全关；全关→全开

        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        # 只改 panel="fx" 的实例，其余（dynamics/eq）原样保留（含 panel 字段）
        changed_ids = set()
        new_effects = []
        for e in effects:
            if getattr(e, "panel", "fx") == "fx":
                new_effects.append(TrackEffectInstance(
                    effect_type=e.effect_type,
                    params={**(e.params or {}), "enabled": target_enabled},
                    enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                    panel="fx",
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
        """W26: 点击效果器列表项 → 打开对应编辑界面。

        所有效果器（含 strip_eq）均可在列表点击编辑，支持多实例。
        EQ 胶囊/DY 胶囊仍作为快捷单例入口独立存在。
        """
        instance_id = item.data(Qt.ItemDataRole.UserRole)
        # 空列表的 "+" 占位项：点击弹 Add Effect 菜单
        if instance_id == _ADD_PLACEHOLDER:
            self._show_add_effect_menu()
            return
        effect_type = item.data(Qt.ItemDataRole.UserRole + 1)
        if instance_id is None or self._effect_chain is None:
            return
        # 找到对应的 TrackEffectInstance
        inst = next((e for e in self._effect_chain.effects if e.instance_id == instance_id), None)
        if inst is None:
            return
        if effect_type == "limiter":
            self._open_limiter_editor(inst)
        elif effect_type == "compressor":
            self._open_compressor_editor(inst)
        elif effect_type == "reverb":
            self._open_reverb_editor(inst)
        elif effect_type == "delay":
            self._open_delay_editor(inst)
        elif effect_type == "saturation":
            self._open_saturation_editor(inst)
        elif effect_type == "imager":
            self._open_imager_editor(inst)
        elif effect_type == "noise_gate":
            self._open_noise_gate_editor(inst)
        elif effect_type == "strip_eq":
            self._open_strip_eq_editor(inst)

    def _on_effect_list_context_menu(self, pos) -> None:
        """W26: 统一右键菜单。

        无论点在项上还是空白，都显示同一个菜单：
        - 项命中：项操作（旁路/上移/下移/删除）+ 分隔符
        - 永远有：添加效果器子菜单（7 种非胶囊效果器，支持多实例）

        这样即使列表填满（无空白），也能添加新效果器。
        """
        item = self._effect_list.itemAt(pos)
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #2a2a2e; color: #e0e0e0; border: 1px solid #3a3a3e; }
            QMenu::item:selected { background-color: #3a3a3e; }
        """)

        if item is not None:
            # 点在项上：项操作
            instance_id = item.data(Qt.ItemDataRole.UserRole)
            inst = None
            if self._effect_chain is not None:
                inst = next((e for e in self._effect_chain.effects if e.instance_id == instance_id), None)
            if inst is not None:
                idx = self._effect_chain.effects.index(inst)
                # 旁路（文字与列表显示统一读 params.enabled）
                p_enabled = bool(inst.params.get("enabled", True)) if inst.params else True
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

        # 永远显示：添加效果器子菜单（8 种效果器，含 Graphic EQ，均支持多实例）
        add_menu = menu.addMenu("+ " + I18N.tr("audio.fx.add", "Add Effect"))
        self._populate_add_effect_menu(add_menu)

        menu.exec(self._effect_list.mapToGlobal(pos))

    def _populate_add_effect_menu(self, menu: QMenu) -> None:
        """向 menu 填充「添加效果器」子项（8 种效果器，含 Graphic EQ，均支持多实例）。"""
        for effect_type in (
            "compressor", "reverb", "delay", "saturation",
            "imager", "noise_gate", "limiter", "strip_eq",
        ):
            display = _effect_display_name(effect_type)
            action = menu.addAction(display)
            action.triggered.connect(lambda checked=False, et=effect_type: self._effect_add(et))

    def _show_add_effect_menu(self) -> None:
        """弹出 Add Effect 菜单（列表内 "+" 占位项点击或右键空白时调用）。"""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #2a2a2e; color: #e0e0e0; border: 1px solid #3a3a3e; }
            QMenu::item:selected { background-color: #3a3a3e; }
        """)
        self._populate_add_effect_menu(menu)
        # 弹在效果器列表下方
        lst = self._effect_list
        pos = lst.mapToGlobal(QPoint(0, lst.height()))
        menu.exec(pos)

    # ─── 效果器链操作（add/remove/move/bypass）───

    def _effect_add(self, effect_type: str) -> None:
        """添加一个效果器到链尾（支持多实例，同类型可重复添加）。"""
        existing = self._effect_chain.effects if self._effect_chain else ()
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_order = max((e.order for e in existing), default=-1) + 1
        inst = TrackEffectInstance(
            effect_type=effect_type,
            params=_effect_default_params(effect_type),
            enabled=True,
            order=new_order,
            panel="fx",  # FX 列表管理的实例（与 DY 胶囊的 panel="dynamics" 区分）
        )
        effects = existing + (inst,)
        self._effect_chain = EffectChainParams(effects=effects)
        self._refresh_effect_list()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    def _effect_remove(self, inst: Any) -> None:
        """删除一个效果器。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 关闭该实例打开中的编辑对话框 —— 否则对话框仍指向已删除
        # 的实例，后续编辑被静默丢弃（instance_id 不再命中 chain 中任何条目），
        # 且每次编辑都向命令栈发出无变化链的重复提交。
        self._close_dialogs_for_instance(inst.instance_id)
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        effects = tuple(e for e in self._effect_chain.effects if e.instance_id != inst.instance_id)
        # 重新编号 order
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type, params=e.params, enabled=e.enabled,
                order=i, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for i, e in enumerate(effects)
        )
        self._effect_chain = EffectChainParams(effects=effects) if effects else None
        self._refresh_effect_list()
        self.track_effects_committed.emit(self._track_id, self._effect_chain)

    # FX 列表 8 类效果器对话框的实例字典属性名（AUD-37902 关闭/守卫共用）
    _EFFECT_DIALOG_ATTRS = (
        '_limiter_dialogs', '_compressor_dialogs', '_reverb_dialogs',
        '_delay_dialogs', '_saturation_dialogs', '_imager_dialogs',
        '_noise_gate_dialogs', '_strip_eq_dialogs',
    )

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
                dlg.deleteLater()

    def _instance_in_chain(self, instance_id: str) -> bool:
        """AUD-37902: instance_id 是否仍存在于当前效果器链（删除后的编辑守卫）。"""
        if self._effect_chain is None:
            return False
        return any(e.instance_id == instance_id for e in self._effect_chain.effects)

    def _effect_move(self, inst: Any, delta: int) -> None:
        """上移/下移一个效果器（调整 order）。"""
        if self._effect_chain is None:
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        effects = list(self._effect_chain.effects)
        idx = next((i for i, e in enumerate(effects) if e.instance_id == inst.instance_id), -1)
        if idx < 0:
            return
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(effects):
            return
        effects[idx], effects[new_idx] = effects[new_idx], effects[idx]
        effects = [
            TrackEffectInstance(
                effect_type=e.effect_type, params=e.params, enabled=e.enabled,
                order=i, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for i, e in enumerate(effects)
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
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        cur_enabled = bool(inst.params.get("enabled", True)) if inst.params else True
        new_enabled = not cur_enabled
        # 仅修改命中实例的 params.enabled，其余字段（含 chain-level enabled）保持不变
        effects = []
        for e in self._effect_chain.effects:
            if e.instance_id == inst.instance_id:
                new_params = dict(e.params) if e.params else {}
                new_params["enabled"] = new_enabled
                effects.append(TrackEffectInstance(
                    effect_type=e.effect_type, params=new_params,
                    enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                    panel=getattr(e, "panel", "fx"),
                ))
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
        for attr in self._EFFECT_DIALOG_ATTRS:
            dialogs = getattr(self, attr, None)
            if not dialogs:
                continue
            dlg = dialogs.get(instance_id)
            if dlg is not None and dlg.isVisible():
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

    def _open_limiter_editor(self, inst: Any) -> None:
        """打开 Limiter 参数对话框。"""
        from src.shared.ui.audio_mixer.limiter_dialog import LimiterDialog
        from src.features.edit.core.timeline_v2 import LimiterParams
        # 若该 limiter 的对话框已打开则不重复（用 instance_id 区分多个 limiter）
        existing = getattr(self, '_limiter_dialogs', {})
        dlg = existing.get(inst.instance_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        params = LimiterParams.from_dict(inst.params) if inst.params else LimiterParams.default()
        dlg = LimiterDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            instance_id=inst.instance_id,
            initial_params=params,
            parent=self.window(),
        )
        # 拖动期 preview（实时听感），松手 committed（命令栈）—— 与 compressor
        # 等 7 类效果器一致（AUD-37900：此前只接 committed 且只走 preview 出口，
        # 用户修改不进命令栈、不可撤销）
        dlg.limiter_preview.connect(
            lambda tid, p: self._on_limiter_edited(inst, p, commit=False)
        )
        dlg.limiter_committed.connect(
            lambda tid, p: self._on_limiter_edited(inst, p, commit=True)
        )
        if not hasattr(self, '_limiter_dialogs'):
            self._limiter_dialogs = {}
        self._limiter_dialogs[inst.instance_id] = dlg
        dlg.show()

    def _on_limiter_edited(self, inst: Any, limiter_params: Any, commit: bool = True) -> None:
        """Limiter 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_params = limiter_params.to_dict()
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type,
                params=new_params if e.instance_id == inst.instance_id else e.params,
                enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），需刷新列表的 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_compressor_editor(self, inst: Any) -> None:
        """打开 Compressor 参数对话框。"""
        from src.shared.ui.audio_mixer.compressor_dialog import CompressorDialog
        from src.features.edit.core.timeline_v2 import CompressorParams
        # 若该 compressor 的对话框已打开则不重复（用 instance_id 区分多个 compressor）
        existing = getattr(self, '_compressor_dialogs', {})
        dlg = existing.get(inst.instance_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        params = CompressorParams.from_dict(inst.params) if inst.params else CompressorParams.default()
        dlg = CompressorDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            instance_id=inst.instance_id,
            initial_params=params,
            parent=self.window(),
        )
        # 拖动期 preview（实时听感），松手 committed（命令栈）
        dlg.compressor_preview.connect(
            lambda tid, p: self._on_compressor_edited(inst, p, commit=False)
        )
        dlg.compressor_committed.connect(
            lambda tid, p: self._on_compressor_edited(inst, p, commit=True)
        )
        if not hasattr(self, '_compressor_dialogs'):
            self._compressor_dialogs = {}
        self._compressor_dialogs[inst.instance_id] = dlg
        dlg.show()

    def _on_compressor_edited(self, inst: Any, comp_params: Any, commit: bool = True) -> None:
        """Compressor 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_params = comp_params.to_dict()
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type,
                params=new_params if e.instance_id == inst.instance_id else e.params,
                enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_reverb_editor(self, inst: Any) -> None:
        """打开 Reverb 参数对话框。"""
        from src.shared.ui.audio_mixer.reverb_dialog import ReverbDialog
        from src.features.edit.core.timeline_v2 import ReverbParams
        existing = getattr(self, '_reverb_dialogs', {})
        dlg = existing.get(inst.instance_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        params = ReverbParams.from_dict(inst.params) if inst.params else ReverbParams.default()
        dlg = ReverbDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            instance_id=inst.instance_id,
            initial_params=params,
            parent=self.window(),
        )
        dlg.reverb_preview.connect(
            lambda tid, p: self._on_reverb_edited(inst, p, commit=False)
        )
        dlg.reverb_committed.connect(
            lambda tid, p: self._on_reverb_edited(inst, p, commit=True)
        )
        if not hasattr(self, '_reverb_dialogs'):
            self._reverb_dialogs = {}
        self._reverb_dialogs[inst.instance_id] = dlg
        dlg.show()

    def _on_reverb_edited(self, inst: Any, reverb_params: Any, commit: bool = True) -> None:
        """Reverb 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_params = reverb_params.to_dict()
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type,
                params=new_params if e.instance_id == inst.instance_id else e.params,
                enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_delay_editor(self, inst: Any) -> None:
        """打开 Delay 参数对话框。"""
        from src.shared.ui.audio_mixer.delay_dialog import DelayDialog
        from src.features.edit.core.timeline_v2 import DelayParams
        existing = getattr(self, '_delay_dialogs', {})
        dlg = existing.get(inst.instance_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        params = DelayParams.from_dict(inst.params) if inst.params else DelayParams.default()
        dlg = DelayDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            instance_id=inst.instance_id,
            initial_params=params,
            parent=self.window(),
        )
        dlg.delay_preview.connect(
            lambda tid, p: self._on_delay_edited(inst, p, commit=False)
        )
        dlg.delay_committed.connect(
            lambda tid, p: self._on_delay_edited(inst, p, commit=True)
        )
        if not hasattr(self, '_delay_dialogs'):
            self._delay_dialogs = {}
        self._delay_dialogs[inst.instance_id] = dlg
        dlg.show()

    def _on_delay_edited(self, inst: Any, delay_params: Any, commit: bool = True) -> None:
        """Delay 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_params = delay_params.to_dict()
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type,
                params=new_params if e.instance_id == inst.instance_id else e.params,
                enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_saturation_editor(self, inst: Any) -> None:
        """打开 Saturation 参数对话框。"""
        from src.shared.ui.audio_mixer.saturation_dialog import SaturationDialog
        from src.features.edit.core.timeline_v2 import SaturationParams
        existing = getattr(self, '_saturation_dialogs', {})
        dlg = existing.get(inst.instance_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        params = SaturationParams.from_dict(inst.params) if inst.params else SaturationParams.default()
        dlg = SaturationDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            instance_id=inst.instance_id,
            initial_params=params,
            parent=self.window(),
        )
        dlg.saturation_preview.connect(
            lambda tid, p: self._on_saturation_edited(inst, p, commit=False)
        )
        dlg.saturation_committed.connect(
            lambda tid, p: self._on_saturation_edited(inst, p, commit=True)
        )
        if not hasattr(self, '_saturation_dialogs'):
            self._saturation_dialogs = {}
        self._saturation_dialogs[inst.instance_id] = dlg
        dlg.show()

    def _on_saturation_edited(self, inst: Any, sat_params: Any, commit: bool = True) -> None:
        """Saturation 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_params = sat_params.to_dict()
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type,
                params=new_params if e.instance_id == inst.instance_id else e.params,
                enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_imager_editor(self, inst: Any) -> None:
        """打开 Imager 参数对话框。"""
        from src.shared.ui.audio_mixer.imager_dialog import ImagerDialog
        from src.features.edit.core.timeline_v2 import ImagerParams
        existing = getattr(self, '_imager_dialogs', {})
        dlg = existing.get(inst.instance_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        params = ImagerParams.from_dict(inst.params) if inst.params else ImagerParams.default()
        dlg = ImagerDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            instance_id=inst.instance_id,
            initial_params=params,
            parent=self.window(),
        )
        dlg.imager_preview.connect(
            lambda tid, p: self._on_imager_edited(inst, p, commit=False)
        )
        dlg.imager_committed.connect(
            lambda tid, p: self._on_imager_edited(inst, p, commit=True)
        )
        if not hasattr(self, '_imager_dialogs'):
            self._imager_dialogs = {}
        self._imager_dialogs[inst.instance_id] = dlg
        dlg.show()

    def _on_imager_edited(self, inst: Any, imager_params: Any, commit: bool = True) -> None:
        """Imager 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_params = imager_params.to_dict()
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type,
                params=new_params if e.instance_id == inst.instance_id else e.params,
                enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_noise_gate_editor(self, inst: Any) -> None:
        """打开 Noise Gate 参数对话框。"""
        from src.shared.ui.audio_mixer.noise_gate_dialog import NoiseGateDialog
        from src.features.edit.core.timeline_v2 import NoiseGateParams
        existing = getattr(self, '_noise_gate_dialogs', {})
        dlg = existing.get(inst.instance_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        params = NoiseGateParams.from_dict(inst.params) if inst.params else NoiseGateParams.default()
        dlg = NoiseGateDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            instance_id=inst.instance_id,
            initial_params=params,
            parent=self.window(),
        )
        dlg.noise_gate_preview.connect(
            lambda tid, p: self._on_noise_gate_edited(inst, p, commit=False)
        )
        dlg.noise_gate_committed.connect(
            lambda tid, p: self._on_noise_gate_edited(inst, p, commit=True)
        )
        if not hasattr(self, '_noise_gate_dialogs'):
            self._noise_gate_dialogs = {}
        self._noise_gate_dialogs[inst.instance_id] = dlg
        dlg.show()

    def _on_noise_gate_edited(self, inst: Any, ng_params: Any, commit: bool = True) -> None:
        """Noise Gate 对话框提交/预览：更新机架中对应条目。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_params = ng_params.to_dict()
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type,
                params=new_params if e.instance_id == inst.instance_id else e.params,
                enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for e in self._effect_chain.effects
        )
        self._effect_chain = EffectChainParams(effects=effects)
        if commit:
            # enabled 通过 params 流转（开关/右键旁路），刷新列表 [name]/灰色显示
            self._refresh_effect_list()
            self.track_effects_committed.emit(self._track_id, self._effect_chain)
        else:
            self.track_effects_preview.emit(self._track_id, self._effect_chain)

    def _open_strip_eq_editor(self, inst: Any) -> None:
        """从 FX 列表打开 Graphic EQ 编辑器（多实例，按 instance_id 区分）。

        与 EQ 胶囊的单例 _eq_dialog 区分：列表入口支持多实例叠加，
        每个实例独立编辑，互不影响。两者共享同一个 effect_chain 存储。
        """
        from src.shared.ui.audio_mixer.strip_eq_dialog import StripEQDialog
        from src.features.edit.core.timeline_v2 import StripEQParams
        # 若该 strip_eq 的对话框已打开则不重复（用 instance_id 区分多个 EQ）
        existing = getattr(self, '_strip_eq_dialogs', {})
        dlg = existing.get(inst.instance_id)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        eq_params = StripEQParams.from_dict(inst.params) if inst.params else StripEQParams.default()
        dlg = StripEQDialog(
            track_id=self._track_id,
            track_name=self._track_name,
            initial_params=eq_params,
            parent=self.window(),
        )
        # EQ 列表实例提交：按 instance_id 精确替换 chain 中对应实例
        dlg.strip_eq_committed.connect(
            lambda tid, p: self._on_strip_eq_edited(inst, p, commit=True)
        )
        if not hasattr(self, '_strip_eq_dialogs'):
            self._strip_eq_dialogs = {}
        self._strip_eq_dialogs[inst.instance_id] = dlg
        dlg.show()

    def _on_strip_eq_edited(self, inst: Any, eq_params: Any, commit: bool = True) -> None:
        """FX 列表 strip_eq 实例提交：按 instance_id 更新 chain 中对应实例。"""
        if self._effect_chain is None:
            return
        # AUD-37902: 实例已被删除（对话框在删除前打开）—— 丢弃编辑，
        # 避免重建出无变化链并向命令栈发出重复提交。
        if not self._instance_in_chain(inst.instance_id):
            return
        from src.features.edit.core.timeline_v2 import TrackEffectInstance, EffectChainParams
        new_params = eq_params.to_dict()
        effects = tuple(
            TrackEffectInstance(
                effect_type=e.effect_type,
                params=new_params if e.instance_id == inst.instance_id else e.params,
                enabled=e.enabled, order=e.order, instance_id=e.instance_id,
                panel=getattr(e, "panel", "fx"),
            ) for e in self._effect_chain.effects
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

    def reset_meter(self) -> None:
        """重置电平表"""
        self._meter.reset()

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
        """标签右键菜单：重命名 / 设色 / 清除颜色 / 隐藏 strip。"""
        menu = QMenu(self)
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
        initial = QColor(self._track_color) if self._track_color else QColor("#ff5722")
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
            return bool(e.params.get("enabled", True)) if e.params else True

        def _panel(e: Any) -> str:
            return getattr(e, "panel", "fx")

        eq_active = any(
            e.effect_type == "strip_eq" and _enabled(e) and _panel(e) == "eq"
            for e in chain_effects
        )
        dy_active = any(
            e.effect_type in ("compressor", "noise_gate", "limiter")
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

        inactive_style = """
            QToolButton {
                font-size: 9px;
                font-weight: bold;
                background-color: #222226;
                color: #555560;
                border-radius: 2px;
                border: none;
                padding: 0px;
            }
            QToolButton:disabled {
                background-color: #1a1a1e;
                color: #444448;
            }
            QToolButton:hover:enabled {
                background-color: #2c2c30;
            }
        """
        cyan_active_style = """
            QToolButton {
                font-size: 9px;
                font-weight: bold;
                background-color: #00d2c1;
                color: #121214;
                border-radius: 2px;
                border: none;
                padding: 0px;
            }
            QToolButton:hover {
                background-color: #00e5d1;
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
                self._eq_badge.setToolTip("")

    def _update_track_label(self) -> None:
        """更新轨道名标签，避免与顶部 tag 重复显示。

        - Master：tag 已是 "Master"，隐藏下方标签避免重复。
        - 普通轨道 track_name 与 tag 相同（如都是 "A1"）：隐藏避免重复。
        - 普通轨道无 track_name 或窄模式：tag 已显示，隐藏避免重复。
        - 普通轨道有独立 track_name（如 "音频 1"）且非窄模式：显示 track_name。
        """
        tag = self._track_tag or ("Master" if self._is_master else "")
        name = self._track_name or ""
        narrow = getattr(self, '_is_narrow', False)
        # tag 已表达身份（Master / A1）或 name 与 tag 重复时，隐藏下方标签。
        if self._is_master or not name or name == tag or narrow:
            self._track_label.setVisible(False)
        else:
            self._track_label.setVisible(True)
            self._track_label.setText(name)

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

    def _update_controls_enabled(self) -> None:
        """更新控件启用状态"""
        self._volume_fader.setEnabled(not self._is_muted)
        self._volume_spin.setEnabled(not self._is_muted)
        self._meter.setEnabled(not self._is_muted)
        self._db_label.setEnabled(not self._is_muted)

        if hasattr(self, '_balance_fader'):
            self._balance_fader.setEnabled(not self._is_muted)
        if hasattr(self, '_balance_spin'):
            self._balance_spin.setEnabled(not self._is_muted)

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
