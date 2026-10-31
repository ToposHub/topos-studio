"""P1 controller 依赖注入契约（需 qapp）。

> 验证 AiEditController.__init__ 接受 P1 依赖（router/compiler/adapter/help/catalog/
> registry）且默认 None（向后兼容）。build_p1_dependencies 的装配契约在
> src/features/ai_harness/tests/test_p1_host_wiring.py。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.invocation_router import InvocationRouter
from src.features.ai_harness.skill_compiler import SkillCompiler
from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
from src.features.ai_harness.help_assistant import HelpAssistant
from src.features.ai_harness.plugin_runtime import PluginManifest, PluginRegistry

pytestmark = pytest.mark.edit_core


def _make_controller(qapp, *, runtime=None, router=None, compiler=None,
                     adapter=None, help_assistant=None, catalog=None,
                     skill_registry=None, plugin_registry=None,
                     skill_plugin_host=None):
    from src.app.controllers.ai_edit_controller import AiEditController

    fake_service = MagicMock()
    fake_service.on_state_changed = None
    fake_service.active_brief_id = "default"
    fake_service.config = MagicMock()

    fake_panel = MagicMock()
    sig_names = (
        "request_submitted", "variant_selected", "apply_requested",
        "cancel_requested", "brief_patch_requested", "context_inspector_requested",
        "composer_scope_requested", "composer_permission_changed",
        "composer_profile_requested", "context_compress_requested",
        "new_session_requested", "show_history_requested",
        "run_cancel_requested", "run_retry_requested", "run_diagnostics_requested",
        "skill_start_requested",
    )
    for n in sig_names:
        getattr(fake_panel, n).connect = MagicMock()
        getattr(fake_panel, n).disconnect = MagicMock()

    return AiEditController(
        panel=fake_panel, service=fake_service, runtime=runtime,
        router=router, compiler=compiler, adapter=adapter,
        help_assistant=help_assistant, catalog=catalog,
        skill_registry=skill_registry, plugin_registry=plugin_registry,
        skill_plugin_host=skill_plugin_host, parent=qapp,
    )


def test_controller_accepts_p1_kwargs(qapp):
    """AiEditController.__init__ 接受 router/compiler/adapter/help/catalog/registry。"""
    runtime = HarnessRuntime()
    service = MagicMock()
    adapter = LegacyHarnessAdapter(service, runtime)
    controller = _make_controller(
        qapp, runtime=runtime, router=InvocationRouter(),
        compiler=SkillCompiler(), adapter=adapter,
        help_assistant=HelpAssistant(),
        catalog=MagicMock(), skill_registry=MagicMock(),
    )
    assert controller.router is not None
    assert controller.compiler is not None
    assert controller.adapter is adapter
    assert controller.help_assistant is not None
    assert controller.catalog is not None
    assert controller.skill_registry is not None


def test_controller_p1_deps_default_none(qapp):
    """未传 P1 依赖时各成员为 None（向后兼容旧测试）。"""
    controller = _make_controller(qapp)
    assert controller.router is None
    assert controller.compiler is None
    assert controller.adapter is None
    assert controller.help_assistant is None
    assert controller.catalog is None
    assert controller.skill_registry is None


def test_controller_projects_plugin_status_and_roundtrips_checkpoint_journal(qapp):
    registry = PluginRegistry()
    manifest = PluginManifest(
        plugin_id="topos.controller.plugin",
        display_name="Controller plugin",
    )
    assert registry.register(manifest)
    controller = _make_controller(qapp, plugin_registry=registry)

    views = controller.plugin_status_views()
    assert len(views) == 1
    assert views[0].plugin_id == manifest.plugin_id
    payload = controller.export_plugin_state()
    assert payload is not None
    assert payload["snapshot"]["plugins"][0]["manifest"]["plugin_id"] == manifest.plugin_id

    restored = _make_controller(qapp, plugin_registry=PluginRegistry())
    report = restored.restore_plugin_state(payload)
    assert report.ok
    assert restored.plugin_snapshot_journal.current_id == payload["current_checkpoint_id"]


def test_controller_restores_project_state_without_reactivating_plugin(qapp):
    registry = PluginRegistry()
    manifest = PluginManifest(
        plugin_id="topos.controller.restore",
        display_name="Controller restore plugin",
    )
    assert registry.register(manifest)
    assert registry.activate(manifest.plugin_id)
    controller = _make_controller(qapp, plugin_registry=registry)
    payload = controller.export_plugin_state()

    report = controller.restore_plugin_state(payload)

    assert report.ok
    assert report.reactivation_required == (manifest.plugin_id,)
    assert registry.get(manifest.plugin_id).state.value == "enabled"
