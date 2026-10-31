"""AI Brief 编辑器子组件（Phase 9 UI-2）。

让用户编辑 ``EditorialBrief``：目标、平台 preset、目标/最小/最大时长、受众、
语气、必要 beat、禁用内容、可用音乐策略（TAEP §11.6 步骤 3）。

约束（TAEP §11.3 / §11.9 UI-2）：

- 产生**待批准** brief patch；批准后版本化保存。
- 用户能完全不接模型地创建 Brief、选择素材与锁定镜头。
- 修改 Brief 只让 variant 过期，**不**破坏已提交 timeline。
- 只发 ``brief_patch_requested`` signal，不直接调 timeline/service。
- 状态回填用 ``_updating`` 锁避免信号递归。
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.ai_editing.editorial_models import (
    DurationTarget,
    EditorialBrief,
    Platform,
    SubtitlePolicy,
)
from src.shared.i18n.i18n import tr


class AiBriefEditor(QWidget):
    """EditorialBrief 表单编辑器。

    编辑产生 draft brief patch；用户点“批准”后发 ``brief_patch_requested``。
    """

    brief_patch_requested = pyqtSignal(object)  # EditorialBrief (新 revision)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._brief: EditorialBrief | None = None
        self._build_ui()

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        title = QLabel(tr("ai_brief.title", "创作 Brief"))
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        form = QFormLayout()

        # 目标
        self._goal_edit = QTextEdit()
        self._goal_edit.setMaximumHeight(60)
        self._goal_edit.setPlaceholderText(tr("ai_brief.goal_hint", "这条片要讲什么？"))
        form.addRow(tr("ai_brief.goal", "目标"), self._goal_edit)

        # 平台
        self._platform_combo = QComboBox()
        for p in Platform:
            self._platform_combo.addItem(p.value)
        form.addRow(tr("ai_brief.platform", "平台"), self._platform_combo)

        # 时长（ideal/min/max 帧）
        dur_row = QHBoxLayout()
        self._dur_ideal = QSpinBox()
        self._dur_ideal.setRange(1, 100000)
        self._dur_ideal.setValue(720)
        self._dur_min = QSpinBox()
        self._dur_min.setRange(1, 100000)
        self._dur_min.setValue(648)
        self._dur_max = QSpinBox()
        self._dur_max.setRange(1, 100000)
        self._dur_max.setValue(792)
        dur_row.addWidget(QLabel(tr("ai_brief.ideal", "理想")))
        dur_row.addWidget(self._dur_ideal)
        dur_row.addWidget(QLabel(tr("ai_brief.min", "最小")))
        dur_row.addWidget(self._dur_min)
        dur_row.addWidget(QLabel(tr("ai_brief.max", "最大")))
        dur_row.addStretch()
        dur_w = QWidget()
        dur_w.setLayout(dur_row)
        form.addRow(tr("ai_brief.duration", "时长(帧)"), dur_w)

        # 受众
        self._audience_edit = QLineEdit()
        self._audience_edit.setPlaceholderText(tr("ai_brief.audience_hint", "首次接触产品的用户"))
        form.addRow(tr("ai_brief.audience", "受众"), self._audience_edit)

        # 语气
        self._tone_edit = QLineEdit()
        self._tone_edit.setPlaceholderText(tr("ai_brief.tone_hint", "自信, 温暖, 快节奏"))
        form.addRow(tr("ai_brief.tone", "语气"), self._tone_edit)

        # 字幕策略
        self._subtitle_combo = QComboBox()
        for sp in SubtitlePolicy:
            self._subtitle_combo.addItem(sp.value)
        form.addRow(tr("ai_brief.subtitle", "字幕"), self._subtitle_combo)

        layout.addLayout(form)

        # 必要 beat
        layout.addWidget(QLabel(tr("ai_brief.beats", "必要叙事节拍")))
        self._beats_list = QListWidget()
        self._beats_list.setMaximumHeight(80)
        for beat in ("hook", "problem", "product_value", "call_to_action"):
            item = QListWidgetItem(beat)
            item.setCheckState(Qt.CheckState.Unchecked)
            self._beats_list.addItem(item)
        layout.addWidget(self._beats_list)

        # 批准按钮
        btn_row = QHBoxLayout()
        self._approve_btn = QPushButton(tr("ai_brief.approve", "批准 Brief"))
        self._approve_btn.clicked.connect(self._on_approve)
        btn_row.addStretch()
        btn_row.addWidget(self._approve_btn)
        btn_w = QWidget()
        btn_w.setLayout(btn_row)
        layout.addWidget(btn_w)

    # ------------------------------------------------------------------ #
    # 状态回填
    # ------------------------------------------------------------------ #

    def set_brief(self, brief: EditorialBrief) -> None:
        """回填现有 Brief（用 _updating 锁避免触发 change signal）。

        AUD-19410：``EditorialBrief.required_beats`` 允许任意字符串（AI 生成/
        导入的 brief 可携带固定 4 项之外的自定义 beat）。回填时把列表里还没
        出现的 beat 追加为可勾选项，保证"批准"收集（_collect_brief 按列表
        勾选态整体覆盖 required_beats）是无损往返，不会静默丢弃自定义 beat。
        """
        self._updating = True
        try:
            self._brief = brief
            self._goal_edit.setPlainText(brief.primary_goal)
            self._platform_combo.setCurrentText(brief.platform.value)
            if brief.target_duration is not None:
                self._dur_ideal.setValue(brief.target_duration.ideal)
                self._dur_min.setValue(brief.target_duration.min)
                self._dur_max.setValue(brief.target_duration.max)
            self._audience_edit.setText(", ".join(brief.audience))
            self._tone_edit.setText(", ".join(brief.tone))
            self._subtitle_combo.setCurrentText(brief.subtitle_policy.value)
            # beats：先补齐列表里缺失的自定义 beat，再统一回填勾选态
            required = tuple(brief.required_beats)
            existing = {
                self._beats_list.item(i).text()
                for i in range(self._beats_list.count())
            }
            for beat in required:
                beat = str(beat).strip()
                if beat and beat not in existing:
                    item = QListWidgetItem(beat)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    self._beats_list.addItem(item)
                    existing.add(beat)
            for i in range(self._beats_list.count()):
                item = self._beats_list.item(i)
                item.setCheckState(
                    Qt.CheckState.Checked if item.text() in required
                    else Qt.CheckState.Unchecked
                )
        finally:
            self._updating = False

    def _collect_brief(self) -> EditorialBrief:
        """从表单收集一个新 EditorialBrief（bump revision）。"""
        beats = tuple(
            self._beats_list.item(i).text()
            for i in range(self._beats_list.count())
            if self._beats_list.item(i).checkState() == Qt.CheckState.Checked
        )
        audience = tuple(
            a.strip() for a in self._audience_edit.text().split(",") if a.strip()
        )
        tone = tuple(
            t.strip() for t in self._tone_edit.text().split(",") if t.strip()
        )
        base = self._brief or EditorialBrief(brief_id="draft")
        return replace(
            base,
            primary_goal=self._goal_edit.toPlainText().strip(),
            platform=Platform(self._platform_combo.currentText()),
            target_duration=DurationTarget(
                self._dur_ideal.value(),
                min(self._dur_min.value(), self._dur_ideal.value()),
                max(self._dur_max.value(), self._dur_ideal.value()),
            ),
            audience=audience,
            tone=tone,
            subtitle_policy=SubtitlePolicy(self._subtitle_combo.currentText()),
            required_beats=beats,
        )

    def _on_approve(self) -> None:
        if self._updating:
            return
        brief = self._collect_brief().with_revision_bumped()
        self._brief = brief
        self.brief_patch_requested.emit(brief)

    @property
    def is_updating(self) -> bool:
        return self._updating
