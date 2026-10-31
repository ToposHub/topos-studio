"""Current-user local IPC transport for the desktop MCP host.

The application owns :class:`LocalMcpIpcHost`; external processes (``toposctl``
or the stdio bridge) only connect to its Unix-domain socket.  No TCP listener,
no arbitrary socket path cleanup, and no client-supplied approval grant are
allowed.  Business routing remains exclusively in :class:`McpServer` /
``ToolFacade``.
"""

from __future__ import annotations

import json
import hashlib
import os
import socket
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, TextIO

from src.features.edit.ai_editing.mcp_gateway import McpClientPolicy, McpServer, McpSession
from src.features.edit.ai_editing.tool_facade import ToolPermission


MAX_MESSAGE_BYTES = 1_048_576


class QtMcpRequestDispatcher:  # pragma: no cover - exercised from app composition tests
    """Marshal socket-thread requests onto the Qt GUI thread synchronously."""

    def __init__(self, server: McpServer, *, timeout_seconds: float = 30.0) -> None:
        from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

        class _Bridge(QObject):
            requested = pyqtSignal(object)

            def __init__(self, target: McpServer) -> None:
                super().__init__()
                self._target = target
                self.requested.connect(self._handle, Qt.ConnectionType.QueuedConnection)

            @pyqtSlot(object)
            def _handle(self, envelope: dict[str, Any]) -> None:
                if envelope.get("abandoned"):
                    # 调用方已超时并收到失败响应：超时必须等价于取消，
                    # 排队中的请求不得再在 GUI 线程执行（否则客户端可能
                    # 重试造成双次副作用）。
                    envelope["event"].set()
                    return
                try:
                    envelope["response"] = self._target.handle_request(
                        envelope["session"], envelope["request"],
                    )
                except Exception as exc:  # noqa: BLE001
                    envelope["response"] = _rpc_error(
                        envelope["request"].get("id"), -32603,
                        f"desktop dispatcher failed: {type(exc).__name__}",
                    )
                finally:
                    envelope["event"].set()

        self._bridge = _Bridge(server)
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    def dispatch(self, session: McpSession, request: dict[str, Any]) -> dict[str, Any]:
        event = threading.Event()
        envelope = {
            "session": session, "request": request, "event": event,
            "abandoned": False,
        }
        self._bridge.requested.emit(envelope)
        if not event.wait(self._timeout_seconds):
            # 标记 abandoned：_handle 在 GUI 线程真正执行前会检查该标记并跳过。
            envelope["abandoned"] = True
            return _rpc_error(request.get("id"), -32603, "desktop host did not respond before timeout")
        return dict(envelope.get("response") or _rpc_error(request.get("id"), -32603, "empty desktop response"))


def default_socket_path() -> Path:
    """Return a per-user endpoint under a mode-0700 runtime directory."""
    runtime = Path(tempfile.gettempdir()) / f"topos-mcp-{os.getuid()}"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        runtime.chmod(0o700)
    except OSError:
        pass
    return runtime / "desktop.sock"


class LocalMcpIpcHost:
    """JSON-lines Unix socket host bound only to the current OS user.

    ``dispatch`` is intentionally injectable.  A desktop host supplies a
    GUI-thread dispatcher; tests can use a direct function.  Worker threads
    never call Qt widgets or OpenGL APIs directly.
    """

    def __init__(
        self,
        server: McpServer,
        *,
        socket_path: str | Path | None = None,
        dispatch: Callable[[McpSession, dict[str, Any]], dict[str, Any]] | None = None,
        context_provider: Callable[[], tuple[str, str, dict[str, bool]]] | None = None,
        policy: McpClientPolicy | None = None,
    ) -> None:
        self._server = server
        requested_path = Path(socket_path) if socket_path is not None else default_socket_path()
        # macOS commonly limits AF_UNIX paths to 104 bytes.  A workspace or
        # pytest temporary directory can exceed it, so retain the caller's
        # stable identity as a hash while placing the real endpoint in our
        # short, mode-0700 per-user runtime directory.
        if len(os.fsencode(str(requested_path))) >= 100:
            digest = hashlib.sha256(str(requested_path).encode("utf-8")).hexdigest()[:16]
            requested_path = default_socket_path().parent / f"client-{digest}.sock"
        self._path = requested_path
        self._dispatch = dispatch or server.handle_request
        self._context_provider = context_provider or (lambda: ("", "", {"mcp_server": False}))
        # Local IPC exposes safe read/preview tools.  No remote caller gets a
        # GUI-issued approval grant through this transport.
        self._policy = policy or McpClientPolicy(
            client_id="local-ipc",
            author="current-os-user",
            max_permission=ToolPermission.PREVIEW,
            data_scope="current_project",
        )
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._server_lock = threading.RLock()

    @property
    def socket_path(self) -> Path:
        return self._path

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> Path:
        if self.running:
            return self._path
        self._prepare_endpoint()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self._path))
            listener.listen(8)
            listener.settimeout(0.2)
            os.chmod(self._path, 0o600)
        except Exception:
            listener.close()
            raise
        self._listener = listener
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="topos-mcp-local-ipc", daemon=True,
        )
        self._thread.start()
        return self._path

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)
        try:
            if self._path.exists() and stat.S_ISSOCK(self._path.stat().st_mode):
                self._path.unlink()
        except FileNotFoundError:
            pass

    def _prepare_endpoint(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._path.parent.chmod(0o700)
        except OSError:
            pass
        if not self._path.exists():
            return
        mode = self._path.stat().st_mode
        if not stat.S_ISSOCK(mode):
            raise RuntimeError(f"refusing to replace non-socket IPC endpoint: {self._path}")
        self._path.unlink()

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                conn, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            threading.Thread(
                target=self._serve_connection, args=(conn,),
                name="topos-mcp-client", daemon=True,
            ).start()

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn:
            if not _peer_is_current_user(conn):
                return
            project_id, profile_id, flags = self._context_provider()
            with self._server_lock:
                session = self._server.open_session(
                    self._policy,
                    active_project_id=project_id,
                    active_profile_id=profile_id,
                    feature_flags=dict(flags),
                )
            try:
                reader = conn.makefile("r", encoding="utf-8", newline="\n")
                writer = conn.makefile("w", encoding="utf-8", newline="\n")
                with reader, writer:
                    for raw_line in reader:
                        if len(raw_line.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
                            _write_json(writer, _rpc_error(None, -32600, "request too large"))
                            continue
                        try:
                            request = json.loads(raw_line)
                        except json.JSONDecodeError:
                            _write_json(writer, _rpc_error(None, -32700, "invalid JSON"))
                            continue
                        if not isinstance(request, dict):
                            _write_json(writer, _rpc_error(None, -32600, "request must be an object"))
                            continue
                        response = self._dispatch(session, request)
                        if "id" in request:
                            _write_json(writer, response)
            finally:
                with self._server_lock:
                    self._server.close_session(session.session_id)


class LocalMcpIpcClient:
    """Minimal current-user client used by ``toposctl`` and the stdio bridge."""

    def __init__(self, socket_path: str | Path, *, timeout_seconds: float = 10.0) -> None:
        self._path = str(socket_path)
        self._timeout_seconds = max(0.1, float(timeout_seconds))

    def invoke(self, tool_name: str, args: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        responses = self.exchange((
            {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0", "id": "call", "method": "tools/call",
                "params": {"name": tool_name, "arguments": dict(args or {})},
            },
        ))
        response = responses[-1] if responses else {}
        if "error" in response:
            return {
                "ok": False,
                "diagnostics": [{"severity": "error", "code": "TAEP_IPC_ERROR", "message": response["error"].get("message", "IPC error")}],
            }
        result = response.get("result", {})
        structured = result.get("structured") if isinstance(result, dict) else None
        return dict(structured) if isinstance(structured, dict) else {
            "ok": False,
            "diagnostics": list(result.get("diagnostics", ())) if isinstance(result, dict) else [],
        }

    def exchange(self, requests: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        # W07：Windows 无 AF_UNIX——AttributeError 不在 run_cli 的 IPC
        # 失败异常域（OSError/ConnectionError 族）内，会裸抛穿透 CLI
        # 结构化错误契约。按"连接不可用"同义抛 ConnectionError，由
        # 上层与 POSIX 连接失败走同一 except 分支（exit 1 + 诊断）。
        if not hasattr(socket, "AF_UNIX"):
            raise ConnectionError(
                "local IPC host unavailable: "
                "AF_UNIX sockets not supported on this platform")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout_seconds)
        try:
            sock.connect(self._path)
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            writer = sock.makefile("w", encoding="utf-8", newline="\n")
            with reader, writer:
                responses: list[dict[str, Any]] = []
                for request in requests:
                    _write_json(writer, request)
                    raw = reader.readline()
                    if not raw:
                        raise ConnectionError("local MCP host closed the connection")
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ConnectionError("local MCP host returned non-object JSON")
                    responses.append(parsed)
                return responses
        finally:
            sock.close()


def proxy_stdio_to_local_host(
    socket_path: str | Path,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> int:
    """Forward one stdio MCP process to a single local-host IPC session."""
    client = LocalMcpIpcClient(socket_path)
    # Use a persistent socket by exchanging as we parse would require a public
    # stream API.  MCP stdio sessions normally initialize first; a fresh local
    # IPC session per request would lose that state, so retain a small direct
    # socket bridge here.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(client._timeout_seconds)
    try:
        sock.connect(str(socket_path))
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        writer = sock.makefile("w", encoding="utf-8", newline="\n")
        with reader, writer:
            for raw_line in input_stream:
                if not raw_line.strip():
                    continue
                if len(raw_line.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
                    _write_json(output_stream, _rpc_error(None, -32600, "request too large"))
                    continue
                try:
                    request = json.loads(raw_line)
                except json.JSONDecodeError:
                    _write_json(output_stream, _rpc_error(None, -32700, "invalid JSON"))
                    continue
                if not isinstance(request, dict):
                    _write_json(output_stream, _rpc_error(None, -32600, "request must be an object"))
                    continue
                _write_json(writer, request)
                if "id" not in request:
                    continue
                response = reader.readline()
                if not response:
                    return 1
                output_stream.write(response)
                output_stream.flush()
    except (OSError, ConnectionError, TimeoutError):
        # The socket pathname is desktop-local implementation detail.  Keep
        # stdio diagnostics useful without disclosing it to a spawned client.
        print("topos local MCP IPC unavailable", file=os.sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


def _peer_is_current_user(conn: socket.socket) -> bool:
    expected_uid = os.geteuid()
    try:
        if hasattr(conn, "getpeereid"):
            uid, _gid = conn.getpeereid()  # type: ignore[attr-defined]
            return int(uid) == expected_uid
        if hasattr(socket, "SO_PEERCRED"):
            import struct
            _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return int(uid) == expected_uid
    except OSError:
        return False
    # AF_UNIX endpoint is mode 0600 under a mode 0700 directory.  Platforms
    # without peer-credential support retain that filesystem boundary.
    return True


def _write_json(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


__all__ = [
    "LocalMcpIpcClient", "LocalMcpIpcHost", "MAX_MESSAGE_BYTES", "QtMcpRequestDispatcher",
    "default_socket_path", "proxy_stdio_to_local_host",
]
