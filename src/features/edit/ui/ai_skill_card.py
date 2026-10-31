"""AI Skill 推荐卡 + 输入表单（plan §10.3 P1.2）。

把 :class:`SkillCatalogView` 的 :class:`SkillCardView` 渲染成聊天里的**推荐卡 +
可编辑输入表单**，并在运行前显示风险 / 模型 / 证据前置条件（plan §10.3 P1.2 验收：
"用户能在聊天中查看 Skill 所需模型、预计工作和输出，确认后才开始"）。

组件：

- :class:`AiSkillCard` —— 单张推荐卡：标题 / 描述 / 风险 / 来源 / 产出 / 预计工作 /
  触发示例 / 前置条件清单 / 可编辑表单 / "开始"按钮（前置条件未满足时禁用并提示）。
- :class:`AiSkillGallery` —— 推荐卡列表（按 recommend 排序），支持过滤。

约束（与既有面板一致）：

- 只发 signal（``start_requested`` 携带 skill_id + 表单值），不执行模型 / 不写
  timeline。
- 表单回填用 ``_updating`` 锁防递归。
- 未满足前置条件时"开始"按钮禁用，hover / tooltip 显示缺什么。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.features.ai_harness.skill_catalog import (
    FormField,
    PrereqReport,
    SkillCardView,
)
from src.shared.i18n.i18n import tr


def _trf(key: str, default: str, **kwargs) -> str:
    """``tr`` 兜底：未命中翻译时补做 ``.format``。"""
    text = tr(key, default, **kwargs)
    if "{" in default and kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


_RISK_COLORS = {
    "只读": "#27ae60",
    "仅提议": "#2980b9",
    "需审批": "#f39c12",
}


class AiSkillCard(QWidget):
    """单张 Skill 推荐卡（plan §10.3 P1.2）。

    Signals：

    - ``start_requested(str, dict)``：(skill_id, 表单值)；前置条件未满足时不发。
    """

    start_requested = pyqtSignal(str, dict)

    def __init__(self, card_view: SkillCardView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._view = card_view
        self._field_widgets: dict[str, QWidget] = {}
        self._build_ui()

    # ---------------- 构建 ---------------- #

    def _build_ui(self) -> None:
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(frame)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        # 标题行：标题 + 风险标签
        title_row = QHBoxLayout()
        title = QLabel(f"<b>{self._view.title}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title, stretch=1)
        if self._view.risk_label:
            risk_color = _RISK_COLORS.get(self._view.risk_label, "gray")
            risk = QLabel(
                f"<span style='color:{risk_color}'>● {self._view.risk_label}</span>"
            )
            risk.setTextFormat(Qt.TextFormat.RichText)
            title_row.addWidget(risk)
        if self._view.unverified:
            unv = QLabel(
                f"<span style='color:#c0392b'>⚠ {_trf('ai_skill.unverified', '未验证')}</span>"
            )
            unv.setTextFormat(Qt.TextFormat.RichText)
            title_row.addWidget(unv)
        v.addLayout(title_row)

        # 描述
        if self._view.description:
            desc = QLabel(self._view.description)
            desc.setWordWrap(True)
            desc.setStyleSheet("color: gray;")
            v.addWidget(desc)

        # 产出 / 预计工作
        meta_parts = []
        if self._view.produces:
            meta_parts.append(
                _trf("ai_skill.produces", "产出：{what}", what=self._view.produces)
            )
        if self._view.estimated_work:
            meta_parts.append(
                _trf("ai_skill.work", "预计：{what}", what=self._view.estimated_work)
            )
        if meta_parts:
            meta = QLabel(" · ".join(meta_parts))
            meta.setStyleSheet("color: gray;")
            meta.setWordWrap(True)
            v.addWidget(meta)

        # 触发示例
        if self._view.trigger_examples:
            ex_txt = " / ".join(self._view.trigger_examples)
            ex = QLabel(
                _trf("ai_skill.examples", "试试：{ex}", ex=ex_txt)
            )
            ex.setStyleSheet("color: #7f8c8d; font-style: italic;")
            ex.setWordWrap(True)
            v.addWidget(ex)

        # 前置条件清单
        if self._view.prereq is not None:
            v.addWidget(self._build_prereq(self._view.prereq))

        # 表单（仅当有字段时）
        if self._view.form_fields:
            v.addWidget(self._build_form(self._view.form_fields))

        # 开始按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._start_btn = QToolButton()
        self._start_btn.setText(tr("ai_skill.start", "开始"))
        ready = self._view.prereq.ready if self._view.prereq else True
        self._start_btn.setEnabled(ready)
        if not ready:
            self._start_btn.setToolTip(self._blocking_tooltip(self._view.prereq))
        else:
            self._start_btn.setToolTip(tr("ai_skill.start_tooltip", "用当前参数运行此 Skill"))
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn)
        v.addLayout(btn_row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

    def _build_prereq(self, prereq: PrereqReport) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(1)
        for check in prereq.checks:
            color = "#27ae60" if check.ok else "#c0392b"
            mark = "✓" if check.ok else "✗"
            lbl = QLabel(
                f"<span style='color:{color}'>{mark}</span> "
                f"<b>{check.label}</b>：{check.detail}"
            )
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setWordWrap(True)
            layout.addWidget(lbl)
        return host

    def _build_form(self, fields) -> QWidget:
        host = QWidget()
        form = QFormLayout(host)
        form.setContentsMargins(0, 4, 0, 0)
        form.setSpacing(4)
        for f in fields:
            w = self._make_field_widget(f)
            self._field_widgets[f.name] = w
            label_txt = f.label + (" *" if f.required else "")
            form.addRow(label_txt, w)
        return host

    def _make_field_widget(self, f: FormField) -> QWidget:
        if f.field_type == "integer":
            w = QSpinBox()
            w.setRange(
                int(f.min_value) if f.min_value is not None else 0,
                int(f.max_value) if f.max_value is not None else 100000,
            )
            if f.default is not None:
                w.setValue(int(f.default))
            return w
        if f.field_type == "number":
            w = QLineEdit()
            if f.default is not None:
                w.setText(str(f.default))
            return w
        if f.field_type == "enum":
            w = QComboBox()
            for opt in f.options:
                w.addItem(opt)
            if f.default in f.options:
                w.setCurrentIndex(f.options.index(f.default))
            return w
        # string / asset_ids 默认用 QLineEdit
        w = QLineEdit()
        if f.default is not None:
            w.setText(str(f.default))
        if f.help_text:
            w.setPlaceholderText(f.help_text)
        return w

    def _blocking_tooltip(self, prereq: PrereqReport) -> str:
        msgs = prereq.blocking_messages
        if not msgs:
            return tr("ai_skill.not_ready", "前置条件未满足")
        return tr("ai_skill.not_ready_detail", "缺少：") + "\n" + "\n".join(msgs)

    # ---------------- 表单值收集 ---------------- #

    def form_values(self) -> dict[str, Any]:
        """收集当前表单值。"""
        values: dict[str, Any] = {}
        for name, w in self._field_widgets.items():
            if isinstance(w, QSpinBox):
                values[name] = w.value()
            elif isinstance(w, QComboBox):
                values[name] = w.currentText()
            elif isinstance(w, QLineEdit):
                txt = w.text().strip()
                values[name] = txt if txt else None
        return values

    def set_form_values(self, values: dict[str, Any]) -> None:
        """预填表单值（P1.5 adjust_params：从原 invocation.inputs 回填表单）。

        遍历已构建的 ``_field_widgets``，按 widget 类型设置值。未知 key / None 值
        跳过（保留 default）；单字段异常不阻断其他字段（防御性）。
        """
        for name, w in self._field_widgets.items():
            if name not in values:
                continue
            val = values[name]
            if val is None:
                continue
            try:
                if isinstance(w, QSpinBox):
                    w.setValue(int(val))
                elif isinstance(w, QComboBox):
                    idx = w.findText(str(val))
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                elif isinstance(w, QLineEdit):
                    w.setText(str(val))
            except Exception:  # noqa: BLE001
                pass

    # ---------------- slots ---------------- #

    def _on_start(self) -> None:
        if self._updating:
            return
        self.start_requested.emit(self._view.skill_id, self.form_values())

    @property
    def skill_id(self) -> str:
        return self._view.skill_id

    @property
    def is_ready(self) -> bool:
        return bool(self._view.prereq and self._view.prereq.ready)


class AiSkillGallery(QWidget):
    """推荐卡列表（plan §10.3 P1.2）。

    Signals：

    - ``start_requested(str, dict)``：转发某张卡的开始请求。
    """

    start_requested = pyqtSignal(str, dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._cards: list[AiSkillCard] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._title = QLabel(f"<b>{tr('ai_skill.gallery_title', '可用 Skill')}</b>")
        self._title.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._title)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        self._host_layout = QVBoxLayout(host)
        self._host_layout.setContentsMargins(0, 0, 0, 0)
        self._host_layout.setSpacing(8)
        self._host_layout.addStretch(1)
        self._scroll.setWidget(host)
        layout.addWidget(self._scroll, stretch=1)

        self._empty = QLabel(tr("ai_skill.gallery_empty", "暂无可用 Skill。"))
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color: gray; padding: 12px;")
        layout.addWidget(self._empty)

    def set_cards(self, card_views: list[SkillCardView]) -> None:
        """用一组 :class:`SkillCardView` 重新渲染。"""
        self._updating = True
        try:
            # 清空旧卡
            while self._host_layout.count() > 1:
                it = self._host_layout.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            self._cards = []
            for cv in card_views:
                card = AiSkillCard(cv)
                card.start_requested.connect(self.start_requested.emit)
                self._cards.append(card)
                self._host_layout.insertWidget(self._host_layout.count() - 1, card)
            self._empty.setVisible(not card_views)
            self._scroll.setVisible(bool(card_views))
        finally:
            self._updating = False

    @property
    def cards(self) -> list[AiSkillCard]:
        return list(self._cards)


__all__ = ["AiSkillCard", "AiSkillGallery"]
