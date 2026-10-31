"""播放性能 HUD（P1-23 / P1-14 播放诊断）。

叠加在时间线面板右上角的小型信息层，500ms 轮询 EditPlaybackController
的公开属性——**不侵入播放热路径**（无信号钩子、无状态写入）：

- 播放状态 / 当前帧 / 速度倍率
- 目标 fps 与实测 fps（HUD 按帧号差分自行计算）
- 累计丢帧
- 磁盘代理就绪度（ready/generating/missing，来自注入的统计回调，
  对应 P1-14「当前实际使用哪一层」的诊断要求）

开关：TimelinePanel.toggle_playback_hud()（View 菜单「播放性能 HUD」）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QWidget

from src.shared.i18n.i18n import I18N

_REFRESH_MS = 500
_MARGIN = 12


class PlaybackHud(QWidget):
    """轮询式播放统计叠加层。"""

    def __init__(
        self,
        controller_getter: Callable[[], Any],
        stats_getter: Optional[Callable[[], dict]] = None,
        parent: Optional[QWidget] = None,
        now_fn: Callable[[], float] = time.monotonic,
        diagnostics_getter: Optional[Callable[[], dict]] = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._get_controller = controller_getter
        self._get_stats = stats_getter
        self._get_diagnostics = diagnostics_getter
        self._now = now_fn

        self._label = QLabel(self)
        self._label.setTextFormat(Qt.TextFormat.RichText)
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self._label.setFont(font)
        self._label.setStyleSheet(
            "background-color: rgba(16,17,20,205); color: #e8e8e8;"
            "border-radius: 6px; padding: 6px 10px;"
        )
        self._label.adjustSize()
        self.resize(self._label.sizeHint())

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._refresh)

        self._last_frame: Optional[int] = None
        self._last_t: Optional[float] = None
        self._measured_fps: float = 0.0

        if parent is not None:
            parent.installEventFilter(self)

        self._refresh()

    # ---- lifecycle ----

    def showEvent(self, event: Any) -> None:
        self._timer.start()
        self._reposition()
        super().showEvent(event)

    def hideEvent(self, event: Any) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def eventFilter(self, obj: Any, event: Any) -> bool:
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    def _reposition(self) -> None:
        panel = self.parentWidget()
        if panel is None:
            return
        self.move(
            max(_MARGIN, panel.width() - self.width() - _MARGIN),
            _MARGIN,
        )
        self.raise_()

    # ---- data ----

    def _refresh(self) -> None:
        controller = self._get_controller() if self._get_controller else None
        if controller is None:
            self._label.setText(I18N.tr("edit.playback_hud.no_data", "无播放数据"))
            return

        state = getattr(controller, "state", None)
        state_name = getattr(state, "name", str(state)) if state is not None else "?"
        frame = int(getattr(controller, "current_frame", 0) or 0)
        target = float(getattr(controller, "fps", 0.0) or 0.0)
        speed = float(getattr(controller, "playback_speed", 1.0) or 1.0)
        dropped = int(getattr(controller, "dropped_frames", 0) or 0)
        playing = bool(getattr(controller, "is_playing", False))

        now = self._now()
        if playing and self._last_frame is not None and self._last_t is not None:
            dt = now - self._last_t
            delta = frame - self._last_frame
            # AUD-20316: 反向播放时 delta < 0，旧条件 `delta >= 0` 使实测
            # fps 冻结在旧值。帧号差的绝对值即速率，正反向都应更新。
            if dt >= 0.2 and delta != 0:
                self._measured_fps = abs(delta) / dt
        elif not playing:
            self._measured_fps = 0.0
        self._last_frame, self._last_t = frame, now

        lines = [
            f"<span style='color:#e8e8e8'>{state_name} · {I18N.tr('edit.playback_hud.frame', '帧')} {frame} · ×{speed:g}</span>"
        ]
        if target > 0:
            if playing and self._measured_fps > 0:
                ratio = self._measured_fps / target
                if ratio >= 0.9:
                    color = "#7fd18b"
                elif ratio >= 0.7:
                    color = "#ffb454"
                else:
                    color = "#ff6b6b"
            else:
                color = "#9a9a9a"
            lines.append(
                f"<span style='color:{color}'>"
                f"{target:g} {I18N.tr('edit.playback_hud.target_fps', 'fps 目标')} · "
                f"{self._measured_fps:.1f} {I18N.tr('edit.playback_hud.measured', '实测')} · "
                f"{I18N.tr('edit.playback_hud.dropped', '丢帧')} {dropped}"
                f"</span>"
            )

        if self._get_stats is not None:
            try:
                stats = self._get_stats() or {}
            except Exception:  # noqa: BLE001 — HUD 永不因统计失败打扰播放
                stats = {}
            if stats:
                lines.append(
                    "<span style='color:#7fb3ff'>"
                    + I18N.tr(
                        "edit.playback_hud.proxy",
                        "代理 R{ready}/G{generating}/M{missing}",
                    ).format(
                        ready=stats.get("ready", 0),
                        generating=stats.get("generating", 0),
                        missing=stats.get("missing", 0),
                    )
                    + "</span>"
                )

        # P0-10: 深度诊断行（p95 / GPU pass / 拷贝字节 / fallback 分布）。
        # 全部经 getter 注入 + try 吞错：诊断缺失或失败绝不影响 HUD 本体。
        if self._get_diagnostics is not None:
            try:
                diag = self._get_diagnostics() or {}
            except Exception:  # noqa: BLE001
                diag = {}
            diag_parts = []
            total_p95 = diag.get("total_ms_p95")
            if total_p95:
                color = "#7fd18b" if total_p95 <= 33.3 else "#ffb454"
                diag_parts.append(
                    f"<span style='color:{color}'>p95 {total_p95:.1f}ms</span>"
                )
            gpu_ms = diag.get("gpu_submit_ms")
            if gpu_ms is not None:
                diag_parts.append(
                    f"<span style='color:#9a9aff'>GPU {gpu_ms:.1f}ms</span>"
                )
            elif diag.get("gpu_time_unavailable"):
                diag_parts.append(
                    "<span style='color:#9a9a9a'>GPU n/a</span>"
                )
            upload_mb = diag.get("cpu_upload_mb")
            download_mb = diag.get("cpu_download_mb")
            if upload_mb is not None and download_mb is not None:
                diag_parts.append(
                    f"<span style='color:#e8c97f'>copy ↑{upload_mb:.1f}MB "
                    f"↓{download_mb:.1f}MB</span>"
                )
            top_fallback = diag.get("top_fallback")
            if top_fallback:
                diag_parts.append(
                    f"<span style='color:#ff9b9b'>{top_fallback}</span>"
                )
            source_mix = diag.get("source_mix")
            if source_mix:
                diag_parts.append(
                    f"<span style='color:#8fd4ff'>{source_mix}</span>"
                )
            if diag_parts:
                lines.append(" ".join(diag_parts))

        self._label.setText("<br>".join(lines))
        self._label.adjustSize()
        self.resize(self._label.sizeHint())
        self._reposition()
