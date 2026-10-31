"""Auto Color / Color Match 建议选择对话框（P1-COLOR-21）。

展示自动校色（或参考帧匹配）的参数建议、应用前的差异指标与预测的
应用后指标；用户勾选要应用的参数组。对话框是纯查询 UI，不修改
工程状态——应用由控制器以单一 undo 完成。

用法：
    keys = AutoColorDialog.get_application(
        parent, suggestion, before_metrics, after_metrics, title)
    if keys is not None:      # None = 取消
        controller.apply(suggestion, keys)
"""
from __future__ import annotations

from typing import Dict, Optional, Set

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N

#: 参数键 → (标签 i18n 键, 默认标签)
_ITEMS = (
    ("wb", "autocolor.wb", "白平衡 (Temp/Tint)"),
    ("exposure", "autocolor.exposure", "曝光 (Exposure)"),
    ("contrast", "autocolor.contrast", "对比度 (Contrast)"),
    ("saturation", "autocolor.saturation", "饱和度 (Saturation)"),
)


def _fmt(value: float) -> str:
    return f"{value:+.1f}"


class AutoColorDialog(QDialog):
    """参数建议复选对话框，返回选中的参数组键。"""

    def __init__(self, parent: Optional[QWidget],
                 suggestion, before_metrics: Dict[str, float],
                 after_metrics: Dict[str, float],
                 title: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(title or "Auto Color")
        self.setMinimumWidth(360)
        self._checkboxes = {}

        layout = QVBoxLayout(self)

        # 建议值摘要
        values = suggestion.items()
        summary = (
            f"Temp {_fmt(values['temp'])}   Tint {_fmt(values['tint'])}   "
            f"Exposure {_fmt(values['exposure'])}\n"
            f"Contrast {_fmt(values['contrast'])}   "
            f"Saturation {_fmt(values['saturation'])}"
            + ("" if suggestion.absolute else "\n"
               + I18N.tr("autocolor.delta_hint", "（增量：叠加在当前调色之上）"))
        )
        layout.addWidget(QLabel(summary))

        # 参数组选择
        group = QGroupBox(I18N.tr("autocolor.apply_items", "选择要应用的参数"))
        group_layout = QVBoxLayout(group)
        suggested = {
            "wb": values["temp"] != 0.0 or values["tint"] != 0.0,
            "exposure": values["exposure"] != 0.0,
            "contrast": values["contrast"] != 0.0,
            "saturation": values["saturation"] != 0.0,
        }
        for key, i18n_key, default in _ITEMS:
            cb = QCheckBox(I18N.tr(i18n_key, default))
            # 有建议值的默认勾选；饱和度默认不勾（无内容先验）
            cb.setChecked(suggested[key] and key != "saturation")
            group_layout.addWidget(cb)
            self._checkboxes[key] = cb
        layout.addWidget(group)

        # 差异指标：应用前 → 预测应用后
        if before_metrics or after_metrics:
            metric_lines = []
            for name, label in (
                ("channel_delta", "autocolor.metric_channel"),
                ("luma_delta", "autocolor.metric_luma"),
                ("chroma_delta", "autocolor.metric_chroma"),
            ):
                if name in before_metrics:
                    before = before_metrics.get(name, 0.0)
                    after = after_metrics.get(name, before)
                    metric_lines.append(
                        f"{I18N.tr(label, name)}: {before:.3f} → {after:.3f}")
            if metric_lines:
                metrics_group = QGroupBox(
                    I18N.tr("autocolor.metrics", "差异指标（前 → 预测后）"))
                metrics_layout = QVBoxLayout(metrics_group)
                metrics_layout.addWidget(QLabel("\n".join(metric_lines)))
                layout.addWidget(metrics_group)

        select_all = QPushButton(I18N.tr("autocolor.toggle_all", "全选 / 全不选"))
        select_all.clicked.connect(self._toggle_all)
        layout.addWidget(select_all)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_all(self) -> None:
        target = not all(cb.isChecked() for cb in self._checkboxes.values())
        for cb in self._checkboxes.values():
            cb.setChecked(target)

    def selected_keys(self) -> Set[str]:
        return {key for key, cb in self._checkboxes.items() if cb.isChecked()}

    @classmethod
    def get_application(cls, parent: Optional[QWidget], suggestion,
                        before_metrics: Dict[str, float],
                        after_metrics: Dict[str, float],
                        title: str = "") -> Optional[Set[str]]:
        """模态询问；返回选中的参数组键集合，取消返回 None。"""
        dialog = cls(parent, suggestion, before_metrics, after_metrics, title)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_keys()
