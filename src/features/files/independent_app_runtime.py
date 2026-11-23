"""Independent application protocol v1; Qt owns state, daemon threads only do IO.

Applications share a Python installation, never the host's QApplication. This
runner is for explicitly trusted local applications, not the third-party sandbox.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from src.shared.plugins.independent_apps import IndependentAppSpec

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65536
MAX_EVENTS = 256
EVENTS = frozenset({"ready", "opened", "dirty", "saved", "exported", "error", "closed"})


def application_python() -> str:
    """Use the source host's interpreter, or an explicitly configured runtime."""
    configured = os.environ.get("TOPOS_APPS_PYTHON", "")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("TOPOS_APPS_PYTHON 必须指向可用 Python 的绝对路径。")
        return str(path)
    if getattr(sys, "frozen", False):
        raise ValueError("打包版需要配置独立应用 Python：TOPOS_APPS_PYTHON。")
    return sys.executable


def validate_input(spec: IndependentAppSpec, paths: tuple[str, ...]) -> str | None:
    """Freeze one existing file; a launcher may also open an empty application."""
    if len(paths) > 1:
        raise ValueError("独立应用一次打开一个文件，请单选后重试。")
    if not paths:
        return None
    if not isinstance(paths[0], str) or not paths[0] or "\x00" in paths[0]:
        raise ValueError("文件路径无效。")
    path = Path(paths[0]).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"文件不存在：{path}")
    if path.suffix.lower() not in spec.extensions:
        raise ValueError(f"{spec.display_name} 不支持 {path.suffix or '此文件类型'}。")
    return str(path)


def check_dependencies(spec: IndependentAppSpec, python: str, path: str | None) -> None:
    """Probe in a worker, using the same interpreter that will run the app."""
    modules = dict(spec.required_modules)
    if path:
        modules.update(spec.format_modules.get(Path(path).suffix.lower(), {}))
    code = (
        "import importlib.util,json,sys; "
        "names=json.loads(sys.argv[1]); "
        "print(json.dumps([n for n in names if importlib.util.find_spec(n) is None]))"
    )
    try:
        result = subprocess.run(
            [python, "-I", "-c", code, json.dumps(list(modules))],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"无法检查独立应用 Python 环境：{exc}") from exc
    if result.returncode:
        raise ValueError("独立应用 Python 环境检查失败：" + result.stderr.decode("utf-8", "replace")[-1000:])
    try:
        missing = json.loads(result.stdout)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Python 环境没有返回有效的依赖检查结果。") from exc
    if missing:
        packages = ", ".join(modules[name] for name in missing)
        raise ValueError(f"{spec.display_name} 缺少依赖：{packages}。请安装到运行环境：{python}")


def decode_event(line: bytes, session_id: str) -> dict[str, Any]:
    if len(line) > MAX_MESSAGE_BYTES:
        raise ValueError("应用消息超过 64 KiB 上限。")
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("应用返回了无效 JSON 消息。") from exc
    if not isinstance(event, dict) or type(event.get("protocol")) is not int \
            or event["protocol"] != PROTOCOL_VERSION or event.get("session_id") != session_id:
        raise ValueError("应用协议版本或会话身份不匹配。")
    if not isinstance(event.get("event"), str) or event["event"] not in EVENTS \
            or not isinstance(event.get("request_id", ""), str):
        raise ValueError("应用事件类型或调用身份无效。")
    return event


@dataclass
class _Session:
    spec: IndependentAppSpec
    session_id: str
    process: subprocess.Popen[bytes]
    cache_root: str = ""
    queue: queue.Queue[bytes] = field(default_factory=lambda: queue.Queue(MAX_EVENTS))
    outbound: queue.Queue[bytes | None] = field(default_factory=lambda: queue.Queue(16))
    stdout_done: threading.Event = field(default_factory=threading.Event)
    overflow: threading.Event = field(default_factory=threading.Event)
    ready: bool = False
    opened: bool = False
    detached: bool = False
    pending_id: str = ""
    pending_path: str | None = None
    pending_at: float = field(default_factory=time.monotonic)
    log_tail: bytes = b""
    io_error: str = ""
    closed_reported: bool = False
    expired_requests: deque[str] = field(default_factory=lambda: deque(maxlen=64))


def _read_stdout(session: _Session) -> None:
    stream = session.process.stdout
    assert stream is not None
    try:
        while True:
            line = stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if session.detached:
                continue  # Keep draining a detached app until it exits.
            if len(line) > MAX_MESSAGE_BYTES:
                session.overflow.set()
                continue
            try:
                session.queue.put_nowait(line)
            except queue.Full:
                session.overflow.set()
    except OSError as exc:
        session.io_error = str(exc)
    finally:
        stream.close()
        # Reap even after the host has released a standalone application.
        session.process.wait()
        if session.cache_root:
            shutil.rmtree(session.cache_root, ignore_errors=True)
        session.stdout_done.set()


def _write_stdin(session: _Session) -> None:
    """Pipe backpressure must never block a Qt slot, including on shutdown."""
    stream = session.process.stdin
    assert stream is not None
    try:
        while True:
            data = session.outbound.get()
            if data is None:
                break
            stream.write(data)
            stream.flush()
    except (OSError, ValueError) as exc:
        session.io_error = str(exc)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _read_stderr(session: _Session) -> None:
    stream = session.process.stderr
    assert stream is not None
    try:
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                break
            session.log_tail = (session.log_tail + chunk)[-8192:]
    except OSError as exc:
        session.io_error = str(exc)
    finally:
        stream.close()


class IndependentAppRuntime(QObject):
    """Nonblocking process supervision; no application code is imported here."""

    opened = pyqtSignal(str, str)  # plugin_id, path (empty for ready empty app)
    files_changed = pyqtSignal(list)
    failed = pyqtSignal(str, str)
    notice = pyqtSignal(str, str)  # Application notifications never finish another request.
    state_changed = pyqtSignal(str, str)

    def __init__(self, parent: QObject | None = None, *, ready_timeout: float = 20,
                 open_timeout: float = 120) -> None:
        super().__init__(parent)
        self._sessions: dict[str, _Session] = {}
        self._ready_timeout = ready_timeout
        self._open_timeout = open_timeout
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)

    def launch(self, spec: IndependentAppSpec, python: str, path: str | None) -> None:
        if self._closed:
            raise ValueError("独立应用宿主已关闭。")
        session = self._sessions.get(spec.plugin_id)
        if session is not None and session.process.poll() is not None:
            raise ValueError("上次应用会话正在收尾，请稍后重试。")
        if session is not None and session.process.poll() is None:
            if session.spec.fingerprint != spec.fingerprint or session.spec.root != spec.root:
                raise ValueError("应用文件已升级，请先关闭旧版本工具窗口，再打开新版本。")
            if not session.ready or session.pending_id:
                raise ValueError("应用正在打开文件，请稍后重试。")
            if path is None:
                self._send(session, "activate")
                self.opened.emit(spec.plugin_id, "")
            else:
                self._open(session, path)
            return
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        session_id = uuid4().hex
        # Ignored package __pycache__ files must never replace trusted source.
        # -B disables writes; a fresh private prefix also prevents reading them.
        cache_root = tempfile.mkdtemp(prefix="topos-app-pycache-")
        env["PYTHONPYCACHEPREFIX"] = cache_root
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # Apply the same policy to app-owned workers.
        try:
            process = subprocess.Popen(
                [python, "-B", "-X", f"pycache_prefix={cache_root}", "-u",
                 str(spec.entrypoint), "--topos-session", session_id],
                cwd=spec.root, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError:
            shutil.rmtree(cache_root, ignore_errors=True)
            raise
        session = _Session(spec, session_id, process, cache_root=cache_root, pending_path=path)
        self._sessions[spec.plugin_id] = session
        threading.Thread(target=_read_stdout, args=(session,), daemon=True,
                         name="topos-app-out").start()
        threading.Thread(target=_read_stderr, args=(session,), daemon=True,
                         name="topos-app-log").start()
        threading.Thread(target=_write_stdin, args=(session,), daemon=True,
                         name="topos-app-in").start()
        self.state_changed.emit(spec.plugin_id, "starting")
        self._timer.start()

    def _send(self, session: _Session, command: str, *, request_id: str = "",
              **payload: Any) -> None:
        message = {"protocol": PROTOCOL_VERSION, "session_id": session.session_id,
                   "command": command, "request_id": request_id, **payload}
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("应用请求超过 64 KiB 上限。")
        try:
            session.outbound.put_nowait(data)
        except queue.Full as exc:
            raise ValueError("应用未及时处理请求，发送队列已满。") from exc

    def running_version(self, plugin_id: str) -> str:
        session = self._sessions.get(plugin_id)
        return session.spec.version if session is not None and session.ready else ""

    def _open(self, session: _Session, path: str) -> None:
        session.pending_id = uuid4().hex
        session.pending_path = path
        session.pending_at = time.monotonic()
        self._send(session, "open", request_id=session.pending_id, path=path)

    def _event(self, session: _Session, event: dict[str, Any]) -> None:
        kind = event["event"]
        plugin_id = session.spec.plugin_id
        if kind == "ready":
            if session.ready:
                raise ValueError("应用重复报告 ready。")
            session.ready = True
            if session.pending_path is not None:
                self._open(session, session.pending_path)
            else:
                self.opened.emit(plugin_id, "")
            self.state_changed.emit(plugin_id, "ready")
            return
        if not session.ready:
            raise ValueError("应用在 ready 之前发送业务事件。")
        request_id = event.get("request_id", "")
        if request_id and request_id in session.expired_requests and kind in ("opened", "error"):
            return  # A slow decoder may finish after the UI stopped waiting.
        if kind == "opened":
            path = self._existing_path(event.get("path"))
            if request_id:
                if request_id != session.pending_id or path != session.pending_path:
                    raise ValueError("应用打开结果与当前请求不匹配。")
                session.pending_id = ""
                session.pending_path = None
                self.opened.emit(plugin_id, path)
            session.opened = True
        elif kind == "error":
            message = event.get("message")
            if not isinstance(message, str) or not message:
                raise ValueError("应用错误消息无效。")
            if session.pending_id and request_id == session.pending_id:
                session.pending_id = ""
                session.pending_path = None
                self.failed.emit(plugin_id, message[:2000])
            else:
                self.notice.emit(plugin_id, message[:2000])
        elif kind == "saved":
            self.files_changed.emit([self._existing_path(event.get("path"))])
        elif kind == "exported":
            paths = event.get("paths")
            if not isinstance(paths, list) or not 1 <= len(paths) <= 64:
                raise ValueError("应用导出路径列表无效。")
            self.files_changed.emit(list(dict.fromkeys(self._existing_path(p) for p in paths)))
        elif kind == "dirty":
            if type(event.get("dirty")) is not bool:
                raise ValueError("应用修改状态无效。")
            self.state_changed.emit(plugin_id, "dirty" if event["dirty"] else "ready")
        elif kind == "closed":
            session.closed_reported = True
            if session.pending_id:
                self.failed.emit(plugin_id, "文件打开已取消：应用窗口已关闭。")
                session.pending_id = ""
            self.state_changed.emit(plugin_id, "closed")

    @staticmethod
    def _existing_path(value: Any) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("应用返回了无效文件路径。")
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"应用返回的文件不存在：{value}")
        return str(path.resolve())

    def _poll(self) -> None:
        for plugin_id, session in tuple(self._sessions.items()):
            try:
                if session.overflow.is_set():
                    raise ValueError("应用输出超出协议消息或队列上限。")
                for _ in range(MAX_EVENTS):
                    try:
                        line = session.queue.get_nowait()
                    except queue.Empty:
                        break
                    self._event(session, decode_event(line, session.session_id))
                if session.io_error and session.process.poll() is None:
                    raise ValueError(f"应用通信失败：{session.io_error}")
                now = time.monotonic()
                if not session.ready and now - session.pending_at > self._ready_timeout:
                    raise ValueError("应用启动超时，未收到 ready。")
                if session.pending_id and now - session.pending_at > self._open_timeout:
                    session.expired_requests.append(session.pending_id)
                    session.pending_id = ""
                    session.pending_path = None
                    self.failed.emit(plugin_id, "文件打开超时；工具窗口保留，可在窗口内继续操作。")
                code = session.process.poll()
                if code is not None and session.stdout_done.is_set() and session.queue.empty():
                    self._sessions.pop(plugin_id, None)
                    if code or not session.ready:
                        tail = session.log_tail.decode("utf-8", "replace")[-2000:]
                        self.failed.emit(plugin_id, f"应用退出（代码 {code}）。{tail}")
                    elif session.pending_id:
                        self.failed.emit(plugin_id, "应用在完成打开请求前退出。")
                    self.state_changed.emit(plugin_id, "stopped")
                    self._stop_writer(session)
            except (ValueError, OSError) as exc:
                self.failed.emit(plugin_id, str(exc))
                self.detach(plugin_id, terminate_startup=not session.ready)
        if not self._sessions:
            self._timer.stop()

    def detach(self, plugin_id: str, *, terminate_startup: bool = False) -> None:
        """Release ownership, preserving an editor that may contain user changes."""
        session = self._sessions.pop(plugin_id, None)
        if session is None:
            return
        session.detached = True
        try:
            self._send(session, "detach")
        except ValueError:
            pass  # A crashed or already closed child needs no detach handshake.
        self._stop_writer(session)
        if terminate_startup and session.process.poll() is None:
            session.process.terminate()
            def force_stop() -> None:
                if session.process.poll() is None:
                    try:
                        session.process.kill()
                    except OSError:
                        pass
            timer = threading.Timer(1, force_stop)
            timer.daemon = True
            timer.start()
        self.state_changed.emit(plugin_id, "stopped")

    @staticmethod
    def _stop_writer(session: _Session) -> None:
        try:
            session.outbound.put_nowait(None)
        except queue.Full:
            # A blocked app cannot delay host shutdown. Its IO thread is daemon;
            # the child remains a standalone app and will eventually see EOF.
            while True:
                try:
                    session.outbound.get_nowait()
                except queue.Empty:
                    break
            session.outbound.put_nowait(None)

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        for plugin_id in tuple(self._sessions):
            self.detach(plugin_id)
