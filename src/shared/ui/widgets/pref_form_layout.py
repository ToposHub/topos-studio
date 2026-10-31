"""统一偏好设置面板的 QFormLayout 标签列对齐。

问题：Preferences 面板每个页面各自创建 QFormLayout，而 QFormLayout 会根据
每页 label 文本长度独立计算标签列宽度。由于各页 label 长度差异很大（如
"Language:" vs "Auto-save interval (minutes):")，每个页面的字段左边缘不在
同一条垂直线上，切换页面时尤其明显。

解决：所有偏好页面的 QFormLayout 走 ``make_pref_form``，统一设置：
- 固定标签列宽度（``PREF_LABEL_WIDTH``），所有字段的左边缘对齐到同一条垂直线；
- 标签左对齐（``AlignLeft|AlignVCenter``），所有标签从同一垂直线开始；
- 字段增长策略 ``ExpandingFieldsGrow``，让字段占据剩余空间；
- 统一 margins / spacing，与节点参数面板观感一致。

空白标签行（checkbox / note 行用 ``addRow("", widget)``）原本会贴到最左边，
通过给空白标签列设置与有标签行相同的固定宽度，让这些行的字段也对齐到同一列。
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFormLayout, QLabel

# 所有偏好页面统一的标签列宽度（像素）。
# 取值依据：能容纳最长 label "Auto-backup interval (min, 0=off):" (36 chars)
# 而不换行，同时不会过宽挤压字段空间。所有 make_pref_label 使用 setFixedWidth
# 强制统一宽度，确保跨页面 QFormLayout 标签列完全对齐。
PREF_LABEL_WIDTH = 270

# 统一的 form 内边距与行间距。
PREF_FORM_MARGINS = (16, 20, 16, 16)
PREF_FORM_SPACING = 14


def make_pref_form(
    parent=None,
    *,
    margins: tuple[int, int, int, int] = PREF_FORM_MARGINS,
    spacing: int = PREF_FORM_SPACING,
) -> QFormLayout:
    """创建一个统一对齐规则的偏好设置 QFormLayout。

    所有偏好页面应使用此工厂方法创建 form，以保证标签列宽度与字段对齐
    在跨页面、跨模块间完全一致。

    Args:
        parent: 父控件（通常是 QGroupBox）。

    Returns:
        配置好统一对齐规则的 QFormLayout。
    """
    form = QFormLayout(parent)
    # 固定标签列宽度：所有字段左边缘对齐到同一条垂直线。
    # FieldGrowthPolicy.ExpandingFieldsGrow 让字段占据标签列之后的剩余空间。
    form.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
    )
    # 标签左对齐 + 垂直居中，所有标签从同一垂直线开始，文字长度差异不影响对齐。
    form.setLabelAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    # 统一 margins / spacing。
    form.setContentsMargins(*margins)
    form.setSpacing(spacing)
    return form


def make_pref_label(
    text: str, tooltip: str = "", width: int = PREF_LABEL_WIDTH
) -> QLabel:
    """创建统一的偏好设置表单标签。

    与 General/Appearance 等页面已有的 PrefFormLabel 保持一致：
    浅灰（TEXT_SECONDARY）小字（12px），消除各页面 label 颜色/字体差异。

    给标签设 **固定宽度** ``PREF_LABEL_WIDTH`` 是实现跨页面对齐的关键：
    QFormLayout 的标签列宽 = 该 form 内最宽标签的自然宽度。所有标签
    固定为同一宽度时，每个 form 的标签列宽完全一致，字段左边缘对齐到
    同一条垂直线。

    Args:
        text: 标签文本。
        tooltip: 可选的工具提示。

    Returns:
        带 PrefFormLabel objectName 的 QLabel。
    """
    label = QLabel(text)
    label.setObjectName("PrefFormLabel")
    # 固定标签宽度：所有 form 的标签列宽完全一致 → 字段左边缘对齐。
    label.setFixedWidth(width)
    if tooltip:
        label.setToolTip(tooltip)
    return label
