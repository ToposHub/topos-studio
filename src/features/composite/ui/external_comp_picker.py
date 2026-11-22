"""Precomp inspector control for selecting and relinking external packages."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QThreadPool, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QWidget,
)

from src.shared.i18n import I18N
from src.shared.project.resource_resolver import sha256_file
from src.shared.project.topos_project_io import MAX_COMPOSITE_ARCHIVE_BYTES
from src.shared.project.topos_project_read_service import ToposProjectReadService

from ..core.external_comp_refs import CompRef, active_reference_host_archive, read_comp_ref
from ..core.output_targets import output_target_records


class _PackageResult(QObject):
    ready = pyqtSignal(object, object, str)


class ExternalCompPicker(QWidget):
    """Inspect a package on a worker and commit one complete reference."""

    def __init__(
        self, parameters: dict[str, Any], commit: Callable[[dict[str, Any]], None],
        *, parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._commit = commit
        self._pending: tuple[_PackageResult, str, str] | None = None
        self._label = QLabel()
        self._choose = QPushButton(I18N.tr("composite.external.choose", "Link Composition…"))
        self._unlink = QPushButton(I18N.tr("composite.external.unlink", "Unlink"))
        self._choose.clicked.connect(self._browse)
        self._unlink.clicked.connect(self._clear)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._choose)
        layout.addWidget(self._unlink)
        self.refresh(parameters)

    def refresh(self, parameters: dict[str, Any]) -> None:
        try:
            ref = read_comp_ref(parameters)
        except ValueError:
            self._label.setText(I18N.tr("composite.external.invalid", "Invalid external link"))
            self._unlink.setEnabled(True)
            return
        self._label.setText(
            I18N.tr("composite.external.unlinked", "External Composition") if ref is None
            else f"{Path(ref.relative_path).name} · {ref.comp_id}"
        )
        self._unlink.setEnabled(ref is not None)

    def _warning(self, message: str) -> None:
        QMessageBox.warning(self, I18N.tr("composite.external.title", "External Composition"), message)

    @pyqtSlot()
    def _clear(self) -> None:
        self._pending = None
        self._commit({})
        self.refresh({"external_comp_ref": {}})

    @pyqtSlot()
    def _browse(self) -> None:
        host = active_reference_host_archive()
        if not host:
            self._warning(I18N.tr("composite.external.save_first", "Save the host project before adding a link."))
            return
        start_dir = str(Path(host).parent)
        filename, _ = QFileDialog.getOpenFileName(
            self, I18N.tr("composite.external.choose", "Link Composition…"), start_dir, "Topos (*.topos)",
        )
        if not filename:
            return
        try:
            relative_path = Path(os.path.relpath(filename, start_dir)).as_posix()
        except ValueError as exc:
            self._warning(str(exc))
            return
        bridge = _PackageResult()
        bridge.ready.connect(self._package_ready, Qt.ConnectionType.QueuedConnection)
        self._pending = bridge, host, relative_path
        self._choose.setEnabled(False)

        def probe() -> None:
            try:
                path = Path(filename)
                status = path.stat()
                if status.st_size > MAX_COMPOSITE_ARCHIVE_BYTES:
                    raise ValueError("Linked package exceeds the composition archive limit")
                revision = sha256_file(filename)
                package = ToposProjectReadService().load_composites(filename, revision=revision)
                latest = path.stat()
                if package is None or (
                    latest.st_dev, latest.st_ino, latest.st_size,
                    latest.st_mtime_ns, latest.st_ctime_ns,
                ) != (
                    status.st_dev, status.st_ino, status.st_size,
                    status.st_mtime_ns, status.st_ctime_ns,
                ):
                    raise ValueError("Package unreadable or changed while loading")
                data = package[1].get("compositions", {})
                targets = []
                for comp_id, graph in data.items() if isinstance(data, dict) else ():
                    if isinstance(graph, dict):
                        for output in output_target_records(graph.get("nodes", ())):
                            title = graph.get("name") or comp_id
                            label = f"{title} ({comp_id}) · {output['name']}"
                            targets.append((label, comp_id, output["name"]))
                bridge.ready.emit(bridge, (package[0], revision, targets), "")
            except (OSError, ValueError, TypeError) as exc:
                bridge.ready.emit(bridge, None, str(exc))

        QThreadPool.globalInstance().start(probe)

    @pyqtSlot(object, object, str)
    def _package_ready(self, bridge: _PackageResult, payload: Any, error: str) -> None:
        pending, self._pending = self._pending, None
        self._choose.setEnabled(True)
        if (pending is None or pending[0] is not bridge
                or not self.isVisible() or not self.isEnabled()):
            return
        _, host, relative_path = pending
        if host != active_reference_host_archive():
            return
        if error:
            self._warning(error)
            return
        project_id, revision, targets = payload
        if not targets:
            self._warning(I18N.tr("composite.external.no_outputs", "This project has no composition outputs."))
            return
        labels = list(dict.fromkeys(target[0] for target in targets))
        title = I18N.tr("composite.external.title", "External Composition")
        chosen, accepted = QInputDialog.getItem(
            self, title, I18N.tr("composite.external.output", "Composition output"), labels, editable=False,
        )
        if not accepted:
            return
        _, comp_id, output_name = next(target for target in targets if target[0] == chosen)
        pinned = I18N.tr("composite.external.pin", "Pin this revision")
        follow = I18N.tr("composite.external.follow", "Follow latest revision")
        policy, accepted = QInputDialog.getItem(
            self, title, I18N.tr("composite.external.version", "Version policy"), [pinned, follow], editable=False,
        )
        if accepted and host == active_reference_host_archive():
            ref = CompRef(project_id, comp_id, revision if policy == pinned else "",
                          relative_path, "pinned" if policy == pinned else "follow")
            value = {**ref.to_dict(), "output_name": output_name}
            self._commit(value)
            self.refresh({"external_comp_ref": value})
