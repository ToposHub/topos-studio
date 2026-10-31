"""PrefDragValue — DragValue 适配器，用于偏好设置面板的整数数值框。

与 DragValue 的差异（让它能 drop-in 替换 QSpinBox）：
- 支持 ``setSpecialValueText``：当值等于最小值时显示该文本
  （如 processing_threads=0 显示 "Auto"、auto_backup_interval=0 显示 "Off"）。
- 提供 ``intValue()`` 返回 int（DragValue.value() 返回 float）。
- ``setRange`` / ``setSuffix`` / ``setSingleStep`` 与 QSpinBox 同名同义。

放在独立模块（而不是 preferences_dialog.py 内）是为了避免模块偏好页面
（如 edit/preferences.py）导入时触发 app.ui 的重依赖链（FilesPanel/QtWebEngine）。

_apply_settings / _load_current_settings 里所有 ``.value()`` 读取处，
统一改用 ``.intValue()``（或 ``int(round(.value()))``）拿 int。
"""
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.export.export_panel import _VALUE_FIXED_WIDTH


class PrefDragValue(DragValue):
    """DragValue 适配器，用于偏好设置面板的整数数值框。"""

    def __init__(self, min_v: int = 0, max_v: int = 100, default: int = 0,
                 suffix: str = "", parent=None) -> None:
        # _special_text 必须在 super().__init__ 之前初始化，
        # 因为 DragValue.__init__ 末尾会调 _update_display → _format_value。
        self._special_text: str = ""
        super().__init__("", min_v=float(min_v), max_v=float(max_v),
                         default=float(default), decimals=0, suffix=suffix,
                         parent=parent)
        self.setFixedValueWidth(_VALUE_FIXED_WIDTH)

    # ---- QSpinBox 兼容 API ----

    def setSpecialValueText(self, text: str) -> None:
        """值等于最小值时显示的特殊文本（如 "Auto"/"Off"）。"""
        self._special_text = text or ""
        self._update_display()

    def specialValueText(self) -> str:
        return self._special_text

    def setRange(self, min_v: float, max_v: float) -> None:  # type: ignore[override]
        super().setRange(float(min_v), float(max_v))
        # range 改变后重新评估 special 显示
        self._update_display()

    def intValue(self) -> int:
        """返回 int 值（供 _apply_settings 读取，替代 QSpinBox.value()）。"""
        return int(round(self.value()))

    # ---- 格式化钩子：special 值时显示特殊文本 ----

    def _format_value(self, v: float) -> str:  # type: ignore[override]
        if self._special_text and abs(v - self._min) < 1e-9:
            return self._special_text
        return super()._format_value(v)
