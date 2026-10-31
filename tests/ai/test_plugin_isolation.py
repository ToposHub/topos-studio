"""P14 process isolation and hard-stop contract tests."""

from __future__ import annotations

import threading
import time

from src.features.ai_harness.plugin_isolation import ProcessIsolatedPlugin
from src.features.ai_harness.plugin_runtime import (
    PluginCapability,
    PluginExecutionPolicy,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    PluginSource,
)
from src.features.ai_harness.plugin_broker import AllowlistResourceBroker, PathGrant
from src.features.ai_harness.plugin_sandbox import PluginSandboxReport


class _NonCooperativePlugin:
    def __init__(self) -> None:
        self.active = False

    def activate(self, _context) -> None:
        self.active = True

    def deactivate(self) -> None:
        self.active = False

    def health(self) -> PluginHealth:
        return PluginHealth("healthy" if self.active else "inactive")

    def invoke(self, tool_name, args, *, approval=None):
        delay = float(args.get("sleep_seconds", 0.0) or 0.0)
        if delay:
            time.sleep(delay)
        return {"ok": True, "tool_name": tool_name, "approval_seen": approval is not None}


def _non_cooperative_factory() -> _NonCooperativePlugin:
    return _NonCooperativePlugin()


def _manifest() -> PluginManifest:
    return PluginManifest(
        plugin_id="topos.isolated",
        display_name="Isolated plugin",
        source=PluginSource.LOCAL_DEV,
        capabilities=(PluginCapability(
            capability_id="isolated.inspect",
            tool_names=("topos.isolated.inspect",),
        ),),
    )


def _registry(events: list[tuple[str, dict]]) -> PluginRegistry:
    registry = PluginRegistry(
        event_sink=lambda _plugin_id, kind, payload: events.append((kind, payload)),
    )
    assert registry.register(
        _manifest(),
        ProcessIsolatedPlugin(_non_cooperative_factory),
        policy=PluginExecutionPolicy(
            timeout_seconds=2.0,
            isolation_mode="process",
        ),
    )
    assert registry.activate("topos.isolated")
    return registry


def test_process_isolation_returns_result_and_uses_spawn_boundary() -> None:
    events: list[tuple[str, dict]] = []
    registry = _registry(events)
    try:
        result = registry.invoke_tool(
            "topos.isolated",
            "topos.isolated.inspect",
            {"sleep_seconds": 0.01},
            run_id="run-isolated-1",
        )
        assert result["ok"] is True
        assert [kind for kind, _payload in events].count("plugin.tool_started") == 1
        assert [kind for kind, _payload in events].count("plugin.tool_completed") == 1
        completed = next(payload for kind, payload in events if kind == "plugin.tool_completed")
        assert completed["run_id"] == "run-isolated-1"
    finally:
        assert registry.deactivate("topos.isolated")


def test_process_isolation_hard_stops_non_cooperative_cancel() -> None:
    events: list[tuple[str, dict]] = []
    cancel_event = threading.Event()
    result: list[dict] = []
    started = threading.Event()

    def observe(kind: str, _payload: dict) -> None:
        if kind == "plugin.tool_started":
            started.set()

    def sink(_plugin_id: str, kind: str, payload: dict) -> None:
        events.append((kind, payload))
        observe(kind, payload)

    registry = PluginRegistry(event_sink=sink)
    assert registry.register(
        _manifest(),
        ProcessIsolatedPlugin(_non_cooperative_factory),
        policy=PluginExecutionPolicy(timeout_seconds=5.0, isolation_mode="process"),
    )
    assert registry.activate("topos.isolated")
    worker = threading.Thread(target=lambda: result.append(registry.invoke_tool(
        "topos.isolated",
        "topos.isolated.inspect",
        {"sleep_seconds": 5.0},
        cancel_event=cancel_event,
        run_id="run-isolated-cancel",
    )))
    started_at = time.monotonic()
    worker.start()
    assert started.wait(timeout=2.0)
    cancel_event.set()
    worker.join(timeout=2.0)
    elapsed = time.monotonic() - started_at
    try:
        assert not worker.is_alive()
        assert result[0]["diagnostics"][0]["code"] == "PLUGIN_CANCELLED"
        assert elapsed < 2.0
        assert any(kind == "plugin.tool_cancelled" for kind, _payload in events)
    finally:
        assert registry.deactivate("topos.isolated")


def test_process_isolation_hard_stops_non_cooperative_timeout() -> None:
    events: list[tuple[str, dict]] = []
    registry = PluginRegistry(
        event_sink=lambda _plugin_id, kind, payload: events.append((kind, payload)),
    )
    assert registry.register(
        _manifest(),
        ProcessIsolatedPlugin(_non_cooperative_factory),
        policy=PluginExecutionPolicy(timeout_seconds=0.05, isolation_mode="process"),
    )
    assert registry.activate("topos.isolated")
    started_at = time.monotonic()
    try:
        result = registry.invoke_tool(
            "topos.isolated",
            "topos.isolated.inspect",
            {"sleep_seconds": 5.0},
            run_id="run-isolated-timeout",
        )
        assert result["diagnostics"][0]["code"] == "PLUGIN_TIMEOUT"
        assert time.monotonic() - started_at < 2.0
        assert any(kind == "plugin.tool_timed_out" for kind, _payload in events)
    finally:
        assert registry.deactivate("topos.isolated")


def test_process_policy_rejects_plain_in_process_plugin() -> None:
    registry = PluginRegistry()
    assert not registry.register(
        _manifest(),
        _NonCooperativePlugin(),
        policy=PluginExecutionPolicy(isolation_mode="process"),
    )


class _HostIOPlugin:
    def __init__(self) -> None:
        self.active = False

    def activate(self, _context) -> None:
        self.active = True

    def deactivate(self) -> None:
        self.active = False

    def health(self) -> PluginHealth:
        return PluginHealth("healthy" if self.active else "inactive")

    def invoke(self, _tool_name, args, *, approval=None, context=None):
        result = context.host_io.read_bytes(args["path"])
        return {"ok": result.ok, "code": result.code, "text": result.data.decode("utf-8")}


def _host_io_factory() -> _HostIOPlugin:
    return _HostIOPlugin()


def test_process_plugin_uses_host_io_rpc_instead_of_opening_files() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as root:
        path = f"{root}/clip.txt"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("clip")
        events: list[tuple[str, dict]] = []
        registry = PluginRegistry(
            event_sink=lambda _pid, kind, payload: events.append((kind, payload)),
            resource_broker=AllowlistResourceBroker(
                path_grants={"topos.isolated": (PathGrant(root),)},
            ),
        )
        assert registry.register(
            _manifest(),
            ProcessIsolatedPlugin(_host_io_factory),
            policy=PluginExecutionPolicy(
                allow_filesystem=True,
                isolation_mode="process",
            ),
        )
        assert registry.activate("topos.isolated")
        try:
            result = registry.invoke_tool(
                "topos.isolated",
                "topos.isolated.inspect",
                {"path": path},
                run_id="run-host-io",
            )
            assert result["ok"] is True
            assert result["text"] == "clip"
            assert any(kind == "plugin.sandbox_degraded" for kind, _payload in events)
        finally:
            assert registry.deactivate("topos.isolated")


def test_required_os_sandbox_fails_closed_without_launcher() -> None:
    registry = PluginRegistry()
    assert registry.register(
        _manifest(),
        ProcessIsolatedPlugin(_non_cooperative_factory),
        policy=PluginExecutionPolicy(
            isolation_mode="process",
            sandbox_mode="required",
        ),
    )
    assert registry.activate("topos.isolated") is False
    assert registry.get("topos.isolated").state.value == "failed"


class _FakeEnforcedSandboxLauncher:
    """Test-only launcher proving the injected launcher contract is used."""

    def report(self) -> PluginSandboxReport:
        return PluginSandboxReport(
            platform="test",
            requested_mode="required",
            available=True,
            enforced=True,
            backend="test-sandbox",
        )

    def launch(self, *, mp_context, target, args, name):
        return mp_context.Process(
            target=target,
            args=args,
            name=name,
            daemon=True,
        )


def test_required_sandbox_uses_injected_launcher_contract() -> None:
    registry = PluginRegistry()
    plugin = ProcessIsolatedPlugin(
        _non_cooperative_factory,
        sandbox_launcher=_FakeEnforcedSandboxLauncher(),
    )
    assert registry.register(
        _manifest(),
        plugin,
        policy=PluginExecutionPolicy(
            isolation_mode="process",
            sandbox_mode="required",
        ),
    )
    assert registry.activate("topos.isolated")
    try:
        result = registry.invoke_tool(
            "topos.isolated",
            "topos.isolated.inspect",
            {},
            run_id="run-required-sandbox",
        )
        assert result["ok"] is True
        assert plugin.sandbox_report().enforced is True
        assert plugin.sandbox_report().backend == "test-sandbox"
    finally:
        assert registry.deactivate("topos.isolated")
