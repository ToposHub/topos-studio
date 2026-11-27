"""Configure and export a portable copy of a saved video project."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

if TYPE_CHECKING:
    from types import ModuleType


class _PackageJob(QRunnable):
    """Run package work without referencing the dialog on the worker thread."""

    class _Signals(QObject):
        done = pyqtSignal(object, object)
        progress = pyqtSignal(int)

    def __init__(self, operation: Callable[[Callable[[int, int], None]], object]) -> None:
        super().__init__()
        self.signals = self._Signals()
        self._operation = operation
        self._percent = -1

    def run(self) -> None:
        try:
            result = self._operation(self._report_progress)
        except Exception as error:
            self.signals.done.emit(None, error)
        else:
            self.signals.done.emit(result, None)

    def _report_progress(self, completed: int, total: int) -> None:
        if total > 0:
            percent = min(100, max(0, completed * 100 // total))
            if percent != self._percent:
                self._percent = percent
                self.signals.progress.emit(percent)


class ProjectPackageDialog(QDialog):
    """Export the saved project without opening or changing the current workspace."""

    def __init__(
        self, project: str, api: ModuleType, *, active: bool, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._api = api
        self._inspect_scope: str | None = None
        self._plan = None
        self._cancel_export: Event | None = None
        self.result_package = None

        self.setWindowTitle(I18N.tr("pm.package_title", "Package Project"))
        self.resize(600, 440)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        title = QLabel(Path(project).stem)
        title.setObjectName("pmTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        title.setToolTip(project)
        layout.addWidget(title)
        if active:
            saved_notice = QLabel(I18N.tr(
                "pm.package_saved_version",
                "The package uses the last saved version of this project.",
            ))
            saved_notice.setObjectName("pmMeta")
            saved_notice.setWordWrap(True)
            layout.addWidget(saved_notice)

        self._form = QFormLayout()
        form = self._form
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setSpacing(10)
        self._scope = ChevronComboBox()
        for key, label, value in (
            ("package_all", "Full project and media pool", "all"),
            ("package_used", "Project and used media", "used"),
            ("package_project_only", "Project file only", "project"),
        ):
            self._scope.addItem(I18N.tr(f"pm.{key}", label), value)
        form.addRow(I18N.tr("pm.package_content", "Include"), self._scope)
        self._format = ChevronComboBox()
        for key, label, value in (
            ("package_folder", "Folder", "folder"),
            ("package_zip", "ZIP archive", "zip"),
        ):
            self._format.addItem(I18N.tr(f"pm.{key}", label), value)
        form.addRow(I18N.tr("pm.package_format", "Format"), self._format)

        destination_row = QHBoxLayout()
        self._destination = QLineEdit()
        self._destination.setReadOnly(True)
        self._destination.setPlaceholderText(I18N.tr("pm.package_select_folder", "Choose a folder"))
        self._choose = QPushButton(I18N.tr("pm.package_choose", "Choose…"))
        self._choose.setObjectName("pmSecondary")
        self._choose.clicked.connect(self._choose_destination)
        destination_row.addWidget(self._destination, 1)
        destination_row.addWidget(self._choose)
        form.addRow(I18N.tr("pm.package_destination", "Destination"), destination_row)
        layout.addLayout(form)

        inspection = QHBoxLayout()
        self._status = QLabel()
        self._status.setObjectName("pmMeta")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        inspection.addWidget(self._status, 1)
        self._recheck = QPushButton(I18N.tr("pm.package_recheck", "Check again"))
        self._recheck.setObjectName("pmSecondary")
        self._recheck.clicked.connect(self._on_scope_changed)
        inspection.addWidget(self._recheck)
        layout.addLayout(inspection)
        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        self._details.setMinimumHeight(112)
        self._details.setAccessibleName(
            I18N.tr("pm.package_dependencies", "Dependencies and missing files")
        )
        layout.addWidget(self._details, 1)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.hide()
        layout.addWidget(self._progress)

        actions = QHBoxLayout()
        actions.addStretch()
        self._cancel = QPushButton(I18N.tr("pm.cancel", "Cancel"))
        self._cancel.setFixedHeight(36)
        self._cancel.clicked.connect(self.reject)
        actions.addWidget(self._cancel)
        self._export = QPushButton(I18N.tr("pm.package_export", "Export package"))
        self._export.setObjectName("pmPrimary")
        self._export.setFixedHeight(36)
        self._export.setDefault(True)
        self._export.setEnabled(False)
        self._export.clicked.connect(self._start_export)
        actions.addWidget(self._export)
        layout.addLayout(actions)

        self._scope.currentIndexChanged.connect(self._on_scope_changed)
        self._on_scope_changed()

    def _choose_destination(self) -> None:
        current = self._destination.text() or str(Path(self._project).parent)
        directory = QFileDialog.getExistingDirectory(
            self, I18N.tr("pm.package_select_folder", "Choose a folder"), current
        )
        if directory:
            self._destination.setText(directory)
            self._destination.setToolTip(directory)
            self._update_export_button()

    def _on_scope_changed(self) -> None:
        scope = self._scope.currentData()
        self._form.setRowVisible(self._format, scope != "project")
        self._plan = None
        self._status.setText(I18N.tr("pm.package_checking", "Checking media and dependencies…"))
        self._details.clear()
        self._recheck.setEnabled(False)
        self._update_export_button()
        if self._inspect_scope is None:
            self._inspect_project(scope)

    def _inspect_project(self, scope: str) -> None:
        self._inspect_scope = scope
        inspect = self._api.inspect_project_package
        project = self._project
        job = _PackageJob(lambda _progress: inspect(project, scope))
        job.signals.done.connect(self._on_inspection_done)
        QThreadPool.globalInstance().start(job)

    @pyqtSlot(object, object)
    def _on_inspection_done(self, plan: object, error: Exception | None) -> None:
        scope = self._scope.currentData()
        if self._inspect_scope != scope:
            self._inspect_project(scope)
            return
        self._inspect_scope = None
        self._recheck.setEnabled(True)
        if error is not None:
            self._status.setText(I18N.tr("pm.package_inspection_failed", "Could not inspect project"))
            self._details.setPlainText(str(error))
            return
        self._plan = plan
        size = self.locale().formattedDataSize(plan.estimated_bytes)
        self._status.setText(I18N.tr("pm.package_estimate", "Estimated size · {size}").format(size=size))
        details = []
        if plan.missing:
            details.append(I18N.tr("pm.package_missing_list", "Missing files"))
            details.extend(f"• {item}" for item in plan.missing)
        if plan.dependencies:
            if details:
                details.append("")
            details.append(I18N.tr("pm.package_dependencies_list", "External dependencies"))
            details.extend(f"• {item}" for item in plan.dependencies)
        if scope == "project":
            details.append(I18N.tr(
                "pm.package_project_note",
                "The recipient will need access to the source media.",
            ))
        elif not details:
            details.append(I18N.tr("pm.package_inspection_ok", "All checked source files are available."))
        self._details.setPlainText("\n".join(details))
        self._update_export_button()

    def _update_export_button(self) -> None:
        self._export.setEnabled(
            self._plan is not None and bool(self._destination.text()) and self._cancel_export is None
        )

    def _start_export(self) -> None:
        if self._plan is None or self._cancel_export is not None:
            return
        missing = self._plan.missing
        if missing:
            reply = QMessageBox.question(
                self,
                I18N.tr("pm.package_incomplete_title", "Export an incomplete package?"),
                I18N.tr(
                    "pm.package_incomplete_message",
                    "{count} files or dependencies could not be collected. Continue with an incomplete package?",
                ).format(count=len(missing)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        project = self._project
        destination = self._destination.text()
        scope = self._scope.currentData()
        zip_output = scope != "project" and self._format.currentData() == "zip"
        export = self._api.export_project_package
        cancel = Event()
        self._cancel_export = cancel
        self._scope.setEnabled(False)
        self._format.setEnabled(False)
        self._choose.setEnabled(False)
        self._recheck.setEnabled(False)
        self._update_export_button()
        self._status.setText(I18N.tr("pm.package_working", "Exporting package…"))
        self._progress.setValue(0)
        self._progress.show()
        job = _PackageJob(lambda progress: export(
            project, destination, scope=scope, zip_output=zip_output, allow_missing=bool(missing),
            expected_missing=missing, progress_callback=progress, cancelled_fn=cancel.is_set,
        ))
        job.signals.progress.connect(self._progress.setValue)
        job.signals.done.connect(self._on_export_done)
        QThreadPool.globalInstance().start(job)

    @pyqtSlot(object, object)
    def _on_export_done(self, result: object, error: Exception | None) -> None:
        self._cancel_export = None
        if isinstance(error, self._api.ProjectPackageCancelled):
            super().reject()
            return
        if error is None:
            self.result_package = result
            self.accept()
            return
        self._progress.hide()
        self._scope.setEnabled(True)
        self._format.setEnabled(self._scope.currentData() != "project")
        self._choose.setEnabled(True)
        self._recheck.setEnabled(True)
        self._cancel.setEnabled(True)
        self._status.setText(I18N.tr("pm.package_export_failed", "Could not export package"))
        self._details.setPlainText(str(error))
        self._update_export_button()

    def reject(self) -> None:
        if self._cancel_export is None:
            super().reject()
        else:
            self._cancel_export.set()
            self._cancel.setEnabled(False)
            self._status.setText(I18N.tr("pm.package_cancelling", "Cancelling export…"))
