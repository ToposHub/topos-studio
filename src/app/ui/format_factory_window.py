"""G1：format_factory 独立 Qt 工具窗口（command/window 接管）。

不走旧 /launch：由 ``NativeToolCommandService`` 的 dispatch 打开（F4 原
子边界内），窗口生命周期经 F5 ``WindowRegistry``（singleton——二次打
开聚焦现有）；关闭即取消全部在途转码（不留孤儿 ffmpeg）。

政策明确：单输入与多选都可批处理，但每一项都有**输入快照**（执行期
漂移即失败）与**输出计划**（冲突 rename/skip/overwrite 显式选择）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from src.features.files.format_factory_service import (
    FormatFactoryService,
    InputSnapshot,
    PlanItem,
    PRESETS,
)
from src.shared.ui.style_sheet import C


class FormatFactoryWindow(QDialog):
    """格式工厂工具窗口：计划 → 执行（进度/取消）→ 失败恢复。"""

    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None,
                 *, service: FormatFactoryService,
                 paths: tuple[str, ...] = ()) -> None:
        super().__init__(parent)
        self._service = service
        self._plan: tuple[PlanItem, ...] = ()
        self._failed: list[PlanItem] = []
        self._running: set[str] = set()
        self._paths: tuple[str, ...] = tuple(paths)
        self.setWindowTitle("格式工厂")
        self.resize(560, 420)
        self.setStyleSheet(
            f"QDialog {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}")
        root = QVBoxLayout(self)
        self.inputs = QLabel("—")
        self.inputs.setWordWrap(True)
        root.addWidget(self.inputs)
        row = QHBoxLayout()
        self.preset = QComboBox()
        for key, spec in PRESETS.items():
            self.preset.addItem(str(spec["title"]), key)
        self.preset.currentIndexChanged.connect(self._replan)
        row.addWidget(self.preset)
        root.addLayout(row)
        policy_row = QHBoxLayout()
        self.policy_rename = QRadioButton("冲突改名")
        self.policy_skip = QRadioButton("跳过已存在")
        self.policy_overwrite = QRadioButton("覆盖")
        self.policy_rename.setChecked(True)
        for button in (self.policy_rename, self.policy_skip,
                       self.policy_overwrite):
            button.toggled.connect(self._replan)
            policy_row.addWidget(button)
        root.addLayout(policy_row)
        self.plan_label = QLabel("—")
        self.plan_label.setWordWrap(True)
        root.addWidget(self.plan_label, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)
        self.retry_failed = QPushButton("重试失败项")
        self.retry_failed.clicked.connect(self._retry_failed)
        self.retry_failed.setVisible(False)
        self.cancel_all = QPushButton("全部取消")
        self.cancel_all.clicked.connect(self._cancel_all)
        actions = QHBoxLayout()
        self.run = QPushButton("开始")
        self.run.setObjectName("pluginPrimary")
        self.run.clicked.connect(self._run)
        actions.addWidget(self.run)
        actions.addWidget(self.retry_failed)
        actions.addWidget(self.cancel_all)
        actions.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        self._set_inputs(tuple(paths))

    # ------------------------------------------------------------ 输入与计划
    def set_inputs(self, paths: tuple[str, ...]) -> None:
        """新调用到达（singleton 聚焦后的刷新）：重置输入与计划。"""
        self._set_inputs(tuple(paths))

    def _set_inputs(self, paths: tuple[str, ...]) -> None:
        self._paths = paths
        self._failed.clear()
        self.retry_failed.setVisible(False)
        if not paths:
            self.inputs.setText("没有输入素材（在 Files 中选择文件后打开）。")
            self._plan = ()
            self.plan_label.setText("—")
            self.run.setEnabled(False)
            return
        self.inputs.setText("输入（快照冻结）：\n" + "\n".join(
            f"· {path}" for path in paths))
        self._replan()
        self.run.setEnabled(True)

    def _conflict_policy(self) -> str:
        if self.policy_skip.isChecked():
            return "skip"
        if self.policy_overwrite.isChecked():
            return "overwrite"
        return "rename"

    def _replan(self, *_: object) -> None:
        if not self._paths:
            return
        preset = self.preset.currentData() or "copy-mp4"
        try:
            self._plan = self._service.build_plan(
                self._paths, preset,
                conflict_policy=self._conflict_policy())
        except (OSError, ValueError) as exc:
            self._plan = ()
            self.plan_label.setText(f"无法生成计划：{exc}")
            self.run.setEnabled(False)
            return
        self.run.setEnabled(True)
        lines = []
        for item in self._plan:
            mark = ""
            if item.conflict.startswith("exists"):
                mark = "（冲突→改名）" if "resolved" in item.conflict else "（已存在）"
            lines.append(f"→ {item.output_path} {mark}")
        self.plan_label.setText("输出计划：\n" + "\n".join(lines))

    # ------------------------------------------------------------ 执行
    def _run(self) -> None:
        if not self._plan:
            return
        preset = self.preset.currentData() or "copy-mp4"
        policy = self._conflict_policy()
        self._running.clear()
        self._failed.clear()
        self.retry_failed.setVisible(False)
        self.progress.setVisible(True)
        self.run.setEnabled(False)
        total = len(self._plan)
        state = {"done": 0, "ok": 0}

        def done_for(item: PlanItem):
            def done(ok: bool, message: str) -> None:
                state["done"] += 1
                if ok:
                    state["ok"] += 1
                else:
                    self._failed.append(item)
                if state["done"] >= total:
                    self._finish(state["ok"], total)
            return done

        for item in self._plan:
            try:
                self._service.execute_item(
                    item, preset, conflict_policy=policy,
                    on_done=done_for(item))
            except (OSError, ValueError, FileExistsError) as exc:
                self._failed.append(item)
                state["done"] += 1
                if state["done"] >= total:
                    self._finish(state["ok"], total)

    def _finish(self, ok: int, total: int) -> None:
        self.progress.setVisible(False)
        self.run.setEnabled(True)
        if self._failed:
            self.retry_failed.setVisible(True)
            self.plan_label.setText(
                f"完成 {ok}/{total}；失败 {len(self._failed)} 项"
                "（可重试——输入快照仍会校验）。")
        else:
            self.plan_label.setText(f"完成 {ok}/{total} 项。")

    def _retry_failed(self) -> None:
        items, self._failed = self._failed, []
        preset = self.preset.currentData() or "copy-mp4"
        policy = self._conflict_policy()
        self.retry_failed.setVisible(False)
        self.progress.setVisible(True)
        total = len(items)
        state = {"done": 0, "ok": 0}

        def done_for(item: PlanItem):
            def done(ok: bool, message: str) -> None:
                state["done"] += 1
                if ok:
                    state["ok"] += 1
                else:
                    self._failed.append(item)
                if state["done"] >= total:
                    self._finish(state["ok"] + (len(self._plan) - total), len(self._plan))
            return done

        for item in items:
            try:
                self._service.retry_item(
                    item, preset, conflict_policy=policy, on_done=done_for(item))
            except (OSError, ValueError, FileExistsError):
                self._failed.append(item)
                state["done"] += 1
                if state["done"] >= total:
                    self._finish(state["ok"] + (len(self._plan) - total), len(self._plan))

    def _cancel_all(self) -> None:
        cancelled = self._service.cancel_all()
        self.plan_label.setText(f"已请求取消 {cancelled} 个在途任务。")

    # ------------------------------------------------------------ 关闭路径
    def closeEvent(self, event) -> None:  # type: ignore[override]
        # G 节退出验收：关闭即取消在途转码——不留孤儿 ffmpeg
        self._service.cancel_all()
        self.closed.emit()
        super().closeEvent(event)
