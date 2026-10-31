"""P1-01/F2.5 多机位角度面板（含角度网格实时监看与波形同步入口）。

多机位组定义挂在成员素材 metadata 上（multicam.METADATA_ASSET_GROUP_KEY）；
时间线片段的 asset_id 始终指向当前角度的真实素材。本面板展示当前多机位
片段所属组：

- 角度网格：每个角度一格（画面 = 该角度在 playhead 时刻的源帧，
  帧号映射与切换命令语义一致），点击在 playhead 处切换角度
  （SwitchMulticamAngleCommand，可撤销）；当前角度高亮；
- F2.5 实时路径：播放期间经 MulticamGridStream 主时钟驱动的有界多路
  CPU 小尺寸解码（latest-wins + 降质阶梯 + generation 取消），每格
  标注"实时/静帧/缺路"模式与降质档；不经过 ThumbnailProvider；
- 静帧路径：暂停/慢速拖动经 ThumbnailProvider 异步取帧（播放时自动
  让路），playhead 变化由控制器推帧、面板内 250ms 合并去抖；
- 波形同步：选择参考角度 → 后台互相关各角度音频 → 偏移写回组
  metadata（面板只上报意图 ``sync_requested``，命令在控制器）。

资源边界：面板隐藏/窗口最小化时停发一切取帧请求；网格流为独立小尺寸
CPU 解码，不占用 Program 的 VT 硬解会话池。
"""

from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.media.thumbnail_provider import ThumbnailProvider

_TILE_THUMB_HEIGHT = 96
# AUD-20301: 未应答取帧请求的滞留超时（秒）。播放期间发起的请求可能永远
# 不应答（provider 挂起让路），到期后由 _prune_stale_pending 清理。
_PENDING_RESULT_TIMEOUT_S = 5.0

# F2.5 tile 模式徽标（实时路径/静帧路径/缺路）
_MODE_BADGE_STYLES = {
    "live": ("LIVE", "background: rgba(0,170,255,0.85); color: #04121c;"),
    "degraded": ("LIVE·", "background: rgba(255,170,0,0.9); color: #1c1404;"),
    "static": ("静帧", "background: rgba(122,132,140,0.85); color: #10151a;"),
    "missing": ("缺路", "background: rgba(232,64,64,0.9); color: #1c0808;"),
}


class _AngleTile(QFrame):
    """网格中的一格：缩略图 + 序号角标 + 标签/偏移行。点击切换角度。"""

    clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(_TILE_THUMB_HEIGHT + 22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._asset_id = ""
        self._path = ""
        self._requested_frame = -1
        self._active = False
        self._apply_style()

        grid = QGridLayout(self)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(2)

        self._thumb = QLabel()
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setFixedHeight(_TILE_THUMB_HEIGHT)
        self._thumb.setStyleSheet(
            "background: #14181d; border-radius: 3px; color: #5a626b;")
        self._thumb.setText("…")
        grid.addWidget(self._thumb, 0, 0, 1, 2)

        # F2.5：模式徽标（实时/静帧/缺路 + 降质档），悬浮缩略图右上角
        self._mode_badge = QLabel("")
        self._mode_badge.setStyleSheet(
            "color: #10151a; font-size: 9px; font-weight: 700;"
            "border-radius: 3px; padding: 1px 4px;")
        self._mode_badge.hide()
        grid.addWidget(self._mode_badge, 0, 1,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        self._key_label = QLabel("")
        self._key_label.setStyleSheet(
            "color: #00aaff; font-size: 11px; font-weight: 700;")
        grid.addWidget(self._key_label, 1, 0)

        self._caption = QLabel("")
        self._caption.setStyleSheet(
            "color: #cfd4da; font-size: 10px;")
        grid.addWidget(self._caption, 1, 1)
        grid.setColumnStretch(1, 1)

    # -- 状态 ------------------------------------------------------------

    def set_caption(self, index: int, label: str, sync_offset: int) -> None:
        self._key_label.setText(str(index) if index < 10 else "·")
        offset_badge = ""
        if sync_offset:
            sign = "+" if sync_offset > 0 else "−"
            offset_badge = f"  [{sign}{abs(sync_offset)}f]"
        text = (label or "")[:18] or "—"
        self._caption.setText(text + offset_badge)
        self._caption.setToolTip(
            f"{label}\nsync offset: {sync_offset} frames")

    def set_request_key(self, asset_id: str, path: str, frame: int) -> None:
        self._asset_id = asset_id
        self._path = path
        self._requested_frame = frame

    def request_key(self) -> tuple[str, str, int]:
        return (self._asset_id, self._path, self._requested_frame)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        scaled = pixmap.scaledToHeight(
            _TILE_THUMB_HEIGHT,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb.setPixmap(scaled)

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self._apply_style()

    def set_mode(self, mode: str, suffix: str = "") -> None:
        """F2.5 模式徽标：live/degraded/static/missing + 降质档后缀。"""
        entry = _MODE_BADGE_STYLES.get(mode)
        if entry is None:
            self._mode_badge.hide()
            return
        text, base_style = entry
        self._mode_badge.setText(text + suffix)
        self._mode_badge.setStyleSheet(base_style + " font-size: 9px;"
                                       " font-weight: 700; border-radius: 3px;"
                                       " padding: 1px 4px;")
        self._mode_badge.show()

    def set_busy(self, busy: bool) -> None:
        current = self._thumb.pixmap()
        if current is None or current.isNull():
            self._thumb.setText("…" if busy else "—")

    def _apply_style(self) -> None:
        if self._active:
            self.setStyleSheet(
                "_AngleTile { border: 2px solid #00aaff; border-radius: 5px; "
                "background: rgba(0, 170, 255, 0.10); }")
        else:
            self.setStyleSheet(
                "_AngleTile { border: 1px solid #2a3138; border-radius: 5px; "
                "background: #191e24; }")

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt 覆写
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class MulticamPanel(QWidget):
    """多机位角度面板：网格监看 + 角度切换 + 波形同步入口。"""

    #: (group_id, angle_asset_id, sync_offset) —— 在 playhead 切到该角度
    switch_angle_requested = pyqtSignal(str, str, int)
    #: 参考角度 asset_id —— 请求按音频波形同步整个组
    sync_requested = pyqtSignal(str)
    #: F2.6：时间码同步（asset_id = 参考角度）与分析中途取消
    tc_sync_requested = pyqtSignal(str)
    sync_cancel_requested = pyqtSignal()

    FRAME_COALESCE_MS = 250

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        grid_stream_factory=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("multicamPanel")
        self._group = None          # MulticamAngleGroup | None
        self._active_asset_id: str = ""
        self._asset_infos: dict = {}     # asset_id → {"path","total_frames"}
        self._tiles: dict[str, _AngleTile] = {}
        # AUD-20301: key → (asset_id, 请求发起时间戳)；带时间戳以便清理
        # 永不应答的滞留请求（播放挂起/素材移除），防止缓慢泄漏。
        self._pending_by_key: dict[tuple[str, int], tuple[str, float]] = {}
        self._sync_running = False
        self._sync_worker_ref = None
        # F2.5 实时网格流：播放会话期间由主时钟驱动，暂停即让路静帧路径
        self._grid_stream_factory = grid_stream_factory
        self._grid_stream = None
        self._grid_generation = 0
        self._playback_active = False
        self._grid_summary_log = None  # 测试钩子：session_summary 记录
        self.destroyed.connect(self._shutdown_grid_stream)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        title = QLabel(I18N.tr("edit.multicam.title", "Multicam"))
        title.setStyleSheet("color: #e8e8e8; font-size: 12px; font-weight: 600;")
        root.addWidget(title)

        self._group_label = QLabel(I18N.tr("edit.multicam.no_group", "无活动多机位片段"))
        self._group_label.setStyleSheet("color: #9aa0a6; font-size: 10px;")
        self._group_label.setWordWrap(True)
        root.addWidget(self._group_label)

        # M13.5：自适应降档徽标（multicam_quality_policy degraded=True
        # 时明示——不静默降档；策略见 core/multicam_adaptive.py）
        self._quality_badge = QLabel("")
        self._quality_badge.setStyleSheet(
            "color: #e0a030; font-size: 10px;"
            " background: rgba(224,160,48,28); border-radius: 3px;"
            " padding: 1px 6px;")
        self._quality_badge.hide()
        root.addWidget(self._quality_badge)

        # -- 波形同步行 --------------------------------------------------
        sync_row = QWidget()
        sync_h = QHBoxLayout(sync_row)
        sync_h.setContentsMargins(0, 0, 0, 0)
        sync_h.setSpacing(6)

        self._reference_combo = QComboBox()
        self._reference_combo.setToolTip(I18N.tr(
            "edit.multicam.reference_tooltip",
            "波形同步的参考角度（其他角度对齐到它）",
        ))
        sync_h.addWidget(self._reference_combo, 1)

        from PyQt6.QtWidgets import QPushButton
        self._sync_button = QPushButton(I18N.tr(
            "edit.multicam.sync_button", "波形同步"))
        self._sync_button.setToolTip(I18N.tr(
            "edit.multicam.sync_tooltip",
            "互相关各角度开头音频段，自动对齐（约数秒）",
        ))
        self._sync_button.clicked.connect(self._on_sync_clicked)
        sync_h.addWidget(self._sync_button)

        # F2.6：时间码同步（无开头共同音频时的替代方式）
        self._tc_sync_button = QPushButton(I18N.tr(
            "edit.multicam.tc_sync_button", "TC 同步"))
        self._tc_sync_button.setToolTip(I18N.tr(
            "edit.multicam.tc_sync_tooltip",
            "按各角度素材时间码元数据差对齐（无需共同音频）",
        ))
        self._tc_sync_button.clicked.connect(self._on_tc_sync_clicked)
        sync_h.addWidget(self._tc_sync_button)

        # F2.6：分析中途取消（worker requestInterruption，零写入）
        self._cancel_sync_button = QPushButton(I18N.tr(
            "edit.multicam.cancel_sync", "取消"))
        self._cancel_sync_button.setVisible(False)
        self._cancel_sync_button.clicked.connect(self._on_cancel_sync_clicked)
        sync_h.addWidget(self._cancel_sync_button)
        root.addWidget(sync_row)

        self._hint = QLabel(I18N.tr(
            "edit.multicam.hint",
            "点击角度或按 1-9 在 playhead 切换（可撤销）；网格显示各角度当前时刻画面。",
        ))
        self._hint.setStyleSheet("color: #787f87; font-size: 10px;")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._grid_host)
        root.addWidget(scroll, 1)

        # playhead 帧合并定时器（高频 playhead 信号 → 低频缩略图请求）
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(self.FRAME_COALESCE_MS)
        self._frame_timer.timeout.connect(self._apply_angle_frames)
        self._latest_frames: dict = {}
        self._latest_infos: dict = {}

        # 全局取帧完成信号（路径+帧号匹配 pending 请求）
        try:
            ThumbnailProvider.instance().frame_thumbnail_ready.connect(
                self._on_frame_thumbnail_ready)
        except Exception:  # noqa: BLE001 — 测试替身/无 GUI 环境下静默
            pass

    # ------------------------------------------------------------------
    # 状态注入（控制器驱动）
    # ------------------------------------------------------------------

    def set_group(
        self,
        group,
        active_asset_id: str = "",
        asset_infos: Optional[dict] = None,
    ) -> None:
        """更新展示的组与当前角度高亮。

        Args:
            group: ``MulticamAngleGroup`` 或 None（清空）。
            active_asset_id: 当前片段使用的角度素材 id（高亮该角度）。
            asset_infos: asset_id → {"path": str, "total_frames": int}，
                角度网格取帧所需（缺省沿用上一次的 infos）。
        """
        self._group = group
        self._active_asset_id = active_asset_id or ""
        if asset_infos is not None:
            self._asset_infos = dict(asset_infos)

        # 清空旧网格
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._tiles.clear()
        self._pending_by_key.clear()
        self._reference_combo.clear()

        if group is None:
            self._group_label.setText(
                I18N.tr("edit.multicam.no_group", "无活动多机位片段"))
            self._sync_button.setEnabled(False)
            self._reference_combo.setEnabled(False)
            if self._grid_stream is not None:
                self._grid_stream.end_session()  # 组清空：作废在途帧
            return

        self._group_label.setText(f"{group.name} · {len(group.angles)} angles")
        columns = 2 if len(group.angles) <= 4 else 3
        for index, angle in enumerate(group.angles, start=1):
            tile = _AngleTile()
            tile.set_caption(index, angle.label or angle.asset_id[:8],
                             angle.sync_offset)
            tile.set_active(angle.asset_id == self._active_asset_id)
            tile.clicked.connect(
                lambda _=False, g=group, a=angle:
                    self.switch_angle_requested.emit(
                        g.group_id, a.asset_id, a.sync_offset)
            )
            self._grid.addWidget(
                tile, (index - 1) // columns, (index - 1) % columns)
            self._tiles[angle.asset_id] = tile
            self._reference_combo.addItem(
                f"{index} · {angle.label or angle.asset_id[:8]}",
                angle.asset_id,
            )

        # 参考角度默认 = 当前角度
        combo_index = self._reference_combo.findData(self._active_asset_id)
        if combo_index >= 0:
            self._reference_combo.setCurrentIndex(combo_index)

        self._sync_button.setEnabled(not self._sync_running and len(group.angles) >= 2)
        self._reference_combo.setEnabled(len(group.angles) >= 2)

    # ------------------------------------------------------------------
    # F2.5 实时路径（播放会话）与静帧路径（暂停/慢速拖动）
    # ------------------------------------------------------------------

    def set_playback_active(self, active: bool) -> None:
        """播放状态切换（控制器在 playback_started/stopped 时驱动）。

        active=True：开启网格流会话（generation bump），各格走实时解码
        路径，绕开 250ms 去抖；active=False：结束会话并立刻回落静帧路径
        （ThumbnailProvider 去抖取帧），各格标注"静帧"。
        """
        self._playback_active = bool(active)
        if active:
            stream = self._ensure_grid_stream()
            if stream is not None:
                self._grid_generation = stream.begin_session()
                for asset_id, tile in self._tiles.items():
                    tile.set_mode("live")
            return
        if self._grid_stream is not None:
            self._grid_stream.end_session()
        for tile in self._tiles.values():
            tile.set_mode("static")
        # 回落静帧：立刻按最新帧走一次去抖路径（暂停瞬间画面不滞留旧帧）
        if self._group is not None and self._latest_frames:
            if not self._frame_timer.isActive():
                self._frame_timer.start()

    def _ensure_grid_stream(self):
        if self._grid_stream is not None:
            return self._grid_stream
        factory = self._grid_stream_factory
        if factory is None:
            from src.features.edit.ui.multicam_grid_stream import (
                MulticamGridStream,
            )
            factory = MulticamGridStream
        try:
            stream = factory()
        except Exception:  # noqa: BLE001 — 无解码环境（headless 测试）静默
            return None
        stream.signals.frame_ready.connect(self._on_grid_frame_ready)
        stream.signals.angle_state.connect(self._on_grid_angle_state)
        stream.signals.session_summary.connect(self._on_grid_session_summary)
        stream.start()
        self._grid_stream = stream
        return stream

    def _shutdown_grid_stream(self, *_args) -> None:
        stream = getattr(self, "_grid_stream", None)
        if stream is not None:
            stream.shutdown()

    def _grid_gate_open(self) -> bool:
        """可见性门控：面板隐藏或窗口最小化时停发取帧请求。"""
        if not self.isVisible():
            return False
        window = self.window()
        return not (window is not None and window.isMinimized())

    def _on_grid_frame_ready(
        self, asset_id: str, timeline_frame: int, qimage: QImage,
        generation: int,
    ) -> None:
        if generation != self._grid_generation:
            return  # 旧会话迟到帧：不回显
        tile = self._tiles.get(asset_id)
        if tile is None or qimage is None:
            return
        tile.set_pixmap(QPixmap.fromImage(qimage))
        # 降质档随帧刷新徽标（掉监看帧/降档可解释）
        stats = self._grid_stream.stats().get(asset_id) \
            if self._grid_stream is not None else None
        if stats is not None:
            level = int(stats.get("level", 0))
            if level > 0:
                tile.set_mode("degraded", suffix=f"L{level}")
            else:
                tile.set_mode("live")

    def _on_grid_angle_state(
        self, asset_id: str, state: str, generation: int,
    ) -> None:
        if generation != self._grid_generation:
            return
        tile = self._tiles.get(asset_id)
        if tile is not None:
            tile.set_mode("live" if state == "live" else "missing")

    def _on_grid_session_summary(self, summary: dict) -> None:
        """会话统计（decoded/掉帧/抽稀/降质档）——掉监看帧可解释。"""
        if self._grid_summary_log is not None:
            self._grid_summary_log(summary)

    def showEvent(self, event) -> None:  # noqa: N802 — Qt 覆写
        super().showEvent(event)
        if self._grid_stream is not None:
            self._grid_stream.set_active(True)
        # 恢复可见：按当前模式立刻补一帧（静帧路径去抖）
        if self._group is not None and self._latest_frames and \
                not self._frame_timer.isActive():
            self._frame_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 — Qt 覆写
        super().hideEvent(event)
        if self._grid_stream is not None:
            self._grid_stream.set_active(False)

    # ------------------------------------------------------------------
    # 角度网格取帧（静帧路径：控制器推帧 → 面板去抖 → ThumbnailProvider）
    # ------------------------------------------------------------------

    def request_angle_frames(
        self,
        frames: dict,
        infos: Optional[dict] = None,
        timeline_frame: int = -1,
    ) -> None:
        """控制器在 playhead 变化时推送各角度应显示的源帧号。

        播放会话中且面板可见：直接进入实时网格流（latest-wins，绕开
        250ms 去抖），同一批次携带同一时间线帧号供"同一时刻"对拍；
        其余情形走静帧路径（250ms 合并去抖 → ThumbnailProvider）。
        """
        self._latest_frames = dict(frames)
        if infos is not None:
            self._latest_infos = dict(infos)
        if self._playback_active and self._grid_stream is not None:
            if self._grid_gate_open():
                self._grid_stream.push(
                    frames, infos or {}, timeline_frame,
                    generation=self._grid_generation)
            return  # 隐藏/最小化：不取帧，恢复可见后补帧
        if not self._frame_timer.isActive():
            self._frame_timer.start()

    def _tile_target_height(self) -> int:
        """M13.5：自适应降档——角度数经 multicam_quality_policy 定档，
        缩略请求高度按档缩放；降档时徽标明示（reason 全文）。"""
        from src.features.edit.core.multicam_adaptive import (
            multicam_quality_policy,
        )
        policy = multicam_quality_policy(len(self._tiles or {}))
        badge = getattr(self, "_quality_badge", None)
        if badge is not None:
            if policy.get("degraded"):
                badge.setText(f"预览降档：{policy.get('reason', '')}")
                badge.show()
            else:
                badge.clear()
                badge.hide()
        return max(48, int(_TILE_THUMB_HEIGHT * float(policy["scale"])))

    def _apply_angle_frames(self) -> None:
        if self._group is None or not self._tiles:
            return
        merged_infos = {**self._asset_infos, **self._latest_infos}
        provider = self._thumbnail_provider()
        if provider is None:
            return
        # M13.5：降档策略在批请求前求值一次（徽标同步刷新）
        tile_height = self._tile_target_height()
        self._prune_stale_pending()
        for asset_id, frame in self._latest_frames.items():
            tile = self._tiles.get(asset_id)
            info = merged_infos.get(asset_id) or {}
            path = info.get("path", "")
            total = int(info.get("total_frames", 0) or 0)
            if tile is None or not path or total < 1:
                continue
            frame = int(max(0, min(int(frame), total - 1)))
            key = (path, frame)
            if tile.request_key() == (asset_id, path, frame):
                continue  # 已请求同一帧
            tile.set_request_key(asset_id, path, frame)
            tile.set_busy(True)
            self._pending_by_key[key] = (asset_id, time.monotonic())
            provider.request_frame_pixmap_async(
                asset_id, path, frame, total,
                target_height=tile_height,
            )

    def _prune_stale_pending(self) -> None:
        """清理超时未应答的取帧请求（AUD-20301）。

        播放期间 provider 挂起让路、或素材被移除时，请求可能永不完成；
        不清理则 ``_pending_by_key`` 缓慢泄漏，迟到结果还会短暂回显过期帧。
        """
        now = time.monotonic()
        stale = [
            key
            for key, (_asset_id, requested_at) in self._pending_by_key.items()
            if now - requested_at > _PENDING_RESULT_TIMEOUT_S
        ]
        for key in stale:
            self._pending_by_key.pop(key, None)

    def _on_frame_thumbnail_ready(self, path: str, frame: int, pixmap: QPixmap) -> None:
        entry = self._pending_by_key.pop((path, frame), None)
        if entry is None:
            return
        asset_id = entry[0]
        tile = self._tiles.get(asset_id)
        if tile is None or pixmap is None:
            return
        # AUD-20301: 该 tile 已改请求更新的帧 → 迟到的旧帧直接丢弃，
        # 不再短暂回显过期画面。
        if tile.request_key() != (asset_id, path, frame):
            return
        tile.set_pixmap(pixmap)

    def _thumbnail_provider(self):
        try:
            return ThumbnailProvider.instance()
        except Exception:  # noqa: BLE001 — 测试替身环境
            return None

    # ------------------------------------------------------------------
    # 波形同步
    # ------------------------------------------------------------------

    def set_sync_running(self, running: bool) -> None:
        """同步 worker 运行期间禁用入口。"""
        self._sync_running = bool(running)
        has_group = self._group is not None and len(self._group.angles) >= 2
        self._sync_button.setEnabled(not self._sync_running and has_group)
        self._cancel_sync_button.setVisible(bool(running))
        self._tc_sync_button.setEnabled(
            not self._sync_running and has_group)
        if running:
            self._sync_button.setText(I18N.tr(
                "edit.multicam.syncing", "同步中…"))
        else:
            self._sync_button.setText(I18N.tr(
                "edit.multicam.sync_button", "波形同步"))

    def _on_sync_clicked(self) -> None:
        asset_id = self._reference_combo.currentData()
        if isinstance(asset_id, str) and asset_id:
            self.sync_requested.emit(asset_id)

    def _on_tc_sync_clicked(self) -> None:
        """F2.6：时间码同步入口（参考角度与波形同步共用下拉）。"""
        asset_id = self._reference_combo.currentData()
        if asset_id:
            self.tc_sync_requested.emit(asset_id)

    def _on_cancel_sync_clicked(self) -> None:
        """F2.6：取消正在进行的波形同步分析。"""
        self.sync_cancel_requested.emit()

    def store_sync_worker(self, worker) -> None:
        """持有 worker 引用防 GC（控制器在完成后置 None）。"""
        self._sync_worker_ref = worker
