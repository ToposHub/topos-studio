"""wd11-blk 修复批次的回归测试（上波 wd10-g51 BLOCKED 白名单阻塞项）。

覆盖 3 个条目的根因修复：

- AUD-14800：``tool_operation_ledger._canonical`` 截断指纹碰撞（错误 REPLAY）
- AUD-14806：``toposctl`` 数值选项 ``int()`` ValueError 以 traceback 崩溃而非 exit 2
- AUD-14807：``toposctl.main()`` 不传 ``call_log``，审计随进程退出丢弃
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.capability_catalog import IDEMPOTENCY_CONTENT_KEY
from src.features.edit.ai_editing.tool_call_log import ToolCallLog
from src.features.edit.ai_editing.tool_operation_ledger import (
    LedgerDecision,
    ToolOperationLedger,
    fingerprint_args,
)
from src.features.edit.ai_editing.tool_result import ToolOutcome, ToolResult
from src.features.edit.ai_editing.toposctl import main, run_cli


# ================================================================
# helpers
# ================================================================


class _StubInvoke:
    """记录所有 invoke 调用的桩（toposctl 测试同款）。"""

    def __init__(self, default_result: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self._default = default_result or {"ok": True, "manifest": {}}

    def __call__(self, tool_name: str, args: dict | None = None, **kw) -> dict:
        self.calls.append((tool_name, args, kw))
        return dict(self._default)


def _read_jsonl(path: Path) -> list[dict]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ================================================================
# AUD-14800 — 指纹规范化截断不再碰撞
# ================================================================


class TestAud14800FingerprintNoCollision:
    def test_long_strings_sharing_prefix_do_not_collide(self):
        # 共享前 2000 字符、尾部不同：修复前 [:2000] 静默截断 → 同指纹
        a = {"note": "x" * 2000 + "tail-alpha"}
        b = {"note": "x" * 2000 + "tail-beta"}
        assert fingerprint_args(a) != fingerprint_args(b)

    def test_string_at_boundary_vs_one_longer_do_not_collide(self):
        # 修复前 "x"*2000 与 "x"*2001 截断后同为 "x"*2000 → 同指纹
        assert fingerprint_args({"note": "x" * 2000}) != fingerprint_args({"note": "x" * 2001})

    def test_lists_sharing_first_64_elements_do_not_collide(self):
        base = [f"item-{i}" for i in range(64)]
        a = {"ops": base + ["same", "alpha"]}
        b = {"ops": base + ["same", "beta"]}
        assert fingerprint_args(a) != fingerprint_args(b)

    def test_dicts_sharing_kept_keys_do_not_collide(self):
        # >64 个 key：排序后前 64 个相同、被丢弃的尾部不同 → 修复前同指纹
        a = {f"k{i:03d}": i for i in range(71)}
        b = {f"k{i:03d}": i for i in range(71)}
        b["k070"] = "different"
        assert fingerprint_args(a) != fingerprint_args(b)

    def test_deep_nesting_beyond_depth_limit_does_not_collide(self):
        # 超过深度上限的子树：修复前一律折叠为常量 "<fingerprint-depth-limit>"
        def nest(leaf: str, levels: int = 8) -> list:
            value: list | str = leaf
            for _ in range(levels):
                value = [value]
            return value  # type: ignore[return-value]

        assert fingerprint_args({"plan": nest("alpha")}) != fingerprint_args({"plan": nest("beta")})

    def test_non_json_scalar_str_overflow_does_not_collide(self):
        class LongStr:
            def __init__(self, tag: str) -> None:
                self._tag = tag

            def __str__(self) -> str:
                return "z" * 2500 + self._tag

        assert fingerprint_args({"obj": LongStr("a")}) != fingerprint_args({"obj": LongStr("b")})

    def test_identical_args_fingerprint_is_stable(self):
        args = {"plan": {"id": "p1"}, "items": [1, "two", 3.0], "note": "x" * 3000}
        assert fingerprint_args(args) == fingerprint_args(args)

    def test_small_args_fingerprint_unchanged_semantics(self):
        # 小参数（不触发任何截断界）语义保持：相同同指纹、不同异指纹
        assert fingerprint_args({"a": "x", "n": 1}) == fingerprint_args({"a": "x", "n": 1})
        assert fingerprint_args({"a": "x"}) != fingerprint_args({"a": "y"})

    def test_ledger_no_wrong_replay_for_prefix_colliding_args(self):
        # 端到端锁定审计判定的危害路径：修复前 B 会被错误 REPLAY 成 A 的结果
        ledger = ToolOperationLedger()
        args_a = {"plan": "P" * 2500 + "#alpha"}
        args_b = {"plan": "P" * 2500 + "#beta"}
        kw = {"strategy": IDEMPOTENCY_CONTENT_KEY}
        first = ledger.begin("r1", "c1", "topos.apply_edit_plan", args_a, **kw)
        assert first.decision == LedgerDecision.EXECUTE
        ledger.finish(first.record, ToolResult(
            tool_name="topos.apply_edit_plan",
            outcome=ToolOutcome.SUCCESS,
            data={"revision": 1},
        ))
        # 前缀相同但实质不同的参数 → 必须重新 EXECUTE（不能 REPLAY 他人的结果）
        second = ledger.begin("r2", "c2", "topos.apply_edit_plan", args_b, **kw)
        assert second.decision == LedgerDecision.EXECUTE
        # 真正相同的参数 → 去重（REPLAY）仍然成立
        replay = ledger.begin("r3", "c3", "topos.apply_edit_plan", args_a, **kw)
        assert replay.decision == LedgerDecision.REPLAY


# ================================================================
# AUD-14806 — 数值选项坏值 → exit 2（usage error），不再 traceback
# ================================================================


class TestAud14806NumericOptionUsageError:
    def test_snapshot_bad_asset_limit_exit_2(self):
        inv = _StubInvoke()
        r = run_cli(["snapshot", "--asset-limit", "abc"], inv)
        assert r.exit_code == 2
        assert any(d.get("code") == "TAEP_SCHEMA_INVALID" for d in r.diagnostics)
        assert r.usage_hint  # usage 陪伴输出
        assert inv.calls == []  # 未到达 invoke

    def test_search_assets_bad_limit_exit_2(self):
        inv = _StubInvoke()
        r = run_cli(["search-assets", "--text", "产品", "--limit", "1.5"], inv)
        assert r.exit_code == 2
        assert any(d.get("code") == "TAEP_SCHEMA_INVALID" for d in r.diagnostics)
        assert inv.calls == []

    def test_help_bad_limit_exit_2(self):
        inv = _StubInvoke(default_result={"ok": True, "results": []})
        r = run_cli(["help", "--query", "导出", "--limit", "ten"], inv)
        assert r.exit_code == 2
        assert inv.calls == []

    def test_usage_error_names_the_offending_flag(self):
        inv = _StubInvoke()
        r = run_cli(["snapshot", "--asset-limit", "abc"], inv)
        reasons = [str(d.get("details", {}).get("reason", "")) for d in r.diagnostics]
        assert any("--asset-limit" in reason and "abc" in reason for reason in reasons)

    def test_valid_numeric_options_still_parse(self):
        inv = _StubInvoke()
        run_cli(["search-assets", "--limit", "5"], inv)
        assert inv.calls[0][1] == {"limit": 5}
        run_cli(["search-assets", "--limit", "-3"], inv)
        assert inv.calls[1][1] == {"limit": -3}

    def test_main_returns_2_for_bad_numeric_option(self, capsys, tmp_path, monkeypatch):
        # main() 层面（连接宿主后）：不抛异常、stdout 单一 JSON、进程退出码 2。
        # 无 --socket 时 run_cli 先判 NOT_CONNECTED（exit 1，同样不再是 traceback），
        # 因此这里桩掉 IPC client 走到参数解析。
        import src.features.edit.ai_editing.mcp_local_ipc as ipc_mod

        class _FakeIpc:
            def __init__(self, socket_path: str) -> None:
                pass

            def invoke(self, tool_name: str, args: dict | None = None, **kw) -> dict:
                return {"ok": True}

        monkeypatch.setattr(ipc_mod, "LocalMcpIpcClient", _FakeIpc)
        # （显式传 sink 路径，避免测试写默认 ~/.topos_cache）
        code = main(
            ["--socket", str(tmp_path / "s.sock"), "search-assets", "--limit", "abc"],
            audit_log_path=str(tmp_path / "audit.jsonl"),
        )
        out = capsys.readouterr().out
        assert code == 2
        data = json.loads(out)  # 单一 JSON，可解析
        assert data["exit_code"] == 2


# ================================================================
# AUD-14807 — main() 传入持久化审计 sink，记录不再随进程丢弃
# ================================================================


class TestAud14807PersistentAuditLog:
    def test_main_persists_cli_audit_jsonl(self, capsys, tmp_path):
        sink = tmp_path / "audit.jsonl"
        code = main(["capabilities"], audit_log_path=str(sink))
        out = capsys.readouterr().out
        assert code == 0
        assert json.loads(out)["exit_code"] == 0
        records = _read_jsonl(sink)
        assert len(records) == 1
        assert records[0]["client"] == "cli"
        assert records[0]["tool_name"] == "topos.get_capabilities"

    def test_main_audit_log_appends_across_invocations(self, capsys, tmp_path):
        sink = tmp_path / "audit.jsonl"
        main(["capabilities"], audit_log_path=str(sink))
        main(["capabilities"], audit_log_path=str(sink))
        capsys.readouterr()
        records = _read_jsonl(sink)
        assert len(records) == 2  # 追加（append-only），不覆盖上一进程的记录
        assert all(r["client"] == "cli" for r in records)

    def test_main_env_var_selects_audit_path(self, capsys, tmp_path, monkeypatch):
        sink = tmp_path / "env-audit.jsonl"
        monkeypatch.setenv("TOPOSCTL_AUDIT_LOG", str(sink))
        code = main(["capabilities"])
        capsys.readouterr()
        assert code == 0
        assert sink.exists()
        assert _read_jsonl(sink)[0]["client"] == "cli"

    def test_main_version_records_no_tool_calls(self, capsys, tmp_path):
        # version 是纯离线常量读取，不产生工具调用 → 审计文件无记录
        sink = tmp_path / "audit.jsonl"
        code = main(["version"], audit_log_path=str(sink))
        capsys.readouterr()
        assert code == 0
        assert not sink.exists() or _read_jsonl(sink) == []

    def test_main_unwritable_sink_degrades_gracefully(self, capsys, tmp_path):
        # 父路径是一个普通文件：sink 不可用 → warning 降级，CLI 契约不破
        blocker = tmp_path / "afile"
        blocker.write_text("not a dir", encoding="utf-8")
        code = main(["version"], audit_log_path=str(blocker / "audit.jsonl"))
        out = capsys.readouterr().out
        assert code == 0
        assert json.loads(out)["exit_code"] == 0

    def test_offline_capabilities_records_into_passed_call_log(self):
        # 修复前：run_cli(["capabilities"]) 离线路径的审计调用以错误的关键字
        # 调 ToolCallLog.record（TypeError 被 except Exception 吞掉）→ 恒 0 条
        log = ToolCallLog()
        r = run_cli(["capabilities"], call_log=log)
        assert r.exit_code == 0
        assert log.count == 1
        rec = log.records()[0]
        assert rec.client == "cli"
        assert rec.tool_name == "topos.get_capabilities"

    def test_run_cli_still_accepts_injected_call_log_for_connected_path(self):
        log = ToolCallLog()
        inv = _StubInvoke()
        r = run_cli(["search-assets", "--text", "x"], inv, call_log=log)
        assert r.exit_code == 0
        assert log.count == 1
        assert log.records()[0].client == "cli"
