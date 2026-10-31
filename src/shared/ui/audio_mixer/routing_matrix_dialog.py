# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Routing Matrix Dialog — 全局路由矩阵 + Spill（§4.2 Routing UI 扩展）。

RoutingDialog（routing_dialog.py）是 per-track 的输出/send 编辑。
本模块提供全局视图：

1. **Routing Matrix**：行=源轨，列=Bus/Master，单元格标记路由关系（output/send）。
   一目了然所有轨→Bus→Master 的拓扑，支持点击单元格快速重路由。
2. **Spill**：点击 Bus 行时展开显示其上游轨道（哪些轨路由到该 Bus）。
3. **警告状态**：环/未知目标即时红色高亮（复用 validate_routing_graph）。

设计原则（计划表 §4.2）：
- UI 自动化创建/删除/重路由，模型、图与 meter 同步。
- cycle 输入即时显示错误。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n import I18N


# 单元格类型（用于颜色区分）
_CELL_EMPTY = 0
_CELL_OUTPUT = 1   # 该轨 output 到此 Bus/Master
_CELL_SEND = 2     # 该轨 send 到此 Bus


def _cell_color(cell_type: int) -> QColor:
    """单元格颜色（output=蓝, send=绿, 空=白）。"""
    if cell_type == _CELL_OUTPUT:
        return QColor(70, 130, 180, 120)   # 钢蓝半透明
    if cell_type == _CELL_SEND:
        return QColor(80, 180, 80, 120)    # 绿半透明
    return QColor(255, 255, 255)           # 白


class RoutingMatrixDialog(QDialog):
    """全局路由矩阵 + Spill（§4.2）。

    信号：
        routing_changed(dict) — 矩阵编辑后发出完整路由快照
            {track_id: {"output_bus_id": Optional[str], "sends": [(bus_id, level)]}}
        spill_requested(str) — 请求展开某 Bus 的上游（由 panel 处理 UI 展开）
    """

    routing_changed = pyqtSignal(object)   # routing snapshot dict
    spill_requested = pyqtSignal(str)      # bus_id

    def __init__(
        self,
        tracks: List[Tuple[str, str, bool]],  # [(track_id, name, is_bus)]
        routing: Dict[str, Dict[str, Any]],
        # routing: {track_id: {"output_bus_id": Optional[str], "sends": [(bus_id, level)], "is_bus": bool}}
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._routing = dict(routing)
        self._spilled_bus: Optional[str] = None

        self.setWindowTitle(
            I18N.tr("audio.mixer.routing_matrix.title", "Routing Matrix")
        )
        self.setModal(False)
        self.setMinimumSize(700, 500)

        # 分离普通轨和 Bus
        self._bus_tracks = [(tid, name) for tid, name, is_bus in tracks if is_bus]
        self._source_tracks = [(tid, name) for tid, name, is_bus in tracks if not is_bus]

        self._table: Optional[QTableWidget] = None
        self._warning_label: Optional[QLabel] = None
        self._spill_label: Optional[QLabel] = None

        self._init_ui()
        self._refresh_matrix()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 标题 + 图例
        header = QHBoxLayout()
        title = QLabel(I18N.tr("audio.mixer.routing_matrix.title", "Routing Matrix"))
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        header.addWidget(title)
        header.addStretch()
        legend_output = QLabel(I18N.tr("audio.mixer.legend_output", "■ Output"))
        legend_output.setStyleSheet("color: #4682B4;")
        legend_send = QLabel(I18N.tr("audio.mixer.legend_send", "■ Send"))
        legend_send.setStyleSheet("color: #50B450;")
        header.addWidget(legend_output)
        header.addWidget(legend_send)
        layout.addLayout(header)

        # 矩阵表格
        # 列：Master + 各 Bus；行：各源轨
        columns = ["Master"] + [name for _, name in self._bus_tracks]
        self._table = QTableWidget(len(self._source_tracks), len(columns))
        self._table.setHorizontalHeaderLabels(columns)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.cellClicked.connect(self._on_cell_clicked)
        # AUD-37814: Spill 交互接线 —— spill 提示文案承诺"点击 Bus 列头查看
        # 上游轨道（Spill）"，但此前从未连接 header 点击，spill()/spill_requested
        # 无法由用户触发（死代码）。
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        layout.addWidget(self._table)

        # Spill 区（展开某 Bus 的上游）
        self._spill_label = QLabel(
            I18N.tr("audio.mixer.routing.spill_hint", "点击 Bus 列头查看上游轨道（Spill）")
        )
        self._spill_label.setWordWrap(True)
        self._spill_label.setStyleSheet("color: #888; padding: 4px;")
        layout.addWidget(self._spill_label)

        # 告警区
        self._warning_label = QLabel("")
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet(
            "color: #ff6b6b; background: rgba(255,107,107,0.1); "
            "padding: 6px; border-radius: 4px;"
        )
        self._warning_label.hide()
        layout.addWidget(self._warning_label)

        # 关闭按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton(I18N.tr("common.close", "关闭"))
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _refresh_matrix(self) -> None:
        """刷新矩阵单元格颜色 + 告警。"""
        columns = ["Master"] + [tid for tid, _ in self._bus_tracks]
        # 反查 bus_id → column index

        for row, (track_id, _) in enumerate(self._source_tracks):
            r = self._routing.get(track_id, {})
            output_bus_id = r.get("output_bus_id") if isinstance(r, dict) else None
            sends = r.get("sends", []) if isinstance(r, dict) else []

            for col in range(len(columns)):
                cell_type = _CELL_EMPTY
                if col == 0:
                    # Master 列：output_bus_id 为 None（直送 Master）
                    if output_bus_id is None:
                        cell_type = _CELL_OUTPUT
                else:
                    bus_id = columns[col]
                    if output_bus_id == bus_id:
                        cell_type = _CELL_OUTPUT
                    else:
                        # 检查 send
                        for s_target, _level in sends:
                            if s_target == bus_id:
                                cell_type = _CELL_SEND
                                break
                item = QTableWidgetItem("")
                item.setBackground(_cell_color(cell_type))
                # Master 列显示 "M"，Bus 列显示 Bus 简称
                if cell_type == _CELL_OUTPUT:
                    item.setText(I18N.tr("audio.mixer.cell_output", "OUT"))
                elif cell_type == _CELL_SEND:
                    item.setText(I18N.tr("audio.mixer.cell_send", "SND"))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(row, col, item)

        # 校验 + 告警
        self._validate_and_warn()

    def _validate_and_warn(self) -> bool:
        """环检测 + 未知目标检测。"""
        from src.features.edit.core.audio_playback import validate_routing_graph
        bus_ids = [tid for tid, _ in self._bus_tracks]
        is_valid, errors = validate_routing_graph(bus_ids, self._routing)
        if errors:
            self._warning_label.setText("\n".join(errors))
            self._warning_label.show()
        else:
            self._warning_label.hide()
        return is_valid

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """点击单元格：切换 output 路由（Master 列=直送，Bus 列=路由到该 Bus）。

        点击 = 切换该轨的 output 到此目标（简单交互）。
        Send 的精细编辑仍由 per-track RoutingDialog 处理。
        """
        if row < 0 or row >= len(self._source_tracks):
            # 列头点击（row < 0 在 QTableWidget 不触发，用 header）
            return
        track_id, _ = self._source_tracks[row]
        columns = ["Master"] + [tid for tid, _ in self._bus_tracks]
        if col == 0:
            new_target = None  # Master
        else:
            new_target = columns[col]

        # 切换：若已是该目标则清除（回 Master），否则设置
        r = self._routing.get(track_id, {})
        current_output = r.get("output_bus_id") if isinstance(r, dict) else None
        if current_output == new_target and col != 0:
            new_target = None  # 切换回 Master

        if not isinstance(r, dict):
            r = {"output_bus_id": None, "sends": [], "is_bus": False}
        r["output_bus_id"] = new_target
        self._routing[track_id] = r

        self._refresh_matrix()
        self.routing_changed.emit(self._routing)

    def _on_header_clicked(self, logical_index: int) -> None:
        """AUD-37814: 点击 Bus 列头 → Spill 展开该 Bus 的上游轨道。

        列 0 是 Master（无 spill 意义），仅 Bus 列触发；Master 列头点击
        显示提示而非静默无响应。
        """
        if logical_index <= 0:
            self._spill_label.setText(
                I18N.tr(
                    "audio.mixer.routing.spill_master_hint",
                    "Master 直达输出，无上游 Bus。点击 Bus 列头查看其上游轨道（Spill）。",
                )
            )
            self._spill_label.setStyleSheet("color: #888; padding: 4px;")
            return
        columns = ["Master"] + [tid for tid, _ in self._bus_tracks]
        if logical_index >= len(columns):
            return
        self.spill(columns[logical_index])

    def spill(self, bus_id: str) -> List[str]:
        """Spill：返回路由到指定 Bus 的所有上游源轨 track_id。

        点击 Bus 列头时调用，展开显示上游轨道。
        """
        upstream: List[str] = []
        for track_id, _ in self._source_tracks:
            r = self._routing.get(track_id, {})
            if isinstance(r, dict):
                if r.get("output_bus_id") == bus_id:
                    upstream.append(track_id)
                else:
                    for s_target, _ in r.get("sends", []):
                        if s_target == bus_id:
                            upstream.append(track_id)
                            break
        self._spilled_bus = bus_id
        # 更新 spill 标签显示上游轨道名
        bus_name = next((name for tid, name in self._bus_tracks if tid == bus_id), bus_id)
        upstream_names = [
            name for tid, name in self._source_tracks if tid in upstream
        ]
        if upstream_names:
            self._spill_label.setText(
                I18N.tr(
                    "audio.mixer.routing.spill_result",
                    "{bus} 上游轨道：{tracks}",
                ).format(bus=bus_name, tracks=", ".join(upstream_names))
            )
            self._spill_label.setStyleSheet("color: #333; padding: 4px; background: #f0f0f0;")
        else:
            self._spill_label.setText(
                I18N.tr(
                    "audio.mixer.routing.spill_empty",
                    "{bus} 无上游轨道",
                ).format(bus=bus_name)
            )
        self.spill_requested.emit(bus_id)
        return upstream

    def get_routing(self) -> Dict[str, Dict[str, Any]]:
        """返回当前路由快照。"""
        return dict(self._routing)
