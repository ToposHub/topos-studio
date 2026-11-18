"""Explicit maintenance over the same catalog used by native Files."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Any
import uuid

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QFileDialog, QWidget

from src.features.files.services.facade import FilesFacade
from src.features.files.services.maintenance import FilesMaintenance


class FilesMaintenanceDialog(QDialog):
    _finished = pyqtSignal(object)

    def __init__(self, facade: FilesFacade, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._facade = facade
        self._service = FilesMaintenance(facade._catalog)
        self._busy = False
        self._closed = False
        self.setWindowTitle("素材库备份与恢复")
        self.resize(520, 230)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("备份当前新素材库、组织关系和回收站。源素材只保存引用，不复制或修改。", self))
        self.create_button = QPushButton("创建完整备份", self)
        self.verify_button = QPushButton("验证已有备份…", self)
        self.restore_button = QPushButton("恢复到新的空数据目录…", self)
        for button in (self.create_button, self.verify_button, self.restore_button):
            layout.addWidget(button)
        self.status = QLabel("恢复不会覆盖当前库；新库可通过独立配置根启动检查。", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.create_button.clicked.connect(lambda: self._run(self._service.backup))
        self.verify_button.clicked.connect(self._verify)
        self.restore_button.clicked.connect(self._restore)
        self._finished.connect(self._done)

    def _run(self, work: Callable[[], Any]) -> None:
        if self._busy or self._closed:
            return
        self._busy = True
        for button in (self.create_button, self.verify_button, self.restore_button):
            button.setEnabled(False)
        self.status.setText("正在处理…")

        def deliver(outcome: Any) -> None:
            if not self._closed:
                self._finished.emit(outcome)

        self._facade.run(key="maintenance:" + uuid.uuid4().hex, request_id=uuid.uuid4().hex,
                         work=work, listener=deliver)

    def _done(self, outcome: Any) -> None:
        if self._closed:
            return
        self._busy = False
        for button in (self.create_button, self.verify_button, self.restore_button):
            button.setEnabled(True)
        if outcome.error is not None:
            self.status.setText("操作失败：" + outcome.error.message)
        elif isinstance(outcome.result, dict):
            self.status.setText("备份完整性验证通过。")
        else:
            self.status.setText("已完成：" + str(outcome.result))

    def _verify(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if selected:
            self._run(lambda: self._service.verify(Path(selected)))

    def _restore(self) -> None:
        backup = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if not backup:
            return
        parent = QFileDialog.getExistingDirectory(self, "选择新的配置目录（其中 files-data 必须为空）")
        if parent:
            self._run(lambda: self._service.restore(Path(backup), Path(parent) / "files-data"))

    def closeEvent(self, event: Any) -> None:
        # A published backup remains owned by the domain runtime, even if its
        # progress dialog closes. Late callbacks never access deleted widgets.
        self._closed = True
        super().closeEvent(event)
