"""F2.1 · Source Patch 设置对话框（V/A 流开关 + 常用源声道选择）。

计划 F2.1 范围明确"先支持选定单声道/立体声常用配置"：本对话框提供
直通 / 立体声 L+R（多声道源）/ 单声道任选源声道 / 立体声对调，以及
"自定义"（保留既有非预设映射只改流开关）。完整逐声道→逐目标轨矩阵
（DaVinci patch matrix 全量形态）登记为后续片。

提交走 ``MediaPoolPanel.apply_source_patch``（UpdateSourcePatchCommand，
项目域单步 Undo），映射随用户选择持久化在工程，不是 UI 临时变量。
"""

from __future__ import annotations

from typing import Optional, Tuple

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.shared.i18n.i18n import I18N

from src.features.edit.core.channel_layout import ChannelLayout
from src.features.edit.core.source_patch import SourcePatchSpec

#: 预设模式键（直通/立体声对/单声道/对调/自定义保留）
_MODE_PASSTHROUGH = "passthrough"
_MODE_STEREO_PAIR = "stereo_pair"
_MODE_MONO = "mono"
_MODE_SWAP = "swap"
_MODE_CUSTOM = "custom"

_SWAPPED_MAP: Tuple[Tuple[int, int], ...] = ((1, 0), (0, 1))


def _channel_labels(n_channels: int) -> list[str]:
    """标准布局用 ITU/ATSC 标签，未知声道数退 '声道 i'。"""
    try:
        layout = ChannelLayout.from_channel_count(n_channels)
    except ValueError:
        return [I18N.tr("edit.source_patch.channel_n", "声道 {n}").format(
            n=i) for i in range(n_channels)]
    return list(layout.channel_labels)


class SourcePatchDialog(QDialog):
    """单素材 Source Patch 编辑。

    Args:
        n_channels: 源素材声道数（未知/无音频传 0，仅显示流开关）。
        current: 当前生效 spec（用于回显）。
    """

    def __init__(
        self,
        n_channels: int,
        current: SourcePatchSpec,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._n_channels = int(n_channels or 0)
        self._current = current
        self._custom_map = current.audio_channel_map

        self.setWindowTitle(I18N.tr(
            "edit.source_patch.title", "Source Patch 设置"))
        self.setModal(True)
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self._video_check = QCheckBox(I18N.tr(
            "edit.source_patch.video_stream", "插入视频流 (V)"))
        self._video_check.setChecked(current.video_enabled)
        self._video_check.stateChanged.connect(self._update_preview)
        form.addRow(self._video_check)

        self._audio_check = QCheckBox(I18N.tr(
            "edit.source_patch.audio_stream", "插入音频流 (A)"))
        self._audio_check.setChecked(current.audio_enabled)
        self._audio_check.stateChanged.connect(self._update_preview)
        form.addRow(self._audio_check)

        self._mode_combo = QComboBox()
        self._labels = _channel_labels(self._n_channels) if self._n_channels else []
        mode, mono_src = self._detect_mode(current.audio_channel_map)
        if self._n_channels:
            self._mode_combo.addItem(
                I18N.tr("edit.source_patch.mode_passthrough", "直通（按源布局）"),
                _MODE_PASSTHROUGH)
            if self._n_channels > 2:
                self._mode_combo.addItem(
                    I18N.tr("edit.source_patch.mode_stereo_pair",
                            "立体声 L+R（只送前两声道）"),
                    _MODE_STEREO_PAIR)
            self._mode_combo.addItem(
                I18N.tr("edit.source_patch.mode_mono", "单声道（选源声道）"),
                _MODE_MONO)
            if self._n_channels >= 2:
                self._mode_combo.addItem(
                    I18N.tr("edit.source_patch.mode_swap", "左右对调"),
                    _MODE_SWAP)
            if mode == _MODE_CUSTOM:
                self._mode_combo.addItem(
                    I18N.tr("edit.source_patch.mode_custom", "自定义（保留当前映射）"),
                    _MODE_CUSTOM)
            idx = self._mode_combo.findData(mode)
            self._mode_combo.setCurrentIndex(max(0, idx))
        else:
            self._mode_combo.setEnabled(False)
        self._mode_combo.currentIndexChanged.connect(self._update_preview)
        form.addRow(I18N.tr("edit.source_patch.audio_map", "音频声道映射:"), self._mode_combo)

        self._mono_combo = QComboBox()
        for i, label in enumerate(self._labels):
            self._mono_combo.addItem(f"{label} ({i})", i)
        if 0 <= mono_src < self._mono_combo.count():
            self._mono_combo.setCurrentIndex(mono_src)
        self._mono_combo.currentIndexChanged.connect(self._update_preview)
        form.addRow(I18N.tr("edit.source_patch.source_channel", "源声道:"), self._mono_combo)

        root.addLayout(form)

        # 混合值提示：明确告知目标轨将收到的声道构成
        self._preview_label = QLabel()
        self._preview_label.setWordWrap(True)
        root.addWidget(self._preview_label)

        # M13.1：完整布线矩阵视图（source_patch_matrix.channel_matrix 的
        # 对话框入口）——逐目标通道 × 逐源声道；未映射目标行显式标
        # "（静音）"——不静默丢通道。随模式选择实时刷新。
        self._matrix_table = QTableWidget()
        self._matrix_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._matrix_table.setMaximumHeight(160)
        self._matrix_visible = bool(self._n_channels)
        if self._matrix_visible:
            root.addWidget(self._matrix_table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            I18N.tr("common.ok", "确定"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            I18N.tr("common.cancel", "取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._update_preview()

    # ── 内部 ──────────────────────────────────────────────────────────

    @staticmethod
    def _detect_mode(channel_map) -> Tuple[str, int]:
        """从现有映射识别预设模式；返回 (mode, mono_source_index)。"""
        if not channel_map:
            return _MODE_PASSTHROUGH, 0
        ordered = tuple(sorted(channel_map, key=lambda pair: pair[1]))
        if ordered == ((0, 0), (1, 1)):
            return _MODE_STEREO_PAIR, 0
        if ordered == _SWAPPED_MAP:
            return _MODE_SWAP, 0
        if len(ordered) == 1 and ordered[0][1] == 0:
            return _MODE_MONO, ordered[0][0]
        return _MODE_CUSTOM, 0

    def _mode(self) -> str:
        if not self._n_channels:
            return _MODE_PASSTHROUGH
        return self._mode_combo.currentData() or _MODE_PASSTHROUGH

    def _channel_map(self):
        mode = self._mode()
        if mode == _MODE_PASSTHROUGH:
            return None
        if mode == _MODE_STEREO_PAIR:
            return ((0, 0), (1, 1))
        if mode == _MODE_MONO:
            return ((int(self._mono_combo.currentData() or 0), 0),)
        if mode == _MODE_SWAP:
            return _SWAPPED_MAP
        return self._custom_map

    def _update_preview(self) -> None:
        self._mono_combo.setEnabled(self._mode() == _MODE_MONO)
        spec = self.spec()
        if not self._n_channels:
            text = I18N.tr(
                "edit.source_patch.preview_no_audio", "该素材无音频声道")
        elif spec.audio_channel_map is None:
            text = I18N.tr(
                "edit.source_patch.preview_passthrough",
                "目标轨将收到：{n} 声道（源布局直通）",
            ).format(n=self._n_channels)
        else:
            sources = spec.resolve_source_channels(self._n_channels)
            names = [self._labels[s] if s < len(self._labels) else str(s)
                     for s in sources]
            text = I18N.tr(
                "edit.source_patch.preview_mapped",
                "目标轨将收到：{n} 声道（源 {names}）",
            ).format(n=len(names), names=" + ".join(names))
        if not spec.video_enabled and not spec.audio_enabled:
            text += "  ⚠ " + I18N.tr(
                "edit.source_patch.both_off_warning",
                "两个流都已关闭：插入/覆盖将被拒绝")
        self._preview_label.setText(text)
        self._refresh_matrix(spec)

    def _refresh_matrix(self, spec) -> None:
        """M13.1：刷新完整布线矩阵视图（模型层 channel_matrix 消费口）。

        行 = 目标通道（未映射行标"（静音）"），列 = 源声道，接入处
        打 ●；模式/映射变化实时刷新。
        """
        table = getattr(self, "_matrix_table", None)
        if table is None or not getattr(self, "_matrix_visible", False):
            return
        from src.features.edit.core.source_patch_matrix import channel_matrix
        matrix = channel_matrix(spec, self._n_channels)
        n_target = len(matrix)
        table.setRowCount(n_target)
        table.setColumnCount(self._n_channels)
        table.setHorizontalHeaderLabels(
            [f"{lbl} ({i})"
             for i, lbl in enumerate(_channel_labels(self._n_channels))])
        target_labels = _channel_labels(max(n_target, 1))[:n_target]
        for t in range(n_target):
            row_wired = any(matrix[t])
            header = (target_labels[t] if t < len(target_labels)
                      else f"T{t}")
            if not row_wired:
                header += "（静音）"
            table.setVerticalHeaderItem(
                t, QTableWidgetItem(header))
            for s in range(self._n_channels):
                item = QTableWidgetItem(
                    "●" if matrix[t][s] else "")
                table.setItem(t, s, item)
        table.resizeColumnsToContents()
        table.resizeRowsToContents()

    # ── 对外 ──────────────────────────────────────────────────────────

    def spec(self) -> SourcePatchSpec:
        return SourcePatchSpec(
            video_enabled=self._video_check.isChecked(),
            audio_enabled=self._audio_check.isChecked(),
            audio_channel_map=self._channel_map(),
        )

    @staticmethod
    def run(
        n_channels: int,
        current: SourcePatchSpec,
        parent=None,
    ) -> Optional[SourcePatchSpec]:
        """模态运行；返回新 spec，取消返回 None。"""
        dialog = SourcePatchDialog(n_channels, current, parent)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.spec()
        return None
