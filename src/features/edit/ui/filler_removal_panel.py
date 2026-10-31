"""M11 UI 入口 · 文稿删除口头语面板（模型链已闭环，此为前台编排）。

接既有 detect_filler_words → build_filler_removal_plan（M11.1 已改真
实 track/clip 映射）→ compile_plan → 审批链（PREVIEW 模式）。面板列
出可删命中与 review 项（低置信人工排除后重检），提交经真实 compiler
编译为 MacroCommand（RangeRippleDeleteCommand——词区间 ripple 删除，
一次 Undo）。未提供时间线/检测回调时拒绝假数据。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from src.features.edit.ai_editing.filler_removal import (
    FillerDetectionResult,
    build_filler_removal_plan,
)
from src.shared.i18n.i18n import I18N


class FillerRemovalPanel(QDialog):
    """删除口头语面板（detect→plan→用户勾选→compile 交审批链）。"""

    def __init__(
        self,
        detection: FillerDetectionResult,
        *,
        project: Any,
        timeline: Any,
        timeline_id: str,
        base_revision: int,
        plan_id: str = "filler-removal",
        on_plan_ready: Optional[Callable[[Any], None]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._detection = detection
        self._project = project
        self._timeline = timeline
        self._timeline_id = timeline_id
        self._base_revision = int(base_revision)
        self._plan_id = plan_id
        self._on_plan_ready = on_plan_ready
        self._plan = None

        self.setWindowTitle(I18N.tr("filler_removal.title", "删除口头语"))
        layout = QVBoxLayout(self)
        n_del = len(detection.deletable)
        n_rev = len(detection.review_hits)
        layout.addWidget(QLabel(
            f"可删命中 {n_del} 项；低置信待复核 {n_rev} 项"
            "（复核项默认不删——人工确认排除后重新检测）"))
        self._status = QLabel()
        layout.addWidget(self._status)

        self._list = QListWidget()
        for hit in detection.deletable:
            item = QListWidgetItem(
                f"{hit.text} @帧 {hit.start_frame}–{hit.end_frame}"
                f"（置信 {hit.confidence:.2f}）")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, hit)
            self._list.addItem(item)
        layout.addWidget(self._list, 1)

        # M11.3：差异预览——勾选项映射到真实片段/区间（审批前可见：
        # 哪些 clip 的哪些帧段将被 ripple 删除）
        self._preview_btn = QPushButton(
            I18N.tr("filler_removal.preview_ranges", "预览删除区间（差异）"))
        self._preview_btn.clicked.connect(self._on_preview)
        layout.addWidget(self._preview_btn)
        self._preview_list = QListWidget()
        self._preview_list.hide()
        layout.addWidget(self._preview_list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            I18N.tr("filler_removal.build_plan", "生成删除计划（进入审批）"))
        buttons.accepted.connect(self._on_build)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_filtered_plan(self):
        """按勾选过滤命中并构建 PREVIEW 计划（None = 无勾选/空计划）。"""
        included = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                included.append(item.data(Qt.ItemDataRole.UserRole))
        if not included:
            self._status.setText(
                I18N.tr("filler_removal.no_hits_selected", "未勾选任何命中——无计划"))
            return None
        from src.features.edit.ai_editing.filler_removal import (
            FillerDetectionResult as FR,
        )
        filtered = FR(hits=tuple(included),
                      review_hits=self._detection.review_hits)
        from src.features.edit.ai_editing.models import PlanTarget
        plan = build_filler_removal_plan(
            filtered,
            target=PlanTarget(
                project_id=str(getattr(self._project, "project_id", "")),
                timeline_id=self._timeline_id),
            base_revision=self._base_revision,
            plan_id=self._plan_id,
            timeline=self._timeline,
            # M11.4：容器片段判定（嵌套/合成命中显式跳过）
            project=self._project)
        if not plan.operations:
            self._status.setText(
                I18N.tr("filler_removal.empty_plan", "计划为空（命中未映射到真实片段？）"))
            return None
        return plan

    def _on_preview(self) -> None:
        """M11.3：差异预览——列出每个 op 的目标 clip 与帧区间。"""
        plan = self._build_filtered_plan()
        if plan is None:
            self._preview_list.hide()
            return
        self._preview_list.clear()
        for op in plan.operations:
            params = op.params
            if "start_frame" in params:
                span = (f"帧 {params['start_frame']}–"
                        f"{params['end_frame']}（ripple 删除）")
            else:
                span = "整段删除"
            self._preview_list.addItem(QListWidgetItem(
                f"{params.get('reason', '')} → "
                f"{params.get('track_id', '')}/"
                f"{params.get('clip_id', '')}｜{span}"))
        total = len(plan.operations)
        self._status.setText(
            f"差异预览：{total} 个区间将删除（一次 Undo）——"
            "核对后「生成删除计划」进入审批")
        self._preview_list.show()

    def _on_build(self) -> None:
        self._plan = self._build_filtered_plan()
        if self._plan is None:
            return
        if self._on_plan_ready is not None:
            self._on_plan_ready(self._plan)
        self.accept()

    @property
    def plan(self):
        """生成的 EditPlan（PREVIEW 模式，交审批链）。"""
        return self._plan


__all__ = ["FillerRemovalPanel"]
