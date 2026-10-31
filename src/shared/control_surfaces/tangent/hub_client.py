"""Asynchronous native client for the local Tangent Hub service."""

from __future__ import annotations

from pathlib import Path
import logging
import socket
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

from .protocol import (
    FRAME_HEADER_SIZE,
    MAX_FRAME_SIZE,
    FromHubCommand,
    PanelInfo,
    TangentCommand,
    decode_message,
    encode_all_change,
    encode_application_definition,
    encode_parameter_value,
)

logger = logging.getLogger(__name__)


class TangentHubClient(QObject):
    """Connect to Tangent Hub without blocking the Qt UI thread.

    The client only owns the managed TCP connection.  It never touches widgets
    or OpenGL objects; consumers receive Qt signals and apply changes on the UI
    thread.  A failed optional Hub connection therefore cannot prevent Topos
    from starting.

    Hub being absent is a normal configuration, not a fault: after a short
    grace period of failed attempts the client reports ``unavailable`` once,
    then only probes the port occasionally in the background without logging
    warnings.  Installing/starting Hub later (or calling ``wake()``) resumes
    the normal fast path.
    """

    #: Failed connect attempts before Hub is considered absent.
    UNAVAILABLE_AFTER_FAILURES = 4
    #: Seconds between silent background probes once Hub is absent.
    UNAVAILABLE_PROBE_DELAY = 30.0

    connected = pyqtSignal()
    disconnected = pyqtSignal()
    unavailable = pyqtSignal()
    panels_received = pyqtSignal(object)
    parameter_change_received = pyqtSignal(int, float)
    # Preserve the legacy signal while exposing the worker-side receive time
    # for integrations that need to pair interleaved X/Y motion events.
    # ``object`` is intentional: a monotonic nanosecond timestamp is larger
    # than Qt's signed 32-bit ``int`` signal type on long-running systems.
    parameter_change_received_at = pyqtSignal(int, float, object)
    parameter_reset_received = pyqtSignal(int)
    parameter_value_requested = pyqtSignal(int)
    action_on_received = pyqtSignal(int)
    action_off_received = pyqtSignal(int)
    error = pyqtSignal(str)

    def __init__(
        self,
        application_name: str,
        system_path: str | Path,
        user_path: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int = 64246,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.application_name = application_name
        self.system_path = str(Path(system_path).resolve())
        self.user_path = str(Path(user_path).resolve()) if user_path else None
        self.host = host
        self.port = port

        self._socket: socket.socket | None = None
        self._socket_lock = threading.Lock()
        self._stop_event = threading.Event()
        # Wakes an in-progress retry/probe wait so stop() joins promptly and
        # wake() can trigger an immediate reconnect attempt.
        self._reconnect_now = threading.Event()
        self._thread: threading.Thread | None = None
        self._reconnect_min_delay = 0.5
        self._reconnect_max_delay = 5.0
        self._consecutive_failures = 0

    @property
    def is_running(self) -> bool:
        """Whether the connection worker is currently active."""

        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the connection worker if it is not already running."""

        if self.is_running:
            return
        self._consecutive_failures = 0
        self._reconnect_now.clear()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="topos-tangent-hub",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the worker and close the socket within a bounded interval."""

        self._stop_event.set()
        self._reconnect_now.set()
        with self._socket_lock:
            current_socket = self._socket
            self._socket = None
        if current_socket is not None:
            try:
                current_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                current_socket.close()
            except OSError:
                pass

        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
            if thread.is_alive():
                # AUD-32801: the bounded join timed out — the worker is stuck
                # in a call that cannot be interrupted (e.g. a blocking recv
                # on a half-dead socket). Keep the reference so ``is_running``
                # stays truthful: an immediate ``start()`` must not misjudge
                # the client as stopped and spawn a second worker alongside
                # the surviving one (duplicate connections / doubled signals,
                # the integration silently misbehaving). The worker re-checks
                # ``_stop_event`` on every loop iteration, so it exits as soon
                # as the blocking call returns; daemon threads never block
                # process shutdown.
                logger.warning(
                    "Tangent Hub worker did not exit within %.1fs; "
                    "keeping the residual thread visible to is_running()",
                    1.0,
                )
                return
        self._thread = None

    def wake(self) -> None:
        """Interrupt the retry/probe wait and reconnect immediately.

        Called when the user explicitly asks for the integration again (for
        example by applying the control-surface preferences after installing
        Tangent Hub), so a previously absent Hub is picked up at once instead
        of at the next background probe.
        """

        self._consecutive_failures = 0
        self._reconnect_now.set()

    def send_parameter_value(
        self,
        parameter_id: int,
        value: float,
        at_default: bool = False,
    ) -> bool:
        """Send a software-control value to Hub."""

        return self._send(
            encode_parameter_value(parameter_id, value, at_default=at_default)
        )

    def send_all_change(self) -> bool:
        """Ask Hub to refresh values currently shown on the panel."""

        return self._send(encode_all_change())

    def _send(self, message: bytes) -> bool:
        with self._socket_lock:
            current_socket = self._socket
            if current_socket is None:
                return False
            try:
                current_socket.sendall(message)
            except OSError as exc:
                # AUD-32800: sendall under the 0.25s socket timeout may
                # have written an unknown number of bytes before raising
                # (socket.timeout is an OSError) — the outbound frame
                # stream is corrupted from here on. Tear the connection
                # down instead of leaving it "healthy": the receive loop's
                # recv() on the closed socket raises, the finally block
                # skips double-closing a different socket, and the normal
                # reconnect path rebuilds a clean stream (and reports the
                # failure once). Previously the next _send appended fresh
                # frames after a truncated one and Hub read a misaligned
                # length prefix forever.
                logger.debug("Tangent Hub send failed: %s", exc)
                self._socket = None
                try:
                    current_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    current_socket.close()
                except OSError:
                    pass
                return False
        return True

    def _run(self) -> None:
        reconnect_delay = self._reconnect_min_delay
        while not self._stop_event.is_set():
            current_socket: socket.socket | None = None
            was_connected = False
            try:
                current_socket = socket.create_connection(
                    (self.host, self.port),
                    timeout=1.0,
                )
                current_socket.settimeout(0.25)
                with self._socket_lock:
                    if self._stop_event.is_set():
                        return
                    self._socket = current_socket
                was_connected = True
                reconnect_delay = self._reconnect_min_delay
                self._consecutive_failures = 0
                self.connected.emit()

                while not self._stop_event.is_set():
                    header = self._receive_exact(current_socket, FRAME_HEADER_SIZE)
                    if header is None:
                        break
                    frame_size = int.from_bytes(header, "big", signed=False)
                    if not 0 < frame_size <= MAX_FRAME_SIZE:
                        raise ValueError(f"invalid Tangent frame size: {frame_size}")
                    payload = self._receive_exact(current_socket, frame_size)
                    if payload is None:
                        break
                    try:
                        command = decode_message(payload)
                    except ValueError as exc:
                        # A malformed/extended frame must not kill the whole
                        # control-surface session. The length prefix lets us
                        # safely discard this frame and continue receiving.
                        logger.warning("Ignoring invalid Tangent frame: %s", exc)
                        self.error.emit(str(exc))
                        continue
                    self._handle_command(command)
            except OSError as exc:
                if not self._stop_event.is_set():
                    self._report_connection_failure(str(exc))
            except ValueError as exc:
                if not self._stop_event.is_set():
                    self._report_connection_failure(str(exc))
            finally:
                with self._socket_lock:
                    if self._socket is current_socket:
                        self._socket = None
                if current_socket is not None:
                    try:
                        current_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        current_socket.close()
                    except OSError:
                        pass
                if was_connected and not self._stop_event.is_set():
                    self.disconnected.emit()

            if not self._stop_event.is_set():
                if (
                    self._consecutive_failures
                    >= self.UNAVAILABLE_AFTER_FAILURES
                ):
                    # Hub looks absent (not installed / not running).  Keep
                    # probing quietly so a later install is picked up, but do
                    # not retry-spam the log or the status bar.
                    wait_seconds = self.UNAVAILABLE_PROBE_DELAY
                    logger.debug(
                        "Tangent Hub still unreachable; probing again in %.0fs",
                        wait_seconds,
                    )
                else:
                    wait_seconds = reconnect_delay
                    logger.info(
                        "Retrying Tangent Hub connection in %.1fs",
                        reconnect_delay,
                    )
                    reconnect_delay = min(
                        reconnect_delay * 2.0,
                        self._reconnect_max_delay,
                    )
                self._reconnect_now.wait(wait_seconds)
                self._reconnect_now.clear()

    def _report_connection_failure(self, detail: str) -> None:
        """Escalate connection failures without spamming the log.

        Only the first failure of a streak is reported through ``error`` so
        consumers can show one transient status.  Once
        ``UNAVAILABLE_AFTER_FAILURES`` consecutive attempts have failed,
        ``unavailable`` is emitted exactly once per streak and further probe
        failures stay silent until Hub answers or ``wake()`` resets the
        streak.
        """

        self._consecutive_failures += 1
        failures = self._consecutive_failures
        if failures == 1:
            logger.info("Tangent Hub connection failed: %s", detail)
            self.error.emit(detail)
        elif failures == self.UNAVAILABLE_AFTER_FAILURES:
            logger.info(
                "Tangent Hub is not reachable after %d attempts "
                "(not installed or not running); staying idle and "
                "probing every %.0fs",
                failures,
                self.UNAVAILABLE_PROBE_DELAY,
            )
            self.unavailable.emit()
        else:
            logger.debug(
                "Tangent Hub connection attempt %d failed: %s",
                failures,
                detail,
            )

    @staticmethod
    def _receive_exact(
        current_socket: socket.socket,
        size: int,
    ) -> bytes | None:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            try:
                chunk = current_socket.recv(remaining)
            except socket.timeout:
                continue
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _handle_command(self, command: TangentCommand) -> None:
        if command.command == FromHubCommand.INITIATE_COMMS:
            protocol_revision, panels = command.payload
            if not isinstance(protocol_revision, int) or not isinstance(panels, tuple):
                raise ValueError("invalid Tangent initiate payload")
            if not all(isinstance(panel, PanelInfo) for panel in panels):
                raise ValueError("invalid Tangent panel list")
            if not self._send(
                encode_application_definition(
                    self.application_name,
                    self.system_path,
                    self.user_path,
                )
            ):
                logger.debug("Tangent Hub disconnected before app definition")
            self.panels_received.emit(panels)
            # This causes Hub to request the values for controls it is showing.
            self._send(encode_all_change())
            return

        if command.command == FromHubCommand.PARAMETER_CHANGE:
            parameter_id, increment = command.payload
            numeric_id = int(parameter_id)
            numeric_increment = float(increment)
            timestamp_ns = time.monotonic_ns()
            self.parameter_change_received.emit(numeric_id, numeric_increment)
            self.parameter_change_received_at.emit(
                numeric_id,
                numeric_increment,
                timestamp_ns,
            )
            return

        if command.command == FromHubCommand.PARAMETER_RESET:
            (parameter_id,) = command.payload
            self.parameter_reset_received.emit(int(parameter_id))
            return

        if command.command == FromHubCommand.PARAMETER_VALUE_REQUEST:
            (parameter_id,) = command.payload
            self.parameter_value_requested.emit(int(parameter_id))
            return

        if command.command == FromHubCommand.ACTION_ON:
            (action_id,) = command.payload
            self.action_on_received.emit(int(action_id))
            return

        if command.command == FromHubCommand.ACTION_OFF:
            (action_id,) = command.payload
            self.action_off_received.emit(int(action_id))
