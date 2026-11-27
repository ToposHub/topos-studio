"""Scan and edit an explicitly registered VST3 insert in the FX rack."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from src.features.edit.core.hosted_vst3 import (
    HostedVst3Params, backend_issue, inspect_plugin, scan_vst3,
)
from src.features.mixer.ui.clip_preview_worker import ClipPreviewWorker
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import apply_float_panel


class Vst3InsertDialog(QDialog):
    """Plugin inspection and processing probes always run in a child process."""

    def __init__(self, sample_rate: float, parent: QWidget,
                 initial: HostedVst3Params | None = None) -> None:
        super().__init__(parent)
        apply_float_panel(self)
        self.setWindowTitle(I18N.tr("audio.fx.vst3.title", "VST3 plug-in"))
        self.resize(440, 460)
        self._initial = initial
        self._sample_rate = sample_rate
        self._worker = ClipPreviewWorker()
        self._worker.preview_ready.connect(self._ready)
        self._worker.preview_failed.connect(self._failed)
        self.finished.connect(self._finish)
        self._job: tuple | None = None
        self._available: tuple[HostedVst3Params, ...] = ()
        self._values: dict[str, QDoubleSpinBox] = {}
        self._changed: set[str] = set()
        self._validated: HostedVst3Params | None = None
        self.result_params: HostedVst3Params | None = None

        root = QVBoxLayout(self)
        picker = QHBoxLayout()
        self._scan = QPushButton(I18N.tr("audio.fx.vst3.scan", "Choose VST3 file…"))
        self._scan.clicked.connect(self._select_plugin)
        picker.addWidget(self._scan)
        self._scan_dir = QPushButton(I18N.tr("audio.fx.vst3.scan_dir", "Choose VST3 bundle…"))
        self._scan_dir.clicked.connect(self._select_bundle)
        picker.addWidget(self._scan_dir)
        root.addLayout(picker)
        self._components = QComboBox()
        self._components.currentIndexChanged.connect(self._preview_component)
        root.addWidget(self._components)
        self._status = QLabel()
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        root.addWidget(self._scroll, 1)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._apply)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)
        self._set_busy(False)

        if issue := backend_issue():
            self._status.setText(issue)
        elif initial is not None:
            self._set_components((initial,))
        else:
            self._status.setText(I18N.tr(
                "audio.fx.vst3.help", "Choose an installed stereo, zero-latency VST3 effect."
            ))

    def _finish(self, _code: int) -> None:
        self._job = None
        self._worker.finish()

    def _set_busy(self, busy: bool) -> None:
        self._scan.setEnabled(not busy and not backend_issue())
        self._scan_dir.setEnabled(not busy and not backend_issue())
        self._components.setEnabled(not busy and self._components.count() > 1)
        self._scroll.setEnabled(not busy)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            not busy and self._validated is not None
        )

    def _new_job(self, stage: str) -> tuple:
        self._job = (stage, uuid4().hex)
        self._status.setText(I18N.tr("audio.fx.vst3.checking", "Checking plug-in in a separate process…"))
        self._set_busy(True)
        return self._job

    def _select_plugin(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, I18N.tr("audio.fx.vst3.pick", "Select VST3 plug-in"), "", "VST3 (*.vst3)"
        )
        self._scan_path(path)

    def _select_bundle(self) -> None:
        self._scan_path(QFileDialog.getExistingDirectory(
            self, I18N.tr("audio.fx.vst3.pick", "Select VST3 plug-in")
        ))

    def _scan_path(self, path: str) -> None:
        if not path:
            return
        key = self._new_job("scan")
        self._worker.submit_cancellable(key, lambda cancel: (key, scan_vst3(path, cancel)))

    def _set_components(self, configs: tuple[HostedVst3Params, ...]) -> None:
        self._available = configs
        previous = self._components.blockSignals(True)
        self._components.clear()
        for config in configs:
            self._components.addItem(config.plugin_name)
        self._components.blockSignals(previous)
        self._preview_component(self._components.currentIndex())

    def _preview_component(self, index: int) -> None:
        if not 0 <= index < len(self._available):
            return
        config = self._available[index]
        rate = self._sample_rate
        self._validated = None
        key = self._new_job("inspect")
        self._worker.submit_cancellable(key, lambda cancel: (key, config, inspect_plugin(config, rate, cancel)))

    def _failed(self, key: tuple, reason: str) -> None:
        if key != self._job:
            return
        self._job = None
        self._status.setText(reason)
        self._set_busy(False)

    def _ready(self, response: tuple) -> None:
        key = response[0]
        if key != self._job:
            return
        self._job = None
        if key[0] == "scan":
            candidates = response[1]
            if self._initial is not None:
                if not any(
                    item.fingerprint == self._initial.fingerprint and item.plugin_name == self._initial.plugin_name
                    for item in candidates
                ):
                    self._status.setText(I18N.tr(
                        "audio.fx.vst3.mismatch", "The selected bundle differs from the saved plug-in build."
                    ))
                    self._set_busy(False)
                    return
                candidates = (self._initial,)
            self._set_components(candidates)
            return
        config = response[1]
        if key[0] == "apply":
            self.result_params = config
            self.accept()
            return
        self._validated = config
        self._build_controls(response[2])
        self._status.setText(I18N.tr(
            "audio.fx.vst3.normalized", "Parameter values use the normalized 0–1 range."
        ))
        self._set_busy(False)

    def _build_controls(self, fields: tuple[dict, ...]) -> None:
        widget = QWidget(self._scroll)
        form = QFormLayout(widget)
        self._values = {}
        self._changed.clear()
        for field in fields:
            key = field["id"]
            control = QDoubleSpinBox()
            control.setRange(0.0, 1.0)
            control.setDecimals(6)
            control.setSingleStep(.01)
            control.setValue(field["value"])
            control.setToolTip(field["display"])
            control.valueChanged.connect(lambda _value, key=key: self._changed.add(key))
            form.addRow(field["label"], control)
            self._values[key] = control
        self._scroll.setWidget(widget)

    def _apply(self) -> None:
        if self._validated is None or self._job is not None:
            return
        values = dict(self._validated.parameters)
        values.update((key, self._values[key].value()) for key in self._changed)
        config = replace(self._validated, parameters=tuple(sorted(values.items())))
        rate = self._sample_rate
        key = self._new_job("apply")
        self._worker.submit_cancellable(key, lambda cancel: (key, config, inspect_plugin(config, rate, cancel)))
