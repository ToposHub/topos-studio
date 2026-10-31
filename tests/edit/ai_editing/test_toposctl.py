"""M10 toposctl CLI 单元测试（§11.1 M10 / §11.2 step 3）。

覆盖：
- run_cli 命令映射（capabilities/snapshot/search-assets/analysis/validate-plan/help）
- 参数解析（--text/--kind/--detail/ASSET_ID/PLAN_JSON）
- 经 ToolFacade.invoke 路由 + 审计（client="cli"）
- apply-plan 无 grant → PERMISSION_DENIED（exit 1）
- validate-plan 内联 JSON
- 未知命令 → exit 2 + usage
- 相同 diagnostics（CLI 与直接 invoke 一致）

红线对照：
- read/validate/preview 优先（§11.2 step 3）
- 写动作需 GUI/ApprovalService grant（CLI 不自带）
- 审计（§4.2 step 1）
"""

from __future__ import annotations

import json
import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.tool_call_log import ToolCallLog
from src.features.edit.ai_editing.toposctl import CliResult, run_cli


# ================================================================
# helpers
# ================================================================


class _StubInvoke:
    """记录所有 invoke 调用，可配置返回值。"""

    def __init__(self, default_result=None, results=None) -> None:
        self.calls: list[tuple] = []
        self._default = default_result or {"ok": True, "manifest": {}}
        self._results = results or {}

    def __call__(self, tool_name, args=None, **kw):
        self.calls.append((tool_name, args, kw))
        if tool_name in self._results:
            return dict(self._results[tool_name])
        return dict(self._default)


# ================================================================
# 命令映射 + 参数解析
# ================================================================


class TestCommandMapping:
    def test_capabilities(self):
        inv = _StubInvoke()
        r = run_cli(["capabilities"], inv)
        assert r.exit_code == 0
        assert r.tool_name == "topos.get_capabilities"
        assert inv.calls[0][0] == "topos.get_capabilities"

    def test_snapshot_with_options(self):
        inv = _StubInvoke()
        run_cli(["snapshot", "--detail", "full", "--timeline", "tl1", "--asset-limit", "10"], inv)
        tool, args, _kw = inv.calls[0]
        assert tool == "topos.get_project_snapshot"
        assert args == {"detail_level": "full", "timeline_id": "tl1", "asset_limit": 10}

    def test_search_assets(self):
        inv = _StubInvoke()
        run_cli(["search-assets", "--text", "产品", "--kind", "transcript", "--limit", "5"], inv)
        tool, args, _kw = inv.calls[0]
        assert tool == "topos.search_assets"
        assert args == {"text": "产品", "kind": "transcript", "limit": 5}

    def test_analysis_requires_asset_id(self):
        inv = _StubInvoke()
        r = run_cli(["analysis", "a1"], inv)
        assert r.exit_code == 0
        assert inv.calls[0][1] == {"asset_id": "a1"}

    def test_analysis_missing_asset_id_usage_error(self):
        inv = _StubInvoke()
        r = run_cli(["analysis"], inv)
        assert r.exit_code == 2
        assert any("ASSET_ID" in d.get("details", {}).get("reason", "") or "usage" in str(d) for d in r.diagnostics) or r.usage_hint

    def test_validate_plan_inline_json(self):
        inv = _StubInvoke()
        plan = {"plan_id": "p1", "operations": []}
        run_cli(["validate-plan", json.dumps(plan)], inv)
        tool, args, _kw = inv.calls[0]
        assert tool == "topos.validate_edit_plan"
        assert args == {"plan": plan}

    def test_preview_is_safe_validate_alias(self):
        inv = _StubInvoke()
        plan = {"plan_id": "p-preview", "operations": []}
        result = run_cli(["preview", json.dumps(plan)], inv)
        assert result.exit_code == 0
        assert inv.calls[0][0] == "topos.validate_edit_plan"
        assert inv.calls[0][1] == {"plan": plan}

    def test_validate_plan_invalid_json_usage_error(self):
        inv = _StubInvoke()
        r = run_cli(["validate-plan", "{not valid json"], inv)
        assert r.exit_code == 2
        assert len(inv.calls) == 0  # 没到 invoke

    def test_help_command(self):
        inv = _StubInvoke(default_result={"ok": True, "results": []})
        run_cli(["help", "--query", "快捷键", "--limit", "10"], inv)
        tool, args, _kw = inv.calls[0]
        assert tool == "topos.search_help"
        assert args == {"query": "快捷键", "limit": 10}

    def test_help_flag(self):
        inv = _StubInvoke()
        r = run_cli(["--help"], inv)
        assert r.exit_code == 0
        assert "toposctl" in r.usage_hint
        assert "--socket PATH" in r.usage_hint
        assert len(inv.calls) == 0


# ================================================================
# apply-plan 权限红线
# ================================================================


class TestApplyPlanPermission:
    def test_apply_plan_without_grant_denied(self):
        """红线（§11.2 step 3）：apply-plan 无 approval → PERMISSION_DENIED（exit 1）。"""
        inv = _StubInvoke(results={
            "topos.apply_edit_plan": {"ok": False, "diagnostics": [
                {"severity": "error", "code": "TAEP_PERMISSION_DENIED",
                 "message": "apply_edit_plan requires host-issued ApprovalGrant"},
            ]},
        })
        r = run_cli(["apply-plan", json.dumps({"plan_id": "p1"})], inv)
        assert r.exit_code == 1  # error diagnostic → exit 1
        assert any(d["code"] == "TAEP_PERMISSION_DENIED" for d in r.diagnostics)

    def test_apply_plan_with_grant_passes_approval(self):
        """宿主注入 grant → approval 透传给 invoke_fn。"""
        inv = _StubInvoke(default_result={"ok": True, "new_revision": 3})
        grant = "grant-abc"  # 简化：直接用字符串作 grant（ToolFacade 接受 str）
        run_cli(["apply-plan", json.dumps({"plan_id": "p1"})], inv, approval_grant=grant)
        _tool, _args, kw = inv.calls[0]
        assert kw.get("approval") == grant


# ================================================================
# 审计 + diagnostics 一致性
# ================================================================


class TestAuditAndDiagnostics:
    def test_missing_local_ipc_host_returns_diagnostic_not_exception(self, tmp_path):
        from src.features.edit.ai_editing.mcp_local_ipc import LocalMcpIpcClient

        result = run_cli(
            ["search-assets", "--text", "产品"],
            LocalMcpIpcClient(tmp_path / "missing.sock").invoke,
        )
        assert result.exit_code == 1
        assert any(d["code"] == "TAEP_PERMISSION_DENIED" for d in result.diagnostics)

    def test_audited_as_cli_client(self):
        """§4.2 step 1：CLI 调用经 ToolCallLog 记录，client="cli"。"""
        log = ToolCallLog()
        inv = _StubInvoke()
        run_cli(["capabilities"], inv, call_log=log)
        assert log.count == 1
        assert log.records()[0].client == "cli"

    def test_error_result_exit_code_1(self):
        """工具返回 error diagnostic → exit 1。"""
        inv = _StubInvoke(default_result={"ok": False, "diagnostics": [
            {"severity": "error", "code": "TAEP_ASSET_NOT_FOUND"},
        ]})
        r = run_cli(["search-assets", "--text", "x"], inv)
        assert r.exit_code == 1
        assert any(d["code"] == "TAEP_ASSET_NOT_FOUND" for d in r.diagnostics)

    def test_same_diagnostics_as_direct_invoke(self):
        """§11.2 完成检查：CLI 与直接 invoke 结果一致。"""
        direct = {"ok": True, "segments": [{"id": "s1"}]}
        inv = _StubInvoke(default_result=direct)
        r = run_cli(["search-assets", "--text", "产品"], inv)
        assert r.result == direct

    def test_unknown_command_exit_2(self):
        inv = _StubInvoke()
        r = run_cli(["bogus-command"], inv)
        assert r.exit_code == 2
        assert any(d["code"] == "TAEP_UNSUPPORTED_OPERATION" for d in r.diagnostics)

    def test_empty_argv_exit_2(self):
        inv = _StubInvoke()
        r = run_cli([], inv)
        assert r.exit_code == 2
        assert "toposctl" in r.usage_hint


# ================================================================
# CliResult 数据合同
# ================================================================


class TestCliResult:
    def test_to_dict_json_serializable(self):
        import json
        r = CliResult(exit_code=0, tool_name="t", result={"ok": True})
        json.dumps(r.to_dict())

    def test_frozen(self):
        r = CliResult(exit_code=0)
        with pytest.raises(Exception):
            r.exit_code = 1  # type: ignore[misc]
