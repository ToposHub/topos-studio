"""G5：code_editor 等价编辑工具窗口（多文件/新建/安全保存/恢复）。

纯 Qt 实现（QPlainTextEdit + QTabWidget）——不依赖旧 routes/JS globals；
文件逻辑全部在 ``code_editor_service``（域层可测）。宿主模式与 G1-G4
一致：F5 singleton 注册表 + 原生命令打开。
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit, QTabWidget,
    QVBoxLayout, QWidget,
)

from src.features.files.code_editor_service import (
    EditorFileError,
    LoadedFile,
    new_file_buffer,
    read_file,
    reload_file,
    save_file,
)
from src.shared.ui.style_sheet import C


class _EditorTab(QWidget):
    def __init__(self, buffer: LoadedFile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.buffer = buffer
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.editor = QPlainTextEdit(self)
        self.editor.setPlainText(buffer.text)
        layout.addWidget(self.editor)
        self.meta = QLabel(self._meta_text())
        layout.addWidget(self.meta)

    def _meta_text(self) -> str:
        name = self.buffer.path or "（未保存）"
        encoding = self.buffer.encoding
        if self.buffer.encoding == "latin-1" and not self.buffer.bom_declared:
            encoding += "（降级读取——保存按原字节域写回）"
        return f"{name} · {encoding}"

    @property
    def text(self) -> str:
        return self.editor.toPlainText()

    def refresh_meta(self) -> None:
        self.meta.setText(self._meta_text())


class CodeEditorWindow(QDialog):
    """多文件代码/文本编辑器（新建与多文件入口；安全保存/恢复）。"""

    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *,
                 paths: tuple[str, ...] = ()) -> None:
        super().__init__(parent)
        self.setWindowTitle("代码编辑器")
        self.resize(760, 520)
        self.setStyleSheet(
            f"QDialog {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}")
        root = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        root.addWidget(self.tabs, 1)
        self.notice = QLabel("")
        root.addWidget(self.notice)
        actions = QHBoxLayout()
        new_button = QPushButton("新建")
        new_button.clicked.connect(self.new_file)
        self.save = QPushButton("保存")
        self.save.clicked.connect(self.save_current)
        self.reload = QPushButton("从磁盘重载")
        self.reload.clicked.connect(self.reload_current)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        for widget in (new_button, self.save, self.reload):
            actions.addWidget(widget)
        actions.addStretch(1)
        actions.addWidget(close_button)
        root.addLayout(actions)
        for path in paths:
            self.open_path(path)
        if self.tabs.count() == 0:
            self.new_file()

    # ------------------------------------------------------------ 入口
    def open_path(self, path: str) -> bool:
        try:
            buffer = read_file(path)
        except EditorFileError as exc:
            self.notice.setText(exc.message)
            return False
        self._add_tab(buffer)
        return True

    def new_file(self) -> None:
        index = 1
        while any(self.tabs.tabText(i).startswith(f"新建 {index}")
                  for i in range(self.tabs.count())):
            index += 1
        self._add_tab(new_file_buffer(), title=f"新建 {index}")

    def _add_tab(self, buffer: LoadedFile, title: str = "") -> None:
        tab = _EditorTab(buffer, self)
        self.tabs.addTab(tab, title or (buffer.path.rsplit("/", 1)[-1]
                                        if buffer.path else "（未保存）"))
        self.tabs.setCurrentWidget(tab)

    def _close_tab(self, index: int) -> None:
        self.tabs.removeTab(index)

    # ------------------------------------------------------------ 保存/恢复
    def current(self) -> Optional[_EditorTab]:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, _EditorTab) else None

    def save_current(self) -> bool:
        tab = self.current()
        if tab is None:
            return False
        if not tab.buffer.path:
            self.notice.setText("新建缓冲尚未绑定路径——请用「另存为…」"
                                "选择目标后保存（本窗口按已有路径保存）。")
            return False
        try:
            tab.buffer = save_file(tab.buffer, tab.text)
        except EditorFileError as exc:
            self.notice.setText(exc.message)
            return False
        tab.refresh_meta()
        self.notice.setText(f"已保存：{tab.buffer.path}")
        return True

    def reload_current(self) -> bool:
        tab = self.current()
        if tab is None or not tab.buffer.path:
            return False
        try:
            tab.buffer = reload_file(tab.buffer)
        except EditorFileError as exc:
            self.notice.setText(exc.message)
            return False
        tab.editor.setPlainText(tab.buffer.text)
        tab.refresh_meta()
        self.notice.setText(f"已从磁盘重载：{tab.buffer.path}")
        return True

    def set_paths(self, paths: tuple[str, ...]) -> None:
        """宿主再次打开（singleton 聚焦）时补充多文件入口。"""
        for path in paths:
            for index in range(self.tabs.count()):
                tab = self.tabs.widget(index)
                if isinstance(tab, _EditorTab) and tab.buffer.path == path:
                    self.tabs.setCurrentIndex(index)
                    break
            else:
                self.open_path(path)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.closed.emit()
        super().closeEvent(event)
