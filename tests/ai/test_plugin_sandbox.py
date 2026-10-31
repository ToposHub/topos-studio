"""P17 platform launcher and external worker contract tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.features.ai_harness.plugin_isolation import ProcessIsolatedPlugin
from src.features.ai_harness.plugin_runtime import (
    PluginCapability,
    PluginExecutionPolicy,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    PluginSource,
)
from src.features.ai_harness.plugin_sandbox import (
    SANDBOX_WORKER_MODULE,
    DarwinSandboxLauncher,
    LinuxBubblewrapLauncher,
    PluginSandboxReport,
    _ExternalSandboxLauncherBase,
)


def test_darwin_launcher_requires_deny_default_profile() -> None:
    with pytest.raises(ValueError, match="deny default"):
        DarwinSandboxLauncher("(version 1)(allow default)")
    with pytest.raises(ValueError, match="not allow default"):
        DarwinSandboxLauncher("(version 1)(deny default)(allow default)")


def test_darwin_launcher_reports_enforcement_only_on_matching_host(monkeypatch) -> None:
    import src.features.ai_harness.plugin_sandbox as sandbox

    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    launcher = DarwinSandboxLauncher("(version 1)(deny default)")

    report = launcher.report()

    assert report.platform == "darwin"
    assert report.available is True
    assert report.enforced is True
    assert report.backend == "macOS sandbox-exec"


def test_darwin_launcher_builds_shell_free_worker_command(monkeypatch) -> None:
    launcher = DarwinSandboxLauncher(
        "(version 1)(deny default)",
        executable="sandbox-exec-test",
        python_executable="python-test",
    )
    captured: dict[str, object] = {}

    def capture(*, mp_context, args, name, command):
        del mp_context
        captured.update(args=args, name=name, command=tuple(command))
        return "process"

    monkeypatch.setattr(launcher, "_launch_process", capture)
    result = launcher.launch(
        mp_context=object(),
        target=object(),
        args=(object(), object(), "topos.test", object()),
        name="topos-plugin-test",
    )

    assert result == "process"
    assert captured["name"] == "topos-plugin-test"
    assert captured["command"] == (
        "sandbox-exec-test",
        "-p",
        "(version 1)(deny default)",
        "python-test",
        "-m",
        SANDBOX_WORKER_MODULE,
    )


def test_bubblewrap_requires_explicit_non_root_read_mount() -> None:
    with pytest.raises(ValueError, match="filesystem root"):
        LinuxBubblewrapLauncher(read_roots=("/",))
    with pytest.raises(ValueError, match="cannot be empty"):
        LinuxBubblewrapLauncher(read_roots=("",))
    with pytest.raises(ValueError, match="at least one read root"):
        LinuxBubblewrapLauncher(read_roots=())


def test_bubblewrap_builds_explicit_mount_and_network_policy(monkeypatch, tmp_path) -> None:
    project_root = tmp_path / "project"
    read_root = tmp_path / "readonly"
    write_root = tmp_path / "writable"
    launcher = LinuxBubblewrapLauncher(
        read_roots=(str(read_root),),
        write_roots=(str(write_root),),
        allow_network=False,
        executable="bwrap-test",
        python_executable="python-test",
        project_root=str(project_root),
    )
    captured: dict[str, object] = {}

    def capture(*, mp_context, args, name, command):
        del mp_context
        captured.update(args=args, name=name, command=tuple(command))
        return "process"

    monkeypatch.setattr(launcher, "_launch_process", capture)
    result = launcher.launch(
        mp_context=object(),
        target=object(),
        args=(object(), object(), "topos.test", object()),
        name="topos-plugin-test",
    )

    command = captured["command"]
    assert result == "process"
    assert isinstance(command, tuple)
    assert "--unshare-net" in command
    assert command[command.index("--ro-bind") + 1:command.index("--ro-bind") + 3] == (
        str(read_root.resolve()),
        str(read_root.resolve()),
    )
    assert command[command.index("--bind") + 1:command.index("--bind") + 3] == (
        str(write_root.resolve()),
        str(write_root.resolve()),
    )
    assert command[-5:] == (
        "--chdir",
        str(project_root.resolve()),
        "python-test",
        "-m",
        SANDBOX_WORKER_MODULE,
    )


class _ExternalWorkerPlugin:
    def __init__(self) -> None:
        self.active = False

    def activate(self, _context) -> None:
        self.active = True

    def deactivate(self) -> None:
        self.active = False

    def health(self) -> PluginHealth:
        return PluginHealth("healthy" if self.active else "inactive")

    def invoke(self, tool_name, args, *, approval=None):
        return {
            "ok": True,
            "tool_name": tool_name,
            "value": args.get("value"),
            "approval_seen": approval is not None,
        }


def _external_worker_factory() -> _ExternalWorkerPlugin:
    return _ExternalWorkerPlugin()


def _external_manifest() -> PluginManifest:
    return PluginManifest(
        plugin_id="topos.external-worker",
        display_name="External worker",
        source=PluginSource.LOCAL_DEV,
        capabilities=(PluginCapability(
            capability_id="external.inspect",
            tool_names=("topos.external.inspect",),
        ),),
    )


class _DirectExternalWorkerLauncher:
    """Test launcher for the external IPC path; it intentionally adds no OS sandbox."""

    def report(self) -> PluginSandboxReport:
        return PluginSandboxReport(
            platform="test",
            requested_mode="required",
            available=True,
            enforced=True,
            backend="test-external-worker",
        )

    def launch(self, *, mp_context, target, args, name):
        del target
        base = _ExternalSandboxLauncherBase(
            executable=sys.executable,
            python_executable=sys.executable,
            project_root=str(Path(__file__).resolve().parents[2]),
        )
        return base._launch_process(
            mp_context=mp_context,
            args=args,
            name=name,
            command=(sys.executable, "-m", SANDBOX_WORKER_MODULE),
        )


def test_external_worker_bootstrap_preserves_plugin_ipc() -> None:
    events: list[tuple[str, dict]] = []
    registry = PluginRegistry(
        event_sink=lambda _plugin_id, kind, payload: events.append((kind, payload)),
    )
    assert registry.register(
        _external_manifest(),
        ProcessIsolatedPlugin(
            _external_worker_factory,
            sandbox_launcher=_DirectExternalWorkerLauncher(),
        ),
        policy=PluginExecutionPolicy(
            isolation_mode="process",
            sandbox_mode="required",
        ),
    )
    assert registry.activate("topos.external-worker"), events
    try:
        result = registry.invoke_tool(
            "topos.external-worker",
            "topos.external.inspect",
            {"value": "ipc-ok"},
            run_id="run-external-worker",
        )
        assert result == {
            "ok": True,
            "tool_name": "topos.external.inspect",
            "value": "ipc-ok",
            "approval_seen": False,
        }
    finally:
        assert registry.deactivate("topos.external-worker")
