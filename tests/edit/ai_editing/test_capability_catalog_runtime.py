"""Runtime wiring for the enriched capability catalog."""

from __future__ import annotations

from src.features.edit.ai_editing.agent_models import AgentTask
from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
from src.features.edit.ai_editing.capability_catalog import (
    CapabilityCatalog,
    CapabilityCatalogEntry,
    DOMAIN_EDITORIAL,
    create_default_capability_catalog,
)
from src.features.edit.ai_editing.tool_facade import ToolFacade, ToolPermission
from src.features.edit.ai_editing.tool_invocation import ToolInvocationRunner


def test_default_catalog_covers_every_transport_tool() -> None:
    from src.features.edit.ai_editing.tool_facade import list_tool_definitions

    catalog = create_default_capability_catalog()
    names = {entry.name for entry in catalog.all_entries()}
    assert {definition.name for definition in list_tool_definitions()} <= names
    assert catalog.require("topos.apply_edit_plan").requires_approval
    assert catalog.require("topos.get_capabilities").execution_mode == "parallel"


def test_facade_advertises_only_catalog_declared_local_tools() -> None:
    catalog = CapabilityCatalog([CapabilityCatalogEntry(
        name="topos.get_capabilities",
        domain=DOMAIN_EDITORIAL,
        permission=ToolPermission.READ.value,
        description="test",
    )])
    facade = ToolFacade(None, capability_catalog=catalog)
    names = {definition.name for definition in facade.list_tools()}
    assert names == {"topos.get_capabilities"}


def test_runner_blocks_undeclared_tool_before_facade_execution() -> None:
    class Facade:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, *_args, **_kwargs):
            self.calls += 1
            return {"ok": True}

    catalog = CapabilityCatalog([CapabilityCatalogEntry(
        name="topos.get_capabilities",
        domain=DOMAIN_EDITORIAL,
        permission=ToolPermission.READ.value,
        description="test",
    )])
    facade = Facade()
    orchestrator = AgentOrchestrator()
    run_id = orchestrator.start_task(AgentTask(session_id="s", title="catalog"))
    runner = ToolInvocationRunner(
        orchestrator, facade, capability_catalog=catalog,
    )

    result = runner.invoke(run_id, "topos.bogus", {})

    assert result.error_code == "TOOL_NOT_DECLARED"
    assert facade.calls == 0
