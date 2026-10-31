"""Honest cross-platform plugin sandbox capability reporting.

Finding ``sandbox-exec``/``bwrap`` is not the same as applying a sandbox to a
running multiprocessing child.  This module reports that distinction so the
host can require an injected OS launcher instead of silently downgrading to a
plain process boundary.
"""

from __future__ import annotations

import platform
import os
from pathlib import Path
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence


SANDBOX_WORKER_MODULE = "src.features.ai_harness.plugin_sandbox_worker"

#: Environment keys the sandboxed worker may inherit from the host process.
#: The worker unpickles and runs third-party plugin code, so anything outside
#: this allowlist (API keys, tokens, other host secrets) must never become
#: implicitly readable inside the sandbox (AUD-22205).  Only boot-critical
#: keys are listed; ``PYTHONPATH`` is rebuilt separately below.
_WORKER_ENV_ALLOWLIST: frozenset[str] = frozenset({
    # Executable lookup / locale basics needed to boot Python itself.
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    # POSIX basics.
    "HOME",
    "TMPDIR",
    # Windows: CPython refuses to start without SYSTEMROOT and friends.
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
})


def _minimal_worker_env() -> dict[str, str]:
    """Build the minimal allowlisted environment for a sandboxed worker.

    The full host ``os.environ`` is deliberately NOT copied: the worker runs
    third-party plugin code and must not implicitly receive host credentials
    (AUD-22205).
    """

    env: dict[str, str] = {}
    for key in sorted(_WORKER_ENV_ALLOWLIST):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


@dataclass(frozen=True)
class PluginSandboxReport:
    platform: str
    requested_mode: str
    available: bool
    enforced: bool
    backend: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "requested_mode": self.requested_mode,
            "available": self.available,
            "enforced": self.enforced,
            "backend": self.backend,
            "reason": self.reason[:500],
        }


class PluginSandboxLauncher(Protocol):
    """Host-injected launcher that can prove an OS sandbox is enforced."""

    def report(self) -> PluginSandboxReport:
        """Return the backend and whether this launcher enforces it."""
        ...

    def launch(
        self,
        *,
        mp_context: Any,
        target: Callable[..., Any],
        args: tuple[Any, ...],
        name: str,
    ) -> Any:
        """Start the sandboxed child using the host's IPC-compatible target."""
        ...


def validate_sandbox_launcher(
    launcher: PluginSandboxLauncher | None,
    *,
    requested_mode: str,
) -> PluginSandboxReport:
    """Validate an injected launcher without trusting a mere executable name."""

    if launcher is None:
        return detect_plugin_sandbox(requested_mode=requested_mode)
    report = launcher.report()
    if not isinstance(report, PluginSandboxReport):
        raise TypeError("sandbox launcher report must be PluginSandboxReport")
    return PluginSandboxReport(
        platform=report.platform,
        requested_mode=str(requested_mode),
        available=bool(report.available),
        enforced=bool(report.enforced),
        backend=report.backend,
        reason=report.reason,
    )


class _ExternalSandboxProcess:
    """Process facade matching the small lifecycle surface used by the host."""

    def __init__(
        self,
        command: Sequence[str],
        connection: Any,
        bootstrap_sender: Any,
        bootstrap_receiver: Any,
        bootstrap: dict[str, Any],
        *,
        cwd: str,
        env: dict[str, str],
    ) -> None:
        self._command = tuple(str(item) for item in command)
        self._connection = connection
        self._bootstrap_sender = bootstrap_sender
        self._bootstrap_receiver = bootstrap_receiver
        self._bootstrap = bootstrap
        self._cwd = cwd
        self._env = env
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._process is not None:
            return
        ipc_fd = int(self._connection.fileno())
        bootstrap_fd = int(self._bootstrap_receiver.fileno())
        env = dict(self._env)
        env["TOPOS_PLUGIN_IPC_FD"] = str(ipc_fd)
        env["TOPOS_PLUGIN_BOOTSTRAP_FD"] = str(bootstrap_fd)
        try:
            self._process = subprocess.Popen(
                list(self._command),
                cwd=self._cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(ipc_fd, bootstrap_fd),
            )
            self._bootstrap_sender.send(self._bootstrap)
            self._bootstrap_sender.close()
            self._bootstrap_receiver.close()
        except Exception:
            process = self._process
            self._process = None
            if process is not None:
                try:
                    process.kill()
                    process.wait(timeout=0.5)
                except (OSError, subprocess.SubprocessError):
                    pass
            for connection in (self._bootstrap_sender, self._bootstrap_receiver):
                try:
                    connection.close()
                except (OSError, EOFError):
                    pass
            raise

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    def kill(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()

    def join(self, timeout: float | None = None) -> None:
        if self._process is None:
            return
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return


class _ExternalSandboxLauncherBase:
    """Shared command/bootstrap implementation for OS command launchers."""

    backend_name = "external-sandbox"

    def __init__(
        self,
        *,
        executable: str,
        python_executable: str | None = None,
        project_root: str | None = None,
        reason: str = "",
    ) -> None:
        self._executable = str(executable)
        self._python = str(python_executable or sys.executable)
        self._project_root = str(Path(project_root or Path(__file__).resolve().parents[3]).resolve())
        self._reason = str(reason)

    def _report(self, *, system: str, requested_mode: str) -> PluginSandboxReport:
        available = platform.system().lower() == system and bool(shutil.which(self._executable))
        return PluginSandboxReport(
            platform=system,
            requested_mode=requested_mode,
            available=available,
            enforced=available,
            backend=self.backend_name if available else "",
            reason=(
                self._reason
                or ("launcher applies an OS policy; profile/root review remains host responsibility" if available
                    else f"{self._executable} is unavailable on this platform")
            ),
        )

    def _launch_process(
        self,
        *,
        mp_context: Any,
        args: tuple[Any, ...],
        name: str,
        command: Sequence[str],
    ) -> _ExternalSandboxProcess:
        if len(args) != 4:
            raise TypeError("sandbox launcher expects worker args: connection, factory, plugin_id, policy")
        connection, factory, plugin_id, policy = args
        bootstrap_sender, bootstrap_receiver = mp_context.Pipe(duplex=True)
        bootstrap = {
            "factory": factory,
            "plugin_id": str(plugin_id),
            "policy": policy,
        }
        # AUD-22205: minimal allowlisted environment.  The worker runs
        # third-party plugin code; inheriting the host's full os.environ
        # would hand it every host credential.  PYTHONPATH is still rebuilt
        # from the host import surface so the worker resolves the same
        # modules (paths only, never secrets).
        env = _minimal_worker_env()
        prior_pythonpath = os.environ.get("PYTHONPATH", "")
        python_paths: list[str] = [self._project_root]
        for candidate in (*sys.path, *prior_pythonpath.split(os.pathsep)):
            candidate = str(candidate).strip()
            if candidate and candidate not in python_paths:
                python_paths.append(candidate)
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        return _ExternalSandboxProcess(
            command,
            connection,
            bootstrap_sender,
            bootstrap_receiver,
            bootstrap,
            cwd=self._project_root,
            env=env,
        )


class DarwinSandboxLauncher(_ExternalSandboxLauncherBase):
    """Launch the plugin worker through macOS ``sandbox-exec``.

    The profile is supplied by the host/deployment layer and passed as one
    argv item; no shell is involved.  The launcher reports enforced only when
    the executable is present and will apply that profile to the worker.
    """

    backend_name = "macOS sandbox-exec"

    def __init__(
        self,
        profile: str,
        *,
        python_executable: str | None = None,
        project_root: str | None = None,
        executable: str = "sandbox-exec",
    ) -> None:
        profile = str(profile or "").strip()
        if (
            not profile.startswith("(version")
            or "(deny default)" not in profile
            or "(allow default)" in profile
        ):
            raise ValueError(
                "macOS sandbox profile must start with version, deny default, and not allow default"
            )
        self._profile = profile
        super().__init__(
            executable=executable,
            python_executable=python_executable,
            project_root=project_root,
            reason="sandbox-exec applies the supplied deny-default profile; profile review remains host responsibility",
        )

    def report(self) -> PluginSandboxReport:
        return self._report(system="darwin", requested_mode="required")

    def launch(
        self,
        *,
        mp_context: Any,
        target: Callable[..., Any],
        args: tuple[Any, ...],
        name: str,
    ) -> _ExternalSandboxProcess:
        del target
        command = (
            self._executable,
            "-p",
            self._profile,
            self._python,
            "-m",
            SANDBOX_WORKER_MODULE,
        )
        return self._launch_process(
            mp_context=mp_context,
            args=args,
            name=name,
            command=command,
        )


class LinuxBubblewrapLauncher(_ExternalSandboxLauncherBase):
    """Launch through bubblewrap with explicit read/write roots."""

    backend_name = "Linux bubblewrap"

    def __init__(
        self,
        *,
        read_roots: Sequence[str],
        write_roots: Sequence[str] = (),
        allow_network: bool = False,
        python_executable: str | None = None,
        project_root: str | None = None,
        executable: str = "bwrap",
    ) -> None:
        self._read_roots = _validate_roots(read_roots)
        self._write_roots = _validate_roots(write_roots)
        if not self._read_roots:
            raise ValueError("bubblewrap launcher requires at least one read root")
        self._allow_network = bool(allow_network)
        super().__init__(
            executable=executable,
            python_executable=python_executable,
            project_root=project_root,
            reason="bubblewrap applies explicit mount and namespace isolation; root review remains host responsibility",
        )

    def report(self) -> PluginSandboxReport:
        return self._report(system="linux", requested_mode="required")

    def launch(
        self,
        *,
        mp_context: Any,
        target: Callable[..., Any],
        args: tuple[Any, ...],
        name: str,
    ) -> _ExternalSandboxProcess:
        del target
        command: list[str] = [
            self._executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
        ]
        if not self._allow_network:
            command.append("--unshare-net")
        for root in self._read_roots:
            command.extend(("--ro-bind", root, root))
        for root in self._write_roots:
            command.extend(("--bind", root, root))
        command.extend(("--chdir", self._project_root, self._python, "-m", SANDBOX_WORKER_MODULE))
        return self._launch_process(
            mp_context=mp_context,
            args=args,
            name=name,
            command=command,
        )


def _validate_roots(roots: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for root in roots:
        raw_root = str(root).strip()
        if not raw_root:
            raise ValueError("sandbox root cannot be empty")
        resolved = str(Path(raw_root).expanduser().resolve())
        if resolved == "/":
            raise ValueError("sandbox root cannot be filesystem root")
        if resolved not in result:
            result.append(resolved)
    return tuple(result)


def detect_plugin_sandbox(*, requested_mode: str = "report") -> PluginSandboxReport:
    """Report candidate OS backends without claiming that one is active."""

    system = platform.system().lower() or "unknown"
    candidates = {
        "darwin": (("sandbox-exec", "macOS sandbox-exec"),),
        "linux": (
            ("bwrap", "Bubblewrap"),
            ("firejail", "Firejail"),
        ),
        "windows": (),
    }
    backend = ""
    for executable, name in candidates.get(system, ()):
        if shutil.which(executable):
            backend = name
            break
    if backend:
        return PluginSandboxReport(
            platform=system,
            requested_mode=str(requested_mode),
            available=True,
            enforced=False,
            backend=backend,
            reason="OS backend detected; an explicit launcher is still required",
        )
    return PluginSandboxReport(
        platform=system,
        requested_mode=str(requested_mode),
        available=False,
        enforced=False,
        reason="no supported OS sandbox backend detected",
    )


__all__ = [
    "DarwinSandboxLauncher",
    "LinuxBubblewrapLauncher",
    "PluginSandboxLauncher",
    "PluginSandboxReport",
    "detect_plugin_sandbox",
    "validate_sandbox_launcher",
]
