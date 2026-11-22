"""Topos Independent App Protocol v1 client; no Topos imports required.

This file may be copied with an application. Keep the protocol compatible when
upgrading the application; its business code has no dependency on the host.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
from collections.abc import Sequence
from typing import Any, BinaryIO, TextIO

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

PROTOCOL = 1
MAX_LINE_BYTES = 65_536
MAX_PENDING_COMMANDS = 64
COMMANDS = frozenset({"open", "activate", "close", "detach"})
EVENTS = frozenset({"ready", "opened", "dirty", "saved", "exported", "error", "closed"})


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the same entry point for standalone and hosted operation."""
    parser = argparse.ArgumentParser(description="Independent Topos application")
    parser.add_argument("file", nargs="?", help="File to open when running standalone")
    parser.add_argument("--topos-session", default=None, metavar="ID")
    return parser.parse_args(argv)


class AppBridge(QObject):
    """Optional JSONL bridge; all callbacks are dispatched on the GUI thread.

    Reading stdin never blocks the GUI. EOF and detach end the host session,
    leaving the application and unsaved documents alive. The host's stdout
    pipe is reserved for protocol; ordinary print output goes to stderr.
    """

    command_received = pyqtSignal(dict)

    def __init__(self, session_id: str | None, parent: QObject | None = None, *,
                 input_stream: BinaryIO | None = None,
                 output_stream: TextIO | None = None) -> None:
        super().__init__(parent)
        self.session_id = session_id
        self._active = bool(session_id)
        # Never block a daemon thread in sys.stdin.buffer.readline(): Python
        # finalization takes that buffered stream's lock and can SIGABRT while
        # the host still holds its stdin pipe open. A duplicated raw descriptor
        # has no interpreter-owned buffered lock and does not delay GUI exit.
        self._input = input_stream
        self._input_fd = os.dup(sys.stdin.fileno()) if self._active and input_stream is None else None
        self._read_buffer = bytearray()
        self._output = output_stream if output_stream is not None else sys.stdout
        self._commands: queue.Queue[dict[str, Any] | None] = queue.Queue(MAX_PENDING_COMMANDS)
        self._stop = threading.Event()
        self._started = False
        self._owns_stdio = self._active and output_stream is None
        self._detached_sink: TextIO | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._drain)
        if self._owns_stdio:
            sys.stdout = sys.stderr

    @property
    def connected(self) -> bool:
        return self._active

    def start(self) -> None:
        if not self._active or self._started:
            return
        self._started = True
        threading.Thread(target=self._read, name="topos-app-input", daemon=True).start()
        self._timer.start()

    def detach(self) -> None:
        """Disconnect without closing the application's window or document."""
        was_active = self._active
        self._active = False
        self._stop.set()
        self._timer.stop()
        if was_active and self._owns_stdio:
            # The host may exit and close both pipes. Protect Python print,
            # logging handlers and native-library diagnostics in the surviving
            # GUI application from BrokenPipeError / SIGPIPE afterwards.
            sink = open(os.devnull, "w", encoding="utf-8")
            self._detached_sink = sink
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            sys.stdout = sink
            sys.stderr = sink

    def emit_event(self, event: str, request_id: str = "", **payload: Any) -> None:
        if not isinstance(event, str) or event not in EVENTS:
            raise ValueError(f"Unknown event: {event}")
        if not isinstance(request_id, str):
            raise ValueError("request_id must be a string")
        if not self._active:
            return
        if any(key in payload for key in ("protocol", "session_id", "event", "request_id")):
            raise ValueError("Payload may not overwrite protocol envelope fields")
        message = {"protocol": PROTOCOL, "session_id": self.session_id,
                   "event": event, "request_id": request_id, **payload}
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise ValueError("Protocol event exceeds the maximum line size")
        try:
            self._output.write(line)
            self._output.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.detach()

    def _enqueue(self, value: dict[str, Any] | None) -> bool:
        while not self._stop.is_set():
            try:
                self._commands.put(value, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _read(self) -> None:
        """Only bytes/JSON/queue work here; no Qt, document or GPU access."""
        try:
            while not self._stop.is_set():
                line = self._read_line()
                if not line:
                    self._enqueue(None)
                    return
                if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                    self._enqueue({"_protocol_error": "Command line exceeds limit or is incomplete"})
                    self._enqueue(None)
                    return
                try:
                    message = json.loads(line.decode("utf-8"))
                    if not isinstance(message, dict):
                        raise ValueError("Command must be an object")
                    if type(message.get("protocol")) is not int or message["protocol"] != PROTOCOL:
                        raise ValueError("Unsupported protocol version")
                    if message.get("session_id") != self.session_id:
                        raise ValueError("Session does not match")
                    if message.get("command") not in COMMANDS:
                        raise ValueError("Unknown command")
                    if not isinstance(message.get("request_id"), str):
                        raise ValueError("request_id must be a string")
                    message["request_id"].encode("utf-8")
                    if message["command"] == "open" and (
                        not isinstance(message.get("path"), str) or not message["path"]
                        or "\x00" in message["path"]
                    ):
                        raise ValueError("open requires a valid path")
                    if message["command"] == "open":
                        message["path"].encode("utf-8")
                except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
                    self._enqueue({"_protocol_error": str(exc)})
                    continue
                if not self._enqueue(message):
                    return
        except (OSError, ValueError):
            self._enqueue(None)
        finally:
            if self._input_fd is not None:
                os.close(self._input_fd)
                self._input_fd = None

    def _read_line(self) -> bytes:
        if self._input is not None:
            return self._input.readline(MAX_LINE_BYTES + 1)
        if self._input_fd is None:
            return b""
        while True:
            newline = self._read_buffer.find(b"\n")
            if newline >= 0:
                result = bytes(self._read_buffer[:newline + 1])
                del self._read_buffer[:newline + 1]
                return result
            if len(self._read_buffer) > MAX_LINE_BYTES:
                return bytes(self._read_buffer)
            chunk = os.read(self._input_fd, min(4096, MAX_LINE_BYTES + 1 - len(self._read_buffer)))
            if not chunk:
                result = bytes(self._read_buffer)
                self._read_buffer.clear()
                return result
            self._read_buffer.extend(chunk)

    def _drain(self) -> None:
        for _ in range(MAX_PENDING_COMMANDS):
            try:
                message = self._commands.get_nowait()
            except queue.Empty:
                return
            if message is None:
                self.detach()
                return
            if "_protocol_error" in message:
                self.emit_event("error", message=str(message["_protocol_error"]), code="INVALID_COMMAND")
            elif message["command"] == "detach":
                self.detach()
                return
            else:
                self.command_received.emit(message)
