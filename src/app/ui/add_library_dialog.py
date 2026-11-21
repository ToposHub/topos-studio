"""Choose how to register a Files library before starting a filesystem task."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.app.ui.library_folder_picker import LibraryFolderPicker
from src.features.files.contracts import is_valid_folder_name
from src.shared.i18n import I18N
from src.shared.ui.style_sheet import C, ERROR_LABEL_STYLE, PRIMARY_DIALOG_BUTTON_STYLE

if TYPE_CHECKING:
    from src.app.ui.native_files_transport import NativeFilesTransport


def _tr(key: str, default: str) -> str:
    return I18N.tr(f"files.add_library.{key}", default)


class AddLibraryDialog(QDialog):
    """Collect the selected directory and, for a new library, its leaf name."""

    def __init__(self, parent: QWidget | None, *, initial_path: str,
                 transport: NativeFilesTransport | None = None) -> None:
        super().__init__(parent)
        self.directory = ""
        self.library_name = ""
        self._home = Path.home()
        self._transport = transport
        self.setWindowTitle(_tr("title", "添加素材库"))
        self.setStyleSheet(f"""
            QRadioButton {{ color: {C.TEXT_SECONDARY}; spacing: 6px; }}
            QRadioButton::indicator {{
                width: 11px; height: 11px; border-radius: 6px;
                border: 1px solid {C.TEXT_HINT}; background: {C.BG_INPUT};
            }}
            QRadioButton::indicator:checked {{ background: {C.ORANGE}; border-color: {C.ORANGE}; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        root.addStrut(480)

        self.reference_mode = QRadioButton(_tr("reference", "引用现有文件夹"), self)
        self.create_mode = QRadioButton(_tr("create", "新建素材库"), self)
        self.reference_mode.setChecked(True)
        modes = QHBoxLayout()
        modes.setSpacing(24)
        modes.addWidget(self.reference_mode)
        modes.addWidget(self.create_mode)
        modes.addStretch()
        root.addLayout(modes)

        self._form = QFormLayout()
        self._form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._form.setSpacing(8)
        root.addLayout(self._form)

        self.directory_edit = QLineEdit(self)
        self.directory_edit.setText(str(self._absolute_path(initial_path) or self._home))
        self._folder_label = QLabel(self)
        self._folder_label.setBuddy(self.directory_edit)
        browse = QPushButton(_tr("browse", "浏览…"), self)
        browse.clicked.connect(self._browse)
        browse.setAutoDefault(False)
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        path_row.addWidget(self.directory_edit, 1)
        path_row.addWidget(browse)
        self._form.addRow(self._folder_label, path_row)

        self.name_edit = QLineEdit(self)
        self._form.addRow(_tr("name", "素材库名称"), self.name_edit)
        self.destination = QLineEdit(self)
        self.destination.setReadOnly(True)
        self.destination.setStyleSheet(f"""
            QLineEdit {{ background: transparent; border: 0; padding: 0; color: {C.TEXT_SECONDARY}; font-size: 13px; }}
        """)
        self._form.addRow(_tr("destination", "将新建于"), self.destination)

        self._validation = QLabel(self)
        self._validation.setWordWrap(True)
        self._validation.setStyleSheet(ERROR_LABEL_STYLE)
        root.addWidget(self._validation)

        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.add_button = actions.button(QDialogButtonBox.StandardButton.Ok)
        self.add_button.setStyleSheet(PRIMARY_DIALOG_BUTTON_STYLE)
        self.add_button.setText(_tr("add", "添加"))
        self.add_button.setDefault(True)
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText(_tr("cancel", "取消"))
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        root.addWidget(actions)

        self.create_mode.toggled.connect(self._mode_changed)
        self.directory_edit.textChanged.connect(self._update_preview)
        self.name_edit.textChanged.connect(self._update_preview)
        self._mode_changed(False)
        self.add_button.setFocus()

    def _mode_changed(self, creating: bool) -> None:
        self._folder_label.setText(_tr("parent_folder", "存放位置") if creating else _tr("folder", "文件夹"))
        self._form.setRowVisible(self.name_edit, creating)
        self._form.setRowVisible(self.destination, creating)
        self._update_preview()

    def _absolute_path(self, value: str) -> Path | None:
        if not value.strip() or any(ord(char) < 32 for char in value):
            return None
        if value == "~":
            return self._home
        if value.startswith(("~/", "~\\")):
            return self._home / value[2:]
        candidate = Path(value)
        return candidate if candidate.is_absolute() else None

    def _selection(self) -> tuple[Path | None, str, str]:
        directory = self._absolute_path(self.directory_edit.text())
        if directory is None:
            return None, "", _tr("invalid_path", "请选择文件夹或输入绝对路径。")
        if not self.create_mode.isChecked():
            return directory, "", ""
        name = self.name_edit.text().strip()
        if not name:
            return directory, "", _tr("name_required", "请输入素材库名称。")
        if not is_valid_folder_name(name):
            return directory, "", _tr("invalid_name", "请输入有效的单级文件夹名称。")
        return directory, name, ""

    def _update_preview(self) -> None:
        directory, name, error = self._selection()
        destination = str(directory / name) if directory is not None and name else ""
        self.destination.setText(destination or "—")
        self.destination.setToolTip(destination)
        self._validation.setText(error)
        self._validation.setVisible(bool(error))
        self.add_button.setEnabled(not error)

    def _browse(self) -> None:
        title = (_tr("choose_parent", "选择素材库存放位置") if self.create_mode.isChecked()
                 else _tr("choose_folder", "选择素材文件夹"))
        start = self._absolute_path(self.directory_edit.text()) or self._home
        picker = LibraryFolderPicker(self, title=title, initial_path=start, home=self._home, transport=self._transport)
        try:
            if picker.exec() == QDialog.DialogCode.Accepted and picker.directory is not None:
                self.directory_edit.setText(str(picker.directory))
        finally:
            picker.deleteLater()

    def accept(self) -> None:
        directory, name, error = self._selection()
        if directory is None or error:
            self._update_preview()
            return
        self.directory = str(directory)
        self.library_name = name
        super().accept()
