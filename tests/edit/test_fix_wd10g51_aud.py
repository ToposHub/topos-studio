"""wd10-g51 修复批次的回归测试（REMEDIATION_PLAN 批 D，ai_editing s-t 段）。

覆盖本批 13 个白名单内条目的根因修复：
AUD-14413 / 14418 / 14508 / 14511 / 14700 / 14701 / 14706 /
14709 / 14710 / 14713 / 14718 / 14725 / 14726。

（AUD-14800 / 14806 / 14807 目标文件 tool_operation_ledger.py、toposctl.py
不在本批白名单，标 BLOCKED，见 fixes/reports/wd10-g51.md。）
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

from src.features.edit.ai_editing.skill_manifest import SkillManifest
from src.features.edit.ai_editing.skill_registry import SkillRegistry
from src.features.edit.ai_editing.skill_runtime import (
    DefaultGraphBuilder,
    SkillInput,
    SkillRuntime,
)
from src.features.edit.ai_editing.snapshot_service import build_snapshot
from src.features.edit.ai_editing.task_artifacts import QaFinding, QaReport
from src.features.edit.ai_editing.task_graph import SingleWriterPolicy
from src.features.edit.ai_editing.timebase import (
    seconds_to_frame_range,
    timecode_to_frames,
)
from src.features.edit.ai_editing.token_estimator import estimate_text_tokens
from src.features.edit.ai_editing.tool_call_log import (
    ToolCallLog,
    ToolCallRecord,
    summarize_args,
    summarize_result,
)
from src.features.edit.ai_editing.tool_calling_client import (
    ToolCallRequest,
    ToolCallResponse,
    run_tool_loop,
)
from src.features.edit.ai_editing.tool_facade import ToolFacade
from src.features.edit.ai_editing.tool_invocation import result_from_facade
from src.shared.media.metadata import Rational


FPS_24 = Rational(24, 1)
FPS_2997 = Rational(30000, 1001)


def _skill_input(skill_id: str) -> SkillInput:
    return SkillInput(
        skill_id=skill_id, asset_ids=("a1",), timeline_id="tl1",
        project_id="p1", base_revision=0, brief_id="b1",
    )


# ================================================================
# AUD-14413 — DefaultGraphBuilder 未知 role 失败可见
# ================================================================


class TestAud14413UnknownRolesVisible:
    def _manifest(self, roles: tuple[str, ...]) -> SkillManifest:
        return SkillManifest(
            skill_id="s.bad", version="1.0.0", title="T", roles=roles,
        )

    def test_all_invalid_roles_raise_instead_of_empty_graph(self):
        with pytest.raises(ValueError, match="none map to a known TaskRole"):
            DefaultGraphBuilder().build(
                self._manifest(("wizard", "orc")), _skill_input("s.bad"),
            )

    def test_empty_roles_still_returns_empty_graph(self):
        g = DefaultGraphBuilder().build(
            self._manifest(()), _skill_input("s.bad"),
        )
        assert g.nodes == ()

    def test_runtime_surfaces_build_failure_not_silent_success(self):
        reg = SkillRegistry(register_official=False)
        reg.register(self._manifest(("wizard",)))
        rt = SkillRuntime(registry=reg, policy=SingleWriterPolicy())
        result = rt.run(_skill_input("s.bad"))
        # 失败可见：degradation + error 诊断，而不是"成功"的空结果
        assert result.degraded is True
        assert "TaskGraph 构造失败" in (result.degradation or "")
        assert any(
            d.get("severity") == "error" and "TaskGraph build failed" in d.get("message", "")
            for d in result.diagnostics
        )
        assert result.artifacts == ()
        # 写候选已在 finally 中释放
        assert rt._policy.holder("tl1") is None

    def test_partially_invalid_roles_warned_and_degraded(self):
        reg = SkillRegistry(register_official=False)
        reg.register(self._manifest(("creative_director", "wizard")))
        rt = SkillRuntime(registry=reg, policy=SingleWriterPolicy())
        result = rt.run(_skill_input("s.bad"))
        # 合法角色照常执行，非法角色被丢弃但必须可见
        assert any(
            d.get("severity") == "warning" and "wizard" in d.get("message", "")
            for d in result.diagnostics
        )
        assert result.degraded is True
        assert "wizard" in (result.degradation or "")
        # creative_director 仍然产出了 artifact
        assert any(a.produced_by == "creative_director" for a in result.artifacts)


# ================================================================
# AUD-14418 — snapshot_service 的 provider 异常收窄 + 留痕
# ================================================================


def _fake_state():
    asset = SimpleNamespace(
        metadata={}, is_offline=False, display_name="素材 A",
        path="/home/secret/clip.mp4", media_type="video",
    )
    project = SimpleNamespace(
        assets={"a1": asset}, project_id="p1", name="Proj",
        revision=0, active_timeline_id=None,
    )
    return SimpleNamespace(project=project, sequences={})


class _LogCollector(logging.Handler):
    """捕获 snapshot_service logger 的 warning（conftest 接管了 caplog）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestAud14418ProviderStatusErrors:
    def test_domain_error_falls_back_with_warning_log(self):
        class Provider:
            def get_status(self, asset_id: str) -> str:
                raise KeyError(asset_id)

        logger = logging.getLogger(
            "src.features.edit.ai_editing.snapshot_service"
        )
        collector = _LogCollector()
        logger.addHandler(collector)
        try:
            snap = build_snapshot(_fake_state(), analysis_provider=Provider())
        finally:
            logger.removeHandler(collector)
        assert snap.assets[0].analysis_status == "unknown"
        assert any(
            "analysis status unavailable" in rec.getMessage()
            for rec in collector.records
        )

    def test_programming_error_is_not_swallowed(self):
        class Provider:
            def get_status(self, asset_id: str) -> str:
                raise AttributeError("provider misconfigured")

        with pytest.raises(AttributeError):
            build_snapshot(_fake_state(), analysis_provider=Provider())

    def test_healthy_provider_status_passthrough(self):
        class Provider:
            def get_status(self, asset_id: str) -> str:
                return "ready"

        snap = build_snapshot(_fake_state(), analysis_provider=Provider())
        assert snap.assets[0].analysis_status == "ready"


# ================================================================
# AUD-14508 — QaReport approved/blocking 不变量
# ================================================================


class TestAud14508QaReportInvariant:
    def test_approved_true_with_blocking_finding_is_clamped_false(self):
        report = QaReport(
            report_id="qa-1",
            findings=(QaFinding(finding_id="f1", severity="blocking", message="x"),),
            approved=True,  # 模型透传的矛盾值
        )
        assert report.approved is False
        assert report.passes is False
        assert report.to_dict()["approved"] is False  # 序列化后同样一致
        assert report.to_dict()["passes"] is False

    def test_approved_true_without_blocking_is_kept(self):
        report = QaReport(
            report_id="qa-2",
            findings=(QaFinding(finding_id="f1", severity="warning"),),
            approved=True,
        )
        assert report.approved is True
        assert report.passes is True

    def test_no_findings_defaults_not_approved(self):
        assert QaReport(report_id="qa-3").approved is False


# ================================================================
# AUD-14511 — SingleWriterPolicy 原子 acquire
# ================================================================


class TestAud14511SingleWriterAtomicity:
    def test_acquire_is_atomic_under_contention(self):
        policy = SingleWriterPolicy()
        winners: list[str] = []
        guard = threading.Lock()
        barrier = threading.Barrier(16)

        def attempt(index: int) -> None:
            barrier.wait()
            if policy.acquire("tl-race", f"skill-{index}"):
                with guard:
                    winners.append(f"skill-{index}")

        threads = [
            threading.Thread(target=attempt, args=(i,)) for i in range(16)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 唯一赢家——修复前两个线程可同时通过 check-then-set 双双获得写候选
        assert len(winners) == 1
        assert policy.holder("tl-race") == winners[0]

    def test_release_and_holder_roundtrip_under_threads(self):
        policy = SingleWriterPolicy()
        assert policy.acquire("tl-1", "skill-A") is True
        policy.release("tl-1", "skill-A")
        assert policy.holder("tl-1") is None


# ================================================================
# AUD-14700 / AUD-14701 — timecode 换算体系 + 字段域校验
# ================================================================


class TestAud14700TimecodeRealFps:
    def test_ntsc_one_hour_matches_seconds_path(self):
        frames = timecode_to_frames("01:00:00:00", FPS_2997)
        assert frames == 107892  # round(3600 * 30000/1001)，不再按名义 30 偏 108 帧
        r = seconds_to_frame_range(0.0, 3600.0, FPS_2997)
        assert abs(r.end_frame - frames) <= 1  # 与秒路径同一帧号体系

    def test_integer_fps_round_trip_unchanged(self):
        assert timecode_to_frames("01:30:45:12", FPS_24) == 5445 * 24 + 12
        assert timecode_to_frames("00:01:00:00", Rational(25, 1)) == 1500


class TestAud14701TimecodeFieldDomain:
    def test_frame_field_over_nominal_fps_rejected(self):
        with pytest.raises(ValueError, match="field out of range"):
            timecode_to_frames("00:00:00:99", FPS_24)

    def test_frame_field_just_under_nominal_accepted(self):
        assert timecode_to_frames("00:00:00:29", FPS_2997) == 29

    def test_frame_field_equal_to_nominal_rejected(self):
        with pytest.raises(ValueError):
            timecode_to_frames("00:00:00:30", FPS_2997)

    @pytest.mark.parametrize("tc", ["-1:00:00:00", "00:60:00:00", "00:00:61:00"])
    def test_hms_domain_violations_rejected(self, tc: str):
        with pytest.raises(ValueError):
            timecode_to_frames(tc, FPS_24)

    def test_non_integer_field_rejected(self):
        with pytest.raises(ValueError):
            timecode_to_frames("00:00:00:ab", FPS_24)


# ================================================================
# AUD-14706 — token 估算的空白字符桶
# ================================================================


class TestAud14706WhitespaceBucket:
    def test_english_estimate_not_inflated_by_spaces(self):
        # 10 拉丁 + 1 空格：latin 10/4=2.5，保底 11/8 → ceil(2.5)=3
        # 修复前空格按 CJK 计：10/4 + 1/1.5 = 3.17 → 4（偏高）
        assert estimate_text_tokens("hello world") == 3

    def test_space_heavy_text_collapses_to_floor(self):
        # 纯空白：不再按 1.5 char/token 计
        assert estimate_text_tokens("     ") == 1

    def test_mixed_words_follow_latin_rate(self):
        assert estimate_text_tokens("a b c d e f g h i j") == 3  # 非 9

    def test_cjk_rate_unchanged(self):
        assert estimate_text_tokens("你好世界") == 3  # ceil(4/1.5)=3


# ================================================================
# AUD-14709 — 审计摘要的值级脱敏
# ================================================================


class TestAud14709ValueLevelRedaction:
    def test_path_embedded_in_ordinary_value_redacted(self):
        s = summarize_args({"text": "/Users/testuser/项目/xxx.prproj"})
        assert "/Users" not in s and "xxx.prproj" not in s
        assert "PATH_REDACTED" in s

    def test_non_blacklisted_path_key_value_redacted(self):
        s = summarize_args({"directory": "/Users/me/media"})
        assert "/Users/me/media" not in s
        assert "PATH_REDACTED" in s

    def test_windows_absolute_path_redacted(self):
        s = summarize_args({"out": "C:\\Users\\me\\clip.mp4"})
        assert "C:\\Users" not in s
        assert "PATH_REDACTED" in s

    def test_secret_shaped_value_redacted(self):
        s = summarize_args({"note": "token=abc123def456"})
        assert "abc123def456" not in s

    def test_openai_style_key_redacted(self):
        s = summarize_args({"label": "sk-abcdefgh12345678"})
        assert "sk-abcdefgh12345678" not in s

    def test_result_error_value_redacted(self):
        s = summarize_result({
            "ok": False,
            "error": "FileNotFoundError: '/Users/me/a.prproj' not found",
        })
        assert "/Users/me/a.prproj" not in s
        assert "ok=False" in s

    def test_plain_values_survive(self):
        s = summarize_args({"text": "产品介绍", "limit": 10})
        assert "产品介绍" in s and "10" in s


# ================================================================
# AUD-14710 — ToolCallLog 线程安全
# ================================================================


def _rec(call_id: str) -> ToolCallRecord:
    return ToolCallRecord(call_id=call_id, timestamp=0.0, tool_name="topos.t")


class TestAud14710ToolCallLogThreadSafety:
    def test_next_call_id_unique_under_concurrency(self):
        log = ToolCallLog()
        barrier = threading.Barrier(8)
        ids: list[str] = []
        guard = threading.Lock()

        def generate() -> None:
            barrier.wait()
            local = [log.next_call_id() for _ in range(250)]
            with guard:
                ids.extend(local)

        threads = [threading.Thread(target=generate) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(ids) == 2000
        assert len(set(ids)) == 2000  # 无重复（审计主键唯一）
        assert set(ids) == {f"tc-{i:08d}" for i in range(1, 2001)}  # 无跳号

    def test_concurrent_records_are_not_lost(self):
        log = ToolCallLog(max_entries=1000)
        barrier = threading.Barrier(8)
        expected: set[str] = set()
        guard = threading.Lock()

        def record_batch(index: int) -> None:
            barrier.wait()
            for j in range(100):
                cid = f"r-{index}-{j}"
                log.record(_rec(cid))
                with guard:
                    expected.add(cid)

        threads = [
            threading.Thread(target=record_batch, args=(i,)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert log.count == 800  # 无交错丢帧
        assert {r.call_id for r in log.records()} == expected


# ================================================================
# AUD-14713 — tool-calling loop 消息顺序协议
# ================================================================


class TestAud14713MessageOrdering:
    def test_assistant_tool_calls_precede_tool_results(self):
        captured: list[list[dict]] = []

        class OneRoundProvider:
            def __init__(self) -> None:
                self._done = False

            def complete_with_tools(self, messages, tools, **kw):
                captured.append(list(messages))
                if not self._done:
                    self._done = True
                    return ToolCallResponse(
                        tool_calls=(ToolCallRequest(
                            call_id="c1", tool_name="topos.search_assets",
                        ),),
                        stop_reason="tool_use",
                    )
                return ToolCallResponse(text="done", finish=True)

        run_tool_loop(
            OneRoundProvider(), lambda name, args, **kw: {"ok": True},
            [{"role": "user", "content": "x"}], [],
        )
        second_turn = captured[1]
        roles = [(m.get("role"), "tool_calls" in m) for m in second_turn]
        assistant_idx = roles.index(("assistant", True))
        tool_idx = next(i for i, (role, _) in enumerate(roles) if role == "tool")
        # OpenAI/Anthropic 协议：assistant(tool_calls) 必须先于 role=tool 结果
        assert assistant_idx < tool_idx
        tool_msg = second_turn[tool_idx]
        assert tool_msg["tool_call_id"] == "c1"

    def test_multi_tool_round_keeps_pairing(self):
        captured: list[list[dict]] = []

        class TwoToolProvider:
            def __init__(self) -> None:
                self._done = False

            def complete_with_tools(self, messages, tools, **kw):
                captured.append(list(messages))
                if not self._done:
                    self._done = True
                    return ToolCallResponse(
                        tool_calls=(
                            ToolCallRequest(call_id="a", tool_name="topos.t1"),
                            ToolCallRequest(call_id="b", tool_name="topos.t2"),
                        ),
                        stop_reason="tool_use",
                    )
                return ToolCallResponse(text="done", finish=True)

        run_tool_loop(
            TwoToolProvider(), lambda name, args, **kw: {"ok": True},
            [{"role": "user", "content": "x"}], [],
        )
        second_turn = captured[1]
        # assistant(tool_calls) 一次、随后每个 tool_call 各一条 role=tool 结果
        tail = second_turn[-3:]
        assert tail[0]["role"] == "assistant" and len(tail[0]["tool_calls"]) == 2
        assert [m["role"] for m in tail[1:]] == ["tool", "tool"]
        assert [m["tool_call_id"] for m in tail[1:]] == ["a", "b"]


# ================================================================
# AUD-14718 — render_preview / request_export 授权门禁
# ================================================================


def _facade(session: SimpleNamespace) -> ToolFacade:
    return ToolFacade(session)


class TestAud14718ApprovalGates:
    def test_render_preview_without_grant_denied(self):
        result = _facade(SimpleNamespace()).invoke("topos.render_preview", {})
        assert result["ok"] is False
        codes = {d.get("code") for d in result.get("diagnostics", [])}
        assert "TAEP_PERMISSION_DENIED" in codes

    def test_render_preview_with_grant_passes_gate(self):
        session = SimpleNamespace(
            get_snapshot=lambda: SimpleNamespace(active_timeline=None),
        )
        result = _facade(session).invoke(
            "topos.render_preview", {}, approval="host-grant",
        )
        # 门禁已过（不再是 PERMISSION_DENIED），走到业务检查
        assert result == {"ok": False, "error": "no active timeline"}

    def test_render_preview_model_supplied_token_ignored(self):
        result = _facade(SimpleNamespace()).invoke(
            "topos.render_preview",
            {"approval_token": "model-forged"},
        )
        assert result["ok"] is False
        codes = {d.get("code") for d in result.get("diagnostics", [])}
        assert "TAEP_PERMISSION_DENIED" in codes

    def test_request_export_without_grant_denied(self):
        result = _facade(SimpleNamespace()).invoke(
            "topos.request_export", {"request": {"timeline_id": "t1"}},
        )
        assert result["ok"] is False
        codes = {d.get("code") for d in result.get("diagnostics", [])}
        assert "TAEP_PERMISSION_DENIED" in codes

    def test_request_export_with_grant_passes_gate(self):
        session = SimpleNamespace(
            request_export=lambda req: {
                "job": None,
                "preflight": SimpleNamespace(ok=False, blocking=["empty"], degraded=[]),
            },
        )
        result = _facade(session).invoke(
            "topos.request_export",
            {"request": {"timeline_id": "t1"}},
            approval="host-grant",
        )
        assert "preflight" in result  # 门禁已过，导出提议/preflight 正常返回
        assert result["preflight"]["blocking"] == ["empty"]


# ================================================================
# AUD-14725 — result_from_facade 的 "ok" 缺省契约
# ================================================================


class TestAud14725OkDefaultContract:
    def test_payload_only_success_dict_is_success(self):
        payload = {"manifest": {"tools": ["a", "b"]}}
        r = result_from_facade("topos.get_capabilities", payload)
        assert r.succeeded
        assert r.data == payload  # 数据不再被丢弃

    def test_explicit_ok_false_is_still_failure(self):
        r = result_from_facade("topos.render_preview", {"ok": False, "error": "x"})
        assert r.failed
        assert r.error_message == "x"

    def test_permission_denied_still_denied(self):
        r = result_from_facade(
            "topos.apply_edit_plan",
            {"ok": False, "diagnostics": [
                {"severity": "error", "code": "PERMISSION_DENIED", "reason": "no"},
            ]},
        )
        assert r.is_denied

    def test_facade_read_tools_return_explicit_ok(self):
        f = _facade(SimpleNamespace())  # analysis_service=None 分支
        result = f.invoke("topos.search_assets", {"text": "x"})
        assert result["ok"] is True
        assert result["segments"] == []

    def test_runner_success_path_for_payload_only_facade(self):
        # 端到端：payload-only 成功返回不再被 runner 报成"工具调用失败"
        from src.features.edit.ai_editing.agent_models import AgentTask
        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
        from src.features.edit.ai_editing.tool_invocation import ToolInvocationRunner

        class PayloadOnlyFacade:
            def invoke(self, name, args, *, approval=None):
                return {"snapshot": {"revision": 7}}

        orch = AgentOrchestrator()
        run_id = orch.start_task(AgentTask(session_id="s1", title="t"))
        runner = ToolInvocationRunner(orch, PayloadOnlyFacade())
        result = runner.invoke(run_id, "topos.get_project_snapshot")
        assert result.succeeded
        assert result.data["snapshot"]["revision"] == 7


# ================================================================
# AUD-14726 — 超时不再改判已确认的结果
# ================================================================


class TestAud14726TimeoutKeepsOutcome:
    def _runner(self, facade) -> "ToolInvocationRunner":
        from src.features.edit.ai_editing.agent_models import AgentTask
        from src.features.edit.ai_editing.agent_orchestrator import AgentOrchestrator
        from src.features.edit.ai_editing.tool_invocation import (
            ToolInvocationRunner,
            ToolTimeoutPolicy,
        )

        orch = AgentOrchestrator()
        run_id = orch.start_task(AgentTask(session_id="s1", title="t"))
        clock = [0.0]

        class _ClockFacade:
            def invoke(self, name, args, *, approval=None):
                clock[0] = 1.0
                return facade.invoke(name, args)

        runner = ToolInvocationRunner(
            orch, _ClockFacade(),
            time_fn=lambda: clock[0],
            timeout_policy=ToolTimeoutPolicy(default_timeout_seconds=0.1),
        )
        return runner, run_id

    def test_slow_success_keeps_success_with_warning_diagnostic(self):
        class OkFacade:
            def invoke(self, name, args):
                return {"ok": True, "value": "late", "new_revision": 3}

        runner, run_id = self._runner(OkFacade())
        result = runner.invoke(run_id, "topos.apply_edit_plan", approval="g")
        assert result.succeeded
        assert result.data["value"] == "late"
        assert result.error_code != "TOOL_TIMEOUT"
        assert any(d.code == "TOOL_TIMEOUT" for d in result.diagnostics)

    def test_slow_failure_keeps_real_error_code(self):
        class FailingFacade:
            def invoke(self, name, args):
                return {"ok": False, "error": "preflight blocking"}

        runner, run_id = self._runner(FailingFacade())
        result = runner.invoke(run_id, "topos.request_export", approval="g")
        assert result.failed
        assert result.error_code == "TOOL_FAILED"
        assert result.error_message == "preflight blocking"
