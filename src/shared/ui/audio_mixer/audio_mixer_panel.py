"""
Audio Mixer Panel - 完整复刻 Kdenlive Audio Mixer

完整的音频混音器面板，包含：
- Master 主混音器
- 多轨道混音器
- 滚动支持
- 折叠/展开
- 信号解耦架构
"""

from typing import Optional, Dict
import logging

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QScrollArea,
    QFrame, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize

from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget, MASTER_TRACK_ID
from src.shared.ui.audio_mixer.mixer_separator import MixerSeparator
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


logger = logging.getLogger(__name__)


# 边距常量
# Strip 外沿只保留一条很细的呼吸空间；轨道之间由深色分隔线承担层次，
# 避免多轨时出现一片过宽的灰色空隙。
MARGIN_AROUND_MIXER = 2


class AudioMixerPanel(QWidget):
    """音频混音器主面板"""

    # 信号（track_id 统一为 str，与 EditTrack.track_id 的 UUID 一致）
    master_mute_toggled = pyqtSignal(bool)
    master_volume_changed = pyqtSignal(float)          # dB
    # W11: 是否存在 LUFS 模式的表（AudioMixer 据此开/关 K-weighting 计算）
    lufs_metering_changed = pyqtSignal(bool)
    track_mute_toggled = pyqtSignal(str, bool)
    track_solo_toggled = pyqtSignal(str, bool)
    track_volume_changed = pyqtSignal(str, float)      # dB（松手时最终提交）
    track_balance_changed = pyqtSignal(str, int)       # balance（松手时最终提交）
    # 拖动期间的纯视觉预览（实时听感 + 视觉联动，不进命令栈）
    track_volume_preview = pyqtSignal(str, float)      # dB
    track_balance_preview = pyqtSignal(str, int)       # balance
    track_record_arm_toggled = pyqtSignal(str, bool)
    show_effects_for_track = pyqtSignal(str)
    # E17: purge_cache 信号已删除——全仓无 emit、处理器仅日志 no-op
    # （AM-11/RMA-12），按计划表清理，避免保留"有声明无入口"的死信号。
    # W8: Strip EQ 信号（StripEQParams | None）
    track_strip_eq_preview = pyqtSignal(str, object)    # 实时听感
    track_strip_eq_committed = pyqtSignal(str, object)   # 命令栈提交
    # W26: FX 机架效果器链信号（EffectChainParams | None）
    track_effects_preview = pyqtSignal(str, object)      # 实时听感
    track_effects_committed = pyqtSignal(str, object)     # 命令栈提交
    # W16: 轨道重命名 / 颜色
    track_name_changed = pyqtSignal(str, str)            # track_id, new_name
    track_color_changed = pyqtSignal(str, object)        # track_id, color(str|None)
    # W22: 打开 Automation Lane
    track_show_automation = pyqtSignal(str)              # track_id
    # AM-08 阶段 4.2: 打开 Routing 对话框 + 路由提交
    track_show_routing = pyqtSignal(str)                 # track_id
    track_routing_committed = pyqtSignal(str, object)    # track_id, payload dict

    # C4: 批量操作信号（一次合成命令，一次 undo）
    batch_solo_cleared = pyqtSignal(list)                # [track_id, ...] 被清除 solo 的轨道
    batch_mute_changed = pyqtSignal(list)                # [(track_id, old_muted), ...] mute 状态变更

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._track_mixers: Dict[str, TrackMixerWidget] = {}
        self._track_separators: Dict[str, QWidget] = {}
        self._master_mixer: Optional[TrackMixerWidget] = None
        self._master_separator: Optional[QWidget] = None
        self._collapsed = False
        self._solo_tracks: list[str] = []
        self._solo_muted: list[str] = []
        self._armed_track: str = ""
        # W15: 逐轨隐藏 + 窄/宽布局
        self._hidden_track_ids: set[str] = set()
        self._narrow_layout: bool = False
        # AUD-37621: LUFS 后端（scipy）可用性缓存。None=未探测。热路径
        # （update_per_track_levels 每帧逐轨）不再重复 import + 探测；
        # 进程内 scipy 可用性不会变化，探测一次即可。
        self._lufs_backend_ok: Optional[bool] = None

        self._init_ui()

    def _init_ui(self) -> None:
        """初始化 UI"""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 轨道混音器滚动区域
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setContentsMargins(0, 0, 0, 0)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # 滚动区域容器
        scroll_container = QWidget()
        scroll_container.setObjectName("mixer_tracks")
        self._tracks_layout = QHBoxLayout(scroll_container)
        self._tracks_layout.setContentsMargins(0, 0, 0, 0)
        self._tracks_layout.setSpacing(0)
        # 子轨道靠右排列：strip 是 Fixed 宽度，多余空间留在左侧，
        # 使所有轨道（含 Master）视觉上右对齐、紧凑排列。
        self._tracks_layout.setAlignment(Qt.AlignmentFlag.AlignRight)

        self._scroll_area.setWidget(scroll_container)
        main_layout.addWidget(self._scroll_area, 1)

        # 主混音器分隔线
        self._master_separator = MixerSeparator(self)
        main_layout.addWidget(self._master_separator)

        # 主混音器
        # AUD-37626: 保留 master_container 引用 —— QLayout.setAlignment(QWidget*)
        # 只作用于布局的直接子项，_master_mixer 位于此嵌套布局内，折叠居中
        # 必须在它的直接父布局（master_container）上设置对齐才生效。
        master_container = QHBoxLayout()
        master_container.setContentsMargins(0, 0, 0, 0)
        self._master_container = master_container
        self._master_mixer = TrackMixerWidget(
            track_id=MASTER_TRACK_ID,
            track_name="",
            track_tag=I18N.tr("audio.mixer.master", "Master"),
            parent=self,
            is_master=True
        )
        self._master_mixer.setContentsMargins(MARGIN_AROUND_MIXER, MARGIN_AROUND_MIXER, MARGIN_AROUND_MIXER, MARGIN_AROUND_MIXER)
        self._master_mixer._update_size_constraints()
        # 记住统一宽度（用 maximumWidth，width() 布局前为 0）
        self._uniform_strip_width = self._master_mixer.maximumWidth()
        self._setup_master_connections()
        master_container.addWidget(self._master_mixer)
        main_layout.addLayout(master_container)

        self._apply_styles()

    def _setup_master_connections(self) -> None:
        """设置主混音器信号连接"""
        if self._master_mixer:
            self._master_mixer.mute_toggled.connect(lambda tid, muted: self.master_mute_toggled.emit(muted))
            self._master_mixer.volume_changed.connect(lambda tid, db: self.master_volume_changed.emit(db))
            self._master_mixer.show_effects.connect(lambda tid: self.show_effects_for_track.emit(tid))

        # C4: panel 右键菜单提供批量操作
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_batch_menu)

    def _apply_styles(self) -> None:
        """应用面板样式"""
        self.setStyleSheet(f"""
            AudioMixerPanel {{
                background-color: {C.BG_PANEL};
            }}
            QScrollArea {{
                background-color: {C.BG_PANEL};
                border: none;
            }}
            QWidget#mixer_tracks {{
                background-color: {C.BG_PANEL};
            }}
            QWidget#mixer_base {{
                background-color: {C.BG_PANEL};
            }}
            QWidget#mixer_alternate {{
                background-color: {C.BG_PANEL};
            }}
        """)

    # ========== 公共接口 ==========

    def register_track(
        self,
        track_id: str,
        track_name: str = "",
        track_tag: str = ""
    ) -> None:
        """注册音频轨道"""
        if track_id in self._track_mixers:
            return

        mixer = TrackMixerWidget(
            track_id=track_id,
            track_name=track_name,
            track_tag=track_tag or "",
            parent=self
        )
        mixer.setContentsMargins(MARGIN_AROUND_MIXER, MARGIN_AROUND_MIXER, MARGIN_AROUND_MIXER, MARGIN_AROUND_MIXER)
        # 外边距会影响 strip 的可用内容宽度；先重算，再统一到 Master 宽度，
        # 保证子轨的推子不会被右缘裁切。
        mixer._update_size_constraints()
        # 强制使用与 Master 相同的固定宽度
        mixer.setFixedWidth(self._uniform_strip_width)
        # AUD-37623: 应用当前窄/宽布局 —— set_narrow_layout 只遍历注册时的
        # 既有 strip，此后 sync_tracks/register_track 新增的 strip 若不在此
        # 应用，窄布局流中会插入一条按宽模式固定宽度的异常宽 strip。
        if self._narrow_layout:
            self._apply_layout_mode(mixer, True)

        # 交替背景色
        color_role = "base" if len(self._track_mixers) % 2 == 0 else "alternate"
        mixer.setObjectName(f"mixer_{color_role}")

        # 添加分隔线（在已有轨道和新轨道之间）
        if self._track_mixers:
            separator = MixerSeparator(self)
            self._tracks_layout.addWidget(separator)
            self._track_separators[track_id] = separator

        # 添加到布局末尾（tracks 从左到右排列，新轨道在最右、靠近 Master）
        self._tracks_layout.addWidget(mixer)
        self._track_mixers[track_id] = mixer

        # 设置信号连接
        self._setup_track_connections(track_id, mixer)

        # 更新最小宽度
        self._update_minimum_width()

    def deregister_track(self, track_id: str) -> None:
        """移除音频轨道"""
        if track_id not in self._track_mixers:
            return

        mixer = self._track_mixers[track_id]
        self._tracks_layout.removeWidget(mixer)
        mixer.deleteLater()

        # 移除分隔线
        if track_id in self._track_separators:
            separator = self._track_separators[track_id]
            self._tracks_layout.removeWidget(separator)
            separator.deleteLater()
            del self._track_separators[track_id]

        del self._track_mixers[track_id]
        self._solo_tracks = [t for t in self._solo_tracks if t != track_id]
        self._solo_muted = [t for t in self._solo_muted if t != track_id]

        # LUFS 消费状态可能随 strip 移除而关闭
        self._recompute_lufs_metering()

        # 更新背景色
        self._update_alternating_colors()
        self._update_minimum_width()

    def clear_all_tracks(self) -> None:
        """清空所有轨道"""
        for track_id in list(self._track_mixers.keys()):
            self.deregister_track(track_id)

        self._solo_tracks.clear()
        self._solo_muted.clear()
        self._armed_track = ""

        if self._master_mixer:
            self._master_mixer.reset_meter()

    # ========== C4: 批量操作 ==========

    def clear_all_solo(self) -> None:
        """C4: 取消所有轨道的 solo 状态，并恢复被 solo 联动静音的轨道。

        发 batch_solo_cleared 信号，controller 负责合成命令（一次 undo）。
        """
        cleared_track_ids: list[str] = []
        for track_id, mixer in self._track_mixers.items():
            if getattr(mixer, '_is_solo', False):
                mixer.set_solo(False)
                cleared_track_ids.append(track_id)
        # 恢复被 solo 联动静音的轨道
        for tid in self._solo_muted:
            if tid in self._track_mixers:
                self._track_mixers[tid].set_mute(False)
        self._solo_tracks.clear()
        self._solo_muted.clear()
        if cleared_track_ids:
            self.batch_solo_cleared.emit(cleared_track_ids)

    def mute_all(self) -> None:
        """C4: 静音所有轨道（保留原 mute 状态供 unmute_all 恢复）。

        发 batch_mute_changed 信号，controller 负责合成命令。
        """
        affected: list[tuple[str, bool]] = []  # (track_id, old_muted)
        for track_id, mixer in self._track_mixers.items():
            if not mixer.is_mute():
                affected.append((track_id, False))
                mixer.set_mute(True)
        if affected:
            self.batch_mute_changed.emit(affected)

    def unmute_all(self) -> None:
        """C4: 取消所有轨道静音。发 batch_mute_changed 信号。"""
        affected: list[tuple[str, bool]] = []
        for track_id, mixer in self._track_mixers.items():
            if mixer.is_mute():
                affected.append((track_id, True))
                mixer.set_mute(False)
        if affected:
            self.batch_mute_changed.emit(affected)

    def _show_batch_menu(self, pos) -> None:
        """C4: panel 右键菜单 —— 批量 solo/mute 操作。"""
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        act_clear_solo = menu.addAction(self.tr("Clear All Solo"))
        act_mute_all = menu.addAction(self.tr("Mute All"))
        act_unmute_all = menu.addAction(self.tr("Unmute All"))
        # 无轨道时禁用
        has_tracks = bool(self._track_mixers)
        act_clear_solo.setEnabled(has_tracks and bool(self._solo_tracks))
        act_mute_all.setEnabled(has_tracks)
        act_unmute_all.setEnabled(has_tracks)

        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is act_clear_solo:
            self.clear_all_solo()
        elif chosen is act_mute_all:
            self.mute_all()
        elif chosen is act_unmute_all:
            self.unmute_all()

    def set_track_name(self, track_id: str, name: str) -> None:
        """设置轨道名称"""
        if track_id in self._track_mixers:
            self._track_mixers[track_id].set_track_name(name)

    def set_track_mute(self, track_id: str, mute: bool) -> None:
        """设置轨道静音"""
        if track_id == MASTER_TRACK_ID and self._master_mixer:
            self._master_mixer.set_mute(mute)
        elif track_id in self._track_mixers:
            self._track_mixers[track_id].set_mute(mute)

    def set_track_solo(self, track_id: str, solo: bool, additive: bool = False) -> None:
        """设置轨道独奏"""
        if track_id not in self._track_mixers:
            return

        if not solo:
            # 取消独奏
            if not additive and len(self._solo_tracks) > 1 and track_id in self._solo_tracks:
                # 恢复所有静音轨道
                for tid in self._solo_muted:
                    if tid in self._track_mixers:
                        self._track_mixers[tid].set_mute(False)
                if track_id in self._track_mixers and self._track_mixers[track_id].is_mute():
                    self._track_mixers[track_id].set_mute(False)
                self._solo_muted.clear()
                self._solo_tracks.clear()
            else:
                self._solo_tracks = [t for t in self._solo_tracks if t != track_id]
                if not self._solo_tracks:
                    for tid in self._solo_muted:
                        if tid in self._track_mixers:
                            self._track_mixers[tid].set_mute(False)
                    if track_id in self._track_mixers and self._track_mixers[track_id].is_mute():
                        self._track_mixers[track_id].set_mute(False)
                    self._solo_muted.clear()
        else:
            # 设置独奏
            if additive:
                if track_id not in self._solo_tracks:
                    self._solo_tracks.append(track_id)
                if track_id in self._solo_muted:
                    # AUD-37622: 把轨道移出联动静音名单的同时必须解除其联动
                    # 静音。此前只移出名单不解除静音，轨道保持"solo 且静音"
                    # （无声），且永远脱离恢复名单（clear_all_solo / 取消独奏
                    # 的恢复循环都不再包含它），静音成为永久残留。
                    self._solo_muted = [t for t in self._solo_muted if t != track_id]
                    target_mixer = self._track_mixers.get(track_id)
                    if target_mixer is not None and target_mixer.is_mute():
                        target_mixer.set_mute(False)
                for tid, mixer in self._track_mixers.items():
                    if tid not in self._solo_tracks:
                        was_muted = mixer.is_mute()
                        mixer.set_mute(True)
                        if tid not in self._solo_muted and not was_muted:
                            self._solo_muted.append(tid)
                        mixer.set_solo(False)
            else:
                self._solo_tracks.clear()
                if self._solo_muted:
                    for tid in self._solo_muted:
                        if tid in self._track_mixers:
                            self._track_mixers[tid].set_mute(False)
                    self._solo_muted.clear()

                for tid, mixer in self._track_mixers.items():
                    if tid != track_id and not mixer.is_mute():
                        mixer.set_mute(True)
                        self._solo_muted.append(tid)
                        mixer.set_solo(False)

                self._solo_tracks.append(track_id)

            self._track_mixers[track_id].set_solo(True)

    def update_track_levels(self, track_id: str, levels: list[float]) -> None:
        """更新轨道电平"""
        if track_id == MASTER_TRACK_ID and self._master_mixer:
            self._master_mixer.update_audio_level(levels)
        elif track_id in self._track_mixers:
            self._track_mixers[track_id].update_audio_level(levels)

    def update_master_level(self, levels: list[float]) -> None:
        """更新 Master 条电平表。"""
        if self._master_mixer:
            self._master_mixer.update_audio_level(levels)

    def set_master_volume_linear(self, volume: float) -> None:
        """W12: 用线性增益(0.0-1.0)静默回填 Master 推子（不发信号）。"""
        if self._master_mixer:
            self._master_mixer.set_volume_linear(volume)

    def set_master_muted(self, muted: bool) -> None:
        """W12: 静默回填 Master 静音状态（不发信号）。"""
        if self._master_mixer:
            self._master_mixer.set_mute(muted)

    def update_per_track_levels(self, levels: dict) -> None:
        """批量更新各轨道电平表。

        Args:
            levels: {track_id: {"peak": [l_db, r_db], "rms": [l_db, r_db]}}
                    （W9 扩展：含 peak 和 rms）。未出现在 dict 中的轨道
                    电平表清零（衰减），实现"无信号时表归零"。

                    向后兼容：若值为 list [l_db, r_db]（旧格式），直接用。
        """
        for track_id, mixer in self._track_mixers.items():
            vals = levels.get(track_id)
            if vals is None:
                # 该轨无信号（静音/非 solo），清零电平表
                mixer.reset_meter()
            else:
                # W9/W11: 根据 meter_mode 选择 peak / rms / lufs
                if isinstance(vals, dict):
                    # 获取该 strip 的电平表模式
                    meter = getattr(mixer, '_meter', None)
                    mode = getattr(meter, 'meter_mode', None)
                    if mode is not None and mode.name == "RMS":
                        meter_vals = vals.get("rms", vals.get("peak", [-100.0, -100.0]))
                    elif mode is not None and mode.name == "LUFS":
                        # AM-01/AUD-37621: LUFS 依赖 scipy 后端。后端不可用时
                        # lufs 读数恒为 -70（伪造），不能直接显示。探测结果
                        # 缓存（_lufs_backend_ok），热路径不再逐帧逐轨 import；
                        # 探测失败按"不可用"处理（fail-closed），绝不默认可用。
                        if self._lufs_backend_ok is None:
                            self._lufs_backend_ok = self._probe_lufs_backend()
                        if self._lufs_backend_ok:
                            meter_vals = vals.get("lufs", vals.get("peak", [-100.0, -100.0]))
                        else:
                            # 后端降级：显示 peak，并在 meter 上标记不可用态
                            meter_vals = vals.get("peak", [-100.0, -100.0])
                            if meter is not None:
                                meter.set_lufs_backend_unavailable(True)
                    else:
                        meter_vals = vals.get("peak", [-100.0, -100.0])
                else:
                    meter_vals = vals
                mixer.update_audio_level(list(meter_vals))

    def _probe_lufs_backend(self) -> bool:
        """探测 LUFS DSP 后端（scipy）是否可用（AUD-37621）。

        探测失败（模块缺失/访问链异常）按"不可用"处理（fail-closed）：
        宁可显示 peak + 降级标记，也不显示伪造 LUFS 读数。
        结果由调用方缓存（进程内 scipy 可用性不变）。
        """
        try:
            from src.features.edit.core.audio_capabilities import (
                is_scipy_available_for_dsp,
            )
            return bool(is_scipy_available_for_dsp())
        except (ImportError, AttributeError, RuntimeError) as e:
            logger.debug("LUFS backend probe failed, treating as unavailable: %s", e)
            return False

    def collapse_mixers(self, collapse: bool) -> None:
        """折叠/展开混音器"""
        self._collapsed = collapse
        self._scroll_area.setVisible(not collapse)
        if self._master_separator:
            self._master_separator.setVisible(not collapse)

        # AUD-37626: 对嵌套布局中的 widget 调 QLayout.setAlignment(QWidget*)
        # 是无效 no-op（只匹配布局直接子项）。改为在 _master_mixer 的直接
        # 父布局 master_container 上设置对齐，折叠居中实际生效。
        container = getattr(self, "_master_container", None)
        if container is not None and self._master_mixer is not None:
            if collapse:
                container.setAlignment(self._master_mixer, Qt.AlignmentFlag.AlignHCenter)
            else:
                # 清除对齐恢复默认拉伸（PyQt6 6.10 无 Qt.AlignmentFlag.Alignment
                # 构造器，用 AlignmentFlag(0) 表示空对齐）
                container.setAlignment(self._master_mixer, Qt.AlignmentFlag(0))

    def is_collapsed(self) -> bool:
        return self._collapsed

    # ========== W15: 逐轨隐藏/显示 + 窄/宽布局切换 ==========

    def hide_track(self, track_id: str) -> None:
        """隐藏指定 strip（保留状态，仅从视图移除）。"""
        if track_id in self._hidden_track_ids:
            return
        self._hidden_track_ids.add(track_id)
        mixer = self._track_mixers.get(track_id)
        sep = self._track_separators.get(track_id)
        if mixer is not None:
            mixer.setVisible(False)
        if sep is not None:
            sep.setVisible(False)

    def show_track(self, track_id: str) -> None:
        """显示之前隐藏的 strip。"""
        if track_id not in self._hidden_track_ids:
            return
        self._hidden_track_ids.discard(track_id)
        mixer = self._track_mixers.get(track_id)
        sep = self._track_separators.get(track_id)
        if mixer is not None:
            mixer.setVisible(True)
        if sep is not None:
            sep.setVisible(True)

    def show_all_tracks(self) -> None:
        """显示所有被隐藏的 strip。"""
        for tid in list(self._hidden_track_ids):
            self.show_track(tid)

    def is_track_hidden(self, track_id: str) -> bool:
        return track_id in self._hidden_track_ids

    def get_hidden_track_ids(self) -> set[str]:
        """返回当前隐藏的 track_id 集合（供 controller 持久化）。"""
        return set(self._hidden_track_ids)

    def set_narrow_layout(self, narrow: bool) -> None:
        """切换窄/宽 strip 布局（W15）。

        窄布局：strip 宽度收缩到紧凑模式（~60px），适合多轨项目；
        宽布局：完整模式（~110px），所有控件可见。
        """
        self._narrow_layout = narrow
        # 遍历所有 strip 应用窄/宽
        for mixer in self._track_mixers.values():
            self._apply_layout_mode(mixer, narrow)
        if self._master_mixer is not None:
            self._apply_layout_mode(self._master_mixer, narrow)

    def _apply_layout_mode(self, mixer: TrackMixerWidget, narrow: bool) -> None:
        """对单个 strip 应用窄/宽布局。"""
        # 调用 strip 的 set_narrow（若支持）
        if hasattr(mixer, 'set_narrow'):
            mixer.set_narrow(narrow)
        else:
            # 退而求其次：调整最大宽度
            mixer.setMaximumWidth(70 if narrow else 180)

    @property
    def is_narrow_layout(self) -> bool:
        return self._narrow_layout

    def get_armed_track(self) -> str:
        """返回当前 Record Arm 的轨道 id（独占 arm 模式：同时只 arm 一轨）。"""
        return self._armed_track

    # ========== 批量同步（供 controller 在 timeline 变化后整体刷新）==========

    def sync_tracks(self, tracks: list) -> None:
        """根据 timeline 的音频轨列表做 diff 同步：新增 register、移除 deregister、
        已存在则静默回填 volume/pan/mute/solo/name。

        Args:
            tracks: [{"track_id","name","tag","volume","pan","is_muted","is_solo"}, ...]
                    volume=线性增益(0.0-2.0)，pan=-1..1。
        """
        new_ids = {t["track_id"] for t in tracks}
        cur_ids = set(self._track_mixers.keys())

        # W17: 清理 solo_muted UI 联动残留
        # 数据模型里若已无 solo 轨，UI 内部的 _solo_muted 列表也必须清空
        any_solo = any(bool(t.get("is_solo", False)) for t in tracks)
        if not any_solo and self._solo_muted:
            for tid in self._solo_muted:
                if tid in self._track_mixers:
                    self._track_mixers[tid].set_mute(False)
            self._solo_muted.clear()
            self._solo_tracks.clear()

        # 1) 移除已不存在的 strip
        for tid in cur_ids - new_ids:
            self.deregister_track(tid)

        # 2) 新增缺失的 strip
        tag_counter = 0
        existing_tags = {m._track_tag for m in self._track_mixers.values()}
        for t in tracks:
            tid = t["track_id"]
            if tid in self._track_mixers:
                continue
            tag = t.get("tag") or ""
            if not tag:
                # 自动生成 A1/A2...，避开已用 tag
                tag_counter += 1
                while f"A{tag_counter}" in existing_tags:
                    tag_counter += 1
                tag = f"A{tag_counter}"
                existing_tags.add(tag)
            self.register_track(tid, track_name=t.get("name", ""), track_tag=tag)

        # 3) 静默回填所有 strip 的值（W17: 仅在实际变化时调 setter，避免无谓 style refresh）
        # AUD-37624/37625: solo/mute 的回填必须感知 solo 联动静音派生态。
        model_muted: Dict[str, bool] = {
            t["track_id"]: bool(t.get("is_muted", False)) for t in tracks
        }
        solo_ids = {t["track_id"] for t in tracks if bool(t.get("is_solo", False))}
        for t in tracks:
            tid = t["track_id"]
            mixer = self._track_mixers.get(tid)
            if mixer is None:
                continue
            name = t.get("name", "")
            if name and name != mixer._track_name:
                mixer.set_track_name(name)
            # W17: 值变化检测（用缓存的当前值对比）
            new_vol = float(t.get("volume", 1.0))
            new_pan = float(t.get("pan", 0.0))
            new_muted = model_muted.get(tid, False)
            new_solo = tid in solo_ids
            if abs(mixer.get_volume_linear() - new_vol) > 1e-6:
                mixer.set_volume_linear(new_vol)
            if abs(mixer.get_pan_linear() - new_pan) > 1e-3:
                mixer.set_pan_linear(new_pan)
            # AUD-37625: solo 激活期间被联动静音的 strip（纯 UI 派生态，模型
            # 只存 is_solo）不得按模型 is_muted=False 复位 —— 否则 M 按钮熄灭
            # 而引擎仍只出独奏轨，显示与听感相反。solo 激活时非独奏轨一律
            # 显示为静音（与 set_track_solo 的派生规则一致）。
            if new_solo or not solo_ids:
                target_mute = new_muted
            else:
                target_mute = True
            if mixer.is_mute() != target_mute:
                mixer.set_mute(target_mute)
            if getattr(mixer, '_is_solo', False) != new_solo:
                mixer.set_solo(new_solo)
            # W8: Strip EQ 回填（若 timeline 提供 strip_eq 字段）
            if "strip_eq" in t:
                mixer.set_strip_eq(t.get("strip_eq"))
            # W26: FX 机架效果器链回填（EQ/Dynamics/FX 列表效果器都存这里）
            if "effect_chain" in t:
                mixer.set_effect_chain(t.get("effect_chain"))
            # W16: 颜色回填
            if "color" in t:
                mixer.set_track_color(t.get("color"))

        # AUD-37624: 回填后重建 solo 联动记账。mixer.set_solo 是 blockSignals
        # 静默回填（不回流 _on_solo_toggled），若不重建，undo/外部恢复 solo 后
        # _solo_tracks 仍为空 → "Clear All Solo" 菜单被错误禁用、后续
        # set_track_solo 的多轨分支判断脱节。_solo_muted 按"非独奏且模型
        # 未显式静音"重算（这些轨道的静音只能来自联动，solo 清除时需恢复）。
        self._solo_tracks = [
            tid for tid, mixer in self._track_mixers.items()
            if getattr(mixer, '_is_solo', False)
        ]
        if self._solo_tracks:
            active_solo = set(self._solo_tracks)
            self._solo_muted = [
                tid for tid in self._track_mixers
                if tid not in active_solo and not model_muted.get(tid, False)
            ]
        else:
            self._solo_muted = []

    # ========== 私有方法 ==========

    def _setup_track_connections(self, track_id: str, mixer: TrackMixerWidget) -> None:
        """设置轨道信号连接"""
        mixer.mute_toggled.connect(lambda tid, muted: self.track_mute_toggled.emit(tid, muted))
        mixer.solo_toggled.connect(lambda tid, solo: self._on_solo_toggled(tid, solo))
        mixer.volume_changed.connect(lambda tid, db: self.track_volume_changed.emit(tid, db))
        mixer.balance_changed.connect(lambda tid, bal: self.track_balance_changed.emit(tid, bal))
        # 拖动期间的纯视觉预览转发
        mixer.volume_preview.connect(lambda tid, db: self.track_volume_preview.emit(tid, db))
        mixer.balance_preview.connect(lambda tid, bal: self.track_balance_preview.emit(tid, bal))
        mixer.record_arm_toggled.connect(lambda tid, armed: self._on_record_arm_toggled(tid, armed))
        mixer.show_effects.connect(lambda tid: self.show_effects_for_track.emit(tid))
        # W8: Strip EQ 信号转发
        mixer.strip_eq_preview.connect(lambda tid, p: self.track_strip_eq_preview.emit(tid, p))
        mixer.strip_eq_committed.connect(lambda tid, p: self.track_strip_eq_committed.emit(tid, p))
        # W26: FX 机架效果器链信号转发
        mixer.track_effects_preview.connect(lambda tid, c: self.track_effects_preview.emit(tid, c))
        mixer.track_effects_committed.connect(lambda tid, c: self.track_effects_committed.emit(tid, c))
        # W16: 重命名 / 颜色 转发
        mixer.name_changed.connect(lambda tid, n: self.track_name_changed.emit(tid, n))
        mixer.color_changed.connect(lambda tid, c: self.track_color_changed.emit(tid, c))
        # W22: Automation Lane
        mixer.show_automation.connect(lambda tid: self.track_show_automation.emit(tid))
        # AM-08 阶段 4.2: Routing 请求转发（panel 打开 RoutingDialog）
        mixer.show_routing.connect(lambda tid: self.track_show_routing.emit(tid))
        # W15: 隐藏 strip 请求
        mixer.hide_requested.connect(self.hide_track)
        # W11: 表模式变化 → 汇总 LUFS 消费状态（AudioMixer 按需计算）
        meter = getattr(mixer, "_meter", None)
        if meter is not None and hasattr(meter, "meter_mode_changed"):
            meter.meter_mode_changed.connect(lambda _name: self._recompute_lufs_metering())
        self._recompute_lufs_metering()

    def _recompute_lufs_metering(self) -> None:
        """汇总所有 strip 的表模式，通知外部是否需要 LUFS 计算。

        无任何 LUFS 模式的表时关闭（K-weighting 占混音热路径近一半），
        有则开启。状态未变化时不重复 emit。
        """
        active = False
        for mixer in self._track_mixers.values():
            meter = getattr(mixer, "_meter", None)
            mode = getattr(meter, "meter_mode", None)
            if mode is not None and getattr(mode, "name", "") == "LUFS":
                active = True
                break
        if active != getattr(self, "_lufs_metering_active", False):
            self._lufs_metering_active = active
            self.lufs_metering_changed.emit(active)

    def is_lufs_metering_active(self) -> bool:
        """当前是否有 strip 处于 LUFS 表模式（供初始化时同步开关）。"""
        return bool(getattr(self, "_lufs_metering_active", False))

    def _on_solo_toggled(self, track_id: str, solo: bool) -> None:
        """处理独奏切换"""
        # 检查 Shift 键状态（QApplication.keyboardModifiers() 返回全局修饰键状态）
        modifiers = QApplication.keyboardModifiers()
        additive = modifiers & Qt.KeyboardModifier.ShiftModifier
        self.set_track_solo(track_id, solo, additive)
        self.track_solo_toggled.emit(track_id, solo)

    def _on_record_arm_toggled(self, track_id: str, armed: bool) -> None:
        """处理 Record Arm 切换（独占 arm：同时只 arm 一轨，专业录音预备 UX）。"""
        if not armed:
            if track_id in self._track_mixers:
                self._track_mixers[track_id].set_armed(False)
            self._armed_track = ""
        else:
            # 取消其他轨道的 arm
            if self._armed_track and self._armed_track in self._track_mixers:
                self._track_mixers[self._armed_track].set_armed(False)
            self._armed_track = track_id
            if track_id in self._track_mixers:
                self._track_mixers[track_id].set_armed(True)

        self.track_record_arm_toggled.emit(track_id, armed)

    def _update_alternating_colors(self) -> None:
        """更新交替背景色"""
        for idx, (track_id, mixer) in enumerate(self._track_mixers.items()):
            color = "base" if idx % 2 == 0 else "alternate"
            mixer.setObjectName(f"mixer_{color}")

    def _update_minimum_width(self) -> None:
        """更新最小宽度"""
        if self._track_mixers:
            first_mixer = next(iter(self._track_mixers.values()))
            mixer_width = first_mixer.minimumWidth() + 1
            visible_count = min(2, len(self._track_mixers))
            recommended_width = mixer_width * visible_count + 3

            if not self._collapsed:
                self._scroll_area.setMinimumWidth(recommended_width)

    def sizeHint(self) -> QSize:
        if self._track_mixers:
            first_mixer = next(iter(self._track_mixers.values()))
            mixer_width = first_mixer.minimumWidth() + 1
            visible_count = min(2, len(self._track_mixers))
            recommended_width = mixer_width * visible_count + 3
            return QSize(recommended_width, 200)
        return QSize(150, 200)
