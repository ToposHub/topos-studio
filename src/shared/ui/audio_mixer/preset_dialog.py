# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Channel Strip Preset Dialog (W13) + A/B Snapshot Bar (W14).

- PresetDialog: 管理用户级通道预设（保存/加载/删除），跨项目存储
- SnapshotBar: A/B 快照切换栏，存到项目内，用于混音方案对比

信号链：
  PresetDialog.apply_preset(str, ChannelStripPreset) → controller 提交 ApplyChannelStripPresetCommand
  SnapshotBar.snapshot_requested(str) → controller 从 timeline 取快照 → ApplyMixSnapshotCommand
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QLineEdit, QMessageBox, QWidget, QSizePolicy,
)

from src.features.edit.core.mixer_preset import (
    ChannelStripPreset, load_presets, add_preset, remove_preset,
)
from src.shared.i18n.i18n import I18N


logger = logging.getLogger(__name__)


class PresetDialog(QDialog):
    """通道预设管理对话框（W13）。

    信号：
        apply_preset(str, ChannelStripPreset): 用户选择应用某预设到目标 track_id
    """

    apply_preset = pyqtSignal(str, object)  # track_id, ChannelStripPreset

    def __init__(
        self,
        track_id: str,
        track_name: str,
        current_preset_source: Optional[ChannelStripPreset] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._track_name = track_name
        self._current_source = current_preset_source  # 用于"保存当前为预设"

        self.setWindowTitle(I18N.tr(
            "audio.mixer.preset.title",
            "Channel Strip Presets — {name}"
        ).format(name=track_name))
        self.setMinimumSize(420, 360)
        self.setModal(False)

        self._init_ui()
        self._refresh_list()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 顶部说明
        info = QLabel(I18N.tr(
            "audio.mixer.preset.info",
            "Presets are stored user-wide (~/.topos/mixer_presets.json) and shared across projects."
        ))
        info.setWordWrap(True)
        layout.addWidget(info)

        # 预设列表
        self._list = QListWidget(self)
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.itemDoubleClicked.connect(self._on_apply_clicked)
        layout.addWidget(self._list, 1)

        # 预设名称输入（保存时用）
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel(I18N.tr("audio.mixer.preset.name", "Name:")))
        self._name_edit = QLineEdit(self)
        self._name_edit.setPlaceholderText(I18N.tr(
            "audio.mixer.preset.name_placeholder", "Preset name"
        ))
        name_layout.addWidget(self._name_edit, 1)
        layout.addLayout(name_layout)

        # 按钮栏
        btn_layout = QHBoxLayout()

        self._save_btn = QPushButton(I18N.tr("audio.mixer.preset.save_current", "Save Current as Preset"))
        self._save_btn.clicked.connect(self._on_save_clicked)
        btn_layout.addWidget(self._save_btn)

        self._apply_btn = QPushButton(I18N.tr("audio.mixer.preset.apply", "Apply"))
        # AUD-37803: clicked 携带 checked(bool) 位置参数，直连带参槽会把 False
        # 当 QListWidgetItem 传入（AttributeError）。用零参 lambda 丢弃该 bool，
        # 双击路径（itemDoubleClicked 传真实 item）保留直连原方法。
        self._apply_btn.clicked.connect(lambda: self._on_apply_clicked())
        btn_layout.addWidget(self._apply_btn)

        self._delete_btn = QPushButton(I18N.tr("audio.mixer.preset.delete", "Delete"))
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        btn_layout.addWidget(self._delete_btn)

        btn_layout.addStretch()
        self._close_btn = QPushButton(I18N.tr("common.close", "Close"))
        self._close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self._close_btn)

        layout.addLayout(btn_layout)

    def _refresh_list(self) -> None:
        """刷新预设列表。"""
        self._list.clear()
        # AUD-37805: load_presets 是同步文件 IO，预设文件损坏/不可读时此前
        # 会直接抛异常炸掉对话框。捕获并降级为空列表 + 用户可见警告。
        try:
            presets = load_presets()
        except (OSError, ValueError) as exc:
            logger.warning("AUD-37805: 读取混音预设失败: %s", exc, exc_info=True)
            QMessageBox.warning(
                self,
                I18N.tr("common.error", "Error"),
                I18N.tr("audio.mixer.preset.load_failed",
                        "Failed to load presets: {detail}").format(detail=exc),
            )
            return
        for p in sorted(presets, key=lambda x: x.name.lower()):
            item = QListWidgetItem(f"{p.name}")
            item.setData(Qt.ItemDataRole.UserRole, p)
            self._list.addItem(item)

    def _on_save_clicked(self) -> None:
        """保存当前 strip 状态为新预设。"""
        if self._current_source is None:
            QMessageBox.warning(self, I18N.tr("common.warning", "Warning"),
                                I18N.tr("audio.mixer.preset.no_source",
                                        "No current strip state to save."))
            return
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, I18N.tr("common.warning", "Warning"),
                                I18N.tr("audio.mixer.preset.name_required",
                                        "Please enter a preset name."))
            return
        preset = ChannelStripPreset(
            name=name,
            volume=self._current_source.volume,
            pan=self._current_source.pan,
            is_muted=self._current_source.is_muted,
            is_solo=self._current_source.is_solo,
            strip_eq=self._current_source.strip_eq,
            # AUD-37804: 补齐 AM-13 扩展字段 —— 此前保存的预设丢弃整条 FX 链
            # 与路由，应用到其他轨道会把这些静默重置为默认（无效果、直送
            # Master、无 sends）。
            effect_chain=self._current_source.effect_chain,
            output_bus_id=self._current_source.output_bus_id,
            sends=self._current_source.sends,
            is_bus=self._current_source.is_bus,
            # created_at 用保存时刻的时间戳（与 mixer_preset.add_preset 的
            # 缺省行为一致），而非复用源 strip 的创建时间
            created_at=datetime.now().isoformat(),
        )
        # AUD-37805: add_preset 落盘可能因权限/磁盘问题失败，此前异常直接
        # 冒泡且用户无感知（保存"静默"丢失）。捕获 IO 错误并明确告知失败原因。
        try:
            add_preset(preset)
        except OSError as exc:
            logger.warning("AUD-37805: 保存混音预设失败: %s", exc, exc_info=True)
            QMessageBox.critical(
                self,
                I18N.tr("common.error", "Error"),
                I18N.tr("audio.mixer.preset.save_failed",
                        "Failed to save preset: {detail}").format(detail=exc),
            )
            return
        self._name_edit.clear()
        self._refresh_list()

    def _on_apply_clicked(self, item: Optional[QListWidgetItem] = None) -> None:
        """应用选中的预设到目标轨道。"""
        if item is None:
            item = self._list.currentItem()
        if item is None:
            return
        preset = item.data(Qt.ItemDataRole.UserRole)
        if preset is None:
            return
        self.apply_preset.emit(self._track_id, preset)

    def _on_delete_clicked(self) -> None:
        """删除选中的预设。"""
        item = self._list.currentItem()
        if item is None:
            return
        preset = item.data(Qt.ItemDataRole.UserRole)
        if preset is None:
            return
        reply = QMessageBox.question(
            self,
            I18N.tr("audio.mixer.preset.confirm_delete", "Delete Preset"),
            I18N.tr("audio.mixer.preset.confirm_delete_msg",
                    "Delete preset '{name}'?").format(name=preset.name),
        )
        if reply == QMessageBox.StandardButton.Yes:
            remove_preset(preset.preset_id)
            self._refresh_list()


class SnapshotBar(QWidget):
    """A/B 混音快照切换栏（W14）。

    信号：
        capture_snapshot(str): 捕获当前混音到 A/B 槽（controller 存到 timeline）
        apply_snapshot(str): 应用 A 或 B 快照（controller 提交 ApplyMixSnapshotCommand）
    """

    capture_snapshot = pyqtSignal(str)  # "A" | "B"
    apply_snapshot = pyqtSignal(str)    # "A" | "B"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        layout.addWidget(QLabel(I18N.tr("audio.mixer.snapshot.label", "Mix A/B:")))

        # A 槽
        self._save_a_btn = QPushButton(I18N.tr("audio.mixer.snapshot.save_a", "→ A"))
        self._save_a_btn.setToolTip(I18N.tr("audio.mixer.snapshot.save_a_tip", "Save current mix to slot A"))
        self._save_a_btn.clicked.connect(lambda: self.capture_snapshot.emit("A"))
        layout.addWidget(self._save_a_btn)

        self._apply_a_btn = QPushButton(I18N.tr("audio.mixer.snapshot.slot_a", "A"))
        self._apply_a_btn.setToolTip(I18N.tr("audio.mixer.snapshot.apply_a", "Recall mix A"))
        self._apply_a_btn.setFixedWidth(32)
        self._apply_a_btn.clicked.connect(lambda: self.apply_snapshot.emit("A"))
        layout.addWidget(self._apply_a_btn)

        # B 槽
        self._save_b_btn = QPushButton(I18N.tr("audio.mixer.snapshot.save_b", "→ B"))
        self._save_b_btn.setToolTip(I18N.tr("audio.mixer.snapshot.save_b_tip", "Save current mix to slot B"))
        self._save_b_btn.clicked.connect(lambda: self.capture_snapshot.emit("B"))
        layout.addWidget(self._save_b_btn)

        self._apply_b_btn = QPushButton(I18N.tr("audio.mixer.snapshot.slot_b", "B"))
        self._apply_b_btn.setToolTip(I18N.tr("audio.mixer.snapshot.apply_b", "Recall mix B"))
        self._apply_b_btn.setFixedWidth(32)
        self._apply_b_btn.clicked.connect(lambda: self.apply_snapshot.emit("B"))
        layout.addWidget(self._apply_b_btn)

        layout.addStretch()
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
