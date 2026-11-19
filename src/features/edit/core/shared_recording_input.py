"""A device capture shared by the recording tracks in one session."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from PyQt6.QtCore import QIODevice
    from PyQt6.QtMultimedia import QAudio, QAudioDevice, QAudioFormat, QAudioSource

logger = logging.getLogger(__name__)

PcmReceiver = Callable[[np.ndarray], None]
ErrorReceiver = Callable[[str], None]


class RecordingInputError(RuntimeError):
    """A selected capture source cannot provide the requested input."""


class SharedRecordingInput:
    """Open one Qt input and route its numbered channels to armed tracks.

    The owner reuses the same object for all tracks patched to one device.
    Subscribe and unsubscribe on the Qt object thread. The first subscriber
    starts capture and the last subscriber releases the audio device.
    """

    def __init__(self, device_id: str | None, channel_count: int, sample_rate: int = 48000) -> None:
        if channel_count < 1 or sample_rate < 1:
            raise ValueError("Input channel count and sample rate must be positive")
        self.device_id = device_id
        self.channel_count = channel_count
        self.sample_rate = sample_rate
        self._subscribers: dict[object, tuple[tuple[int, ...], PcmReceiver, ErrorReceiver]] = {}
        self._source: QAudioSource | None = None
        self._io: QIODevice | None = None
        self._source_channels = channel_count
        self._pending_bytes = b""

    def validate_channels(self, channels: tuple[int, ...]) -> None:
        """Reject invalid patches before starting a device."""
        if (not channels
                or any(not isinstance(channel, int) or not 0 <= channel < self.channel_count
                       for channel in channels)
                or len(channels) != len(set(channels))):
            raise ValueError("Input channels must be unique and within the available range")

    @staticmethod
    def available_devices() -> tuple[tuple[str, str, int], ...]:
        """List the IDs, display names and maximum channel counts of input devices."""
        from PyQt6.QtMultimedia import QMediaDevices

        return tuple((bytes(device.id()).decode("utf-8", "replace"),
                      device.description(), device.maximumChannelCount())
                     for device in QMediaDevices.audioInputs())

    def monitor(
        self, track_id: str, channel_indices: tuple[int, ...],
        on_level: Callable[[float], None], on_error: ErrorReceiver | None = None,
    ) -> bool:
        """Publish input peak in dBFS while the track is armed."""
        def receive_pcm(pcm: np.ndarray) -> None:
            peak = float(np.max(np.abs(pcm)))
            on_level(float(20 * np.log10(max(peak, 1e-9))))

        def receive_error(message: str) -> None:
            if on_error is not None:
                on_error(message)
            else:
                logger.warning("%s", message)

        try:
            self.subscribe(("monitor", track_id), channel_indices, receive_pcm, receive_error)
        except (ImportError, RuntimeError, ValueError) as exc:
            receive_error(str(exc))
            return False
        return True

    def unmonitor(self, track_id: str) -> None:
        self.unsubscribe(("monitor", track_id))

    def subscribe(
        self, owner: object, channels: tuple[int, ...], receive_pcm: PcmReceiver,
        receive_error: ErrorReceiver,
    ) -> None:
        self.validate_channels(channels)
        if not self._subscribers:
            self._open_device()
        self._subscribers[owner] = (channels, receive_pcm, receive_error)

    def unsubscribe(self, owner: object) -> None:
        if self._subscribers.pop(owner, None) is not None and not self._subscribers:
            self._close_device()

    def _capture_format(self, device: QAudioDevice) -> QAudioFormat:
        from PyQt6.QtMultimedia import QAudioFormat

        maximum = device.maximumChannelCount()
        if self.channel_count > maximum:
            raise RecordingInputError(
                f"{device.description()} has only {maximum} input channels"
            )
        fmt = QAudioFormat()
        fmt.setSampleRate(self.sample_rate)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
        # Some stereo and multichannel devices only open in their native width.
        # Prefer their advertised width to synthesized narrower channel layouts.
        preferred = device.preferredFormat().channelCount()
        for count in dict.fromkeys((preferred, maximum, self.channel_count)):
            if self.channel_count <= count <= maximum:
                fmt.setChannelCount(count)
                if device.isFormatSupported(fmt):
                    return fmt
        raise RecordingInputError(
            f"{device.description()} doesn't support {self.sample_rate} Hz float capture "
            f"with at least {self.channel_count} input channels"
        )

    def _open_device(self) -> None:
        from PyQt6.QtMultimedia import QAudio, QAudioSource, QMediaDevices

        if self.device_id is None:
            device = QMediaDevices.defaultAudioInput()
        else:
            device = next(
                (candidate for candidate in QMediaDevices.audioInputs()
                 if bytes(candidate.id()).decode("utf-8", "replace") == self.device_id),
                None,
            )
        if device is None or device.isNull():
            raise RecordingInputError("The selected recording input is unavailable")
        fmt = self._capture_format(device)
        source = QAudioSource(device, fmt)
        self._source = source
        try:
            io = source.start()
            if io is None or source.error() != QAudio.Error.NoError:
                raise RecordingInputError(
                    f"Could not open recording input {device.description()} ({source.error().name})"
                )
            self._io = io
            self._source_channels = fmt.channelCount()
            source.stateChanged.connect(lambda state: self._on_source_state_changed(source, state))
            io.readyRead.connect(self._on_audio_ready)
        except (RuntimeError, TypeError):
            self._close_device()
            raise

    def _close_device(self) -> None:
        source, self._source = self._source, None
        io, self._io = self._io, None
        self._pending_bytes = b""
        if source is not None:
            if io is not None:
                try:
                    io.readyRead.disconnect(self._on_audio_ready)
                except (RuntimeError, TypeError):
                    logger.debug("Recording input read signal was already disconnected")
            try:
                source.stop()
                source.deleteLater()
            except RuntimeError:
                logger.debug("Recording input was already closed")

    def _on_source_state_changed(self, source: QAudioSource, state: QAudio.State) -> None:
        from PyQt6.QtMultimedia import QAudio

        if source is self._source and state == QAudio.State.StoppedState:
            self._fail_subscribers(f"Recording input stopped ({source.error().name})")

    def _on_audio_ready(self) -> None:
        if self._io is None:
            return
        try:
            data = bytes(self._io.readAll())
        except RuntimeError as exc:
            self._fail_subscribers(f"Could not read recording input: {exc}")
            return
        self._dispatch_bytes(data)

    def _dispatch_bytes(self, data: bytes) -> None:
        data = self._pending_bytes + data
        bytes_per_sample = np.dtype(np.float32).itemsize
        bytes_per_frame = self._source_channels * bytes_per_sample
        complete = len(data) - len(data) % bytes_per_frame
        self._pending_bytes = data[complete:]
        if not complete:
            return
        pcm = np.frombuffer(data, dtype=np.float32, count=complete // bytes_per_sample)
        frames = pcm.reshape(-1, self._source_channels).T
        for owner, subscription in tuple(self._subscribers.items()):
            if self._subscribers.get(owner) is not subscription:
                continue
            channels, receive_pcm, receive_error = subscription
            try:
                receive_pcm(np.ascontiguousarray(frames[channels, :]))
            except (OSError, ValueError, RuntimeError) as exc:
                self.unsubscribe(owner)
                self._notify_error(receive_error, f"Could not process recording input: {exc}")

    def _fail_subscribers(self, message: str) -> None:
        subscribers = tuple(self._subscribers.values())
        self._subscribers.clear()
        self._close_device()
        for _channels, _receive_pcm, receive_error in subscribers:
            self._notify_error(receive_error, message)

    @staticmethod
    def _notify_error(receive_error: ErrorReceiver, message: str) -> None:
        try:
            receive_error(message)
        except Exception:
            # An application callback must not prevent the other tracks from
            # receiving the device failure and finalizing their partial takes.
            logger.exception("Recording input failure callback raised")
