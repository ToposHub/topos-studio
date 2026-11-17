"""G2：image_factory 独立工具窗口（批处理 command/window 接管）。

与 G1 同款宿主模式：F5 WindowRegistry singleton、F3/F4 原生命令三段；
关闭即取消在途批次。生成结果经**同一索引入口回库**（outputs_completed →
宿主 refresh + start_scan，不开第二套索引）。
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QRadioButton, QVBoxLayout, QWidget,
)

from src.features.files.image_factory_service import (
    BatchResult,
    ImageFactoryService,
    PRESETS,
)
from src.shared.ui.style_sheet import C


class ImageFactoryWindow(QDialog):
    """图片工厂：真实选中数组批处理（逐项结果/取消/失败恢复/回库）。"""

    outputs_completed = pyqtSignal(list)   # [(source, output)] —— 回库入口
    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None,
                 *, service: ImageFactoryService,
                 paths: tuple[str, ...] = ()) -> None:
        super().__init__(parent)
        self._service = service
        self._paths: tuple[str, ...] = tuple(paths)
        self._worker: Any = None
        self.setWindowTitle("图片工厂")
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
        row.addWidget(self.preset)
        root.addLayout(row)
        policy_row = QHBoxLayout()
        self.policy_rename = QRadioButton("冲突改名")
        self.policy_skip = QRadioButton("跳过已存在")
        self.policy_overwrite = QRadioButton("覆盖")
        self.policy_rename.setChecked(True)
        for button in (self.policy_rename, self.policy_skip,
                       self.policy_overwrite):
            policy_row.addWidget(button)
        root.addLayout(policy_row)
        self.plan_label = QLabel("—")
        self.plan_label.setWordWrap(True)
        root.addWidget(self.plan_label, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)
        actions = QHBoxLayout()
        self.run = QPushButton("开始批处理")
        self.run.setObjectName("pluginPrimary")
        self.run.clicked.connect(self._run)
        self.cancel = QPushButton("取消")
        self.cancel.clicked.connect(self._service.cancel)
        self.retry_failed = QPushButton("重试失败项")
        self.retry_failed.clicked.connect(self._retry_failed)
        self.retry_failed.setVisible(False)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        actions.addWidget(self.run)
        actions.addWidget(self.cancel)
        actions.addWidget(self.retry_failed)
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        self._failed: list[Any] = []
        self.set_inputs(tuple(paths))

    # ------------------------------------------------------------ 输入/计划
    def set_inputs(self, paths: tuple[str, ...]) -> None:
        self._paths = tuple(paths)
        self._failed = []
        self.retry_failed.setVisible(False)
        if not paths:
            self.inputs.setText("没有输入素材（在 Files 中选择文件后打开）。")
            self.plan_label.setText("—")
            self.run.setEnabled(False)
            return
        self.inputs.setText(f"输入（{len(paths)} 项，快照冻结）：\n" + "\n".join(
            f"· {path}" for path in paths))
        self._replan()

    def _replan(self) -> None:
        if not self._paths:
            return
        try:
            plan = self._service.build_plan(
                self._paths, self.preset.currentData() or "png",
                conflict_policy=self._policy())
        except (OSError, ValueError) as exc:
            self.plan_label.setText(f"无法生成计划：{exc}")
            self.run.setEnabled(False)
            return
        self.run.setEnabled(True)
        lines = []
        for item in plan:
            mark = "（冲突→改名）" if "resolved" in item.conflict else (
                "（已存在）" if item.conflict else "")
            lines.append(f"→ {item.output_path} {mark}")
        self.plan_label.setText("输出计划：\n" + "\n".join(lines))

    def _policy(self) -> str:
        if self.policy_skip.isChecked():
            return "skip"
        if self.policy_overwrite.isChecked():
            return "overwrite"
        return "rename"

    # ------------------------------------------------------------ 执行
    def _run(self) -> None:
        self._execute(self._plan_items())

    def _plan_items(self):
        return self._service.build_plan(
            self._paths, self.preset.currentData() or "png",
            conflict_policy=self._policy())

    def _execute(self, plan) -> None:
        if not plan:
            return
        self._failed = []
        self.retry_failed.setVisible(False)
        self.run.setEnabled(False)
        self.progress.setValue(0)
        import threading
        self._worker = threading.Thread(
            target=self._run_batch, args=(tuple(plan),), daemon=True,
            name="image-factory-batch")
        self._worker.start()

    def _run_batch(self, plan) -> None:
        import time
        total = len(plan)
        last_ui = [0.0]

        def on_item(index: int, total_count: int, path: str, ok: bool,
                    message: str) -> None:
            now = time.monotonic()
            if now - last_ui[0] > 0.02 or index + 1 == total_count:
                last_ui[0] = now
                # worker 线程只改数值/文本——Qt 侧这些调用线程安全性由
                # 数值性质保证（进度条 setValue 跨线程在 Qt 是排队语义）
                self.progress.setValue(int((index + 1) * 100 / total_count))
                if not ok:
                    self._failed.append(path)

        def on_done(result: BatchResult) -> None:
            self._batch_done(result)

        self._service.execute(plan, self.preset.currentData() or "png",
                              conflict_policy=self._policy(),
                              on_item=on_item, on_done=on_done)

    def _batch_done(self, result: BatchResult) -> None:
        # worker 线程到达：回库信号（宿主在 GUI 消费）与 UI 文本
        if result.outputs:
            self.outputs_completed.emit(
                [{"source": source, "output": output}
                 for source, output in result.outputs])
        ok = len(result.outputs)
        fail = len(result.failures)
        if fail:
            self.retry_failed.setVisible(True)
            self.plan_label.setText(
                f"完成 {ok} 项，失败 {fail} 项（可重试）；成功项已回库。")
        else:
            self.plan_label.setText(f"完成 {ok} 项；结果已回库。")
        self.run.setEnabled(True)

    def _retry_failed(self) -> None:
        failed_sources = [
            item.source.path for item in self._plan_items()
            if item.source.path in set(self._failed)]
        if not failed_sources:
            return
        self.set_inputs(tuple(failed_sources))
        self._run()

    # ------------------------------------------------------------ 关闭路径
    def closeEvent(self, event) -> None:  # type: ignore[override]
        # G 节退出验收：关闭即取消在途批次
        self._service.cancel()
        self.closed.emit()
        super().closeEvent(event)
