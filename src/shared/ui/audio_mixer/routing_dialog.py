"""
Routing Dialog — Bus 路由编辑对话框（AM-08 阶段 4.2 Routing UI）。

提供专业混音器的路由编辑能力：
- 输出目标选择（直送 Master 或路由到指定 Bus）
- Send slot 列表（post-fader 发送，可增删/调 level/启用/选目标）
- 实时环检测告警（创建会导致引擎崩溃的环前阻止提交）

信号模式（与 StripEQDialog 一致，三段式节流）：
    routing_preview(str, dict)  — 拖动期实时预览（不进命令栈）
    routing_committed(str, dict) — 松手/离散操作时提交（进命令栈）

payload dict 结构：
    {
        "output_bus_id": Optional[str],   # None=Master
        "sends": [Send, ...],             # 完整 sends 列表（替换语义）
    }

设计原则（计划表 4.2）：
- UI 自动化创建/删除/重路由，模型、图与 meter 同步。
- cycle 输入即时显示错误（validate_routing_graph 实时告警）。
- 不允许无声跳过：环或未知目标必须告知用户。
"""

from __future__ import annotations

from typing import Any, List, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n import I18N


class _SendSlotWidget(QFrame):
    """单个 Send slot 控件（目标选择 + level + enable + 删除）。

    信号：
        changed — 离散字段变化（目标切换/开关；供父对话框收集 + 提交）
        preview — level 拖动期的连续变化（AUD-37811：供父对话框发
                  routing_preview 实时预览，不进命令栈）
        removed — 用户点击删除按钮（AUD-37812：父对话框据此把槽位
                  控件从 UI 移除，保持 UI 与模型状态一致）
    """
    changed = pyqtSignal()
    preview = pyqtSignal()
    removed = pyqtSignal()

    def __init__(
        self,
        send: Any,
        bus_options: List[tuple],  # [(track_id, name), ...]
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._send_id = getattr(send, "send_id", "")
        # AUD-37813: 只写不读的 _bus_options 死属性已移除。
        self.setFrameShape(QFrame.Shape.Box)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        # 启用开关
        from src.shared.ui.widgets.toggle_switch import ToggleSwitch
        self._enable_switch = ToggleSwitch()
        self._enable_switch.setChecked(bool(getattr(send, "enabled", True)))
        self._enable_switch.toggled.connect(self._on_changed)
        layout.addWidget(self._enable_switch)

        # 目标选择
        self._target_combo = QComboBox()
        self._target_combo.addItem("(选择 Bus)", "")
        for bus_id, bus_name in bus_options:
            self._target_combo.addItem(bus_name, bus_id)
        # 选中当前目标
        current_target = getattr(send, "target_bus_id", "")
        for i in range(self._target_combo.count()):
            if self._target_combo.itemData(i) == current_target:
                self._target_combo.setCurrentIndex(i)
                break
        self._target_combo.currentIndexChanged.connect(self._on_changed)
        layout.addWidget(self._target_combo, stretch=1)

        # level 推子（0.0-1.0，post-fader 发送量）
        from src.shared.ui.widgets.drag_value import DragValue
        self._level_spin = DragValue(
            label="", min_v=0.0, max_v=1.0,
            default=float(getattr(send, "level", 1.0)),
            decimals=2, suffix="", compact=True,
        )
        self._level_spin.valueChanged.connect(self._on_changed)
        self._level_spin.interaction_started.connect(self._on_drag_started)
        self._level_spin.interaction_ended.connect(self._on_drag_ended)
        layout.addWidget(self._level_spin)

        # 删除按钮
        self._remove_btn = QPushButton("✕")
        self._remove_btn.setFixedWidth(28)
        self._remove_btn.setToolTip(I18N.tr("audio.mixer.routing.remove_send", "删除此 Send"))
        self._remove_btn.clicked.connect(self._on_remove)
        layout.addWidget(self._remove_btn)

        self._dragging = False
        self._removed = False

    def _on_changed(self) -> None:
        if self._dragging:
            # AUD-37811: 拖动 level 期间发 preview（实时听感/视觉反馈），
            # 而非完全静默到松手才提交。
            self.preview.emit()
        else:
            self.changed.emit()

    def _on_drag_started(self) -> None:
        self._dragging = True

    def _on_drag_ended(self) -> None:
        self._dragging = False
        self.changed.emit()

    def _on_remove(self) -> None:
        # AUD-37812: 标记删除并通知父对话框 —— 由父对话框把本控件从
        # 槽位列表/布局中移除后再触发提交，保证 UI 与模型状态一致。
        self._removed = True
        self.removed.emit()

    @property
    def is_removed(self) -> bool:
        return self._removed

    @property
    def is_target_empty(self) -> bool:
        """目标是否为空（占位项被选中，或目标 Bus 已不存在时停在占位项）。"""
        return not (self._target_combo.currentData() or "")

    def collect(self) -> Optional[Any]:
        """收集此 slot 的 Send。

        已标记删除或目标为空（占位项）时返回 None：
        Send 对空 target_bus_id 的构造校验会抛 ValueError（timeline_v2.Send），
        这里在构造前过滤，避免在 Qt 槽内未捕获崩溃（AUD-37809）。
        """
        if self._removed:
            return None
        target = self._target_combo.currentData() or ""
        if not target:
            return None
        from src.features.edit.core.timeline_v2 import Send
        return Send(
            send_id=self._send_id,
            target_bus_id=target,
            level=float(self._level_spin.value()),
            enabled=bool(self._enable_switch.isChecked()),
        )


class RoutingDialog(QDialog):
    """Bus 路由编辑对话框（AM-08 阶段 4.2）。

    信号：
        routing_preview(str, dict)  — 拖动期实时预览
        routing_committed(str, dict) — 松手/离散操作提交
    """

    routing_preview = pyqtSignal(str, object)   # track_id, payload dict
    routing_committed = pyqtSignal(str, object)  # track_id, payload dict

    def __init__(
        self,
        track_id: str,
        track_name: str,
        output_bus_id: Optional[str],
        sends: tuple,
        bus_tracks: List[tuple],  # [(track_id, name), ...] 候选 Bus
        parent: Optional[QWidget] = None,
        routing_snapshot: Optional[dict] = None,
    ) -> None:
        """初始化 Routing 对话框。

        Args:
            routing_snapshot: AUD-37810 —— 其他轨道的真实路由快照
                {track_id: {"output_bus_id": Optional[str],
                            "sends": [(bus_id, level), ...],
                            "is_bus": bool}}。
                提供时环检测在完整路由图上进行（可检出 Bus→Bus 环）；
                缺省时退化为仅含当前轨道的简化图（只能检出当前轨道
                引入的未知目标，Bus 间既有环不可见）。调用方接线见
                edit_controller._on_mixer_show_routing。
        """
        super().__init__(parent)
        self._track_id = track_id
        self._bus_tracks = bus_tracks
        self._routing_snapshot = routing_snapshot

        self.setWindowTitle(
            I18N.tr("audio.mixer.routing.title", "Routing — {name}").format(name=track_name)
        )
        self.setModal(False)
        self.setMinimumSize(520, 420)

        self._output_bus_id = output_bus_id
        self._initial_sends = tuple(sends or ())
        self._send_slots: List[_SendSlotWidget] = []

        self._output_combo: Optional[QComboBox] = None
        self._warning_label: Optional[QLabel] = None
        self._sends_container: Optional[QWidget] = None
        self._sends_layout: Optional[QVBoxLayout] = None

        # AUD-37808: 初始装载状态锁。_load_initial_state 的程序化变更
        # （setCurrentIndex 等）不得触发 _on_committed —— 否则对话框刚打开
        # 就以空 sends 列表发射 routing_committed，消费端按替换语义删除
        # 该轨全部 Send（数据丢失）。
        self._loading = True
        self._init_ui()
        self._load_initial_state()
        self._loading = False
        # 装载完成后做一次纯校验（仅显示既有告警，不发射 committed）：
        # 目标 Bus 已被删除的 send 等问题应在对话框打开时即可见，
        # 而非等到用户第一次交互。
        self._validate_and_warn(self._collect())

    # ────────────────────────── UI 构建 ──────────────────────────

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 标题
        title = QLabel(
            I18N.tr("audio.mixer.routing.header", "输出路由与发送")
        )
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        # ── 输出目标 ──
        output_group = QGroupBox(
            I18N.tr("audio.mixer.routing.output_group", "输出目标（Output）")
        )
        og_layout = QVBoxLayout(output_group)
        self._output_combo = QComboBox()
        self._output_combo.addItem("Master（直送）", None)
        for bus_id, bus_name in self._bus_tracks:
            self._output_combo.addItem(bus_name, bus_id)
        self._output_combo.currentIndexChanged.connect(self._on_committed)
        og_layout.addWidget(self._output_combo)
        layout.addWidget(output_group)

        # ── Send slots ──
        sends_group = QGroupBox(
            I18N.tr("audio.mixer.routing.sends_group", "发送（Sends, post-fader）")
        )
        sg_layout = QVBoxLayout(sends_group)

        # 滚动区域容纳多个 send slot
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._sends_container = QWidget()
        self._sends_layout = QVBoxLayout(self._sends_container)
        self._sends_layout.setContentsMargins(0, 0, 0, 0)
        self._sends_layout.setSpacing(4)
        self._sends_layout.addStretch()
        scroll.setWidget(self._sends_container)
        sg_layout.addWidget(scroll)

        # 添加 send 按钮
        self._add_send_btn = QPushButton(
            I18N.tr("audio.mixer.routing.add_send", "+ 添加发送")
        )
        add_btn = self._add_send_btn
        if not self._bus_tracks:
            # AUD-37809: 无候选 Bus 时无法构造有效 Send（空 target 会触发
            # Send 的 ValueError 校验），禁用入口并说明原因。
            add_btn.setEnabled(False)
            add_btn.setToolTip(I18N.tr(
                "audio.mixer.routing.add_send_disabled",
                "项目中没有 Bus 轨，无法添加发送",
            ))
        add_btn.clicked.connect(self._on_add_send)
        sg_layout.addWidget(add_btn)

        layout.addWidget(sends_group, stretch=1)

        # ── 告警区 ──
        self._warning_label = QLabel("")
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet(
            "color: #ff6b6b; background: rgba(255,107,107,0.1); "
            "padding: 6px; border-radius: 4px;"
        )
        self._warning_label.hide()
        layout.addWidget(self._warning_label)

        # ── 关闭按钮 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton(I18N.tr("common.close", "关闭"))
        close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _load_initial_state(self) -> None:
        """加载初始状态到 UI。"""
        # 输出目标
        for i in range(self._output_combo.count()):
            if self._output_combo.itemData(i) == self._output_bus_id:
                self._output_combo.setCurrentIndex(i)
                break
        # sends
        for send in self._initial_sends:
            self._add_send_slot(send)

    def _add_send_slot(self, send: Any) -> None:
        """添加一个 send slot 控件。"""
        slot = _SendSlotWidget(send, self._bus_tracks, parent=self._sends_container)
        slot.changed.connect(self._on_committed)
        # AUD-37811: level 拖动期实时预览
        slot.preview.connect(self._on_preview)
        # AUD-37812: 删除 send 时同步移除槽位控件（UI 与模型状态一致）
        slot.removed.connect(lambda s=slot: self._on_send_removed(s))
        # 插入到 stretch 之前
        self._sends_layout.insertWidget(self._sends_layout.count() - 1, slot)
        self._send_slots.append(slot)

    def _on_send_removed(self, slot: _SendSlotWidget) -> None:
        """AUD-37812: 用户删除某 send 槽位 —— 从 UI 与槽位列表同步移除后提交。

        此前 _SendSlotWidget._on_remove 只置标记发 changed，槽位控件既不从
        布局移除也不隐藏：模型侧 collect() 已跳过该 send，但 UI 上仍显示
        一行"已删除"的 slot，用户无法分辨删除是否生效。
        """
        if slot in self._send_slots:
            self._send_slots.remove(slot)
        if self._sends_layout is not None:
            self._sends_layout.removeWidget(slot)
        slot.setParent(None)
        slot.deleteLater()
        self._on_committed()

    # ────────────────────────── 事件处理 ──────────────────────────

    def _on_add_send(self) -> None:
        """添加新 send（默认目标第一个 Bus，level 1.0）。"""
        if not self._bus_tracks:
            # AUD-37809: 无候选 Bus 时按钮已禁用，此处兜底（防程序化触发）
            QMessageBox.information(
                self,
                I18N.tr("audio.mixer.routing.no_bus_title", "无法添加发送"),
                I18N.tr(
                    "audio.mixer.routing.no_bus_msg",
                    "项目中没有 Bus 轨。请先创建一条 Bus 轨再添加发送。",
                ),
            )
            return
        from src.features.edit.core.timeline_v2 import Send
        default_target = self._bus_tracks[0][0]
        new_send = Send(target_bus_id=default_target, level=1.0, enabled=True)
        self._add_send_slot(new_send)
        self._on_committed()

    def _on_committed(self) -> None:
        """收集当前状态，校验，发信号。"""
        if self._loading:
            # AUD-37808: 初始装载期的程序化变更不提交（仅用户交互后才发射）。
            return
        payload = self._collect()
        # 实时环检测告警
        if not self._validate_and_warn(payload):
            # AUD-37810: 校验失败（路由环/未知目标）时阻止提交 —— 模块
            # docstring 承诺"创建会导致引擎崩溃的环前阻止提交"，此前
            # 返回值被丢弃、无效 payload 照样 emit。告警已显示，用户
            # 修正后再提交。
            return
        # 提交（离散操作：output change, add/remove send, enable toggle）
        self.routing_committed.emit(self._track_id, payload)

    def _on_preview(self) -> None:
        """Send level 拖动期实时预览（AUD-37811）。

        构造当前 payload（collect 容忍进行中的拖动值）发 routing_preview，
        不进命令栈；松手后由 _on_drag_ended → changed → _on_committed 提交。
        """
        if self._loading:
            return
        self.routing_preview.emit(self._track_id, self._collect())

    def _on_close(self) -> None:
        self.accept()

    # ────────────────────────── 收集与校验 ──────────────────────────

    def _collect(self) -> dict:
        """收集当前 UI 状态为 payload dict。"""
        output_bus_id = self._output_combo.currentData()
        sends = []
        for slot in self._send_slots:
            collected = slot.collect()
            if collected is not None:
                sends.append(collected)
        return {
            "output_bus_id": output_bus_id,
            "sends": sends,
        }

    def _validate_and_warn(self, payload: dict) -> bool:
        """实时环检测 + 未知目标检测，显示告警。

        Returns:
            True=有效，False=有问题（告警已显示）
        """
        from src.features.edit.core.audio_playback import validate_routing_graph

        # 构建 routing dict（仅 Bus 轨参与环检测）
        bus_ids = [bid for bid, _ in self._bus_tracks]
        routing = {bid: {"output_bus_id": None, "sends": [], "is_bus": True} for bid in bus_ids}
        # AUD-37810: 注入其他轨道的真实路由快照（调用方提供时）。此前所有
        # 其他 Bus 的 output 一律置 None，validate_routing_graph 的拓扑环检测
        # 对这些全 None 节点永远无环 —— Bus→Bus 环完全漏检。
        if self._routing_snapshot:
            for tid, r in self._routing_snapshot.items():
                if isinstance(r, dict):
                    routing[tid] = {
                        "output_bus_id": r.get("output_bus_id"),
                        "sends": list(r.get("sends", []) or []),
                        "is_bus": bool(r.get("is_bus", False)),
                    }
        # 当前 track 的拟定路由（最后写入，覆盖快照中该轨的旧状态）
        routing[self._track_id] = {
            "output_bus_id": payload.get("output_bus_id"),
            "sends": [(s.target_bus_id, s.level) for s in payload.get("sends", [])],
            "is_bus": self._track_id in set(bus_ids),
        }

        is_valid, graph_errors = validate_routing_graph(bus_ids, routing)
        errors: List[str] = list(graph_errors)
        # AUD-37809: 空 target 的 slot 无法构成有效 Send（collect 会跳过），
        # 按"不允许无声跳过"原则在告警区明示，避免用户误以为已生效。
        if any(not slot.is_removed and slot.is_target_empty for slot in self._send_slots):
            errors.append(I18N.tr(
                "audio.mixer.routing.empty_target_warn",
                "存在未选择目标的发送：请选择 Bus，否则该发送不会生效。",
            ))
        if errors:
            self._warning_label.setText("\n".join(errors))
            self._warning_label.show()
        else:
            self._warning_label.hide()
        return is_valid

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """关闭时发最终提交。

        AUD-37810: 校验失败（环/未知目标）时同样阻止提交，与
        _on_committed 的门控一致 —— 无效路由不得经关闭路径落盘。
        """
        payload = self._collect()
        if self._validate_and_warn(payload):
            self.routing_committed.emit(self._track_id, payload)
        super().closeEvent(event)
