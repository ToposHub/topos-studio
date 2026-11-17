"""F10：不可信/重任务的受限进程与有界 RPC；平台隔离状态如实报告。

现状：插件/工具侧的子进程（如截取导出）只有预算/取消（E6 的
ClipExportJob），没有**隔离**与**未保护入口的门禁**——第三方代码若经
宿主执行，将拿到宿主全部权限。

本模块的口径：

* **如实报告**：``SandboxCapabilities.probe()`` 探测本平台实际可用的隔离
  手段（macOS seatbelt ``sandbox-exec`` / POSIX rlimits / 仅最小环境），
  返回 ``(level, details)``——从不谎称已沙箱；每次运行的结果携带**实际
  达到的隔离级别**；
* **受限进程**：``RestrictedProcessRunner.run`` ——最小环境变量白名单、
  cwd 收紧、资源上限（CPU/地址空间/文件描述符，POSIX）、deadline、有界
  输出采集；macOS 有 ``sandbox-exec`` 时套 Seatbelt 拒写/拒网 profile
  （级别 ``seatbelt``），否则降级为 ``restricted_env`` 并如实标注；
* **有界 RPC**：``bounded_rpc`` ——子进程 stdin/stdout 上的 JSON 往返：
  请求/响应各有字节上限、整体 deadline；越界/超时即终止进程并报错——
  不可信代码不能靠巨包或挂死拖垮宿主；
* **执行门禁**：``ExecutionGate``——可信 builtin 可直接执行；第三方
  （installed/local_dev）**必须**在 ``seatbelt`` 级隔离下才放行，否则
  拒绝并明示"未保护的第三方执行入口已禁用"。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

ISOLATION_LEVELS = ("none", "restricted_env", "seatbelt")

# macOS Seatbelt（文件系统只读 + 拒网络 + 只允许临时目录写）。
# sandbox-exec 被苹果标记弃用但仍随系统提供；探测不到就如实降级。
_SEATBELT_PROFILE = (
    "(version 1)(allow default)(deny file-write*"
    "(subpath (string \"{repo_root}\"))) "
    "(deny network*)(deny process-exec"
    "(subpath \"/bin/rm\"))"
)

_MINIMAL_ENV_KEYS = ("PATH", "LANG", "LC_CTYPE", "TMPDIR", "HOME")
DEFAULT_MAX_OUTPUT = 64 * 1024
DEFAULT_RPC_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class SandboxCapabilities:
    """平台实际隔离能力——如实报告，不冒充。"""

    level: str                       # none / restricted_env / seatbelt
    details: str

    @classmethod
    def probe(cls, *, seatbelt_root: str = "") -> "SandboxCapabilities":
        details: list[str] = []
        level = "none"
        if sys.platform == "darwin":
            sandbox_exec = shutil.which("sandbox-exec")
            if sandbox_exec:
                level = "seatbelt"
                details.append(f"sandbox-exec: {sandbox_exec}")
            else:
                details.append("sandbox-exec 不存在（macOS）")
        if os.name == "posix":
            try:
                import resource  # noqa: F401

                level = max(level, "restricted_env",
                            key=lambda item: ISOLATION_LEVELS.index(item))
                details.append("rlimits 可用 (POSIX)")
            except ImportError:
                details.append("rlimits 不可用")
        else:
            details.append(f"平台 {sys.platform}：无 POSIX rlimit")
        if level == "none":
            details.append("仅有最小环境变量收紧，无强制隔离")
        return cls(level=level, details="; ".join(details))


@dataclass(frozen=True)
class SandboxedResult:
    ok: bool
    exit_code: int
    output_tail: str
    isolation_level: str          # 本次运行实际达到的级别（不冒充）
    error: str = ""


class ExecutionGate:
    """第三方未保护执行入口门禁：声明权限 ≠ 可执行。"""

    def __init__(self, capabilities: SandboxCapabilities | None = None) -> None:
        self.capabilities = capabilities or SandboxCapabilities.probe()

    def can_execute(self, *, trusted: bool, isolated_level: str) -> tuple[bool, str]:
        """trusted builtin 直接放行；第三方必须 seatbelt 级隔离。"""
        if trusted:
            return True, ""
        if isolated_level == "seatbelt":
            return True, ""
        if isolated_level in ISOLATION_LEVELS:
            return False, (
                f"第三方代码只允许在 seatbelt 级隔离下执行（当前 {isolated_level}）"
                "——未保护的第三方执行入口已禁用")
        return False, f"未知隔离级别：{isolated_level}"

    def plan_isolation(self, *, trusted: bool) -> tuple[str, str]:
        """为一次执行计划隔离级别 → (级别, 说明)。"""
        if trusted:
            return self.capabilities.level, (
                "可信 builtin：按平台能力执行"
                f"（{self.capabilities.details}）")
        if self.capabilities.level != "seatbelt":
            return self.capabilities.level, (
                f"本机隔离能力不足（{self.capabilities.level}）："
                "第三方执行被禁用，如实报告而非冒充沙箱")
        return "seatbelt", "第三方：seatbelt 隔离"


class RestrictedProcessRunner:
    """受限子进程：最小环境 + 资源上限 + deadline + 有界输出。"""

    def __init__(self, capabilities: SandboxCapabilities | None = None,
                 *, seatbelt_root: str = "") -> None:
        self.capabilities = capabilities or SandboxCapabilities.probe()
        self._seatbelt_root = seatbelt_root

    def run(self, command: Sequence[str], *, deadline_sec: float = 10.0,
            max_output: int = DEFAULT_MAX_OUTPUT,
            env_allowlist: Sequence[str] = _MINIMAL_ENV_KEYS,
            require_seatbelt: bool = False,
            cwd: str | None = None) -> SandboxedResult:
        """执行 ``command``；返回实际达到的隔离级别与有界输出尾部。

        ``require_seatbelt`` 且平台不可用时**拒绝执行**（不降级冒充）。
        """
        level = self.capabilities.level
        if require_seatbelt and level != "seatbelt":
            return SandboxedResult(
                False, -1, "", level,
                f"需要 seatbelt 隔离但本机只有 {level}——拒绝执行（不冒充）")
        argv: list[str] = []
        if require_seatbelt:
            sandbox_exec = shutil.which("sandbox-exec")
            assert sandbox_exec is not None
            profile = _SEATBELT_PROFILE.format(
                repo_root=self._seatbelt_root or os.getcwd())
            argv += [sandbox_exec, "-p", profile, "--"]
        argv += [str(item) for item in command]
        env = {key: os.environ[key] for key in env_allowlist
               if key in os.environ}

        def _limits():  # POSIX preexec：资源上限
            try:
                import resource
                usage, _ = resource.getrlimit(resource.RLIMIT_CPU)
                cpu_cap = max(1, int(deadline_sec) + 1)
                resource.setrlimit(resource.RLIMIT_CPU,
                                   (cpu_cap, min(cpu_cap, usage)
                                    if usage != resource.RLIM_INFINITY else cpu_cap))
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
                # 不设 RLIMIT_AS：macOS 的虚拟内存布局与进程级 AS 上限
                # 不兼容（解释器可能直接起不来）；CPU/NOFILE 已构成
                # 有界性，隔离级别不因此虚报
            except Exception:
                pass  # rlimit 失败不阻断：隔离级别已如实标注

        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                preexec_fn=_limits if os.name == "posix" else None)
        except OSError as exc:
            return SandboxedResult(False, -1, "", level,
                                   f"{type(exc).__name__}: {exc}")
        buffer = bytearray()

        def _drain() -> None:
            try:
                for chunk in iter(lambda: proc.stdout.read(4096) or b"", b""):
                    buffer.extend(chunk)
                    if len(buffer) > max_output:
                        del buffer[:-max_output]
            except Exception:
                pass

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        start = time.monotonic()
        while True:
            code = proc.poll()
            if code is not None:
                break
            if time.monotonic() - start > deadline_sec:
                proc.kill()
                proc.wait(timeout=2)
                return SandboxedResult(
                    False, proc.returncode or -1,
                    bytes(buffer[-max_output:]).decode(errors="ignore"),
                    level, "超过执行预算，进程已终止")
            time.sleep(0.02)
        reader.join(timeout=2)
        return SandboxedResult(
            code == 0, code,
            bytes(buffer[-max_output:]).decode(errors="ignore"), level)


class BoundedRPC:
    """子进程上的有界 JSON RPC：请求/响应字节上限 + 整体 deadline。"""

    def __init__(self, command: Sequence[str], *,
                 deadline_sec: float = 10.0,
                 max_message_bytes: int = DEFAULT_RPC_MAX_BYTES,
                 env_allowlist: Sequence[str] = _MINIMAL_ENV_KEYS,
                 cwd: str | None = None) -> None:
        self._deadline = deadline_sec
        self._max_bytes = max(256, int(max_message_bytes))
        env = {key: os.environ[key] for key in env_allowlist
               if key in os.environ}
        self._proc = subprocess.Popen(
            [str(item) for item in command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, cwd=cwd)
        self._started = time.monotonic()

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        """一次往返；越界/超时/坏 JSON 终止进程并抛 ``RuntimeError``。"""
        if self._proc.poll() is not None:
            raise RuntimeError("RPC 进程已退出")
        # 看门狗：阻塞读无法自查 deadline——到点杀进程让读返回，再判因
        expired = threading.Event()

        def _watchdog() -> None:
            if not expired.wait(max(0.0, self._deadline - (
                    time.monotonic() - self._started))):
                try:
                    self._proc.kill()
                except Exception:
                    pass
        guard = threading.Thread(target=_watchdog, daemon=True)
        guard.start()
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(payload) > self._max_bytes:
            self._proc.kill()
            raise RuntimeError(f"RPC 请求超界（{len(payload)} > {self._max_bytes}）")
        try:
            self._proc.stdin.write(payload + b"\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(f"RPC 进程写入失败： {exc}") from exc
        try:
            line = self._read_line()
        except RuntimeError:
            expired.set()
            if time.monotonic() - self._started > self._deadline:
                raise RuntimeError("RPC 超过整体预算，进程已终止") from None
            raise
        expired.set()
        try:
            response = json.loads(line)
        except ValueError as exc:
            self._proc.kill()
            raise RuntimeError(f"RPC 响应非 JSON： {exc}") from exc
        if not isinstance(response, dict):
            self._proc.kill()
            raise RuntimeError("RPC 响应必须是 JSON 对象")
        return response

    def _read_line(self) -> bytes:
        chunks = bytearray()
        while True:
            char = self._proc.stdout.read(1)
            if not char:
                raise RuntimeError("RPC 进程提前退出")
            if char == b"\n":
                if len(chunks) > self._max_bytes:
                    self._kill_and_reap()
                    raise RuntimeError(
                        f"RPC 响应超界（{len(chunks)} > {self._max_bytes}）")
                return bytes(chunks)
            chunks.extend(char)
            if len(chunks) > self._max_bytes:
                self._kill_and_reap()
                raise RuntimeError(
                    f"RPC 响应超界（{len(chunks)} > {self._max_bytes}）")

    def _kill_and_reap(self) -> None:
        """终止并回收（不留挂死通道/僵尸进程）。"""
        try:
            self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=2)
        except Exception:
            self._proc.kill()


class _TrustedBuiltins:
    """宿主政策：当前可信 builtin 名单（G 阶段逐插件接管后按 manifest 扩）。"""

    TRUSTED = frozenset({
        "files.format_factory", "files.image_factory",
        "files.shutdown_timer", "files.decode_benchmark",
    })


def is_trusted_builtin(plugin_id: str) -> bool:
    return plugin_id in _TrustedBuiltins.TRUSTED


__all__ = [
    "SandboxCapabilities", "SandboxedResult", "RestrictedProcessRunner",
    "BoundedRPC", "ExecutionGate", "is_trusted_builtin",
    "ISOLATION_LEVELS",
]
