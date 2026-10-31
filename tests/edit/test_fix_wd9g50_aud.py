"""wd9-g50 修复批次的回归测试（REMEDIATION_PLAN 批 D，AUD-14109~14410）。

覆盖本批 14 个白名单内条目的根因修复：
AUD-14109 / 14202 / 14205 / 14208 / 14212 / 14300 / 14301 / 14302 /
14306 / 14310 / 14312 / 14313 / 14316 / 14317。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.features.edit.ai_editing.agent_models import AgentTaskKind
from src.features.edit.ai_editing.postcondition_checker import (
    PostconditionChecker,
    VerificationContext,
    VerificationOutcome,
    create_default_checker,
)
from src.features.edit.ai_editing.privacy_guard import redact_text
from src.features.edit.ai_editing.provider_resume import make_fingerprint
from src.features.edit.ai_editing.release_evidence import TelemetryCollector
from src.features.edit.ai_editing.release_gate import evaluate_release_gate
from src.features.edit.ai_editing.release_test_matrix import (
    MatrixCase,
    evaluate_test_matrix,
)
from src.features.edit.ai_editing.remote_transcript_provider import (
    RemoteTranscriptError,
    RemoteTranscriptProvider,
)
from src.features.edit.ai_editing.replan_policy import (
    DECISION_FINISH,
    DECISION_REQUEST_CLARIFICATION,
    ReplanLimits,
    ReplanPolicy,
    TRIGGER_REVISION_CONFLICT,
)
from src.features.edit.ai_editing.run_context import (
    PERMISSION_RESTRICTED_AUTO,
    RunContext,
    validate_run_context,
)
from src.features.edit.ai_editing.schema import (
    build_plan_json_schema,
    validate_plan_structure,
)
from src.features.edit.ai_editing.diagnostics import DiagnosticSeverity
from src.features.edit.ai_editing.timebase import TimebaseService


# --------------------------------------------------------------------------- #
# AUD-14109：零 postcondition = FAILED（fail-closed）
# --------------------------------------------------------------------------- #


class TestAud14109PostconditionFailClosed:
    def test_unregistered_kind_fails_closed(self) -> None:
        result = PostconditionChecker().check(
            VerificationContext(task_kind=AgentTaskKind.EXPORT, reply="done")
        )
        assert result.outcome == VerificationOutcome.FAILED
        assert result.reasons
        assert not result.verified

    @pytest.mark.parametrize(
        "kind",
        [
            AgentTaskKind.SPEECH_TO_TEXT,
            AgentTaskKind.TEXT_TO_SPEECH,
            AgentTaskKind.VISION_UNDERSTANDING,
            AgentTaskKind.EMBEDDING,
            AgentTaskKind.ANALYSIS,
            AgentTaskKind.EXPORT,
            AgentTaskKind.UNKNOWN,
        ],
    )
    def test_default_checker_unregistered_kinds_fail(self, kind: str) -> None:
        result = create_default_checker().check(
            VerificationContext(task_kind=kind, reply="x")
        )
        assert result.outcome == VerificationOutcome.FAILED

    def test_agent_kind_has_baseline_postcondition(self) -> None:
        checker = create_default_checker()
        ok = checker.check(
            VerificationContext(task_kind=AgentTaskKind.AGENT, reply="hello")
        )
        assert ok.outcome == VerificationOutcome.PASSED
        bad = checker.check(
            VerificationContext(task_kind=AgentTaskKind.AGENT, reply="  ")
        )
        assert bad.outcome == VerificationOutcome.FAILED


# --------------------------------------------------------------------------- #
# AUD-14202：make_fingerprint 规范化（跨进程稳定）
# --------------------------------------------------------------------------- #


class TestAud14202FingerprintCanonicalization:
    def test_set_order_does_not_change_fingerprint(self) -> None:
        a = make_fingerprint("p", "m", {"tags": {"b", "a", "c"}})
        b = make_fingerprint("p", "m", {"tags": frozenset({"c", "b", "a"})})
        assert a.fingerprint == b.fingerprint

    def test_path_and_datetime_normalize_to_stable_strings(self) -> None:
        import datetime as dt

        a = make_fingerprint("p", "m", {"path": Path("/tmp/x.mp4"), "at": dt.datetime(2026, 9, 28, 12, 0)})
        b = make_fingerprint("p", "m", {"path": "/tmp/x.mp4", "at": "2026-09-28T12:00:00"})
        assert a.fingerprint == b.fingerprint

    def test_unserializable_object_raises_value_error(self) -> None:
        class Opaque:
            pass

        with pytest.raises(ValueError):
            make_fingerprint("p", "m", {"obj": Opaque()})

    def test_empty_provider_or_model_rejected(self) -> None:
        with pytest.raises(ValueError):
            make_fingerprint("", "m", {})
        with pytest.raises(ValueError):
            make_fingerprint("p", "", {})

    def test_plain_json_payload_fingerprint_unchanged_semantics(self) -> None:
        fp = make_fingerprint("prov", "model", {"a": 1, "b": [1, 2]})
        assert fp.fingerprint == make_fingerprint("prov", "model", {"b": [1, 2], "a": 1}).fingerprint


# --------------------------------------------------------------------------- #
# AUD-14205：telemetry metadata 值级脱敏
# --------------------------------------------------------------------------- #


class TestAud14205MetadataValueRedaction:
    def test_mid_string_secret_and_path_are_redacted(self) -> None:
        collector = TelemetryCollector(opt_in=True)
        raw = "render failed at /Users/testuser/proj/x.mov (bearer sk-abcdef123456)"
        collector.emit("render", status="error", metadata={"error": raw})
        (event,) = collector.events()
        assert "/Users/testuser" not in event.metadata["error"]
        assert "sk-abcdef123456" not in event.metadata["error"]
        assert redact_text(raw) == event.metadata["error"]

    def test_absolute_path_prefix_value_still_dropped(self) -> None:
        collector = TelemetryCollector(opt_in=True)
        collector.emit("t", metadata={"note": "/var/log/nowhere.txt"})
        (event,) = collector.events()
        # 以绝对路径开头的值整条丢弃（不保留脱敏残骸）
        assert event.metadata == {}

    def test_non_string_values_pass_through(self) -> None:
        collector = TelemetryCollector(opt_in=True)
        collector.emit("t", metadata={"count": 3, "ratio": 0.5, "ok": True, "nil": None})
        (event,) = collector.events()
        assert event.metadata == {"count": 3, "ratio": 0.5, "ok": True, "nil": None}


# --------------------------------------------------------------------------- #
# AUD-14208：首 token 硬门槛 fail-closed
# --------------------------------------------------------------------------- #


class TestAud14208FirstTokenNoData:
    def test_runs_without_first_token_observation_fails(self) -> None:
        result = evaluate_release_gate({"total_runs": 10})
        assert not result.passed
        assert any("首 token" in f and "无数据" in f for f in result.failures)
        check = next(c for c in result.checks if c.name == "first_token_latency")
        assert check.passed is False
        assert check.value is None

    def test_cancel_missing_still_skipped(self) -> None:
        result = evaluate_release_gate({"total_runs": 5, "avg_first_token_latency_ms": 100.0})
        assert result.passed
        assert all(c.name != "cancel_latency" for c in result.checks)

    def test_zero_runs_reports_run_data_failure(self) -> None:
        result = evaluate_release_gate({"total_runs": 0})
        assert not result.passed
        assert any("无运行数据" in f for f in result.failures)


# --------------------------------------------------------------------------- #
# AUD-14212：min_pass_rate 生效
# --------------------------------------------------------------------------- #


def _case(name: str, passed: bool, category: str = "dataset") -> MatrixCase:
    return MatrixCase(name=name, category=category, passed=passed)


_ALL_CATEGORIES = ("dataset", "performance", "fault", "security")


def _matrix_cases(failed: int) -> tuple[MatrixCase, ...]:
    cases = []
    for category in _ALL_CATEGORIES:
        cases.append(_case(f"{category}-ok", True, category))
    for i in range(failed):
        cases.append(_case(f"bad-{i}", False, "dataset"))
    return tuple(cases)


class TestAud14212MinPassRate:
    def test_rate_below_threshold_rejects(self) -> None:
        result = evaluate_test_matrix(_matrix_cases(2), min_pass_rate=1.0)
        assert not result.passed
        assert result.case_pass_rate == 4 / 6
        assert set(result.failed_cases) == {"bad-0", "bad-1"}

    def test_rate_at_threshold_passes(self) -> None:
        result = evaluate_test_matrix(_matrix_cases(1), min_pass_rate=0.8)
        assert result.passed
        assert result.case_pass_rate == 0.8
        # 失败明细仍然完整可见
        assert result.failed_cases == ("bad-0",)

    def test_strict_threshold_still_zero_tolerance(self) -> None:
        result = evaluate_test_matrix(_matrix_cases(1), min_pass_rate=1.0)
        assert not result.passed


# --------------------------------------------------------------------------- #
# AUD-14300/14301/14302：remote transcript provider 输入净化
# --------------------------------------------------------------------------- #


def _bare_provider() -> RemoteTranscriptProvider:
    provider = RemoteTranscriptProvider.__new__(RemoteTranscriptProvider)
    provider._profile = SimpleNamespace(provider_kind="openai")
    provider._timebase = TimebaseService()
    return provider


class TestAud14300ConfidenceSanitized:
    def test_segment_non_numeric_confidence_falls_back(self) -> None:
        provider = _bare_provider()
        segments = provider._segments_from_payload(
            {"segments": [
                {"start_time": 0, "end_time": 500, "text": "hi",
                 "confidence": "高"},
            ]},
            fps="25/1", path="whatever.wav",
        )
        assert len(segments) == 1
        assert segments[0].confidence == 0.9

    def test_word_non_numeric_confidence_falls_back(self) -> None:
        provider = _bare_provider()
        segments = provider._segments_from_payload(
            {"segments": [
                {"start_time": 0, "end_time": 500, "text": "hi",
                 "words": [{"text": "hi", "start": 0, "end": 100,
                            "confidence": {"nested": 1}}]},
            ]},
            fps="25/1", path="whatever.wav",
        )
        assert segments[0].words[0].confidence == 0.9

    def test_numeric_confidence_preserved(self) -> None:
        provider = _bare_provider()
        segments = provider._segments_from_payload(
            {"segments": [
                {"start_time": 0, "end_time": 500, "text": "hi", "confidence": 0.42},
            ]},
            fps="25/1", path="whatever.wav",
        )
        assert segments[0].confidence == pytest.approx(0.42)


class TestAud14301DurationFailClosed:
    def test_duration_ms_returns_none_when_ffprobe_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("no ffprobe")

        monkeypatch.setattr(
            "src.features.edit.ai_editing.remote_transcript_provider.subprocess.run",
            boom,
        )
        assert RemoteTranscriptProvider._duration_ms("x.wav") is None

    def test_duration_ms_parses_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completed = SimpleNamespace(stdout="12.5\n")
        monkeypatch.setattr(
            "src.features.edit.ai_editing.remote_transcript_provider.subprocess.run",
            lambda *a, **k: completed,
        )
        assert RemoteTranscriptProvider._duration_ms("x.wav") == pytest.approx(12500.0)

    def test_whole_text_without_duration_raises_sanitized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _bare_provider()
        monkeypatch.setattr(RemoteTranscriptProvider, "_duration_ms", lambda _self, _p: None)
        with pytest.raises(RemoteTranscriptError):
            provider._segments_from_payload(
                {"text": "整段文本"}, fps="25/1", path="x.wav"
            )

    def test_whole_text_with_duration_maps_full_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _bare_provider()
        monkeypatch.setattr(RemoteTranscriptProvider, "_duration_ms", lambda _self, _p: 2000.0)
        segments = provider._segments_from_payload(
            {"text": "整段文本"}, fps="25/1", path="x.wav"
        )
        assert len(segments) == 1
        # 2s @ 25fps → end_frame 至少 50 帧，绝不再是 1 秒/25 帧钳位
        assert segments[0].end_frame >= 50


class TestAud14302TimeFallback:
    def test_invalid_time_uses_fallback(self) -> None:
        assert RemoteTranscriptProvider._time_ms(None, fallback=123.0) == 123.0
        assert RemoteTranscriptProvider._time_ms("abc", fallback=77.0) == 77.0
        assert RemoteTranscriptProvider._time_ms(float("nan"), fallback=5.0) == 5.0

    def test_null_start_time_falls_back_to_cursor(self) -> None:
        provider = _bare_provider()
        segments = provider._segments_from_payload(
            {"segments": [
                {"start_time": 0, "end_time": 1000, "text": "first"},
                {"start_time": None, "end_time": 2000, "text": "second"},
            ]},
            fps="25/1", path="x.wav",
        )
        assert len(segments) == 2
        # 第二段的 start 不回帧 0，而是沿用第一段 end（1000ms @ 25fps = 25 帧）
        assert segments[1].start_frame == segments[0].end_frame
        assert segments[1].start_frame > 0


# --------------------------------------------------------------------------- #
# AUD-14306：max_clarifications 硬上限
# --------------------------------------------------------------------------- #


class TestAud14306MaxClarifications:
    def test_revision_conflict_hits_cap_and_finishes(self) -> None:
        policy = ReplanPolicy(ReplanLimits(max_clarifications=2))
        d1 = policy.decide(TRIGGER_REVISION_CONFLICT)
        d2 = policy.decide(TRIGGER_REVISION_CONFLICT)
        assert d1.kind == DECISION_REQUEST_CLARIFICATION
        assert d2.kind == DECISION_REQUEST_CLARIFICATION
        d3 = policy.decide(TRIGGER_REVISION_CONFLICT)
        assert d3.kind == DECISION_FINISH
        assert "澄清" in d3.reason

    def test_cap_wins_regardless_of_trigger(self) -> None:
        policy = ReplanPolicy(ReplanLimits(max_clarifications=1))
        policy.decide(TRIGGER_REVISION_CONFLICT)
        assert policy.decide(TRIGGER_REVISION_CONFLICT).kind == DECISION_FINISH

    def test_reset_clears_clarification_count(self) -> None:
        policy = ReplanPolicy(ReplanLimits(max_clarifications=1))
        policy.decide(TRIGGER_REVISION_CONFLICT)
        policy.reset()
        assert policy.decide(TRIGGER_REVISION_CONFLICT).kind == DECISION_REQUEST_CLARIFICATION


# --------------------------------------------------------------------------- #
# AUD-14310：selected_asset_ids 三态语义
# --------------------------------------------------------------------------- #


def _ctx(**overrides: object) -> RunContext:
    base: dict[str, object] = {"run_id": "r1", "project_id": "p1"}
    base.update(overrides)
    return RunContext(**base)  # type: ignore[arg-type]


class TestAud14310AssetScope:
    def test_none_means_explicitly_unrestricted(self) -> None:
        ctx = _ctx(selected_asset_ids=None)
        assert ctx.asset_in_scope("anything")

    def test_empty_tuple_denies_all(self) -> None:
        ctx = _ctx(selected_asset_ids=())
        assert not ctx.asset_in_scope("anything")
        assert not ctx.asset_in_scope("a1")

    def test_roundtrip_preserves_none_vs_empty(self) -> None:
        unrestricted = _ctx(selected_asset_ids=None)
        assert RunContext.from_dict(unrestricted.to_dict()).selected_asset_ids is None
        empty = _ctx(selected_asset_ids=())
        assert RunContext.from_dict(empty.to_dict()).selected_asset_ids == ()

    def test_missing_key_deserializes_to_none(self) -> None:
        ctx = RunContext.from_dict({"run_id": "r", "project_id": "p"})
        assert ctx.selected_asset_ids is None

    def test_restricted_auto_requires_explicit_scope(self) -> None:
        issues = validate_run_context(
            _ctx(permission=PERMISSION_RESTRICTED_AUTO, budget_units=10.0)
        )
        assert any(i.code == "SCOPE_REQUIRED_FOR_AUTO" for i in issues)
        scoped = validate_run_context(
            _ctx(
                permission=PERMISSION_RESTRICTED_AUTO,
                budget_units=10.0,
                selected_asset_ids=("a1",),
            )
        )
        assert not any(i.code == "SCOPE_REQUIRED_FOR_AUTO" for i in scoped)


# --------------------------------------------------------------------------- #
# AUD-14312/14313：snapshot 确定性与探测缓存
# --------------------------------------------------------------------------- #


class TestAud14312DeterministicPackOrder:
    def test_model_pack_entries_sorted(self) -> None:
        from src.features.edit.ai_editing import runtime_capability_snapshot as rcs

        class _Status:
            def __init__(self, usable: bool) -> None:
                self.usable = usable
                self.is_demo = False
                self.detail = ""

        class _Manager:
            def get_status(self, pack_id: str) -> _Status:
                return _Status(True)

        snapshot = rcs.build_runtime_capability_snapshot(
            model_pack_manager=_Manager(),
            gpu_prober=lambda: {"status": "UNAVAILABLE", "devices": []},
        )
        pack_ids = [
            e.component_id[len("models."):]
            for e in snapshot.entries
            if e.component_id.startswith("models.")
        ]
        assert pack_ids == sorted(pack_ids)


class TestAud14313ProbeCache:
    def test_default_gpu_probe_cached_across_builds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.features.edit.ai_editing import runtime_capability_snapshot as rcs

        calls: list[int] = []

        def fake_probe() -> dict[str, object]:
            calls.append(1)
            return {"status": "UNAVAILABLE", "reason_code": "CACHED_PROBE",
                    "detail": None, "devices": []}

        rcs.reset_runtime_probe_cache()
        monkeypatch.setattr(rcs, "detect_host_gpu_info", fake_probe)
        try:
            s1 = rcs.build_runtime_capability_snapshot()
            s2 = rcs.build_runtime_capability_snapshot()
            assert len(calls) == 1
            assert s1.snapshot_id == s2.snapshot_id
            assert s1.entry("gpu.display").reason_code == "CACHED_PROBE"
        finally:
            rcs.reset_runtime_probe_cache()

    def test_injected_prober_not_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.features.edit.ai_editing import runtime_capability_snapshot as rcs

        calls: list[int] = []

        def prober() -> dict[str, object]:
            calls.append(1)
            return {"status": "UNAVAILABLE", "devices": []}

        rcs.build_runtime_capability_snapshot(gpu_prober=prober)
        rcs.build_runtime_capability_snapshot(gpu_prober=prober)
        assert len(calls) == 2


# --------------------------------------------------------------------------- #
# AUD-14316/14317：schema 校验器
# --------------------------------------------------------------------------- #


def _plan(operations: list[dict[str, object]]) -> dict[str, object]:
    return {
        "protocol": "taep/1.0",
        "plan_id": "p1",
        "base_revision": 0,
        "target": {"project_id": "prj", "timeline_id": "tl"},
        "operations": operations,
    }


class TestAud14316NonStringKind:
    @pytest.mark.parametrize("bad_kind", [["add_clip"], {"k": 1}, 12])
    def test_non_string_kind_is_schema_invalid(self, bad_kind: object) -> None:
        findings = validate_plan_structure(
            _plan([{"op_id": "o1", "kind": bad_kind, "params": {}}])
        )
        assert findings
        severities = {f[0] for f in findings}
        codes = {f[1].value if hasattr(f[1], "value") else f[1] for f in findings}
        assert DiagnosticSeverity.ERROR in severities
        assert all(f[3] == "o1" or f[3] is None for f in findings)
        assert "SCHEMA_INVALID" in codes or any(
            getattr(f[1], "name", str(f[1])) == "SCHEMA_INVALID" for f in findings
        )

    def test_string_kind_still_validates(self) -> None:
        findings = validate_plan_structure(
            _plan([{"op_id": "o1", "kind": "add_clip",
                    "params": {"track_id": "t", "asset_id": "a",
                               "source": {}, "timeline": {}}}])
        )
        assert findings == []


class TestAud14317DocMatchesPassthrough:
    def test_module_docstring_no_false_rejection_claim(self) -> None:
        from src.features.edit.ai_editing import schema as schema_mod

        assert "未知 core 字段默认拒绝" not in schema_mod.__doc__
        assert "透传" in schema_mod.__doc__

    def test_unknown_core_field_passes_and_numeric_key_warns(self) -> None:
        findings = validate_plan_structure(
            _plan([{"op_id": "o1", "kind": "set_clip_fade",
                    "params": {"clip_id": "c1", "fade_in_duration": 5,
                               "unknown_future_field": 1, "123": 1}}])
        )
        warnings = [f for f in findings if f[0] == DiagnosticSeverity.WARNING]
        errors = [f for f in findings if f[0] == DiagnosticSeverity.ERROR]
        assert len(warnings) == 1  # 纯数字键
        assert errors == []

    def test_generated_schema_documents_passthrough(self) -> None:
        schema = build_plan_json_schema()
        params = schema["properties"]["operations"]["items"]["properties"]["params"]
        assert "透传" in params["description"]
