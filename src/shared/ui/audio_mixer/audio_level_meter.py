"""
Audio Level Meter Widget - 完美复刻 Kdenlive AudioLevelWidget

实现专业的音频电平表，包含：
- 多声道支持（立体声 L/R）
- IEC 标准 dB 刻度
- 峰值保持指示器
- 削波指示器
- 实心填充/渐变填充样式
- 块线（Block Lines）
- 悬浮提示
- 右键菜单配置
"""

from typing import Optional
from PyQt6.QtWidgets import QWidget, QMenu, QToolTip, QSizePolicy
from PyQt6.QtCore import Qt, QRectF, QSize, QSettings, pyqtSignal
from PyQt6.QtGui import (
    QColor, QPainter, QPixmap, QFont, QFontMetrics,
    QPen, QBrush, QLinearGradient, QAction, QCursor, QPalette
)
from enum import Enum
import time


# QSettings 键前缀（用户级电平表偏好持久化）
_METER_SETTINGS_GROUP = "AudioMixer/LevelMeter"


class LevelStyle(Enum):
    SOLID = 0
    GRADIENT = 1


class PeakStyle(Enum):
    COLORFUL = 0
    MONOCHROME = 1


class TickLabelsMode(Enum):
    HIDE = 0
    SHOW = 1
    SHOW_IF_SPACE = 2


class MeterMode(Enum):
    """电平表测量模式（W11）。"""
    PEAK = 0      # Sample peak（dBFS）— 当前默认
    RMS = 1       # 均方根（dBFS RMS）
    LUFS = 2      # 短时响度（LUFS，EBU R128）


# IEC 标准 dB 刻度
def iec_scale(db: float) -> float:
    """IEC 标准音频电平缩放"""
    if db < -70.0:
        return 0.0
    elif db < -60.0:
        return (db + 70.0) * 0.0025
    elif db < -50.0:
        return (db + 60.0) * 0.005 + 0.025
    elif db < -40.0:
        return (db + 50.0) * 0.0075 + 0.075
    elif db < -30.0:
        return (db + 40.0) * 0.015 + 0.15
    elif db < -20.0:
        return (db + 30.0) * 0.02 + 0.3
    else:
        return (db + 20.0) * 0.025 + 0.5


def iec_inverse(scale_val: float) -> float:
    """iec_scale 的逆函数：给定标度值 [0, 1.0]，返回对应 dB（[-70, 0]）。

    用于让推子位置与电平表峰值在同一 IEC 刻度上对齐（专业混音器标准做法）。
    覆盖 iec_scale 的完整定义域 [-70, 0] dB（s ∈ [0, 1.0]）。
    """
    s = max(0.0, min(1.0, float(scale_val)))
    # 对应 iec_scale 各分段反解（分段单调递增，反解唯一）
    if s <= 0.0:
        return -70.0
    elif s <= 0.025:  # [-70, -60] → [0, 0.025]
        return (s - 0.0) / 0.0025 - 70.0
    elif s <= 0.075:  # [-60, -50] → [0.025, 0.075]
        return (s - 0.025) / 0.005 - 60.0
    elif s <= 0.15:   # [-50, -40] → [0.075, 0.15]
        return (s - 0.075) / 0.0075 - 50.0
    elif s <= 0.3:    # [-40, -30] → [0.15, 0.3]
        return (s - 0.15) / 0.015 - 40.0
    elif s <= 0.5:    # [-30, -20] → [0.3, 0.5]
        return (s - 0.3) / 0.02 - 30.0
    else:  # s > 0.5：[-20, 0] → [0.5, 1.0]
        # iec_scale 在 db>=-20 用 (db+20)*0.025+0.5，反解得：
        return (s - 0.5) / 0.025 - 20.0


def db_to_position(db: float, max_db: float, length: int) -> int:
    """将 dB 值转换为像素位置。

    max_db 是电平表顶部对应的 dB 值（通常 0.0 = 0dBFS 满量程）。
    注意 max_db=0.0 是合法值，不应被视为"无效"。
    """
    max_scale = iec_scale(max_db)
    if max_scale <= 0:
        return 0
    scale = iec_scale(db) / max_scale
    return round(scale * length)


class AudioLevelMeter(QWidget):
    """专业音频电平表，完美复刻 Kdenlive"""

    # 测量模式变化（W11）：Panel 汇总后控制 AudioMixer 的 LUFS 按需计算。
    meter_mode_changed = pyqtSignal(str)

    # 常量
    MIN_DISPLAY_DB = -100.0
    CLIPPING_THRESHOLD_DB = 0.0
    CHANNEL_BORDER_WIDTH = 1
    # 窄轨道中电平条仅作为状态反馈：缩至 6px / 声道，给推子刻度留出空间。
    MIN_SECONDARY_AXIS = 2
    MAX_SECONDARY_AXIS = 6
    MARGIN_LABEL_LEVELS = 1
    TICK_MARK_LENGTH = 2
    TICK_MARK_THICKNESS = 1
    BLOCK_LINE_SPACING = 4
    PEAK_INDICATOR_WIDTH = 2
    CLIPPING_INDICATOR_SIZE = 5
    CLIPPING_INDICATOR_SPACING = 4
    # C1: peak hold 衰减速率（dB/秒），与帧率解耦。12 dB/s 是 IEC 标准的 fallback 速率。
    PEAK_DECAY_DB_PER_SEC = 12.0

    # dB 刻度值（达芬奇 0dBFS 标度：0 顶格，向下 5, 10, 15, 20, 30, 40, 50, 60）
    # -60 与推子新下限 VOLUME_DB_MIN=-60 对齐。
    # 不含 0：满量程(0dBFS)由条形底部红色 + clip 指示器表达，
    # 避免底部重复标 0 与推子刻度视觉冲突。
    DB_SCALE = [-5, -10, -15, -20, -30, -40, -50, -60]

    # 颜色阈值
    GREEN_THRESHOLD = -18.0
    YELLOW_THRESHOLD = -6.0

    # 预分配颜色常量（避免每帧每通道新建 QColor）
    _CLR_GREEN = QColor(0, 200, 0)
    _CLR_YELLOW = QColor(255, 220, 0)
    _CLR_RED = QColor(255, 0, 0)
    _CLR_PEAK_NEUTRAL = QColor(200, 200, 200)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        orientation: Qt.Orientation = Qt.Orientation.Vertical,
        tick_labels_mode: TickLabelsMode = TickLabelsMode.SHOW,
        show_clipping: bool = True,
        channels: int = 2,
        compact_mode: bool = False,
        max_db: float = 0.0
    ) -> None:
        super().__init__(parent)
        self._orientation = orientation
        self._tick_labels_mode = tick_labels_mode
        self._show_clipping = show_clipping
        self._display_clipping = show_clipping
        self._audio_channels = channels
        self._is_hovered = False
        self._display_tooltip = False
        self._compact_mode = compact_mode

        # 电平数据
        self._value_decibels: list[float] = [self.MIN_DISPLAY_DB] * channels
        self._peak_decibels: list[float] = [self.MIN_DISPLAY_DB] * channels
        self._value_positions: list[int] = [0] * channels
        self._peak_positions: list[int] = [0] * channels
        self._clipping_states: list[bool] = [False] * channels
        self._clipping_frames: list[int] = [0] * channels
        self._clipping_start_time: list[float] = [0.0] * channels
        # A7: 上次 set_audio_values 的输入值缓存，用于值变化短路（避免静音稳定态无谓重绘）
        self._last_input_decibels: list[float] = [self.MIN_DISPLAY_DB] * channels
        # C1: peak 衰减的时间戳，用于基于时间的衰减（与帧率解耦）
        self._last_peak_decay_time: float = time.perf_counter()

        # 样式配置
        self._level_style = LevelStyle.SOLID
        self._draw_block_lines = True
        self._peak_style = PeakStyle.COLORFUL
        # max_db: 电平表顶部对应的 dB 值。
        # 默认 0.0（0dBFS 满量程）；与推子配对使用时可设为推子最大增益（如 +24），
        # 使电平表刻度与推子刻度视觉对齐。
        self._max_db = max_db
        # W11: 测量模式（PEAK / RMS / LUFS）
        self._meter_mode = MeterMode.PEAK
        # AM-01: LUFS 后端不可用标志。后端降级时不能显示伪造 LUFS 读数，
        # paintEvent 据此绘制降级标记（斜纹/警告色），告知用户 LUFS 未生效。
        self._lufs_backend_unavailable = False

        # 布局参数
        self._border_offset = 0
        self._border_offset_with_labels = 0
        self._primary_axis_length = 0
        self._secondary_axis_length = 0
        self._draw_labels = True

        # 缓存
        self._background_cache: Optional[QPixmap] = None
        # A8: 垂直电平条固定渐变缓存（在 _update_layout 时重建）
        self._cached_vertical_gradient: Optional[QLinearGradient] = None
        # AUD-37612: 横向电平条与 compact 模式的渐变同样只依赖布局，
        # 在 _update_layout 时一并缓存，避免 paint 每帧新建 QLinearGradient。
        self._cached_horizontal_gradients: dict = {}
        self._cached_compact_gradient: Optional[QLinearGradient] = None

        # 字体（电平刻度数值）：直接锁定像素高度，不依赖比例换算 ——
        # app 默认字体或 HiDPI 下比例换算结果不可控，固定像素值更可靠且视觉一致。
        # 5px = 实际渲染高度 6px，比常规 UI 字体小但清晰可读。
        font = QFont()
        font.setPixelSize(5)
        self.setFont(font)

        # 右键菜单
        self._setup_context_menu()

        self._update_layout()
        # 根据字体度量和条形常量一次性计算精确宽度（不依赖运行时 widget 尺寸）：
        # 可选的刻度数字宽 + 间距 + L/R 条形宽 + 1px 右边框。
        # 子轨隐藏电平 dB 标尺时不再保留空白列，条形可贴齐左缘。
        if self._orientation == Qt.Orientation.Vertical:
            fm = QFontMetrics(self.font())
            label_w = (fm.boundingRect("-45").width()
                       if self._tick_labels_mode != TickLabelsMode.HIDE else 0)
            label_margin = (self.MARGIN_LABEL_LEVELS
                            if self._tick_labels_mode != TickLabelsMode.HIDE else 0)
            bars_w = (self._audio_channels * self.MAX_SECONDARY_AXIS
                      + (self._audio_channels + 1) * self.CHANNEL_BORDER_WIDTH)
            ideal_w = label_w + label_margin + bars_w
            self.setFixedWidth(ideal_w)
        else:
            self.setMinimumWidth(20)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding
        )

    # ========== 公共接口 ==========

    def set_audio_values(self, values: list[float]) -> None:
        """设置音频电平值（dB）"""
        # A7: 值变化短路 —— 记录上次的输入值，若新值与上次完全相同（差值 < 0.01dB）
        # 且 peak 已稳定（无衰减空间），则跳过整个 update，避免无谓重绘。
        # 这对静音稳定态、solo 时其他 15 轨每帧 reset 的场景收益显著。
        new_vals: list[float] = []
        for val in values[:self._audio_channels]:
            new_vals.append(max(self.MIN_DISPLAY_DB, min(self.CLIPPING_THRESHOLD_DB, val)))
        while len(new_vals) < self._audio_channels:
            new_vals.append(self.MIN_DISPLAY_DB)

        # A7: 与上次输入对比（容差 0.01dB）
        if (len(self._last_input_decibels) == len(new_vals)
                and all(abs(a - b) < 0.01 for a, b in zip(self._last_input_decibels, new_vals))
                and all(p <= self.MIN_DISPLAY_DB + 0.01 for p in self._peak_decibels)):
            # 输入未变且 peak 已触底，无需重绘
            return
        self._last_input_decibels = list(new_vals)

        # 就地更新，避免每帧新建 list
        for i in range(self._audio_channels):
            self._value_decibels[i] = new_vals[i]

        # 初始化峰值
        if len(self._peak_decibels) != len(self._value_decibels):
            self._peak_decibels = self._value_decibels.copy()
        else:
            # C1: 基于时间的峰值衰减（与帧率解耦）。
            # 旧实现每帧固定 -0.2dB，高刷屏衰减太快、低帧率太慢。
            # 改用 dt × PEAK_DECAY_DB_PER_SEC（12 dB/s），任意刷新率下衰减速度一致。
            now = time.perf_counter()
            dt = now - self._last_peak_decay_time
            self._last_peak_decay_time = now
            # 防御：dt 异常大（如暂停后恢复）时 clamp 到 0.5s，避免一次衰减几十 dB
            dt = max(0.0, min(dt, 0.5))
            decay = self.PEAK_DECAY_DB_PER_SEC * dt
            for i in range(len(self._value_decibels)):
                self._peak_decibels[i] -= decay
                if self._value_decibels[i] > self._peak_decibels[i]:
                    self._peak_decibels[i] = self._value_decibels[i]
                if self._peak_decibels[i] < self.MIN_DISPLAY_DB:
                    self._peak_decibels[i] = self.MIN_DISPLAY_DB

        # 削波检测
        if self._show_clipping and self._display_clipping:
            self._update_clipping_states()

        self._update_positions()
        self.update()

    def reset(self) -> None:
        """重置电平表"""
        # A7: 若所有声道已是 MIN_DISPLAY_DB 且无削波，则跳过重绘
        # （update_per_track_levels 对未出现的轨道每帧调 reset，避免 16 轨
        # solo 一轨时其余 15 轨每帧无意义 update）。
        already_reset = (
            all(v <= self.MIN_DISPLAY_DB + 0.01 for v in self._value_decibels)
            and all(p <= self.MIN_DISPLAY_DB + 0.01 for p in self._peak_decibels)
            and (not self._show_clipping or not any(self._clipping_states))
        )
        if already_reset and getattr(self, '_last_input_decibels', None) is not None:
            # 标记输入缓存为初始态，确保后续 set_audio_values 能正确检测变化
            if hasattr(self, '_last_input_decibels'):
                self._last_input_decibels = [self.MIN_DISPLAY_DB] * len(self._value_decibels)
            return

        for i in range(len(self._value_decibels)):
            self._value_decibels[i] = self.MIN_DISPLAY_DB
            self._peak_decibels[i] = self.MIN_DISPLAY_DB
        if self._show_clipping:
            self._clipping_states = [False] * self._audio_channels
            self._clipping_frames = [0] * self._audio_channels
        if hasattr(self, '_last_input_decibels'):
            self._last_input_decibels = [self.MIN_DISPLAY_DB] * len(self._value_decibels)
        self._update_positions()
        self.update()

    def set_level_style(self, style: LevelStyle) -> None:
        self._level_style = style
        if style == LevelStyle.GRADIENT:
            self._draw_block_lines = False
        self._draw_background()
        self.update()

    def set_draw_block_lines(self, draw: bool) -> None:
        self._draw_block_lines = draw
        self._draw_background()
        self.update()

    def set_peak_style(self, style: PeakStyle) -> None:
        self._peak_style = style
        self._draw_background()
        self.update()

    def set_meter_mode(self, mode: "MeterMode") -> None:
        """切换测量模式（W11）。

        发出 meter_mode_changed：AudioMixerPanel 汇总后通知 AudioMixer
        开/关 LUFS 计算（无 LUFS 消费者时跳过 K-weighting，实测占混音
        热路径近一半耗时）。
        """
        self._meter_mode = mode
        self._draw_background()
        self.update()
        # AUD-37611: 移除宽泛 except —— Qt 直连槽（panel._recompute_lufs_metering
        # → lufs_metering_changed → AudioMixer.set_lufs_metering）内的真实错误
        # 必须正常传播，不得静默吞掉导致 K-weighting 开关停在错误状态。
        self.meter_mode_changed.emit(mode.name)

    @property
    def meter_mode(self) -> "MeterMode":
        return self._meter_mode

    def set_lufs_backend_unavailable(self, unavailable: bool) -> None:
        """标记 LUFS 后端是否不可用（AM-01）。

        后端降级时 meter 不显示伪造 LUFS 读数，而是绘制降级告警标记，
        让用户明确知道"LUFS 未生效"而非"信号为静音"。
        仅在状态实际变化时触发重绘。
        """
        if self._lufs_backend_unavailable != unavailable:
            self._lufs_backend_unavailable = unavailable
            self.update()

    @property
    def is_lufs_backend_unavailable(self) -> bool:
        """LUFS 后端是否不可用（仅当当前模式为 LUFS 时相关）。"""
        return self._lufs_backend_unavailable

    # ========== 右键菜单 ==========

    def _setup_context_menu(self) -> None:
        from src.shared.i18n.i18n import I18N

        # 从 QSettings 恢复用户偏好（默认：Gradient + Colorful，与工具栏电平表一致）
        settings = QSettings()
        settings.beginGroup(_METER_SETTINGS_GROUP)
        use_gradient = settings.value("gradient_fill", True, type=bool)
        show_block_lines = settings.value("block_lines", False, type=bool)
        monochrome_peak = settings.value("monochrome_peak", False, type=bool)
        meter_mode_str = settings.value("meter_mode", "PEAK", type=str)
        settings.endGroup()

        self._solid_action = QAction(I18N.tr("audio.mixer.solid_fill", "Solid Fill"), self)
        self._solid_action.setCheckable(True)
        self._solid_action.setChecked(not use_gradient)
        self._solid_action.triggered.connect(lambda checked: self._on_fill_style_changed(False))

        self._gradient_action = QAction(I18N.tr("audio.mixer.gradient_fill", "Gradient Fill"), self)
        self._gradient_action.setCheckable(True)
        self._gradient_action.setChecked(use_gradient)
        self._gradient_action.triggered.connect(lambda checked: self._on_fill_style_changed(True))

        self._block_lines_action = QAction(I18N.tr("audio.mixer.show_block_lines", "Show Block Lines"), self)
        self._block_lines_action.setCheckable(True)
        self._block_lines_action.setChecked(show_block_lines)
        self._block_lines_action.triggered.connect(self._on_block_lines_toggled)

        self._colorful_peak_action = QAction(I18N.tr("audio.mixer.colorful_peak", "Colorful Peak Indicator"), self)
        self._colorful_peak_action.setCheckable(True)
        self._colorful_peak_action.setChecked(not monochrome_peak)
        self._colorful_peak_action.triggered.connect(lambda checked: self._on_peak_style_changed(False))

        self._monochrome_peak_action = QAction(I18N.tr("audio.mixer.monochrome_peak", "Monochrome Peak Indicator"), self)
        self._monochrome_peak_action.setCheckable(True)
        self._monochrome_peak_action.setChecked(monochrome_peak)
        self._monochrome_peak_action.triggered.connect(lambda checked: self._on_peak_style_changed(True))

        # W11: 测量模式（Peak / RMS / LUFS）
        self._meter_mode_peak_action = QAction(I18N.tr("audio.mixer.meter_mode_peak", "Meter Mode: Peak (dBFS)"), self)
        self._meter_mode_peak_action.setCheckable(True)
        self._meter_mode_peak_action.setChecked(meter_mode_str == "PEAK")
        self._meter_mode_peak_action.triggered.connect(lambda: self._on_meter_mode_changed(MeterMode.PEAK, "PEAK"))

        self._meter_mode_rms_action = QAction(I18N.tr("audio.mixer.meter_mode_rms", "Meter Mode: RMS (dBFS)"), self)
        self._meter_mode_rms_action.setCheckable(True)
        self._meter_mode_rms_action.setChecked(meter_mode_str == "RMS")
        self._meter_mode_rms_action.triggered.connect(lambda: self._on_meter_mode_changed(MeterMode.RMS, "RMS"))

        self._meter_mode_lufs_action = QAction(I18N.tr("audio.mixer.meter_mode_lufs", "Meter Mode: LUFS (EBU R128)"), self)
        self._meter_mode_lufs_action.setCheckable(True)
        self._meter_mode_lufs_action.setChecked(meter_mode_str == "LUFS")
        self._meter_mode_lufs_action.triggered.connect(lambda: self._on_meter_mode_changed(MeterMode.LUFS, "LUFS"))

        # 应用恢复的偏好到实际渲染状态（走 setter 以触发背景重绘）
        # 此时 widget 尚未可见，setter 内的 _draw_background/update 是安全的（no-op 或缓存）
        self._level_style = LevelStyle.GRADIENT if use_gradient else LevelStyle.SOLID
        self._draw_block_lines = show_block_lines if not use_gradient else False
        self._peak_style = PeakStyle.MONOCHROME if monochrome_peak else PeakStyle.COLORFUL
        # 测量模式
        mode_map = {"PEAK": MeterMode.PEAK, "RMS": MeterMode.RMS, "LUFS": MeterMode.LUFS}
        self._meter_mode = mode_map.get(meter_mode_str, MeterMode.PEAK)

    def _on_fill_style_changed(self, use_gradient: bool) -> None:
        """切换 Solid/Gradient 填充，持久化到 QSettings。"""
        self._solid_action.setChecked(not use_gradient)
        self._gradient_action.setChecked(use_gradient)
        self.set_level_style(LevelStyle.GRADIENT if use_gradient else LevelStyle.SOLID)
        settings = QSettings()
        settings.beginGroup(_METER_SETTINGS_GROUP)
        settings.setValue("gradient_fill", use_gradient)
        settings.endGroup()

    def _on_block_lines_toggled(self, checked: bool) -> None:
        """切换 Block Lines，持久化到 QSettings。"""
        self.set_draw_block_lines(checked)
        settings = QSettings()
        settings.beginGroup(_METER_SETTINGS_GROUP)
        settings.setValue("block_lines", checked)
        settings.endGroup()

    def _on_peak_style_changed(self, monochrome: bool) -> None:
        """切换 Peak Indicator 风格，持久化到 QSettings。"""
        self._colorful_peak_action.setChecked(not monochrome)
        self._monochrome_peak_action.setChecked(monochrome)
        self.set_peak_style(PeakStyle.MONOCHROME if monochrome else PeakStyle.COLORFUL)
        settings = QSettings()
        settings.beginGroup(_METER_SETTINGS_GROUP)
        settings.setValue("monochrome_peak", monochrome)
        settings.endGroup()

    def _on_meter_mode_changed(self, mode: "MeterMode", mode_str: str) -> None:
        """切换测量模式（W11），持久化到 QSettings。"""
        self._meter_mode_peak_action.setChecked(mode == MeterMode.PEAK)
        self._meter_mode_rms_action.setChecked(mode == MeterMode.RMS)
        self._meter_mode_lufs_action.setChecked(mode == MeterMode.LUFS)
        self.set_meter_mode(mode)
        settings = QSettings()
        settings.beginGroup(_METER_SETTINGS_GROUP)
        settings.setValue("meter_mode", mode_str)
        settings.endGroup()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction(self._solid_action)
        menu.addAction(self._gradient_action)
        menu.addSeparator()
        menu.addAction(self._block_lines_action)
        menu.addSeparator()
        menu.addAction(self._colorful_peak_action)
        menu.addAction(self._monochrome_peak_action)
        menu.addSeparator()
        # W11: 测量模式子菜单
        mode_menu = menu.addMenu("Meter Mode")
        mode_menu.addAction(self._meter_mode_peak_action)
        mode_menu.addAction(self._meter_mode_rms_action)
        mode_menu.addAction(self._meter_mode_lufs_action)
        menu.exec(event.globalPos())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    # ========== 事件处理 ==========

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._is_hovered = True
        self._display_tooltip = True
        self._update_layout()
        self._draw_background()
        self._update_tooltip()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._is_hovered = False
        self._display_tooltip = False
        self._update_layout()
        self._draw_background()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_layout()
        self._draw_background()

    def paintEvent(self, event) -> None:
        if self._compact_mode:
            self._paint_compact()
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 绘制背景缓存
        if self._background_cache:
            painter.drawPixmap(0, 0, self._background_cache)

        if not self._value_decibels:
            painter.end()
            return

        # 绘制电平
        self._draw_levels(painter)

        # 绘制削波指示器
        if self._display_clipping:
            self._draw_clipping_indicators(painter)

        # AM-01: LUFS 后端不可用时叠加降级标记，避免用户误读伪造的 LUFS 数值
        if self._meter_mode == MeterMode.LUFS and self._lufs_backend_unavailable:
            self._draw_lufs_degraded_overlay(painter)

        # 更新 tooltip
        if self._display_tooltip:
            self._update_tooltip()

    # ========== 绘制方法 ==========

    def _paint_compact(self) -> None:
        """Compact rendering: clean rounded bars with smooth gradient, no block lines."""
        from PyQt6.QtGui import QPainterPath

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        pad = 4
        radius = 4.0
        bar_gap = 2
        bar_radius = 2.0

        # --- Outer rounded background ---
        bg_rect = QRectF(pad, pad, w - 2 * pad, h - 2 * pad)
        bg_path = QPainterPath()
        bg_path.addRoundedRect(bg_rect, radius, radius)

        # Subtle dark fill (no border)
        painter.fillPath(bg_path, QColor(255, 255, 255, 8))

        # --- Bar geometry (horizontal, two channels stacked vertically) ---
        bar_x = pad + 2
        bar_w = w - 2 * pad - 4
        inner_h = h - 2 * pad - 2
        bar_h = max(3, (inner_h - bar_gap) // self._audio_channels)

        for ch in range(self._audio_channels):
            db_val = self._value_decibels[ch] if ch < len(self._value_decibels) else self.MIN_DISPLAY_DB
            bar_y = pad + 1 + ch * (bar_h + bar_gap)

            # Bar background (empty track)
            bar_rect = QRectF(bar_x, bar_y, bar_w, bar_h)
            bar_path = QPainterPath()
            bar_path.addRoundedRect(bar_rect, bar_radius, bar_radius)
            painter.fillPath(bar_path, QColor(255, 255, 255, 10))

            # Level fill
            if db_val > self.MIN_DISPLAY_DB:
                _max_scale = iec_scale(self._max_db)
                fill_ratio = iec_scale(db_val) / _max_scale if _max_scale > 0 else 0
                fill_ratio = max(0.0, min(1.0, fill_ratio))
                fill_w = bar_w * fill_ratio

                if fill_w > 1:
                    fill_rect = QRectF(bar_x, bar_y, fill_w, bar_h)
                    fill_path = QPainterPath()
                    fill_path.addRoundedRect(fill_rect, bar_radius, bar_radius)

                    # AUD-37612: 复用 _update_layout 缓存的紧凑渐变
                    #（端点只依赖 pad/宽度），避免每帧每通道新建
                    # QLinearGradient + 4 个 QColor。
                    gradient = self._cached_compact_gradient
                    if gradient is None:
                        # 防御性兜底：layout 未初始化时现场构建
                        gradient = QLinearGradient(bar_x, 0, bar_x + bar_w, 0)
                        gradient.setColorAt(0.0, QColor(46, 204, 113))     # green
                        gradient.setColorAt(0.7, QColor(241, 196, 15))     # yellow
                        gradient.setColorAt(0.9, QColor(231, 76, 60))      # red
                        gradient.setColorAt(1.0, QColor(192, 57, 43))      # dark red

                    painter.fillPath(fill_path, QBrush(gradient))

                # Peak indicator (thin bright line)
                peak_db = self._peak_decibels[ch] if ch < len(self._peak_decibels) else self.MIN_DISPLAY_DB
                if peak_db > self.MIN_DISPLAY_DB:
                    _max_scale = iec_scale(self._max_db)
                    peak_ratio = iec_scale(peak_db) / _max_scale if _max_scale > 0 else 0
                    peak_ratio = max(0.0, min(1.0, peak_ratio))
                    peak_x = bar_x + bar_w * peak_ratio
                    if peak_x > bar_x + 1:
                        peak_color = QColor(255, 255, 255, 180)
                        painter.fillRect(
                            QRectF(peak_x - 0.5, bar_y, 1.0, bar_h),
                            peak_color
                        )

        # --- Clipping indicator: red dot at top-right corner (with persistence) ---
        if self._show_clipping:
            is_clipping = any(self._clipping_states[:self._audio_channels])
            if is_clipping:
                dot_size = 5
                dot_x = w - pad - dot_size + 1
                dot_y = pad - 1
                clip_rect = QRectF(dot_x, dot_y, dot_size, dot_size)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(231, 76, 60))
                painter.drawRoundedRect(clip_rect, 1.5, 1.5)

        # AM-01: LUFS 后端不可用降级标记（compact 模式）
        if self._meter_mode == MeterMode.LUFS and self._lufs_backend_unavailable:
            self._draw_lufs_degraded_overlay(painter)

        painter.end()

    def _draw_lufs_degraded_overlay(self, painter: "QPainter") -> None:
        """绘制 LUFS 后端降级标记（AM-01）。

        在电平表上方覆盖半透明警告色 + "N/A" 文字，明确告知用户当前
        不是真实 LUFS 读数（后端 SciPy 不可用，已静默旁路）。
        """
        from src.shared.i18n.i18n import I18N
        rect = self.rect()
        painter.save()
        # 半透明橙色覆盖层，弱化底层读数
        painter.fillRect(rect, QColor(102, 68, 0, 120))
        # 警告文字（横向放置；窄条时仍可辨识）
        painter.setPen(QColor(255, 204, 102))
        font = QFont()
        font.setPixelSize(max(7, min(10, rect.width() - 2)))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignCenter,
            I18N.tr("audio.meter.lufs_na", "LUFS\nN/A"),
        )
        painter.restore()

        # Update tooltip
        if self._display_tooltip:
            self._update_tooltip()

    def _draw_background(self) -> None:
        if self.height() == 0 or self.width() == 0 or self._audio_channels == 0:
            return

        size = self.size()
        # HiDPI 支持
        scaling_factor = self.devicePixelRatioF()
        scaled_size = QSize(round(size.width() * scaling_factor), round(size.height() * scaling_factor))

        self._background_cache = QPixmap(scaled_size)
        self._background_cache.setDevicePixelRatio(scaling_factor)
        self._background_cache.fill(Qt.GlobalColor.transparent)

        painter = QPainter(self._background_cache)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 绘制边框和背景
        self._draw_channel_borders(painter)

        # 绘制 dB 刻度
        if self._draw_labels or self._is_hovered:
            self._draw_db_scale(painter)

        painter.end()

    def _draw_channel_borders(self, painter: QPainter) -> None:
        # 暗色内凹槽底色与超暗边框，彻底消除白色线条感
        pen = QPen(QColor("#1c1c22"))
        pen.setWidthF(self.CHANNEL_BORDER_WIDTH)
        painter.setPen(pen)

        bg_color = QColor("#1e1e24")  # 深色内凹槽 (Recessed Well)，略浅以增强层次

        if self._orientation == Qt.Orientation.Vertical:
            total_width = self._audio_channels * self._secondary_axis_length + (self._audio_channels + 1) * self.CHANNEL_BORDER_WIDTH
            vertical_offset = self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING if self._display_clipping else 0
            rect = QRectF(
                self._border_offset,
                vertical_offset,
                total_width,
                self._primary_axis_length + 2 * self.CHANNEL_BORDER_WIDTH
            )
            painter.fillRect(rect, bg_color)
            painter.drawRect(rect)

            # 声道分隔线（暗色）
            for i in range(1, self._audio_channels):
                x = self._border_offset + i * (self._secondary_axis_length + self.CHANNEL_BORDER_WIDTH)
                painter.drawLine(
                    x, vertical_offset + self.CHANNEL_BORDER_WIDTH,
                    x, vertical_offset + self._primary_axis_length + 1
                )
        else:
            total_height = self._audio_channels * self._secondary_axis_length + (self._audio_channels + 1) * self.CHANNEL_BORDER_WIDTH
            v_offset = max(0, (self.height() - total_height) // 2)
            horizontal_offset = self._border_offset
            rect = QRectF(
                horizontal_offset, v_offset,
                self._primary_axis_length + 2 * self.CHANNEL_BORDER_WIDTH,
                total_height
            )
            painter.fillRect(rect, bg_color)
            painter.drawRect(rect)

            for i in range(1, self._audio_channels):
                y = v_offset + i * (self._secondary_axis_length + self.CHANNEL_BORDER_WIDTH)
                painter.drawLine(
                    horizontal_offset + self.CHANNEL_BORDER_WIDTH, y,
                    horizontal_offset + self._primary_axis_length + 1, y
                )

    def _draw_db_scale(self, painter: QPainter) -> None:
        # 精细暗色刻度文本
        pen = QPen(QColor("#888894"))
        pen.setWidthF(self.TICK_MARK_THICKNESS)
        painter.setPen(pen)
        painter.setOpacity(0.75)
        painter.setFont(self.font())

        if self._orientation == Qt.Orientation.Vertical:
            self._draw_vertical_db_scale(painter)
        else:
            self._draw_horizontal_db_scale(painter)

        painter.setOpacity(1.0)

    def _draw_vertical_db_scale(self, painter: QPainter) -> None:
        label_height = QFontMetrics(self.font()).height()
        prev_y = -1.0
        label_margin = 2

        for db in self.DB_SCALE:
            pos = db_to_position(db, self._max_db, self._primary_axis_length)
            vertical_offset = self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING if self._display_clipping else 0
            y = vertical_offset + (self._primary_axis_length - pos)

            label_y = y - label_height // 2
            if label_y + label_height > self.height():
                label_y = self.height() - label_height
            elif label_y < 0:
                label_y = 0

            if prev_y < 0 or label_y - (prev_y + label_height) > label_margin:
                # 达芬奇标度：0 顶格，向下 5, 10, 15, 20, 30, 40, 50
                label = str(abs(int(db)))
                painter.drawText(
                    QRectF(0, label_y, self._border_offset - self.MARGIN_LABEL_LEVELS, label_height),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    label
                )
                # 微型 2px 刻度线
                painter.drawLine(
                    self._border_offset - self.TICK_MARK_LENGTH, y,
                    self._border_offset, y
                )
                prev_y = label_y

    def _draw_horizontal_db_scale(self, painter: QPainter) -> None:
        label_height = QFontMetrics(self.font()).ascent()
        prev_x = -1.0
        label_margin = 2
        y = self._audio_channels * self._secondary_axis_length + (self._audio_channels + 1) * self.CHANNEL_BORDER_WIDTH

        for db in self.DB_SCALE:
            x = db_to_position(db, self._max_db, self._primary_axis_length)
            label = str(int(db))
            label_width = QFontMetrics(self.font()).horizontalAdvance(label)
            label_x = x - label_width // 2

            if label_x + label_width > self.width():
                label_x = self.width() - label_width
            elif label_x < 0:
                label_x = 0

            if prev_x < 0 or prev_x - (label_x + label_width) > label_margin:
                painter.drawText(label_x, y + self.MARGIN_LABEL_LEVELS + label_height, label)
                painter.drawLine(x, y, x, y + self.TICK_MARK_LENGTH)
                prev_x = label_x

    def _draw_levels(self, painter: QPainter) -> None:
        if self._level_style == LevelStyle.SOLID:
            self._draw_levels_solid(painter)
        else:
            self._draw_levels_gradient(painter)

        # 绘制峰值指示器
        for i in range(self._audio_channels):
            self._draw_peak_indicator(painter, i)

        # 绘制块线
        if self._draw_block_lines and self._level_style == LevelStyle.SOLID:
            self._render_block_lines(painter)

    def _draw_levels_solid(self, painter: QPainter) -> None:
        for i in range(self._audio_channels):
            db_val = self._value_decibels[i] if i < len(self._value_decibels) else self.MIN_DISPLAY_DB
            if db_val <= self.MIN_DISPLAY_DB:
                continue

            pos = self._value_positions[i] if i < len(self._value_positions) else 0

            if self._orientation == Qt.Orientation.Vertical:
                secondary_offset = self._channel_to_secondary_offset(i)
                secondary_len = self._secondary_axis_length

                # 垂直电平表：Qt y 轴向下。电平条从底部向上填充。
                # pos = iec_scale(db) * L，表示"电平条的高度"（从底部量起）。
                # 条覆盖区域：y ∈ [L-pos, L]
                #
                # 颜色阈值（从底到顶）：
                #   底部 (-∞) → GREEN_THRESHOLD(-18dB): 绿色
                #   GREEN_THRESHOLD → YELLOW_THRESHOLD(-6dB): 黄色
                #   YELLOW_THRESHOLD → 顶部(0dB/max): 红色
                L = self._primary_axis_length
                vertical_offset = self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING if self._display_clipping else 0

                pos_g = db_to_position(self.GREEN_THRESHOLD, self._max_db, L)
                pos_y = db_to_position(self.YELLOW_THRESHOLD, self._max_db, L)

                # 条的 y 范围（Qt 坐标，0=顶部）
                bar_top = L - pos       # 条最高点（当前电平位置）
                bar_bot = L             # 条最低点（底部）
                green_y = L - pos_g     # GREEN_THRESHOLD 的 y 坐标
                yellow_y = L - pos_y    # YELLOW_THRESHOLD 的 y 坐标

                # 绿色段：底部(bar_bot) 到 GREEN_THRESHOLD(green_y)
                # 如果条没超过 GREEN_THRESHOLD（bar_top > green_y），只画到 bar_top
                g_top = max(green_y, bar_top)
                g_bot = bar_bot
                if g_bot > g_top:
                    painter.fillRect(
                        QRectF(secondary_offset, vertical_offset + g_top, secondary_len, g_bot - g_top),
                        self._CLR_GREEN
                    )

                # 黄色段：GREEN_THRESHOLD(green_y) 到 YELLOW_THRESHOLD(yellow_y)
                # 如果条没超过 YELLOW_THRESHOLD（bar_top > yellow_y），只画到 bar_top
                if pos > pos_g:
                    y_top = max(yellow_y, bar_top)
                    y_bot = green_y
                    if y_bot > y_top:
                        painter.fillRect(
                            QRectF(secondary_offset, vertical_offset + y_top, secondary_len, y_bot - y_top),
                            self._CLR_YELLOW
                        )

                # 红色段：从 yellow_y 到 bar_top
                # 仅当条超过 YELLOW_THRESHOLD 才有红色
                if pos > pos_y:
                    r_top = bar_top
                    r_bot = yellow_y
                    if r_bot > r_top:
                        painter.fillRect(
                            QRectF(secondary_offset, vertical_offset + r_top, secondary_len, r_bot - r_top),
                            self._CLR_RED
                        )
            else:
                # Horizontal: bars fill left to right
                secondary_offset = self._channel_to_secondary_offset(i)
                secondary_len = self._secondary_axis_length
                horizontal_offset = self._border_offset

                green_end = min(db_to_position(self.GREEN_THRESHOLD, self._max_db, self._primary_axis_length), pos)
                yellow_end = min(db_to_position(self.YELLOW_THRESHOLD, self._max_db, self._primary_axis_length), pos)

                # 绿色段 (from start to green threshold)
                if green_end > 0:
                    painter.fillRect(
                        QRectF(horizontal_offset, secondary_offset, green_end, secondary_len),
                        self._CLR_GREEN
                    )
                # 黄色段 (from green threshold to yellow threshold)
                if yellow_end > green_end and db_val >= self.GREEN_THRESHOLD:
                    painter.fillRect(
                        QRectF(horizontal_offset + green_end, secondary_offset, yellow_end - green_end, secondary_len),
                        self._CLR_YELLOW
                    )
                # 红色段 (from yellow threshold to current position)
                if db_val >= self.YELLOW_THRESHOLD and pos > yellow_end:
                    painter.fillRect(
                        QRectF(horizontal_offset + yellow_end, secondary_offset, pos - yellow_end, secondary_len),
                        self._CLR_RED
                    )

    def _draw_levels_gradient(self, painter: QPainter) -> None:
        for i in range(self._audio_channels):
            db_val = self._value_decibels[i] if i < len(self._value_decibels) else self.MIN_DISPLAY_DB
            if db_val <= self.MIN_DISPLAY_DB:
                continue

            pos = self._value_positions[i] if i < len(self._value_positions) else 0
            secondary_offset = self._channel_to_secondary_offset(i)
            secondary_len = self._secondary_axis_length

            if self._orientation == Qt.Orientation.Vertical:
                vertical_offset = self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING if self._display_clipping else 0
                L = self._primary_axis_length

                # 垂直翻转：条从底部(L)向上填到 L-pos
                fill_top = vertical_offset + (L - pos)

                # A8: 复用 _update_layout 时缓存的固定渐变（不随当前值变化），
                # 避免每帧每通道新建 QLinearGradient + 4 个 QColor。
                gradient = self._cached_vertical_gradient
                if gradient is None:
                    # 防御性兜底：layout 未初始化时现场构建
                    gradient = QLinearGradient(0, vertical_offset, 0, vertical_offset + L)
                    gradient.setColorAt(0.0, QColor(192, 57, 43))
                    gradient.setColorAt(0.15, QColor(231, 76, 60))
                    gradient.setColorAt(0.35, QColor(241, 196, 15))
                    gradient.setColorAt(1.0, QColor(46, 204, 113))

                painter.fillRect(
                    QRectF(secondary_offset, fill_top, secondary_len, pos),
                    gradient
                )
            else:
                # Horizontal: bars fill left to right.
                # AUD-37612: 渐变只依赖布局（offset/L）与档位，复用
                # _update_layout 缓存，与垂直路径 A8 语义对齐（色标锚定
                # 整条轨道而非当前填充尖端）；低于绿色阈值用纯色刷。
                horizontal_offset = self._border_offset

                if db_val >= 0:
                    gradient = self._cached_horizontal_gradients.get('hot')
                    if gradient is None:
                        gradient = QLinearGradient(horizontal_offset, 0, horizontal_offset + self._primary_axis_length, 0)
                        gradient.setColorAt(0.0, QColor(0, 200, 0))
                        gradient.setColorAt(1.0, QColor(255, 0, 0))
                elif db_val >= self.GREEN_THRESHOLD:
                    gradient = self._cached_horizontal_gradients.get('warm')
                    if gradient is None:
                        gradient = QLinearGradient(horizontal_offset, 0, horizontal_offset + self._primary_axis_length, 0)
                        gradient.setColorAt(0.0, QColor(0, 200, 0))
                        gradient.setColorAt(1.0, QColor(255, 220, 0))
                else:
                    gradient = None

                if gradient is not None:
                    painter.fillRect(
                        QRectF(horizontal_offset, secondary_offset, pos, secondary_len),
                        gradient
                    )
                else:
                    painter.fillRect(
                        QRectF(horizontal_offset, secondary_offset, pos, secondary_len),
                        self._CLR_GREEN
                    )

    def _draw_peak_indicator(self, painter: QPainter, channel_index: int) -> None:
        if channel_index >= len(self._peak_positions):
            return
        pos = self._peak_positions[channel_index]
        if pos < 0:
            return

        if self._peak_style == PeakStyle.COLORFUL:
            db_val = self._peak_decibels[channel_index] if channel_index < len(self._peak_decibels) else self.MIN_DISPLAY_DB
            if db_val >= 0:
                color = self._CLR_RED
            elif db_val >= self.YELLOW_THRESHOLD:
                color = self._CLR_YELLOW
            else:
                color = self._CLR_PEAK_NEUTRAL
        else:
            color = self._CLR_PEAK_NEUTRAL

        secondary_offset = self._channel_to_secondary_offset(channel_index)
        secondary_len = self._secondary_axis_length

        if self._orientation == Qt.Orientation.Vertical:
            vertical_offset = self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING if self._display_clipping else 0
            # 垂直翻转：pos 是从顶部算的偏移，条从底部填，峰值指示器在 L-pos 处
            L = self._primary_axis_length
            y_pos = vertical_offset + (L - pos)
            painter.fillRect(
                QRectF(secondary_offset, y_pos, secondary_len, self.PEAK_INDICATOR_WIDTH),
                color
            )
        else:
            horizontal_offset = self._border_offset
            painter.fillRect(
                QRectF(horizontal_offset + pos - self.PEAK_INDICATOR_WIDTH, secondary_offset, self.PEAK_INDICATOR_WIDTH, secondary_len),
                color
            )

    def _render_block_lines(self, painter: QPainter) -> None:
        pen = QPen(self._get_border_color())
        pen.setWidthF(1.0)
        painter.setPen(pen)

        secondary_len = self._secondary_axis_length
        block_gap = 0
        block_line_length = secondary_len - 2 * block_gap

        if self._orientation == Qt.Orientation.Vertical:
            vertical_offset = self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING if self._display_clipping else 0
            L = self._primary_axis_length

            for i in range(self._audio_channels):
                if i >= len(self._value_positions) or self._value_positions[i] < 0:
                    continue

                pos = self._value_positions[i]
                secondary_offset = self._channel_to_secondary_offset(i)

                # 垂直翻转：条从底部(L)填到 L-pos，块线画在填充区域内
                y = vertical_offset + (L - pos) + self.BLOCK_LINE_SPACING
                while y < vertical_offset + L:
                    painter.drawLine(
                        secondary_offset + block_gap, y,
                        secondary_offset + block_gap + block_line_length, y
                    )
                    y += self.BLOCK_LINE_SPACING
        else:
            horizontal_offset = self._border_offset

            for i in range(self._audio_channels):
                if i >= len(self._value_positions) or self._value_positions[i] < 0:
                    continue

                pos = self._value_positions[i]
                secondary_offset = self._channel_to_secondary_offset(i)

                x = horizontal_offset + pos - self.BLOCK_LINE_SPACING
                while x > horizontal_offset + self.CHANNEL_BORDER_WIDTH:
                    painter.drawLine(
                        x, secondary_offset + block_gap,
                        x, secondary_offset + block_gap + block_line_length
                    )
                    x -= self.BLOCK_LINE_SPACING

    def _draw_clipping_indicators(self, painter: QPainter) -> None:
        for i in range(self._audio_channels):
            if not self._clipping_states[i]:
                continue

            secondary_offset = self._channel_to_secondary_offset(i)
            color = QColor(255, 0, 0)

            if self._orientation == Qt.Orientation.Vertical:
                painter.fillRect(
                    QRectF(
                        secondary_offset, 0,
                        self._secondary_axis_length,
                        self.CLIPPING_INDICATOR_SIZE
                    ),
                    color
                )
            else:
                # Horizontal: clipping indicator at the right side
                clip_x = self._border_offset + self._primary_axis_length + self.CHANNEL_BORDER_WIDTH + self.CLIPPING_INDICATOR_SPACING
                painter.fillRect(
                    QRectF(
                        clip_x, secondary_offset,
                        self.CLIPPING_INDICATOR_SIZE,
                        self._secondary_axis_length
                    ),
                    color
                )

    # ========== 内部工具方法 ==========

    def _update_positions(self) -> None:
        n = self._audio_channels
        # 确保列表长度匹配（通常 __init__ 已预分配）
        if len(self._value_positions) != n:
            self._value_positions = [0] * n
            self._peak_positions = [0] * n

        for i in range(n):
            db_val = self._value_decibels[i]
            if db_val <= self.MIN_DISPLAY_DB:
                self._value_positions[i] = -1
            else:
                self._value_positions[i] = db_to_position(db_val, self._max_db, self._primary_axis_length)

            peak_val = self._peak_decibels[i] if i < len(self._peak_decibels) else self.MIN_DISPLAY_DB
            if peak_val <= self.MIN_DISPLAY_DB:
                self._peak_positions[i] = -1
            else:
                self._peak_positions[i] = db_to_position(peak_val, self._max_db, self._primary_axis_length)

    def _update_clipping_states(self) -> None:
        now = time.perf_counter()
        clipping_hold_sec = 4.0  # 削波指示器保持 4 秒

        for i in range(self._audio_channels):
            is_clipping = self._value_decibels[i] >= self.CLIPPING_THRESHOLD_DB
            if is_clipping:
                self._clipping_states[i] = True
                self._clipping_frames[i] = 0
                self._clipping_start_time[i] = now
            elif self._clipping_states[i]:
                if now - self._clipping_start_time[i] >= clipping_hold_sec:
                    self._clipping_states[i] = False

    def _update_layout(self) -> None:
        fm = QFontMetrics(self.font())
        if self._orientation == Qt.Orientation.Vertical:
            self._border_offset_with_labels = fm.boundingRect("-45").width() + self.MARGIN_LABEL_LEVELS
            # 条形区固定宽度（MAX_SECONDARY_AXIS × 通道 + 边框）
            bars_w = (self._audio_channels * self.MAX_SECONDARY_AXIS
                      + (self._audio_channels + 1) * self.CHANNEL_BORDER_WIDTH)
            if self._tick_labels_mode == TickLabelsMode.HIDE:
                self._draw_labels = False
                self._border_offset = self.TICK_MARK_LENGTH
            elif self._tick_labels_mode == TickLabelsMode.SHOW_IF_SPACE:
                # 同时检查垂直高度（标签纵向不挤）和水平宽度（标签+条形横向不重叠）。
                # 旧逻辑只比 height，导致 Master（高）画标签时条形被压扁/裁切，
                # 而 A1（矮）不画标签——两者宽度不一致且 Master 标签与条形重叠。
                enough_height = self.height() > self._border_offset_with_labels + 50
                enough_width = self.width() >= self._border_offset_with_labels + bars_w
                enough_space = enough_height and enough_width
                self._draw_labels = enough_space
                self._border_offset = self._border_offset_with_labels if enough_space else self.TICK_MARK_LENGTH
            else:
                self._draw_labels = True
                self._border_offset = self._border_offset_with_labels
        else:
            self._border_offset_with_labels = fm.height() + self.MARGIN_LABEL_LEVELS
            if self._tick_labels_mode == TickLabelsMode.HIDE:
                self._draw_labels = False
                self._border_offset = self.TICK_MARK_LENGTH
            elif self._tick_labels_mode == TickLabelsMode.SHOW_IF_SPACE:
                enough_space = self.width() > self._border_offset_with_labels + 100
                self._draw_labels = enough_space
                self._border_offset = self._border_offset_with_labels if enough_space else self.TICK_MARK_LENGTH
            else:
                self._draw_labels = True
                self._border_offset = self._border_offset_with_labels

        # 计算轴长度
        if self._orientation == Qt.Orientation.Vertical:
            available_height = self.height() - 2 * self.CHANNEL_BORDER_WIDTH
            if self._display_clipping:
                available_height -= self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING
            full_blocks = available_height // self.BLOCK_LINE_SPACING
            self._primary_axis_length = max(1, full_blocks * self.BLOCK_LINE_SPACING - self.CHANNEL_BORDER_WIDTH)
            # 确保 Master（画标签）和 Track（不画标签）条形宽度永远一致。
            self._secondary_axis_length = self.MAX_SECONDARY_AXIS
        else:
            available_width = self.width() - 2 * self.CHANNEL_BORDER_WIDTH
            if self._display_clipping:
                available_width -= self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING
            full_blocks = available_width // self.BLOCK_LINE_SPACING
            self._primary_axis_length = max(1, full_blocks * self.BLOCK_LINE_SPACING - self.CHANNEL_BORDER_WIDTH)

            drawable_height = self.height() - self._border_offset
            self._secondary_axis_length = max(
                self.MIN_SECONDARY_AXIS,
                (drawable_height - (self._audio_channels + 1) * self.CHANNEL_BORDER_WIDTH) // self._audio_channels
            )
            self._secondary_axis_length = min(self._secondary_axis_length, self.MAX_SECONDARY_AXIS)

        # A8: 预构建并缓存垂直电平条的固定渐变（顶部=max_db 红 → 底部=绿）。
        # 渐变只依赖 vertical_offset 和 L（_primary_axis_length），不随当前 dB 值变化，
        # 因此在 layout 变化时重建一次即可，避免 paint 时每帧每通道新建 QLinearGradient + QColor。
        if self._orientation == Qt.Orientation.Vertical:
            v_off = (self.CLIPPING_INDICATOR_SIZE + self.CLIPPING_INDICATOR_SPACING
                     if self._display_clipping else 0)
            L = self._primary_axis_length
            self._cached_vertical_gradient = QLinearGradient(0, v_off, 0, v_off + L)
            self._cached_vertical_gradient.setColorAt(0.0, QColor(192, 57, 43))    # dark red (顶部)
            self._cached_vertical_gradient.setColorAt(0.15, QColor(231, 76, 60))   # red
            self._cached_vertical_gradient.setColorAt(0.35, QColor(241, 196, 15))  # yellow
            self._cached_vertical_gradient.setColorAt(1.0, QColor(46, 204, 113))   # green (底部)
        else:
            self._cached_vertical_gradient = None

        # AUD-37612: 横向电平条渐变缓存 —— 与 A8 同理，渐变端点只依赖
        # border_offset 与 _primary_axis_length（不随当前 dB 值变化），按
        # 档位（红 hot / 黄 warm）预构建两支；低于绿色阈值的段绘制时直接
        # 用纯色 _CLR_GREEN，无需渐变对象。
        if self._orientation == Qt.Orientation.Horizontal:
            h_off = self._border_offset
            h_len = self._primary_axis_length
            hot = QLinearGradient(h_off, 0, h_off + h_len, 0)
            hot.setColorAt(0.0, self._CLR_GREEN)
            hot.setColorAt(1.0, self._CLR_RED)
            warm = QLinearGradient(h_off, 0, h_off + h_len, 0)
            warm.setColorAt(0.0, self._CLR_GREEN)
            warm.setColorAt(1.0, self._CLR_YELLOW)
            self._cached_horizontal_gradients = {'hot': hot, 'warm': warm}
        else:
            self._cached_horizontal_gradients = {}

        # AUD-37612: compact 模式的渐变端点只依赖 pad(4) 与宽度，同样缓存。
        # （QLinearGradient 坐标为 widget 坐标，resize 时随 _update_layout 重建。）
        c_pad = 4
        c_bar_x = c_pad + 2
        c_bar_w = max(0, self.width() - 2 * c_pad - 4)
        self._cached_compact_gradient = QLinearGradient(c_bar_x, 0, c_bar_x + c_bar_w, 0)
        self._cached_compact_gradient.setColorAt(0.0, QColor(46, 204, 113))     # green
        self._cached_compact_gradient.setColorAt(0.7, QColor(241, 196, 15))     # yellow
        self._cached_compact_gradient.setColorAt(0.9, QColor(231, 76, 60))      # red
        self._cached_compact_gradient.setColorAt(1.0, QColor(192, 57, 43))      # dark red

    def _channel_to_secondary_offset(self, channel_index: int) -> int:
        if self._orientation == Qt.Orientation.Vertical:
            return self._border_offset + self.CHANNEL_BORDER_WIDTH + channel_index * (self._secondary_axis_length + self.CHANNEL_BORDER_WIDTH)
        else:
            total_height = self._audio_channels * self._secondary_axis_length + (self._audio_channels + 1) * self.CHANNEL_BORDER_WIDTH
            v_offset = max(0, (self.height() - total_height) // 2)
            return v_offset + self.CHANNEL_BORDER_WIDTH + channel_index * (self._secondary_axis_length + self.CHANNEL_BORDER_WIDTH)

    def _get_border_color(self) -> QColor:
        color = self.palette().color(QPalette.ColorRole.WindowText)
        color.setAlphaF(0.4)
        return color

    def _update_tooltip(self) -> None:
        if not self.isEnabled():
            return

        tip = ""
        channels = len(self._value_decibels)
        for i in range(channels):
            if channels == 2:
                tip += "L: " if i == 0 else "R: "
            else:
                tip += f"CH{i+1}: "

            if self._value_decibels[i] <= self.MIN_DISPLAY_DB:
                tip += "No audio"
            else:
                tip += f"{self._value_decibels[i]:+.2f}dB"

            if i < channels - 1:
                tip += "\n"

        QToolTip.showText(QCursor.pos(), tip, self)
