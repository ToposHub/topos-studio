"""Asynchronous composition playback through the shared audio pipeline."""

from __future__ import annotations

from threading import Event
from typing import Any

import numpy as np
from PyQt6.QtCore import QObject, QThreadPool, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtMultimedia import QMediaDevices

from src.features.composite.core.comp_audio import sample_boundary, waveform_envelope
from src.features.composite.core.composition_pcm import (
    SAMPLE_RATE, CompositionPCM, composition_audio_layers, render_composition_pcm,
)
from src.shared.playback.topos_audio import ToposAudioPipeline


class _PreviewPCM:
    def __init__(self, samples: np.ndarray) -> None:
        self.samples = samples
        self.duration = len(samples) / SAMPLE_RATE
        self.index = 0

    def seek(self, seconds: float) -> None:
        self.index = min(len(self.samples), max(0, round(seconds * SAMPLE_RATE)))

    def read(self, frames: int) -> np.ndarray:
        end = min(self.index + frames, len(self.samples))
        block = self.samples[self.index:end]
        self.index = end
        return block

    def close(self) -> None:
        pass


class CompositionAudioPreview(QObject):
    failed = pyqtSignal(str)
    _ready = pyqtSignal(object, object)
    _waveforms_ready = pyqtSignal(object, object)
    waveforms_ready = pyqtSignal(object)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._ready.connect(self._accept, Qt.ConnectionType.QueuedConnection)
        self._waveforms_ready.connect(self._accept_waveforms, Qt.ConnectionType.QueuedConnection)
        self._pending: Event | None = None
        self._pending_waveforms: Event | None = None
        self._pipeline: ToposAudioPipeline | None = None
        self._samples: np.ndarray | None = None
        self._device_id = b""
        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._on_outputs_changed)
        self._first = self._position = 0
        self._fps: float = 24.0

    def start(self, graph: dict, project: Any, first: int, last: int,
              frame: int, fps: float) -> None:
        self.stop()
        self._first, self._position, self._fps = first, frame, fps
        pending = self._pending = Event()

        def prepare() -> None:
            result = render_composition_pcm(graph, first, last, fps=fps,
                                            project=project, cancel_check=pending.is_set)
            samples = None
            if result.samples is not None:
                samples = np.rint(np.clip(result.samples.T, -1.0, 1.0) * 32767).astype(np.int16)
            if not pending.is_set():
                try:
                    self._ready.emit(pending, CompositionPCM(samples, result.state, result.detail))
                except RuntimeError:
                    # Closing the page can destroy the queued receiver during decoding.
                    return

        QThreadPool.globalInstance().start(prepare)

    def update_position(self, frame: int, *, seek: bool = False) -> None:
        self._position = frame
        if seek and self._pipeline is not None:
            self._pipeline.seek(self._seconds())

    def stop(self) -> None:
        if self._pending is not None:
            self._pending.set()
            self._pending = None
        self._retire_pipeline()
        self._samples = None

    def _retire_pipeline(self) -> None:
        if self._pipeline is not None:
            self._pipeline.retire()
            self._pipeline = None

    def request_waveforms(self, graph: dict, project: Any) -> None:
        self.clear_waveforms()
        pending = self._pending_waveforms = Event()

        def prepare() -> None:
            envelopes = {}
            for layer_id, layer, _ in composition_audio_layers(graph):
                if pending.is_set():
                    return
                first = max(0, layer.get("in_frame") or 0)
                last = layer.get("out_frame")
                last = graph.get("duration_frames", 0) - 1 if last is None else last
                selected = {**layer, "audio": {**layer["audio"], "mute": False, "solo": False}}
                isolated = {**graph, "layers": {layer_id: selected}, "layer_stacks": {}}
                result = render_composition_pcm(isolated, first, last, project=project,
                                                sample_rate=8000, cancel_check=pending.is_set)
                if result.samples is not None:
                    samples = result.samples.T
                    bucket = max(1, len(samples) // 512)
                    lower, upper = waveform_envelope(samples, bucket)
                    envelopes[layer_id] = (lower, upper)
            if not pending.is_set():
                try:
                    self._waveforms_ready.emit(pending, envelopes)
                except RuntimeError:
                    return

        QThreadPool.globalInstance().start(prepare)

    def clear_waveforms(self) -> None:
        if self._pending_waveforms is not None:
            self._pending_waveforms.set()
            self._pending_waveforms = None
        self.waveforms_ready.emit({})

    @pyqtSlot(object, object)
    def _accept_waveforms(self, pending: Event, envelopes: dict) -> None:
        if self._pending_waveforms is pending and not pending.is_set():
            self._pending_waveforms = None
            self.waveforms_ready.emit(envelopes)

    def _seconds(self) -> float:
        offset = (sample_boundary(self._position, self._fps, SAMPLE_RATE)
                  - sample_boundary(self._first, self._fps, SAMPLE_RATE))
        return max(0, offset) / SAMPLE_RATE

    @pyqtSlot(object, object)
    def _accept(self, pending: Event, result: CompositionPCM) -> None:
        if self._pending is not pending or pending.is_set():
            return
        self._pending = None
        if result.samples is None:
            if result.detail:
                self.failed.emit(f"Audio: {result.state} ({result.detail})")
            return
        self._samples = result.samples
        self._start_pipeline()

    def _start_pipeline(self) -> None:
        if self._samples is None:
            return
        self._device_id = bytes(QMediaDevices.defaultAudioOutput().id())
        pipeline = ToposAudioPipeline(_PreviewPCM(self._samples), start_seconds=self._seconds())
        pipeline.failed.connect(self.failed)
        self._pipeline = pipeline

    @pyqtSlot()
    def _on_outputs_changed(self) -> None:
        if self._samples is None or self._device_id == bytes(QMediaDevices.defaultAudioOutput().id()):
            return
        self._retire_pipeline()
        self._start_pipeline()
