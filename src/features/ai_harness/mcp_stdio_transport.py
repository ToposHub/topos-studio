"""MCP stdio transport with current-protocol and legacy compatibility.

The current MCP revision is stateless and carries client metadata on every
request.  A large part of the installed MCP ecosystem still uses the
``initialize``/``initialized`` exchange, so this transport probes the modern
``server/discover`` method and falls back to the legacy handshake when needed.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Any

from src.features.ai_harness.mcp_outbound import StdioTransport
from src.features.ai_harness.mcp_protocol import (
    LATEST_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSION,
    add_request_metadata,
)

_LOG = logging.getLogger(__name__)

_PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION
_CLIENT_NAME = "topos-mcp-client"
_CLIENT_VERSION = "1.1"
_DEFAULT_STARTUP_TIMEOUT = 30.0
_DEFAULT_REQUEST_TIMEOUT = 180.0
_STDERR_CAP = 200
# AUD-22104：stdout 队列必须有界——server 空闲期持续输出的通知消息否则
# 会在进程内无限累积。满时丢弃最旧消息并计数（_dropped_stdout_messages）。
_STDOUT_QUEUE_MAX = 1024


class McpTransportError(RuntimeError):
    """stdio 传输层错误（启动失败 / 帧错误 / 超时 / 子进程退出）。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class SubprocessStdioTransport(StdioTransport):
    """Launch an external MCP server and exchange newline-delimited JSON-RPC."""

    def __init__(
        self,
        command: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self._command = tuple(command)
        full_env = dict(os.environ)
        if env:
            full_env.update({key: str(value) for key, value in env.items()})
        self._env = full_env
        self._cwd = cwd
        self._startup_timeout = max(0.1, float(startup_timeout))
        self._request_timeout = max(0.1, float(request_timeout))

        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()
        # AUD-22103：_proc 的取放专用短锁——close() 不再等请求锁（最长
        # request_timeout），但仍需要与 _spawn 的原子交接，避免并发 close+start
        # 把新进程清掉却不终止（孤儿进程）。锁序恒为 _lock → _proc_lock。
        self._proc_lock = threading.Lock()
        self._next_id = 1
        self._started = False
        self._mode: str | None = None  # modern / legacy
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stdout_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=_STDOUT_QUEUE_MAX,
        )
        # AUD-22104：因队列满被丢弃的消息计数（诊断用；由泄放线程独占写）。
        self._dropped_stdout_messages = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the subprocess and negotiate modern vs legacy MCP."""
        with self._lock:
            if self._started:
                return
            self._spawn()
            try:
                # Current MCP makes server/discover optional.  A server that
                # implements it gives us an unambiguous modern-protocol signal;
                # otherwise retain compatibility with the legacy ecosystem.
                probe = self._roundtrip(
                    self._with_metadata({
                        "jsonrpc": "2.0", "id": self._alloc_id(),
                        "method": "server/discover", "params": {},
                    }),
                    timeout=self._startup_timeout,
                )
                if isinstance(probe.get("result"), dict):
                    self._mode = "modern"
                else:
                    self._legacy_handshake()
                self._started = True
            except Exception:
                self._terminate_current_process()
                raise

    def _spawn(self) -> None:
        try:
            proc = subprocess.Popen(  # noqa: S603 — host-controlled command
                list(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._build_env(),
                cwd=self._cwd,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise McpTransportError(
                f"MCP server command not found: {self._command[0]!r}（{exc}）"
            ) from exc
        except Exception as exc:
            raise McpTransportError(f"failed to spawn MCP server: {exc}") from exc

        # AUD-22102：队列与进程在本代捕获为局部变量并作为线程参数传入——
        # close() 后立即 start() 时，旧泄放线程的 None 哨兵只会落进旧队列，
        # 绝不投毒新队列（线程不再回读 self._stdout_queue / self._proc）。
        out_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=_STDOUT_QUEUE_MAX,
        )
        self._stdout_queue = out_queue
        self._dropped_stdout_messages = 0
        with self._proc_lock:
            self._proc = proc
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="mcp-stderr", daemon=True,
        )
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, args=(proc, out_queue),
            name="mcp-stdout", daemon=True,
        )
        self._stderr_thread.start()
        self._stdout_thread.start()

    def _legacy_handshake(self) -> None:
        req_id = self._alloc_id()
        response = self._roundtrip({
            "jsonrpc": "2.0", "id": req_id, "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
            },
        }, timeout=self._startup_timeout)
        result = response.get("result")
        if not isinstance(result, dict):
            self._raise_if_error(response)
            raise McpTransportError(
                f"MCP initialize 握手无 result：{response}；stderr={self._stderr_tail()}"
            )
        self._mode = "legacy"
        self._write({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        })

    def _build_env(self) -> dict[str, str]:
        """Prepend an absolute npx/node directory to PATH when necessary."""
        env = dict(self._env)
        first = self._command[0] if self._command else ""
        if os.path.isabs(first) and os.path.dirname(first):
            bin_dir = os.path.dirname(first)
            path = env.get("PATH", os.environ.get("PATH", ""))
            env["PATH"] = f"{bin_dir}{os.pathsep}{path}" if path else bin_dir
        return env

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._stderr_lines.append(line)
                if len(self._stderr_lines) > _STDERR_CAP:
                    del self._stderr_lines[: len(self._stderr_lines) - _STDERR_CAP]
                _LOG.debug("mcp stderr: %s", line)
        except Exception:  # noqa: BLE001 — child shutdown
            pass

    def _drain_stdout(
        self, proc: "subprocess.Popen[bytes] | None",
        out_queue: "queue.Queue[dict[str, Any] | None]",
    ) -> None:
        """Read the child's stdout into ``out_queue``（本代专用，AUD-22102）。

        队列有界（AUD-22104）：满时丢弃最旧消息并计数。丢弃策略保证哨兵
        ``None`` 永远能入队（腾位重试），_roundtrip 不会因队列满而误等。
        """
        if proc is None or proc.stdout is None:
            self._put_stdout(out_queue, None)
            return
        try:
            for raw in iter(proc.stdout.readline, b""):
                message = self._parse(raw)
                if message is not None:
                    self._put_stdout(out_queue, message)
        except Exception:  # noqa: BLE001 — child shutdown
            pass
        finally:
            self._put_stdout(out_queue, None)

    def _put_stdout(
        self, out_queue: "queue.Queue[dict[str, Any] | None]",
        message: "dict[str, Any] | None",
    ) -> None:
        """有界入队：满时丢最旧并计数（AUD-22104）。仅泄放线程调用。"""
        while True:
            try:
                out_queue.put_nowait(message)
                return
            except queue.Full:
                try:
                    out_queue.get_nowait()
                except queue.Empty:
                    pass
                self._dropped_stdout_messages += 1

    # ------------------------------------------------------------------ #
    # StdioTransport 协议
    # ------------------------------------------------------------------ #

    def send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send one request, strictly correlate its JSON-RPC response id."""
        if not self._started:
            self.start()
        with self._lock:
            req_id = request.get("id")
            if req_id is None:
                req_id = self._alloc_id()
                request = {**request, "id": req_id}
            prepared = self._with_metadata(request) if self._mode == "modern" else request
            response = self._roundtrip(prepared, timeout=self._request_timeout)
            self._raise_if_error(response)
            return response

    def close(self) -> None:
        """Shutdown without waiting behind an in-flight request (AUD-22103).

        ``send_request`` 在锁内最长阻塞 ``request_timeout``（默认 180s）等响应；
        close 若排队等同一把锁，应用退出路径会被拖死。因此 close **不获取**
        ``self._lock``：置位关闭标志后直接终止子进程——阻塞中的 ``_roundtrip``
        会因 stdout EOF（泄放线程入队 None 哨兵）立即失败返回。与并发
        ``start()`` 竞争时 close 获胜（start 的握手因进程被终止而失败并清理）。
        """
        self._started = False
        self._mode = None
        self._terminate_current_process()

    def _terminate_current_process(self) -> None:
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ #
    # 帧读写 / 协议辅助
    # ------------------------------------------------------------------ #

    def _with_metadata(self, request: dict[str, Any]) -> dict[str, Any]:
        return add_request_metadata(
            request,
            protocol_version=_PROTOCOL_VERSION,
            client_name=_CLIENT_NAME,
            client_version=_CLIENT_VERSION,
        )

    def _roundtrip(self, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        self._write(request)
        req_id = request.get("id")
        if req_id is None:
            return {}
        # AUD-22102：等待固定在本代队列上——close→start 重启后 self._stdout_queue
        # 已换代，绝不从新队列消费（新队列的哨兵属于下一次请求）。
        out_queue = self._stdout_queue
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._send_cancel(req_id)
                raise McpTransportError(
                    f"MCP 请求超时（id={req_id}, method={request.get('method')}）；"
                    f"stderr={self._stderr_tail()}"
                )
            try:
                message = out_queue.get(timeout=remaining)
            except queue.Empty as exc:
                self._send_cancel(req_id)
                raise McpTransportError(
                    f"MCP 请求超时（id={req_id}, method={request.get('method')}）；"
                    f"stderr={self._stderr_tail()}"
                ) from exc
            if message is None:
                raise McpTransportError(
                    f"MCP 子进程已退出 / 无响应（id={req_id}）；stderr={self._stderr_tail()}"
                )
            if message.get("id") == req_id:
                return message
            _LOG.debug("mcp 忽略非匹配消息: %s", message.get("method") or message)

    def _send_cancel(self, request_id: Any) -> None:
        """Best-effort stdio cancellation; never masks the original timeout."""
        try:
            cancellation = {
                "jsonrpc": "2.0", "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": "timeout"},
            }
            if self._mode == "modern":
                cancellation = self._with_metadata(cancellation)
            self._write(cancellation)
        except Exception:  # noqa: BLE001
            pass

    def _write(self, msg: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpTransportError("MCP 子进程未运行（无法写入）")
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpTransportError(
                f"写入 MCP 子进程失败：{exc}；stderr={self._stderr_tail()}"
            ) from exc

    @staticmethod
    def _parse(line: bytes) -> dict[str, Any] | None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            _LOG.debug("mcp 非 JSON 行（忽略）: %s", text[:200])
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _raise_if_error(msg: dict[str, Any]) -> None:
        err = msg.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            raise McpTransportError(
                f"MCP JSON-RPC 错误 {code}: {err.get('message')}",
                code=code if isinstance(code, int) else None,
            )

    def _alloc_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    def _stderr_tail(self) -> str:
        return " | ".join(self._stderr_lines[-6:]) if self._stderr_lines else "(empty)"


__all__ = ["McpTransportError", "SubprocessStdioTransport"]
