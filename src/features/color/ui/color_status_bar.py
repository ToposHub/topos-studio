"""色彩监看状态栏（P1-COLOR-22 / P2-COLOR-10）。

在 viewer 底部常驻显示有效色彩链路：
    Source → Working → Display (Output) │ provider 徽标
源/工作/显示任一环节降级（如 OCIO 回退 builtin）时黄色警示，
tooltip 提供完整诊断详情。所有状态由控制器喂入；widget 不做探测。
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)

# FFmpeg/容器 tag → 人类可读短标
_PRIMARIES_LABEL = {
    'bt709': '709', 'bt2020': '2020', 'smpte170m': '601',
    'smpte431': 'P3', 'smpte432': 'P3', 'bt470bg': '601PAL',
    'smpte240m': '240M', 'applep3': 'P3',
}
_TRANSFER_LABEL = {
    'bt709': 'γ709', 'srgb': 'sRGB', 'smpte2084': 'PQ',
    'st2084': 'PQ', 'pq': 'PQ', 'arib-std-b67': 'HLG', 'hlg': 'HLG',
    'linear': 'Lin', 'prophoto': 'ProPhoto',
}


def format_source_state(transfer: Optional[str], primaries: Optional[str],
                        range_tag: Optional[str]) -> str:
    """生成源段短标，如 ``BT.2020 PQ lim``。"""
    tf = (transfer or 'bt709').lower()
    pm = (primaries or 'bt709').lower()
    rg = (range_tag or '').lower()
    tf_label = _TRANSFER_LABEL.get(tf, tf.upper() if tf else 'γ709')
    pm_label = _PRIMARIES_LABEL.get(pm, pm.upper() if pm else '709')
    rg_label = ''
    if rg in ('limited', 'mpeg', 'tv', '16-235'):
        rg_label = ' lim'
    elif rg in ('full', 'jpeg', 'pc', '0-255'):
        rg_label = ' full'
    return f"BT.{pm_label} {tf_label}{rg_label}"


def format_provider_badge(diag: Optional[dict]):
    """provider 诊断 → (文本, 是否警示, tooltip)。

    diag 来自 get_color_provider_diagnostics()（P0-COLOR-05）。
    """
    if not isinstance(diag, dict) or not diag.get('active_provider'):
        return ('?', True, I18N.tr('colorbar.no_diag', '无 provider 诊断'))
    active = str(diag.get('active_provider', ''))
    requested = str(diag.get('requested_engine', ''))
    reason = str(diag.get('fallback_reason') or '')
    ocio_ok = bool(diag.get('ocio_config_loaded'))

    if active == 'ocio' and ocio_ok:
        text = 'OCIO'
        warning = False
    elif active == 'ocio':
        text = 'OCIO?'
        warning = True
    else:
        text = 'builtin'
        warning = bool(requested and requested != active and reason)

    lines = [f"provider: {active}",
             f"requested: {requested or '-'}",
             f"OCIO config: {'ok' if ocio_ok else 'not loaded'}"]
    path = diag.get('ocio_config_path')
    if path:
        lines.append(f"config: {path}")
    if reason:
        lines.append(f"fallback: {reason}")
    return (text, warning, '\n'.join(lines))


class ColorMonitorStatusBar(QWidget):
    """viewer 底部色彩链路状态栏。

    分段：``SRC … → WORK … → OUT … │ provider``。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source = {'transfer': None, 'primaries': None, 'range': None}
        self._working = ''
        self._display = ''
        self._provider_diag: Optional[dict] = None
        self._decode_path = ''
        self._denoise_quality_provider = None
        self._denoise_quality: Optional[str] = None
        # CV-B09：已应用的折叠宽度（apply_available_width 去重用）。
        self._applied_width: Optional[int] = None

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 10, 2)
        layout.setSpacing(0)

        self._src_label = QLabel('')
        self._work_label = QLabel('')
        self._out_label = QLabel('')
        self._provider_label = QLabel('')
        self._denoise_label = QLabel('')

        # SRC→WORK→OUT 链路段（含箭头）：窄视口折叠整段到 tooltip。
        self._chain_widgets: list[QWidget] = [self._src_label]
        layout.addWidget(self._src_label)
        for arrow, label in ((self._make_arrow(), self._work_label),
                             (self._make_arrow(), self._out_label)):
            self._chain_widgets.append(arrow)
            layout.addWidget(arrow)
            self._chain_widgets.append(label)
            layout.addWidget(label)
        self._chain_sep = self._make_sep()
        layout.addWidget(self._chain_sep)
        layout.addWidget(self._denoise_label)
        self._denoise_sep = self._make_sep()
        layout.addWidget(self._denoise_sep)

        base = ("QLabel { background: transparent; border: none;"
                " font-size: 11px; padding: 0 3px; }")
        for label in (self._src_label, self._work_label, self._out_label):
            label.setStyleSheet(
                base + " color: rgba(190, 210, 230, 220);")
        self._denoise_label.setStyleSheet(
            base + " color: rgba(170, 200, 240, 220); padding: 0;")
        self._denoise_label.setToolTip(I18N.tr(
            'colorbar.denoise_tooltip',
            '视频降噪渲染档位：preview（播放）/ paused（暂停）/ export（导出）'))
        self._provider_label.setStyleSheet(base + " padding-right: 0;")

        # P1-COLOR-13: 播放效果降级指示器（自刷新 500ms，读管理器单例）
        from src.shared.ui.effect_degradation_indicator import (
            EffectDegradationIndicator,
        )
        # provider 徽标（OCIO/builtin ＋ fallback 警示）：初始提交起一直
        # 只设置样式、从未加入布局——从未真正可见（CV-B09 安全区回归
        # 抓出）。补挂到链路段之后；窄窗折叠只折叠链路文字，徽标保留。
        layout.addWidget(self._provider_label)
        self.degradation_indicator = EffectDegradationIndicator()
        layout.addWidget(self.degradation_indicator)

        self._refresh()

        # 降噪档位轮询（provider 注入，避免 widget 反向依赖引擎）。
        # AUD-25301: 定时器不再无条件常驻——仅当「已注入 provider 且控件
        # 可见」时运行；否则以 2Hz 空转唤醒事件循环。
        from PyQt6.QtCore import QTimer
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(500)
        self._poll_timer.timeout.connect(self._poll_denoise_quality)
        self._update_poll_timer_active()
        self._poll_denoise_quality()

    def _update_poll_timer_active(self) -> None:
        """AUD-25301: 轮询启停的唯一裁决点（provider 且可见才运行）。"""
        if self._denoise_quality_provider is not None and self.isVisible():
            if not self._poll_timer.isActive():
                self._poll_timer.start()
        else:
            self._poll_timer.stop()

    def showEvent(self, event) -> None:
        """可见时恢复轮询（AUD-25301）。"""
        super().showEvent(event)
        self._update_poll_timer_active()

    def hideEvent(self, event) -> None:
        """不可见时暂停轮询（AUD-25301）。"""
        super().hideEvent(event)
        self._update_poll_timer_active()

    # ---- 数据入口 ----

    def set_denoise_quality_provider(self, provider) -> None:
        """注入降噪档位探测回调（返回 'preview'/'paused'/'export'/None）。

        AUD-25301: 传 None 撤销 provider 时停表，不再空转轮询。
        """
        self._denoise_quality_provider = provider
        self._update_poll_timer_active()
        self._poll_denoise_quality()

    def _poll_denoise_quality(self) -> None:
        provider = self._denoise_quality_provider
        quality = None
        if provider is not None:
            try:
                quality = provider()
                self._denoise_provider_failed = False
            except Exception as exc:  # noqa: BLE001 —— provider 为注入回调，需兜底
                quality = None
                # AUD-25300：不再无痕吞错。持续失败每 500ms 一次，只在
                # 首次失败（或成功后再次失败）时记录告警日志，避免刷屏。
                if not getattr(self, '_denoise_provider_failed', False):
                    self._denoise_provider_failed = True
                    logger.warning(
                        "denoise quality provider 持续失败，状态栏降噪档位"
                        " 将不可用: %s", exc, exc_info=True)
        if quality != self._denoise_quality:
            self._denoise_quality = quality
            self._refresh()

    def set_source_state(self, transfer: Optional[str] = None,
                         primaries: Optional[str] = None,
                         range_tag: Optional[str] = None) -> None:
        if transfer is not None:
            self._source['transfer'] = transfer
        if primaries is not None:
            self._source['primaries'] = primaries
        if range_tag is not None:
            self._source['range'] = range_tag
        self._refresh()

    def set_working_space(self, name: str) -> None:
        self._working = name or ''
        self._refresh()

    def set_display_space(self, name: str) -> None:
        self._display = name or ''
        self._refresh()

    def set_provider_diagnostics(self, diag: Optional[dict]) -> None:
        self._provider_diag = diag
        self._refresh()

    def set_decode_path(self, path: str) -> None:
        self._decode_path = path or ''
        self._refresh()

    # ---- CV-B09：窄视口折叠 ----

    def _chain_text(self) -> str:
        """完整链路单行文本（折叠时的 tooltip 内容）。"""
        return " → ".join(filter(None, (
            self._src_label.text(), self._work_label.text(),
            self._out_label.text())))

    def apply_available_width(self, width: int) -> None:
        """按可用宽度折叠：链路文字让位，provider/HDR 诊断保留。

        窄视口（<380px）隐藏 SRC→WORK→OUT 链路段、整段进 tooltip；
        极窄（<220px）连降噪档位一起折叠。provider 徽标（含 fallback
        警示）与降级指示器始终保留——它们是监看诊断的核心，不允许为
        视觉精简而丢失。
        """
        try:
            width = int(width)
        except (TypeError, ValueError):
            return
        if width == self._applied_width:
            return
        self._applied_width = width
        compact = width < 380
        tiny = width < 220
        for widget in self._chain_widgets:
            widget.setVisible(not compact)
        self._chain_sep.setVisible(not compact)
        self._denoise_label.setVisible(not tiny and bool(self._denoise_quality))
        self._denoise_sep.setVisible(not tiny and bool(self._denoise_quality))
        tooltip = self._chain_text() if compact else ""
        if tooltip:
            self.setToolTip(tooltip)
        else:
            self.setToolTip("")

    # ---- 渲染 ----

    def _make_arrow(self) -> QLabel:
        label = QLabel('→')
        label.setStyleSheet(
            "QLabel { background: transparent; border: none; color:"
            " rgba(120, 140, 160, 200); font-size: 11px; }")
        return label

    def _make_sep(self) -> QLabel:
        label = QLabel('│')
        label.setStyleSheet(
            "QLabel { background: transparent; border: none; color:"
            " rgba(90, 100, 110, 180); font-size: 11px; padding: 0 6px; }")
        return label

    def _refresh(self) -> None:
        self._src_label.setText(
            'SRC ' + format_source_state(
                self._source.get('transfer'),
                self._source.get('primaries'),
                self._source.get('range')))
        self._work_label.setText(
            'WORK ' + (self._working or 'scene-linear'))
        self._out_label.setText(
            'OUT ' + (self._display or 'sRGB'))
        if self._denoise_quality:
            self._denoise_label.setText(f'DN·{self._denoise_quality}')
            # 尊重 CV-B09 极窄折叠状态（apply_available_width 已折叠时
            # 不因数据刷新重新展开）。
            tiny = (self._applied_width is not None
                    and self._applied_width < 220)
            self._denoise_label.setVisible(not tiny)
            self._denoise_sep.setVisible(not tiny)
        else:
            self._denoise_label.hide()
            self._denoise_sep.hide()
        text, warning, tooltip = format_provider_badge(self._provider_diag)
        if self._decode_path:
            text = f"{text} · {self._decode_path}"
        self._provider_label.setText(text)
        color = ('rgba(255, 190, 80, 240)' if warning
                 else 'rgba(130, 220, 150, 230)')
        self._provider_label.setStyleSheet(
            "QLabel { background: transparent; border: none; color:"
            f" {color}; font-size: 11px; padding: 0 3px 0 0; }}")
        self._provider_label.setToolTip(tooltip)
        src_tip = I18N.tr(
            'colorbar.source_tooltip',
            '源媒体容器 tag（primaries / transfer / range）')
        self._src_label.setToolTip(src_tip)
        if self._applied_width is not None and self._applied_width < 380:
            self.setToolTip(self._chain_text())
        self.adjustSize()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(18, 18, 22, 215))
        painter.drawRoundedRect(self.rect(), 6.0, 6.0)
        painter.setPen(QPen(QColor(70, 75, 85, 160), 1))
        painter.drawRoundedRect(self.rect(), 6.0, 6.0)
        painter.end()
        super().paintEvent(event)
