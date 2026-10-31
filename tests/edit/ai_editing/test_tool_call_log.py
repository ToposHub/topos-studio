"""M10 Tool-call 审计日志 单元测试（§4.2 step 1 / §11.2 / §12.1）。

覆盖：
- ToolCallRecord 数据合同 + to_dict JSON serializable
- summarize_args / summarize_result 脱敏（secret/path redacted + 截断）
- extract_error_codes 从 diagnostics 提取
- ToolCallLog append-only + next_call_id 单调 + FIFO 上限
- 查询：by_client / by_tool / by_skill / errors
- record_invoke helper：成功/失败两路都记

红线对照：
- 不记 secret / 绝对路径（§0 第四条；summarize_args redacts）
- JSON serializable（§4.2 step 1）
- append-only（审计不可篡改）
"""

from __future__ import annotations

import json
import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.tool_call_log import (
    ToolCallLog,
    ToolCallRecord,
    extract_error_codes,
    record_invoke,
    summarize_args,
    summarize_result,
)


# ================================================================
# summarize_args / summarize_result 脱敏
# ================================================================


class TestSummarize:
    def test_summarize_args_redacts_sensitive_keys(self):
        """红线：api_key/token/path 等脱敏（§0 第四条）。"""
        s = summarize_args({"text": "产品", "api_key": "sk-xxx", "path": "/Users/me/secret"})
        assert "<redacted>" in s
        assert "sk-xxx" not in s
        assert "/Users/me/secret" not in s
        assert "产品" in s

    def test_summarize_args_recursive_redact(self):
        """红线修复：嵌套结构里的 secret/path 必须脱敏。

        apply_edit_plan 的 args 是 {"plan": {...}}，request_export 是 {"request": {...}}；
        output_path/api_key 常出现在嵌套层，浅匹配抓不到。
        """
        s = summarize_args({
            "request": {"output_path": "/Users/me/out.mp4", "api_key": "sk-nested"},
            "plan": {"text": "keep", "filename": "/etc/passwd"},
        })
        assert "/Users/me/out.mp4" not in s
        assert "sk-nested" not in s
        assert "/etc/passwd" not in s
        assert "<redacted>" in s
        assert "keep" in s  # 非敏感值保留

    def test_summarize_args_redacts_list_items(self):
        """列表里的嵌套 dict 也要递归脱敏。"""
        s = summarize_args({
            "files": [{"file_path": "/secret/a"}, {"url": "http://x/api?token=1"}],
        })
        assert "/secret/a" not in s
        assert "http://x/api?token=1" not in s
        assert "<redacted>" in s

    def test_summarize_args_redacts_expanded_sensitive_keys(self):
        """扩展后的敏感 key 列表：file_path/filepath/src/dst/url/endpoint 等。"""
        for key in ("file_path", "filepath", "src", "dst", "filename",
                    "url", "endpoint", "uri", "host", "secret", "password"):
            s = summarize_args({key: f"VALUE-{key}"})
            assert f"VALUE-{key}" not in s, f"{key} 未脱敏"
            assert "<redacted>" in s

    def test_summarize_args_empty(self):
        assert summarize_args(None) == ""
        assert summarize_args({}) == ""

    def test_summarize_args_truncates_long(self):
        s = summarize_args({"text": "x" * 500})
        assert len(s) <= 205  # 200 + ellipsis
        assert s.endswith("…")

    def test_summarize_result_extracts_key_flags(self):
        s = summarize_result({"ok": True, "new_revision": 5, "segments": [1, 2, 3]})
        assert "ok=True" in s
        assert "new_revision=5" in s
        assert "segments=3" in s

    def test_summarize_result_empty(self):
        assert summarize_result(None) == ""

    def test_summarize_result_truncates(self):
        s = summarize_result({"note": "y" * 500})
        assert len(s) <= 205


# ================================================================
# extract_error_codes
# ================================================================


class TestExtractErrors:
    def test_extracts_error_codes_only(self):
        result = {"diagnostics": [
            {"severity": "error", "code": "TAEP_PERMISSION_DENIED"},
            {"severity": "warning", "code": "TAEP_ASSET_NOT_FOUND"},
            {"severity": "error", "code": "TAEP_SCHEMA_INVALID"},
        ]}
        codes = extract_error_codes(result)
        assert codes == ("TAEP_PERMISSION_DENIED", "TAEP_SCHEMA_INVALID")

    def test_empty_when_no_diagnostics(self):
        assert extract_error_codes(None) == ()
        assert extract_error_codes({}) == ()
        assert extract_error_codes({"diagnostics": []}) == ()


# ================================================================
# ToolCallRecord JSON serializable
# ================================================================


class TestToolCallRecord:
    def test_to_dict_json_serializable(self):
        """§4.2 step 1：to_dict 可 json.dumps。"""
        rec = ToolCallRecord(
            call_id="tc-1", timestamp=1234.5, tool_name="topos.search_assets",
            client="mcp", args_summary="text=产品", result_summary="segments=2",
            error_codes=("TAEP_PERMISSION_DENIED",),
        )
        d = rec.to_dict()
        # 必须可序列化
        json.dumps(d)
        assert d["tool_name"] == "topos.search_assets"
        assert d["error_codes"] == ["TAEP_PERMISSION_DENIED"]

    def test_frozen(self):
        rec = ToolCallRecord(call_id="x", timestamp=0.0, tool_name="t")
        with pytest.raises(Exception):
            rec.tool_name = "changed"  # type: ignore[misc]


# ================================================================
# ToolCallLog append-only + 查询
# ================================================================


def _make_rec(**kw) -> ToolCallRecord:
    base = dict(call_id="x", timestamp=0.0, tool_name="topos.search_assets")
    base.update(kw)
    return ToolCallRecord(**base)


class TestToolCallLog:
    def test_append_and_count(self):
        log = ToolCallLog()
        assert log.count == 0
        log.record(_make_rec(call_id="a", client="mcp"))
        log.record(_make_rec(call_id="b", client="cli"))
        assert log.count == 2

    def test_records_snapshot(self):
        log = ToolCallLog()
        log.record(_make_rec(call_id="a"))
        recs = log.records()
        assert len(recs) == 1
        assert recs[0].call_id == "a"

    def test_next_call_id_monotonic(self):
        log = ToolCallLog()
        id1 = log.next_call_id()
        id2 = log.next_call_id()
        assert id1 == "tc-00000001"
        assert id2 == "tc-00000002"

    def test_fifo_max_entries(self):
        log = ToolCallLog(max_entries=3)
        for i in range(5):
            log.record(_make_rec(call_id=f"r{i}"))
        assert log.count == 3
        # 丢弃最旧的 r0, r1
        ids = [r.call_id for r in log.records()]
        assert ids == ["r2", "r3", "r4"]

    def test_by_client(self):
        log = ToolCallLog()
        log.record(_make_rec(call_id="a", client="mcp"))
        log.record(_make_rec(call_id="b", client="cli"))
        log.record(_make_rec(call_id="c", client="mcp"))
        assert len(log.by_client("mcp")) == 2
        assert len(log.by_client("cli")) == 1

    def test_by_tool(self):
        log = ToolCallLog()
        log.record(_make_rec(call_id="a", tool_name="topos.search_assets"))
        log.record(_make_rec(call_id="b", tool_name="topos.get_capabilities"))
        assert len(log.by_tool("topos.search_assets")) == 1

    def test_by_skill(self):
        log = ToolCallLog()
        log.record(_make_rec(call_id="a", skill_id="s1"))
        log.record(_make_rec(call_id="b", skill_id="s2"))
        assert len(log.by_skill("s1")) == 1

    def test_errors_filter(self):
        log = ToolCallLog()
        log.record(_make_rec(call_id="ok", succeeded=True))
        log.record(_make_rec(call_id="fail", succeeded=False))
        errs = log.errors()
        assert len(errs) == 1
        assert errs[0].call_id == "fail"

    def test_clear(self):
        log = ToolCallLog()
        log.record(_make_rec())
        log.clear()
        assert log.count == 0
        assert log.next_call_id() == "tc-00000001"  # counter reset

    def test_to_dict_json_serializable(self):
        log = ToolCallLog()
        log.record(_make_rec(call_id="a"))
        json.dumps(log.to_dict())


# ================================================================
# record_invoke helper
# ================================================================


class TestRecordInvoke:
    def test_records_successful_invoke(self):
        log = ToolCallLog()
        def invoke(name, args, **kw):
            return {"ok": True, "segments": [{"id": "s1"}]}
        result, rec = record_invoke(
            log, invoke, "topos.search_assets", {"text": "产品"},
            client="mcp", client_session_id="sess-1", skill_id="s1",
        )
        assert result["ok"] is True
        assert rec.tool_name == "topos.search_assets"
        assert rec.client == "mcp"
        assert rec.client_session_id == "sess-1"
        assert rec.skill_id == "s1"
        assert rec.succeeded is True
        assert "产品" in rec.args_summary

    def test_records_failed_invoke_result(self):
        """invoke 返回 ok=False → succeeded=False。"""
        log = ToolCallLog()
        def invoke(name, args, **kw):
            return {"ok": False, "diagnostics": [
                {"severity": "error", "code": "TAEP_PERMISSION_DENIED"},
            ]}
        result, rec = record_invoke(log, invoke, "topos.apply_edit_plan", {})
        assert rec.succeeded is False
        assert "TAEP_PERMISSION_DENIED" in rec.error_codes

    def test_records_exception_and_reraises(self):
        """invoke 抛异常 → 记 failed 记录后重新 raise（不吞）。"""
        log = ToolCallLog()
        def invoke(name, args, **kw):
            raise RuntimeError("network down")
        with pytest.raises(RuntimeError, match="network down"):
            record_invoke(log, invoke, "topos.search_assets", {})
        recs = log.records()
        assert len(recs) == 1
        assert recs[0].succeeded is False

    def test_redacts_sensitive_args(self):
        """红线：secret 不进 args_summary（§0 第四条）。"""
        log = ToolCallLog()
        def invoke(name, args, **kw):
            return {"ok": True}
        _, rec = record_invoke(
            log, invoke, "t", {"api_key": "sk-secret", "text": "ok"},
        )
        assert "sk-secret" not in rec.args_summary
        assert "<redacted>" in rec.args_summary
