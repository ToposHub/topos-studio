"""P0 plugin manifest and lifecycle contract tests."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from src.features.ai_harness.plugin_runtime import (
    PluginCapability,
    PluginHealth,
    PluginExecutionPolicy,
    PluginManifest,
    PluginRegistry,
    PluginSource,
    PluginState,
)
from src.features.ai_harness.plugin_broker import (
    AllowlistResourceBroker,
    NetworkGrant,
    PathGrant,
)


def _manifest(*, declared_hash: str = "") -> PluginManifest:
    return PluginManifest(
        plugin_id="topos.subtitle",
        display_name="Subtitle Plugin",
        source=PluginSource.LOCAL_DEV,
        capabilities=(PluginCapability(
            capability_id="subtitle.inspect",
            description="inspect subtitles",
            tool_names=("topos.plugin.inspect_subtitle",),
            permission="read",
        ),),
        declared_hash=declared_hash,
    )


class _Plugin:
    def __init__(self, *, fail=False, block=False) -> None:
        self.fail = fail
        self.block = block
        self.active = False
        self.calls = 0
        self.output = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.context = None

    def activate(self, context) -> None:
        if self.fail:
            raise RuntimeError("plugin boot failed")
        self.active = True
        self.context = context

    def deactivate(self) -> None:
        self.active = False

    def health(self) -> PluginHealth:
        return PluginHealth("healthy" if self.active else "inactive")

    def invoke(self, tool_name, args, *, approval=None):
        self.calls += 1
        if self.block:
            self.started.set()
            self.release.wait(timeout=2)
        if self.output is not None:
            return self.output
        return {"ok": True, "tool_name": tool_name, "args": dict(args)}


def test_manifest_hash_and_local_source_are_deterministic() -> None:
    manifest = _manifest()
    assert len(manifest.content_hash) == 64
    assert manifest.unverified
    assert _manifest().content_hash == manifest.content_hash
    assert replace(manifest, declared_hash=manifest.content_hash).verify_declared_hash()


def test_manifest_rejects_forbidden_capability() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        PluginCapability(
            capability_id="unsafe",
            tool_names=("call_shell",),
        )
    assert PluginCapability(
        capability_id="write",
        permission="write",
    ).requires_approval


def test_registry_isolates_activation_failures_and_emits_safe_events() -> None:
    events: list[tuple[str, str, dict]] = []
    registry = PluginRegistry(
        event_sink=lambda plugin_id, kind, payload: events.append((plugin_id, kind, payload)),
    )
    plugin = _Plugin()
    manifest = _manifest()
    assert registry.register(manifest, plugin)
    assert registry.activate(manifest.plugin_id)
    record = registry.get(manifest.plugin_id)
    assert record is not None
    assert record.state == PluginState.ACTIVE
    assert registry.providers_for_capability("subtitle.inspect") == (manifest.plugin_id,)
    assert registry.deactivate(manifest.plugin_id)
    assert registry.get(manifest.plugin_id).state == PluginState.ENABLED

    failed_manifest = replace(manifest, plugin_id="topos.failed")
    assert registry.register(failed_manifest, _Plugin(fail=True))
    assert not registry.activate(failed_manifest.plugin_id)
    assert registry.get(failed_manifest.plugin_id).state == PluginState.FAILED
    assert any(kind == "plugin.failed" for _, kind, _ in events)


def test_registry_rejects_hash_mismatch_and_duplicate_registration() -> None:
    registry = PluginRegistry()
    manifest = _manifest()
    assert not registry.register(replace(manifest, declared_hash="bad"))
    assert registry.register(manifest)
    assert not registry.register(manifest)
    assert not registry.register(replace(manifest, version="0.9.0"), replace_existing=True)
    assert registry.register(replace(manifest, version="1.1.0"), replace_existing=True)
    assert registry.get(manifest.plugin_id).state == PluginState.ENABLED
    assert registry.get(manifest.plugin_id).manifest.version == "1.1.0"


def test_manifest_only_plugin_can_be_activated_and_disabled() -> None:
    registry = PluginRegistry()
    manifest = _manifest()
    assert registry.register(manifest)
    assert registry.activate(manifest.plugin_id)
    assert registry.get(manifest.plugin_id).health.status == "healthy"
    assert registry.disable(manifest.plugin_id) is False
    assert registry.deactivate(manifest.plugin_id)
    assert registry.disable(manifest.plugin_id)
    assert registry.get(manifest.plugin_id).state == PluginState.DISABLED


def test_execution_policy_enforces_input_and_output_boundaries() -> None:
    registry = PluginRegistry()
    plugin = _Plugin()
    manifest = _manifest()
    policy = PluginExecutionPolicy(max_input_chars=20, max_output_chars=40)
    assert registry.register(manifest, plugin, policy=policy)
    assert registry.activate(manifest.plugin_id)

    oversized_input = registry.invoke_tool(
        manifest.plugin_id,
        "topos.plugin.inspect_subtitle",
        {"text": "x" * 100},
    )
    assert oversized_input["diagnostics"][0]["code"] == "PLUGIN_INPUT_LIMIT"
    assert plugin.calls == 0

    plugin.output = {"ok": True, "text": "x" * 100}
    oversized_output = registry.invoke_tool(
        manifest.plugin_id,
        "topos.plugin.inspect_subtitle",
        {},
    )
    assert oversized_output["diagnostics"][0]["code"] == "PLUGIN_OUTPUT_LIMIT"
    assert plugin.calls == 1


def test_execution_policy_rejects_parallel_call_when_limit_is_reached() -> None:
    registry = PluginRegistry()
    plugin = _Plugin(block=True)
    manifest = _manifest()
    assert registry.register(
        manifest,
        plugin,
        policy=PluginExecutionPolicy(max_concurrent_calls=1),
    )
    assert registry.activate(manifest.plugin_id)

    result: list[dict] = []
    worker = threading.Thread(
        target=lambda: result.append(registry.invoke_tool(
            manifest.plugin_id,
            "topos.plugin.inspect_subtitle",
            {},
        )),
    )
    worker.start()
    assert plugin.started.wait(timeout=1)
    busy = registry.invoke_tool(manifest.plugin_id, "topos.plugin.inspect_subtitle", {})
    assert busy["diagnostics"][0]["code"] == "PLUGIN_BUSY"
    plugin.release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result[0]["ok"] is True


def test_execution_policy_requires_positive_limits() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        PluginExecutionPolicy(max_concurrent_calls=0)
    with pytest.raises(ValueError, match="positive integer"):
        PluginExecutionPolicy(max_input_chars=True)
    with pytest.raises(ValueError, match="positive number"):
        PluginExecutionPolicy(timeout_seconds=0)


def test_plugin_context_uses_policy_bound_path_and_network_broker(tmp_path) -> None:
    plugin = _Plugin()
    manifest = _manifest()
    broker = AllowlistResourceBroker(
        path_grants={manifest.plugin_id: (PathGrant(str(tmp_path)),)},
        network_grants={manifest.plugin_id: (NetworkGrant("api.example.com"),)},
    )
    registry = PluginRegistry(resource_broker=broker)
    assert registry.register(
        manifest,
        plugin,
        policy=PluginExecutionPolicy(allow_filesystem=True, allow_network=True),
    )
    assert registry.activate(manifest.plugin_id)
    assert plugin.context is not None

    allowed_path = plugin.context.resource_broker.check_path(
        manifest.plugin_id, str(tmp_path / "clip.json"), mode="read",
    )
    escaped_path = plugin.context.resource_broker.check_path(
        manifest.plugin_id, str(tmp_path / ".." / "outside.json"), mode="read",
    )
    allowed_url = plugin.context.resource_broker.check_network(
        manifest.plugin_id, "https://api.example.com/v1/check",
    )
    denied_url = plugin.context.resource_broker.check_network(
        manifest.plugin_id, "https://evil.example.net/v1/check",
    )

    assert allowed_path.allowed is True
    assert escaped_path.code == "PLUGIN_PATH_NOT_ALLOWLISTED"
    assert allowed_url.allowed is True
    assert denied_url.code == "PLUGIN_NETWORK_NOT_ALLOWLISTED"


class _CooperativePlugin(_Plugin):
    def invoke(self, tool_name, args, *, approval=None, context=None):
        while True:
            if context is not None:
                context.raise_if_cancelled()
            time.sleep(0.001)


def test_cooperative_cancel_emits_lifecycle_events_with_run_id() -> None:
    events: list[tuple[str, str, dict]] = []
    registry = PluginRegistry(
        event_sink=lambda plugin_id, kind, payload: events.append((plugin_id, kind, payload)),
    )
    plugin = _CooperativePlugin()
    manifest = _manifest()
    assert registry.register(manifest, plugin)
    assert registry.activate(manifest.plugin_id)
    cancel = threading.Event()
    result: list[dict] = []
    worker = threading.Thread(
        target=lambda: result.append(registry.invoke_tool(
            manifest.plugin_id,
            "topos.plugin.inspect_subtitle",
            cancel_event=cancel,
            run_id="run-cancel-1",
        )),
    )
    worker.start()
    time.sleep(0.01)
    cancel.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result[0]["diagnostics"][0]["code"] == "PLUGIN_CANCELLED"
    kinds = [kind for _, kind, _ in events]
    assert "plugin.tool_started" in kinds
    assert "plugin.tool_cancelled" in kinds
    assert any(payload.get("run_id") == "run-cancel-1" for _, _, payload in events)


def test_cooperative_deadline_is_structured_timeout() -> None:
    registry = PluginRegistry()
    plugin = _CooperativePlugin()
    manifest = _manifest()
    assert registry.register(manifest, plugin)
    assert registry.activate(manifest.plugin_id)

    result = registry.invoke_tool(
        manifest.plugin_id,
        "topos.plugin.inspect_subtitle",
        timeout_seconds=0.01,
        run_id="run-timeout-1",
    )

    assert result["diagnostics"][0]["code"] == "PLUGIN_TIMEOUT"
