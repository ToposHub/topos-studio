"""模板库页 —— AI Workflow 的默认入口（plan §10.7.4「先官方模板」的产品化）。

> 用户旅程收敛：模板库选卡 → 参数表单 → Run；自由画布降级为「高级编辑」。

- 官方模板按 ``category`` 分组渲染为卡片（display_name / description / category）。
- 双击或「使用」按钮 → emit ``template_selected(template_id)``（host 调
  ``panel.load_template``）。
- 纯 Qt 列表视图；不依赖画布 / moderngl。
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QEvent, QObject, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import tr


class TemplateGalleryPage(QWidget):
    """官方模板卡片库（AI Workflow 弹窗默认页）。

    Signals:
        template_selected(str): 用户选择模板（template_id）。
    """

    template_selected = pyqtSignal(str)

    def __init__(self, template_registry: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._template_registry = template_registry
        self._build_ui()

    # ---------------- 构建 ---------------- #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        header = QLabel(tr("workflow.gallery.title", "选择一个工作流模板"))
        header.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(header)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        self._cards_layout = QVBoxLayout(host)
        self._cards_layout.setContentsMargins(4, 4, 4, 4)
        self._cards_layout.setSpacing(6)
        self._cards_layout.addStretch(1)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)
        self.rebuild()

    def rebuild(self) -> None:
        """按注册表重建卡片（模板集合运行期不变，重建成本低）。"""
        # 清掉旧卡片行（保留末尾 stretch）
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        templates = self._template_registry.all_templates()
        by_category: dict[str, list[Any]] = {}
        for entry in templates:
            by_category.setdefault(entry.category, []).append(entry)

        for category in sorted(by_category):
            label = QLabel(tr(
                f"workflow.gallery.category.{category}", category,
            ))
            label.setStyleSheet("color: #9ca3af; font-weight: 600; margin-top: 6px;")
            self._cards_layout.addWidget(label)
            for entry in by_category[category]:
                self._cards_layout.addWidget(
                    self._make_card(entry), stretch=0,
                )
            self._cards_layout.addStretch(1)

    def _make_card(self, entry: Any) -> QWidget:
        card = QFrame()
        card.setObjectName(f"template-card-{entry.template_id}")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        # AUD-23924: 模块文档承诺「双击卡片」选择模板——在此登记卡片，
        # 由 eventFilter 把双击转成 template_selected。
        card.setProperty("template_id", str(entry.template_id))
        card.installEventFilter(self)
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 8, 10, 8)
        text_host = QWidget()
        text = QVBoxLayout(text_host)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        name = QLabel(entry.display_name)
        name.setStyleSheet("font-weight: 600;")
        desc = QLabel(entry.description)
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #9ca3af;")
        text.addWidget(name)
        text.addWidget(desc)
        row.addWidget(text_host, 1)
        use_button = QPushButton(tr("workflow.gallery.use", "使用"))
        use_button.clicked.connect(
            lambda _=False, tid=str(entry.template_id):
                self.template_selected.emit(tid)
        )
        row.addWidget(use_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return card

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        """双击模板卡片 → 选择该模板（AUD-23924，与「使用」按钮等效）。"""
        if (
            event is not None
            and event.type() == QEvent.Type.MouseButtonDblClick
            and isinstance(watched, QFrame)
        ):
            tid = watched.property("template_id")
            if tid:
                self.template_selected.emit(str(tid))
                return True
        return super().eventFilter(watched, event)


__all__ = ["TemplateGalleryPage"]
