# DORMANT(2026-10-20 第五轮审计 E22): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""AI Variant 列表子组件（Phase 9 UI-3）。

显示 A/B/C 方案对比卡，比较时长、beat coverage、约束、音乐/字幕/效果摘要
（TAEP §11.6 步骤 7-9）。用户可设为候选、请求重规划、试听、比较。

约束（TAEP §11.3 VariantListPanel）：只更新 variant/decision 状态，不直接 edit clip。
只发 signal；状态回填用 ``_updating`` 锁。
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.ai_editing.assistant_events import VariantCard
from src.shared.i18n.i18n import tr


class _VariantCardWidget(QFrame):
    """单张 variant 卡片。"""

    selected = pyqtSignal(str)  # variant_id
    preview_requested = pyqtSignal(str)
    compare_requested = pyqtSignal(str)

    def __init__(
        self,
        card: VariantCard,
        fps: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._variant_id = card.variant_id
        self.setFrameShape(QFrame.Shape.Box)
        self.setStyleSheet("QFrame { border: 1px solid #ccc; border-radius: 4px; margin: 2px; padding: 4px; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)

        # 标题行
        header = QHBoxLayout()
        name = QLabel(card.label or card.variant_id)
        name.setStyleSheet("font-weight: bold;")
        header.addWidget(name)
        header.addStretch()
        # 约束状态
        constraint_icon = "✓" if card.hard_constraints_passed else "✗"
        constraint_label = QLabel(constraint_icon)
        constraint_label.setStyleSheet(
            "color: green;" if card.hard_constraints_passed else "color: red;"
        )
        header.addWidget(constraint_label)
        layout.addLayout(header)

        # 时长（AUD-19622）：duration_frames 是工程帧数，而工程 fps 可变
        # （Rational，如 24000/1001）。fps 已知时按其换算秒数；未知时退回
        # 显示帧数，绝不用硬编码 24.0 换算出失真的秒数。
        if not card.duration_frames:
            duration_label = QLabel(tr("ai_variant.duration", "时长: {s}s", s=0))
        elif fps is not None and fps > 0:
            dur_s = round(card.duration_frames / float(fps), 1)
            duration_label = QLabel(tr("ai_variant.duration", "时长: {s}s", s=dur_s))
        else:
            duration_label = QLabel(
                tr("ai_variant.duration_frames", "时长: {n}帧", n=card.duration_frames)
            )
        duration_label.setObjectName("variantDurationLabel")
        layout.addWidget(duration_label)

        # beat coverage / pace
        layout.addWidget(QLabel(
            tr("ai_variant.coverage", "节拍覆盖: {c}%", c=int(card.beat_coverage * 100))
        ))
        if card.pace_estimate:
            layout.addWidget(QLabel(
                tr("ai_variant.pace", "节奏: {p}", p=round(card.pace_estimate, 2))
            ))

        # 音乐/字幕标记
        tags = []
        if card.has_music:
            tags.append(tr("ai_variant.has_music", "音乐"))
        if card.has_subtitle:
            tags.append(tr("ai_variant.has_subtitle", "字幕"))
        if tags:
            layout.addWidget(QLabel(" · ".join(tags)))

        # 状态
        layout.addWidget(QLabel(tr("ai_variant.status", "状态: {s}", s=card.status)))

        # 按钮行
        btn_row = QHBoxLayout()
        preview_btn = QPushButton(tr("ai_variant.preview", "预览"))
        preview_btn.clicked.connect(lambda: self.preview_requested.emit(self._variant_id))
        btn_row.addWidget(preview_btn)

        compare_btn = QPushButton(tr("ai_variant.compare", "比较"))
        compare_btn.clicked.connect(lambda: self.compare_requested.emit(self._variant_id))
        btn_row.addWidget(compare_btn)

        select_btn = QPushButton(tr("ai_variant.select", "采用…"))
        select_btn.clicked.connect(lambda: self.selected.emit(self._variant_id))
        btn_row.addWidget(select_btn)
        btn_row.addStretch()

        btn_w = QWidget()
        btn_w.setLayout(btn_row)
        layout.addWidget(btn_w)


class AiVariantList(QWidget):
    """variant 对比列表（最多先显示 3 个方案，TAEP §11.9 UI-3）。"""

    variant_selected = pyqtSignal(str)
    variant_preview_requested = pyqtSignal(str)
    variant_compare_requested = pyqtSignal(str)

    MAX_VISIBLE = 3

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        title = QLabel(tr("ai_variant.section_title", "方案"))
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.addStretch()
        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll)

        self._empty_label = QLabel(tr("ai_variant.empty", "尚无方案。提交需求后助手会生成至少两个可比较的方案。"))
        self._empty_label.setStyleSheet("color: gray; padding: 8px;")
        self._empty_label.setWordWrap(True)
        layout.addWidget(self._empty_label)

    def set_variants(
        self, variants: tuple[VariantCard, ...], fps: float | None = None
    ) -> None:
        """回填 variant 列表（最多 MAX_VISIBLE 张卡）。

        Args:
            variants: variant 卡片数据。
            fps: 工程帧率，用于把 ``duration_frames`` 换算成秒。None 时
                卡片显示帧数而不是用假定的帧率换算（AUD-19622）。
        """
        self._updating = True
        try:
            # 清空旧卡
            while self._container_layout.count() > 1:
                item = self._container_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            visible = variants[: self.MAX_VISIBLE]
            self._empty_label.setVisible(len(visible) == 0)
            for card in visible:
                cw = _VariantCardWidget(card, fps=fps)
                cw.selected.connect(self.variant_selected.emit)
                cw.preview_requested.connect(self.variant_preview_requested.emit)
                cw.compare_requested.connect(self.variant_compare_requested.emit)
                self._container_layout.insertWidget(self._container_layout.count() - 1, cw)
        finally:
            self._updating = False

    @property
    def is_updating(self) -> bool:
        return self._updating
