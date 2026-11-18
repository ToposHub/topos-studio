"""Native, bounded read-only Vault search; no legacy web host."""
from __future__ import annotations

from typing import Any
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QVBoxLayout, QWidget
from src.shared.vault.client import VaultClient


class _Signals(QObject):
    done = pyqtSignal(int, object, str)


class _Search(QRunnable):
    def __init__(self, generation: int, client: VaultClient, text: str) -> None:
        super().__init__()
        self.generation, self.client, self.text = generation, client, text
        self.signals = _Signals()

    def run(self) -> None:
        try:
            if not self.client.is_available():
                self.signals.done.emit(self.generation, (), "素材库查询服务离线；请启动 topos-vault serve。")
                return
            files = self.client.search(self.text, limit=100)
            stats = self.client.get_stats()
            self.signals.done.emit(self.generation, files,
                                  f"找到 {len(files)} 项 · 素材库 {stats.total if stats else '未知'} 项")
        except (OSError, ValueError, RuntimeError) as exc:
            self.signals.done.emit(self.generation, (), f"查询失败：{exc}")


class AILibraryWindow(QDialog):
    closed = pyqtSignal()
    path_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None, *, client: VaultClient | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 素材库")
        self.resize(720, 500)
        self._client = client or VaultClient(timeout=1.5)
        self._generation = 0
        self._closed = False
        self._task: _Search | None = None
        root = QVBoxLayout(self)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索素材库；查询服务和模型缺失时会显示原因")
        self.go = QPushButton("搜索")
        row.addWidget(self.search, 1)
        row.addWidget(self.go)
        root.addLayout(row)
        self.notice = QLabel("输入关键词搜索")
        root.addWidget(self.notice)
        self.results = QListWidget()
        root.addWidget(self.results, 1)
        self.go.clicked.connect(self.query)
        self.search.returnPressed.connect(self.query)
        self.results.itemActivated.connect(lambda item: self.path_requested.emit(str(item.data(Qt.ItemDataRole.UserRole))))

    def query(self) -> None:
        if self._task is not None:
            return  # one bounded request at a time
        self._closed = False
        self._generation += 1
        self.results.clear()
        self.notice.setText("正在查询…")
        self.go.setEnabled(False)
        task = _Search(self._generation, self._client, self.search.text())
        self._task = task
        task.signals.done.connect(self._finished)
        QThreadPool.globalInstance().start(task)

    def _finished(self, generation: int, files: Any, message: str) -> None:
        self._task = None
        if self._closed or generation != self._generation:
            return
        self.go.setEnabled(True)
        self.notice.setText(message)
        from PyQt6.QtWidgets import QListWidgetItem
        for file in files:
            if not isinstance(file, dict):
                continue
            path = file.get("path", "")
            if not isinstance(path, str) or not path:
                continue
            item = QListWidgetItem(str(file.get("name") or path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.results.addItem(item)

    def closeEvent(self, event: Any) -> None:
        self._closed = True
        self._generation += 1
        self.closed.emit()
        super().closeEvent(event)

    def showEvent(self, event: Any) -> None:
        self._closed = False
        self.go.setEnabled(self._task is None)
        super().showEvent(event)
