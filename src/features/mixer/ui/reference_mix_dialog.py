"""Timeline reference registration and output-clock A/B audition."""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Callable
from uuid import uuid4

from PyQt6.QtCore import QSignalBlocker, QTimer
from PyQt6.QtGui import QHideEvent, QShowEvent
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from src.features.edit.core.audio_sync import resolve_audio_source
from src.features.edit.core.project import EditProject
from src.features.edit.core.reference_mix import ReferenceMixRef
from src.features.edit.core.reference_mix_session import (
    PreparedReference, ReferenceCapture, ReferenceMeasurement, ReferenceMixMonitor,
    measure_reference_capture, prepare_reference, reference_from_delivery_manifest, scale_sample,
)
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.mixer.ui.clip_preview_worker import ClipPreviewWorker
from src.shared.i18n.i18n import I18N


def _tr(key: str, default: str, **kwargs: object) -> str:
    return I18N.tr(f"mixer.reference.{key}", default, **kwargs)


class ReferenceMixDialog(QDialog):
    """Reference IDs and anchors commit to history. Audition stays local."""

    def __init__(
        self,
        context: Callable[[], tuple[EditProject | None, TimelineSequence | None, ReferenceMixMonitor | None]],
        commit: Callable[[str, str, ReferenceMixRef | None], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_tr("title", "Reference Mix A/B"))
        self.resize(520, 300)
        self._context = context
        self._commit = commit
        self._project: EditProject | None = None
        self._sequence: TimelineSequence | None = None
        self._monitor: ReferenceMixMonitor | None = None
        self._context_id: tuple[str, str] | None = None
        self._references: tuple[ReferenceMixRef, ...] = ()
        self._asset_list: tuple[tuple[str, str], ...] = ()
        self._source_identity: tuple[str, float] | None = None
        self._prepared_ref: ReferenceMixRef | None = None
        self._pending: tuple | None = None
        self._capture: ReferenceCapture | None = None
        self._measurement: ReferenceMeasurement | None = None
        self._committing = False
        self._worker = ClipPreviewWorker()
        self._worker.preview_ready.connect(self._on_job_ready)
        self._worker.preview_failed.connect(self._on_job_failed)
        self.destroyed.connect(self._worker.finish)
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._poll)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.reference_list = QComboBox(self)
        self.reference_list.currentIndexChanged.connect(self._on_reference_changed)
        form.addRow(_tr("reference", "Saved reference"), self.reference_list)
        self.asset_list = QComboBox(self)
        form.addRow(_tr("media_pool", "Media Pool audio"), self.asset_list)
        self.label = QLineEdit(self)
        form.addRow(_tr("label", "Name"), self.label)
        self.source_in = QDoubleSpinBox(self)
        self.source_in.setDecimals(0)
        self.source_in.setRange(0, 2**53 - 1)
        form.addRow(_tr("source_in", "Source in-point (source samples)"), self.source_in)
        self.anchor = QDoubleSpinBox(self)
        self.anchor.setDecimals(0)
        self.anchor.setRange(-(2**53 - 1), 2**53 - 1)
        form.addRow(_tr("anchor", "Timeline anchor (timeline samples)"), self.anchor)
        layout.addLayout(form)

        edit_actions = QHBoxLayout()
        self.save_button = QPushButton(_tr("save", "Save reference"), self)
        self.save_button.clicked.connect(self._save_reference)
        edit_actions.addWidget(self.save_button)
        self.remove_button = QPushButton(_tr("remove", "Remove reference"), self)
        self.remove_button.clicked.connect(self._remove_reference)
        edit_actions.addWidget(self.remove_button)
        self.bwf_button = QPushButton(_tr("bwf", "Use BWF time reference"), self)
        self.bwf_button.clicked.connect(self._use_bwf_anchor)
        edit_actions.addWidget(self.bwf_button)
        layout.addLayout(edit_actions)
        self.print_button = QPushButton(_tr("register_print", "Register verified Full Mix"), self)
        self.print_button.setToolTip(_tr("register_print_tip", "Read delivery.json next to the imported WAV."))
        self.print_button.clicked.connect(self._register_print)
        layout.addWidget(self.print_button)

        audition = QHBoxLayout()
        self.a_button = QPushButton(_tr("programme", "A · Programme"), self)
        self.b_button = QPushButton(_tr("audition_reference", "B · Reference"), self)
        for button in (self.a_button, self.b_button):
            button.setCheckable(True)
            audition.addWidget(button)
        self.a_button.clicked.connect(self._select_programme)
        self.b_button.clicked.connect(self._select_reference)
        self.measure_button = QPushButton(_tr("measure", "Measure 3 s"), self)
        self.measure_button.clicked.connect(self._measure)
        audition.addWidget(self.measure_button)
        layout.addLayout(audition)

        adjustment = QHBoxLayout()
        self.match_button = QPushButton(_tr("match", "Match loudness"), self)
        self.match_button.setCheckable(True)
        self.match_button.clicked.connect(self._match)
        adjustment.addWidget(self.match_button)
        self.align_button = QPushButton(_tr("align", "Apply suggested anchor"), self)
        self.align_button.clicked.connect(self._align)
        adjustment.addWidget(self.align_button)
        layout.addLayout(adjustment)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.report = QLabel(self)
        self.report.setWordWrap(True)
        layout.addWidget(self.report)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(_tr("close", "Close"))
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self.asset_list.currentIndexChanged.connect(self._update_buttons)
        self._update_buttons()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.sync_context()
        reference = self._selected_reference()
        if reference is not None and self._prepared_ref != reference and self._pending is None:
            self._prepare(reference, save=False)
        self._timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        self._invalidate_measurement()
        if self._monitor is not None:
            self._monitor.select_reference(False)
        if self._pending is not None:
            self._pending = None
            self._worker.cancel()
        super().hideEvent(event)

    def reset_session(self) -> None:
        self._worker.cancel()
        self._pending = None
        self._prepared_ref = None
        self._source_identity = None
        self._invalidate_measurement()
        if self._monitor is not None:
            self._monitor.clear()

    def sync_context(self) -> None:
        if self._committing:
            return
        project, sequence, monitor = self._context()
        identity = (project.project_id, sequence.timeline_id) if project and sequence else None
        changed = identity != self._context_id or monitor is not self._monitor
        project_changed = project is not self._project
        sequence_changed = sequence is not self._sequence
        if not changed and not project_changed and not sequence_changed:
            return
        assets_changed = project_changed and (project is None or self._project is None
                                              or project.assets is not self._project.assets)
        if changed:
            self.reset_session()
        elif sequence and self._sequence and sequence.revision != self._sequence.revision:
            self._invalidate_measurement()
        self._project, self._sequence, self._monitor = project, sequence, monitor
        self._context_id = identity
        entries = tuple(ref for ref in sequence.reference_mixes if isinstance(ref, ReferenceMixRef)) if sequence else ()
        assets = self._asset_list
        if changed or assets_changed:
            assets = (tuple(sorted((asset.display_name, asset.asset_id)
                                   for asset in project.assets.values() if asset.is_audio))
                      if project else ())
        if changed or assets != self._asset_list:
            previous_asset = self.asset_list.currentData() if not changed else None
            with QSignalBlocker(self.asset_list):
                self.asset_list.clear()
                for name, asset_id in assets:
                    self.asset_list.addItem(name, asset_id)
                index = self.asset_list.findData(previous_asset)
                if index >= 0:
                    self.asset_list.setCurrentIndex(index)
            self._asset_list = assets
        if changed or entries != self._references:
            selected = self.reference_list.currentData() if not changed else None
            self._references = entries
            self._refresh_reference_list(selected)
            if self._selected_reference() != self._prepared_ref or changed:
                self._on_reference_changed()
        if self._prepared_ref is not None and self._resolve_current_source(self._prepared_ref) != self._source_identity:
            ref = self._prepared_ref
            self.reset_session()
            if self.isVisible():
                self._prepare(ref, save=False)
        self._update_buttons()

    def _refresh_reference_list(self, selected: str | None) -> None:
        with QSignalBlocker(self.reference_list):
            self.reference_list.clear()
            self.reference_list.addItem(_tr("new", "New reference…"), None)
            for reference in self._references:
                self.reference_list.addItem(reference.label or reference.asset_id, reference.version_id)
            index = self.reference_list.findData(selected)
            if index <= 0 and selected is None and self._references:
                index = 1
            self.reference_list.setCurrentIndex(max(0, index))

    def _selected_reference(self) -> ReferenceMixRef | None:
        selected = self.reference_list.currentData()
        return next((ref for ref in self._references if ref.version_id == selected), None)

    def _resolve_current_source(self, reference: ReferenceMixRef) -> tuple[str, float] | None:
        asset = self._project.assets.get(reference.asset_id) if self._project else None
        if asset is None or not asset.is_audio or asset.is_offline:
            return None
        return resolve_audio_source(self._project, asset)

    def _on_reference_changed(self) -> None:
        self.reset_session()
        reference = self._selected_reference()
        if reference is not None and self._sequence is not None:
            self.asset_list.setCurrentIndex(self.asset_list.findData(reference.asset_id))
            self.label.setText(reference.label)
            self.source_in.setValue(reference.source_start_sample)
            self.anchor.setValue(scale_sample(reference.timeline_anchor_sample,
                                              reference.timeline_rate, self._sequence.sample_rate))
            if self.isVisible():
                self._prepare(reference, save=False)
        else:
            self.label.clear()
            self.source_in.setValue(0)
            self.anchor.setValue(0)
            self.status.setText(_tr("choose_audio", "Choose an audio asset from the Media Pool."))
        self._update_buttons()

    def _save_reference(self) -> None:
        self._commit_draft(verified_print=False)

    def _register_print(self) -> None:
        self._commit_draft(verified_print=True)

    def _commit_draft(self, *, verified_print: bool) -> None:
        asset_id = self.asset_list.currentData()
        if not asset_id or self._sequence is None:
            return
        selected = self._selected_reference()
        asset = self._project.assets.get(asset_id) if self._project else None
        if asset is None:
            return
        same_asset = selected is not None and selected.asset_id == asset_id
        new_version = selected is not None and selected.mix_version is not None and (verified_print or not same_asset)
        if same_asset and not new_version:
            reference = replace(selected, label=self.label.text().strip() or asset.display_name,
                                source_start_sample=int(self.source_in.value()),
                                timeline_anchor_sample=int(self.anchor.value()),
                                timeline_rate=self._sequence.sample_rate)
        else:
            reference = ReferenceMixRef(selected.version_id if selected and not new_version else str(uuid4()), asset_id,
                                        int(self.source_in.value()), int(self.anchor.value()),
                                        label=self.label.text().strip() or asset.display_name,
                                        provenance="media_pool", timeline_rate=self._sequence.sample_rate)
        self._prepare(reference, save=True, verified_print=verified_print)

    def _remove_reference(self) -> None:
        reference = self._selected_reference()
        if reference is not None and self._sequence is not None:
            if self._commit(self._sequence.timeline_id, reference.version_id, None):
                self.sync_context()

    def _prepare(self, reference: ReferenceMixRef, *, save: bool, verified_print: bool = False) -> None:
        source = self._resolve_current_source(reference)
        if source is None:
            self.status.setText(_tr("offline", "Reference media is missing or offline."))
            return
        if self._monitor is None or self._sequence is None or self._context_id is None:
            self.status.setText(_tr("no_engine", "The timeline audio engine is unavailable."))
            return
        if self._pending is not None:
            self._worker.cancel()
        from src.features.edit.core.native_mix_layout import mix_layout

        configured_layout = getattr(self._sequence, "master_audio_layout", "")
        fallback_channels = mix_layout(configured_layout).channel_count if configured_layout else 2
        rate, channels = self._monitor.render_format or (self._sequence.sample_rate, fallback_channels)
        key = ("reference", self._context_id, str(uuid4()))
        self._pending = key
        self.status.setText(_tr("preparing", "Preparing reference audio…"))
        self._update_buttons()
        path, offset = source
        unknown_rate = save and (self._selected_reference() is None
                                 or self._selected_reference().asset_id != reference.asset_id)

        def job(cancelled: Callable[[], bool]) -> tuple | None:
            candidate = reference_from_delivery_manifest(path, reference) if verified_print else reference
            prepared = prepare_reference(path, candidate, rate, channels, offset_seconds=offset,
                                         allow_unknown_source_rate=unknown_rate and not verified_print,
                                         cancel_check=cancelled)
            return (key, (candidate, prepared, source, save)) if prepared is not None else None

        self._worker.submit_cancellable(key, job)

    def _on_job_ready(self, result: tuple) -> None:
        key, payload = result
        self.sync_context()
        if key != self._pending or key[1] != self._context_id or self._monitor is None:
            return
        self._pending = None
        if key[0] == "measure":
            measurement = payload
            source = self._monitor.source
            if (source is not None and measurement.match_gain is not None
                    and source.sample_peak * measurement.match_gain > 10.0 ** (-1.0 / 20.0)):
                measurement = replace(measurement, match_gain=None)
            self._measurement = measurement
            self._show_measurement(measurement)
        else:
            reference, prepared, source, save = payload
            assert isinstance(prepared, PreparedReference)
            if self._resolve_current_source(reference) != source:
                self.status.setText(_tr("offline", "Reference source changed during preparation."))
                self._update_buttons()
                return
            if save:
                reference = replace(reference, original_rate=prepared.source_rate)
                self._committing = True
                try:
                    committed = self._commit(key[1][1], reference.version_id, reference)
                finally:
                    self._committing = False
                if not committed:
                    self.status.setText(_tr("rejected", "Could not save the reference."))
                    self._update_buttons()
                    return
                _project, canonical, _monitor = self._context()
                if canonical is not None and canonical.timeline_id == key[1][1]:
                    self._references = tuple(ref for ref in canonical.reference_mixes
                                             if isinstance(ref, ReferenceMixRef))
                self._refresh_reference_list(reference.version_id)
                if canonical is not None:
                    self.anchor.setValue(scale_sample(reference.timeline_anchor_sample,
                                                      reference.timeline_rate, canonical.sample_rate))
            self._invalidate_measurement()
            self._prepared_ref = reference
            self._source_identity = source
            self._monitor.set_source(prepared)
            self.sync_context()
            ready = _tr("ready", "Ready · {rate} Hz · {channels} playback channels",
                        rate=prepared.sample_rate, channels=prepared.channels)
            if reference.mix_version is not None:
                ready += " · " + _tr("print_revision", "Verified print, revision {revision}",
                                    revision=reference.mix_version.source_revision)
            self.status.setText(ready)
        self._update_buttons()

    def _on_job_failed(self, key: tuple, error: str) -> None:
        if key == self._pending:
            self._pending = None
            self.status.setText(_tr("failed", "Reference operation failed") + " · " + error)
            self._update_buttons()

    def _select_programme(self) -> None:
        if self._monitor is not None:
            self._monitor.select_reference(False)
        self._update_buttons()

    def _select_reference(self) -> None:
        if self._monitor is not None:
            self._monitor.select_reference(True)
        self._update_buttons()

    def _use_bwf_anchor(self) -> None:
        source = self._monitor.source if self._monitor else None
        if source and self._sequence and source.bwf_time_reference is not None and self._prepared_ref is not None:
            self.anchor.setValue(scale_sample(source.bwf_time_reference + int(self.source_in.value()),
                                              source.source_rate, self._sequence.sample_rate))

    def _measure(self) -> None:
        if not self._monitor:
            return
        self._invalidate_measurement()
        self._capture = self._monitor.start_measurement()
        self.status.setText(_tr("measure_pending", "Play the timeline in Programme or SIP to measure three seconds."))
        self._update_buttons()

    def _invalidate_measurement(self) -> None:
        if self._pending is not None and self._pending[0] == "measure":
            self._worker.cancel()
            self._pending = None
        if self._monitor is not None:
            self._monitor.cancel_measurement()
            self._monitor.set_match_gain(1.0)
        self._capture = None
        self._measurement = None
        self.report.clear()

    def _show_measurement(self, measurement: ReferenceMeasurement) -> None:
        unknown = _tr("unknown", "Unavailable")
        programme = (f"{measurement.programme_lufs:.1f} LUFS"
                     if measurement.programme_lufs is not None else unknown)
        reference = (f"{measurement.reference_lufs:.1f} LUFS"
                     if measurement.reference_lufs is not None else unknown)
        match_gain = (f"{20 * math.log10(measurement.match_gain):+.1f} dB"
                      if measurement.match_gain is not None else unknown)
        self.status.setText(_tr("measured", "Measured samples {start}–{end} at {rate} Hz",
                                start=measurement.start_sample,
                                end=measurement.start_sample + measurement.sample_count,
                                rate=measurement.sample_rate))
        summary = _tr("measurement", "A {programme} · B {reference} · Match {gain}",
                      programme=programme, reference=reference, gain=match_gain)
        if measurement.estimated_delay is not None:
            summary += "\n" + _tr("delay", "Reference delay {samples:+d} samples · correlation {confidence:.2f}",
                                   samples=measurement.estimated_delay,
                                   confidence=measurement.confidence or 0.0)
        self.report.setText(summary)

    def _match(self, checked: bool) -> None:
        if self._monitor is not None and self._measurement and self._measurement.match_gain:
            self._monitor.set_match_gain(self._measurement.match_gain if checked else 1.0)
        self._update_buttons()

    def _align(self) -> None:
        measurement = self._measurement
        reference = self._prepared_ref
        if measurement and reference and self._sequence and self._can_align():
            anchor = scale_sample(reference.timeline_anchor_sample, reference.timeline_rate,
                                  self._sequence.sample_rate)
            correction = scale_sample(measurement.estimated_delay or 0, measurement.sample_rate,
                                      self._sequence.sample_rate)
            self.anchor.setValue(anchor - correction)
            self._prepare(replace(reference, timeline_anchor_sample=anchor - correction,
                                  timeline_rate=self._sequence.sample_rate), save=True)

    def _can_align(self) -> bool:
        value = self._measurement
        return bool(value and value.estimated_delay and value.confidence and value.confidence >= 0.75)

    def _poll(self) -> None:
        self.sync_context()
        monitor = self._monitor
        if monitor is None:
            return
        source = monitor.source
        if (source and monitor.render_format is not None and self._prepared_ref
                and self._pending is None and (source.sample_rate, source.channels) != monitor.render_format):
            reference = self._prepared_ref
            self.reset_session()
            self._prepare(reference, save=False)
        capture = self._capture
        if capture is None:
            return
        captured = monitor.measurement_result(capture)
        if captured is None:
            return
        self._capture = None
        monitor.cancel_measurement()
        if isinstance(captured, str):
            reason = (_tr("range_error", "Choose an interval covered by the reference.")
                      if captured == "reference_range" else
                      _tr("transport_error", "Measurement interrupted by playback or device changes."))
            self.status.setText(reason)
            self._update_buttons()
            return
        key = ("measure", self._context_id, str(uuid4()))
        self._pending = key
        self.status.setText(_tr("measuring", "Measuring loudness and alignment…"))
        self._update_buttons()

        def job(cancelled: Callable[[], bool]) -> tuple | None:
            measurement = measure_reference_capture(captured, cancelled)
            return (key, measurement) if measurement is not None else None

        self._worker.submit_cancellable(key, job)

    def _update_buttons(self) -> None:
        monitor = self._monitor
        source = monitor.source if monitor is not None else None
        busy = self._pending is not None or self._capture is not None
        ref_selected = bool(monitor and monitor.reference_selected)
        self.asset_list.setEnabled(not busy and bool(self._asset_list))
        self.label.setEnabled(not busy)
        self.source_in.setEnabled(not busy)
        self.anchor.setEnabled(not busy)
        self.save_button.setEnabled(not busy and bool(self.asset_list.currentData() and monitor))
        self.print_button.setEnabled(self.save_button.isEnabled())
        self.remove_button.setEnabled(not busy and self._selected_reference() is not None)
        self.bwf_button.setEnabled(
            not busy and source is not None and source.bwf_time_reference is not None
            and self._prepared_ref is not None and self._prepared_ref.asset_id == self.asset_list.currentData())
        self.a_button.setChecked(not ref_selected)
        self.b_button.setChecked(ref_selected)
        self.b_button.setEnabled(source is not None)
        self.measure_button.setEnabled(not busy and source is not None)
        self.match_button.setEnabled(not busy and bool(self._measurement and self._measurement.match_gain))
        self.match_button.setChecked(bool(monitor and monitor.match_gain != 1.0))
        self.align_button.setEnabled(not busy and self._can_align())
