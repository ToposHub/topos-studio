"""wd2-g12 批 D 修复回归测试（REMEDIATION_PLAN 批 D：AUD-22201..AUD-22415）。

逐条锁定 16 项 Major 修复后的契约：

- AUD-22201 manifest/capability Mapping 字段冻结（content_hash 不可漂移）。
- AUD-22202 activate() 与 register(replace_existing=True) 竞态：新条目绝不
  被标记 ACTIVE，旧实例被 deactivate 清理。
- AUD-22205 沙箱 worker 环境最小化（宿主凭据不进沙箱）。
- AUD-22210 generated_media 的 import_to_pool 审批门禁。
- AUD-22309 finish() 先校验迁移后写字段；终态 run 不被覆盖、不发伪事件。
- AUD-22310 start() 的 detached 判定在锁内（契约：detach 后新 run 必标 detached）。
- AUD-22311 cancel() 拒绝终态 run。
- AUD-22315 strict 不变量引擎：违例可见（日志 + violations + raise_if_violated）。
- AUD-22322 畸形 manifest 数值不再炸掉目录表单构建。
- AUD-22402 注入的 fake_executor 被真实调用。
- AUD-22403 STALE_REVISION 与草稿 base_revision 真实比对。
- AUD-22409 register_into 强制 draft.* 前缀 + local_dev 来源。
- AUD-22410 produces_plan 只在 policy 允许审批写时为 True。
- AUD-22411 草稿深拷贝（workflow_hash 与内容不脱钩）。
- AUD-22414 Supervisor 记账有界（prompt 不驻留、终态清理、LRU 上限）。
- AUD-22415 continue_child 轮次检查与回写原子（并发 follow-up 轮次互异）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.invocation_models import (
    Invocation,
    InvocationScope,
    PermissionLevel,
    RunEventKind,
    RunPolicy,
    RunStatus,
)
from src.features.ai_harness.plugin_runtime import (
    PluginCapability,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    PluginState,
)
from src.features.ai_harness.plugin_sandbox import (
    DarwinSandboxLauncher,
    _minimal_worker_env,
)
from src.features.ai_harness.result_card import default_actions, generated_media_result
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.runtime_invariants import RuntimeInvariantEngine
from src.features.ai_harness.skill_catalog import build_form_fields
from src.features.ai_harness.skill_dry_run import (
    DryRunFixture,
    SkillDryRunner,
    TimelineSnapshot,
    make_auto_caption_fixture,
)
from src.features.ai_harness.skill_release import SkillRelease, SkillReleaseGate
from src.features.ai_harness.skill_studio_drafts import new_draft_from_workflow
from src.features.ai_harness.subagent_runtime import (
    SubagentDefinition,
    SubagentError,
    SubagentPolicy,
    SubagentRegistry,
    SubagentResult,
    SubagentSupervisor,
)
from src.features.edit.ai_editing.agent_models import AgentEventKind
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanMode,
    PlanTarget,
)
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_VOICEOVER_POLISH,
    SkillManifest,
)
from src.features.edit.ai_editing.skill_registry import SkillRegistry


def _invocation(*, session_id: str = "sess1") -> Invocation:
    return Invocation(
        source="chat",
        skill_id="",
        scope=InvocationScope(project_id="p1", selected_asset_ids=("a1",)),
        policy=RunPolicy(permission=PermissionLevel.PROPOSE),
        parent_session_id=session_id,
        user_text="test",
    )


def _auto_caption_draft():
    from src.features.ai_harness.editorial_skills import p2_official_manifests
    from src.features.ai_harness.skill_studio_drafts import new_draft_from_template

    manifest = next(
        m for m in p2_official_manifests() if m.skill_id == "topos.auto_caption"
    )
    return new_draft_from_template(
        author="dev-alice", template=manifest, title="自动字幕草稿",
    )


# ================================================================
# AUD-22201 / AUD-22202 — plugin_runtime
# ================================================================


class TestAud22201FrozenManifestMappings:
    def test_caller_cannot_drift_content_hash(self) -> None:
        meta = {"k": {"nested": 1}}
        cap_schema = {"type": "object"}
        cap_meta = {"m": [1]}
        manifest = PluginManifest(
            plugin_id="org.test.aud22201",
            display_name="AUD-22201",
            version="1.0.0",
            capabilities=(
                PluginCapability(
                    capability_id="cap",
                    input_schema=cap_schema,
                    metadata=cap_meta,
                ),
            ),
            metadata=meta,
        )
        before = manifest.content_hash
        # 调用方在注册后原地改传入对象（顶层 + 嵌套）
        meta["k"]["nested"] = 999
        meta["extra"] = True
        cap_schema["type"] = "array"
        cap_meta["m"].append(2)
        assert manifest.content_hash == before
        payload = manifest.to_dict()
        assert payload["metadata"] == {"k": {"nested": 1}}
        assert payload["capabilities"][0]["input_schema"] == {"type": "object"}
        assert payload["capabilities"][0]["metadata"] == {"m": [1]}

    def test_frozen_mappings_reject_inplace_mutation(self) -> None:
        manifest = PluginManifest(
            plugin_id="org.test.aud22201b",
            display_name="AUD-22201b",
            metadata={"a": 1},
        )
        with pytest.raises(TypeError):
            manifest.metadata["a"] = 2  # type: ignore[index]
        capability = PluginCapability(
            capability_id="c1", input_schema={"type": "object"},
        )
        with pytest.raises(TypeError):
            capability.input_schema["type"] = "array"  # type: ignore[index]


class _TrackingPlugin:
    def __init__(self) -> None:
        self.activate_calls = 0
        self.deactivate_calls = 0

    def activate(self, context) -> None:
        self.activate_calls += 1

    def deactivate(self) -> None:
        self.deactivate_calls += 1

    def health(self) -> PluginHealth:
        return PluginHealth(status="healthy")


class _BlockingPlugin(_TrackingPlugin):
    """activate() 阻塞到测试放行，用于制造 activate/register 竞态窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def activate(self, context) -> None:
        super().activate(context)
        self.entered.set()
        assert self.release.wait(timeout=5.0), "test never released activate()"


class TestAud22202ActivateReplaceRace:
    def test_replacement_during_activation_never_marks_newcomer_active(self) -> None:
        registry = PluginRegistry()
        plugin_id = "org.test.aud22202"
        v1 = PluginManifest(plugin_id=plugin_id, display_name="Race", version="1.0.0")
        v2 = PluginManifest(plugin_id=plugin_id, display_name="Race", version="2.0.0")
        old_plugin = _BlockingPlugin()
        new_plugin = _TrackingPlugin()
        assert registry.register(v1, old_plugin)

        results: list[bool] = []

        def _activate() -> None:
            results.append(registry.activate(plugin_id))

        thread = threading.Thread(target=_activate)
        thread.start()
        try:
            assert old_plugin.entered.wait(timeout=5.0)
            # 激活窗口内条目仍 ENABLED → 允许替换（旧行为触发点）
            assert registry.register(v2, new_plugin, replace_existing=True)
            old_plugin.release.set()
        finally:
            old_plugin.release.set()
            thread.join(timeout=5.0)
        assert not thread.is_alive()

        # 旧实现把新条目标 ACTIVE；修复后 activate 失败返回、新条目保持 ENABLED
        assert results == [False]
        record = registry.get(plugin_id)
        assert record is not None
        assert record.state == PluginState.ENABLED
        assert record.manifest.version == "2.0.0"
        assert new_plugin.activate_calls == 0
        # 旧实例已执行 activate → 必须被 deactivate 清理（孤儿不残留）
        assert old_plugin.deactivate_calls == 1
        # 新条目未被激活 → 不应可被 invoke（ACTIVE 门禁）
        outcome = registry.invoke_tool(plugin_id, "noop")
        assert outcome["ok"] is False

    def test_plain_activation_still_succeeds(self) -> None:
        registry = PluginRegistry()
        plugin = _TrackingPlugin()
        manifest = PluginManifest(
            plugin_id="org.test.aud22202ok", display_name="OK", version="1.0.0",
        )
        assert registry.register(manifest, plugin)
        assert registry.activate("org.test.aud22202ok") is True
        assert registry.get("org.test.aud22202ok").state == PluginState.ACTIVE
        assert plugin.activate_calls == 1


# ================================================================
# AUD-22205 — plugin_sandbox 最小环境
# ================================================================


class TestAud22205MinimalWorkerEnv:
    def test_allowlist_excludes_host_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_SECRET_TOKEN", "hunter2")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = _minimal_worker_env()
        assert "TOPOS_SECRET_TOKEN" not in env
        assert "OPENAI_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"

    def test_launch_process_env_carries_no_credentials(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TOPOS_SECRET_TOKEN", "hunter2")
        monkeypatch.setenv("PATH", "/usr/bin")
        ctx = multiprocessing.get_context()
        ipc_parent, ipc_child = ctx.Pipe(duplex=True)
        launcher = DarwinSandboxLauncher("(version 1)(deny default)")
        process = None
        try:
            process = launcher._launch_process(  # noqa: SLF001 - 测试观测构造产物
                mp_context=ctx,
                args=(ipc_parent, None, "org.test.plugin", None),
                name="worker",
                command=("sandbox-exec",),
            )
            env = process._env  # noqa: SLF001
            assert "TOPOS_SECRET_TOKEN" not in env
            assert env.get("PATH") == "/usr/bin"
            # import 面仍可用（worker 需解析与宿主相同的模块）
            assert env.get("PYTHONPATH")
        finally:
            ipc_parent.close()
            ipc_child.close()
            if process is not None:
                process._bootstrap_sender.close()  # noqa: SLF001
                process._bootstrap_receiver.close()  # noqa: SLF001


# ================================================================
# AUD-22210 — result_card generated_media 审批门禁
# ================================================================


class TestAud22210GeneratedMediaApprovalGate:
    def test_import_to_pool_disabled_before_approval(self) -> None:
        actions = default_actions("generated_media", approved=False, applied=False)
        import_action = next(a for a in actions if a.kind == "import_to_pool")
        assert import_action.enabled is False
        assert "审批" in import_action.disabled_reason

    def test_import_to_pool_enabled_after_approval(self) -> None:
        actions = default_actions("generated_media", approved=True, applied=False)
        import_action = next(a for a in actions if a.kind == "import_to_pool")
        assert import_action.enabled is True

    def test_place_on_timeline_still_proposes(self) -> None:
        # place_on_timeline 是提案语义（走统一审批/CommandStack），保持可用
        actions = default_actions("generated_media", approved=False, applied=False)
        place = next(a for a in actions if a.kind == "place_on_timeline")
        assert place.enabled is True

    def test_factory_default_card_gates_import(self) -> None:
        card = generated_media_result("r1", "TTS", "30 秒")  # 默认 approved=False
        import_action = next(a for a in card.available_actions if a.kind == "import_to_pool")
        assert import_action.enabled is False


# ================================================================
# AUD-22309 / AUD-22310 / AUD-22311 — runtime
# ================================================================


class TestAud22309FinishValidatesBeforeWrite:
    def test_illegal_transition_writes_nothing(self) -> None:
        runtime = HarnessRuntime()
        run_id = runtime.start(_invocation())
        assert runtime.finish(run_id, success=True) is False  # DRAFT→SUCCEEDED 非法
        record = runtime.get_run(run_id)
        assert record.status == RunStatus.DRAFT
        assert record.finished_at is None
        assert not any(
            e.kind == RunEventKind.RUN_STATE_CHANGED for e in record.events
        )

    def test_terminal_run_not_overwritten_and_no_pseudo_event(self) -> None:
        runtime = HarnessRuntime()
        run_id = runtime.start(_invocation())
        assert runtime.mark_running(run_id)
        assert runtime.finish(run_id, success=True)
        before = runtime.get_run(run_id)
        assert runtime.finish(
            run_id, success=False, error_code="LATE", error_message="boom",
        ) is True
        after = runtime.get_run(run_id)
        assert after.status == RunStatus.SUCCEEDED
        assert after.error_code == ""
        assert after.error_message == ""
        assert len(after.events) == len(before.events)  # 不发伪 RUN_STATE_CHANGED
        assert any(d.get("code") == "LATE" for d in after.diagnostics)

    def test_failed_finish_still_records_error_fields(self) -> None:
        runtime = HarnessRuntime()
        run_id = runtime.start(_invocation())
        assert runtime.mark_running(run_id)
        assert runtime.finish(
            run_id, success=False, error_code="E1", error_message="bad",
        )
        record = runtime.get_run(run_id)
        assert record.status == RunStatus.FAILED
        assert record.error_code == "E1"
        assert record.finished_at is not None


class TestAud22310StartDetachedUnderLock:
    def test_new_run_of_detached_session_is_detached(self) -> None:
        runtime = HarnessRuntime()
        runtime.detach_session("sess-gone")
        run_id = runtime.start(_invocation(session_id="sess-gone"))
        snapshot = runtime.export_snapshot()
        entry = next(r for r in snapshot["runs"] if r["run_id"] == run_id)
        assert entry["session_detached"] is True
        # detach 后写路径被拒（§1.4 红线）
        assert runtime.mark_running(run_id) is False

    def test_live_session_run_not_detached(self) -> None:
        runtime = HarnessRuntime()
        run_id = runtime.start(_invocation(session_id="sess-alive"))
        entry = next(
            r for r in runtime.export_snapshot()["runs"] if r["run_id"] == run_id
        )
        assert entry["session_detached"] is False


class TestAud22311CancelRejectsTerminalRuns:
    def test_cancel_on_succeeded_run_returns_false(self) -> None:
        runtime = HarnessRuntime()
        run_id = runtime.start(_invocation())
        assert runtime.mark_running(run_id)
        assert runtime.finish(run_id, success=True)
        assert runtime.cancel(run_id) is False
        assert runtime.is_cancelled(run_id) is False
        record = runtime.get_run(run_id)
        assert not any(
            e.kind == RunEventKind.RUN_CANCEL_REQUESTED for e in record.events
        )

    def test_cancel_on_active_run_still_works(self) -> None:
        runtime = HarnessRuntime()
        run_id = runtime.start(_invocation())
        assert runtime.mark_running(run_id)
        assert runtime.cancel(run_id) is True
        assert runtime.is_cancelled(run_id) is True
        assert runtime.cancel(run_id) is True  # 幂等


# ================================================================
# AUD-22315 — runtime_invariants strict 可见性
# ================================================================


class _SwallowingOrchestrator:
    """镜像 AgentOrchestrator._notify_all：订阅者异常被吞。"""

    def __init__(self) -> None:
        self._events: list[object] = []
        self._subs: list = []

    def subscribe(self, fn) -> object:
        self._subs.append(fn)

        def _unsub() -> None:
            if fn in self._subs:
                self._subs.remove(fn)

        return _unsub

    def get_events(self, run_id: str) -> list:
        return [e for e in self._events if getattr(e, "run_id", "") == run_id]

    def publish(self, event: object) -> None:
        self._events.append(event)
        for sink in list(self._subs):
            try:
                sink(event)
            except Exception:  # noqa: BLE001 - 与真实 orchestrator 相同的吞没
                pass


def _tool_finished_event(run_id: str, call_id: str) -> object:
    return SimpleNamespace(
        run_id=run_id,
        event_id=f"evt-{call_id}",
        kind=AgentEventKind.RUN_TOOL_FINISHED,
        payload={"call_id": call_id, "error_code": ""},
    )


class TestAud22315StrictViolationsVisible:
    def test_strict_raise_survives_sink_swallow(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        orchestrator = _SwallowingOrchestrator()
        engine = RuntimeInvariantEngine(orchestrator, strict=True)  # type: ignore[arg-type]
        engine.install()
        with caplog.at_level(
            logging.ERROR, logger="src.features.ai_harness.runtime_invariants",
        ):
            orchestrator.publish(_tool_finished_event("run-1", "call-1"))
        # 1) publish 未因 strict 抛错而中断（orchestrator 吞掉）
        # 2) 违例被记录
        assert [v.code for v in engine.violations] == ["TOOL_FINISH_WITHOUT_START"]
        # 3) 日志保证可见
        assert "runtime invariant violated" in caplog.text
        # 4) 宿主/测试可事后断言（strict 语义恢复）
        with pytest.raises(RuntimeError, match="TOOL_FINISH_WITHOUT_START"):
            engine.raise_if_violated()

    def test_non_strict_records_without_raising(self) -> None:
        orchestrator = _SwallowingOrchestrator()
        engine = RuntimeInvariantEngine(orchestrator)  # type: ignore[arg-type]
        engine.install()
        orchestrator.publish(_tool_finished_event("run-2", "call-2"))
        assert len(engine.violations) == 1
        engine.raise_if_violated()  # 非 strict：不抛
        engine.clear()
        assert engine.violations == ()


# ================================================================
# AUD-22322 — skill_catalog 数值安全转换
# ================================================================


class TestAud22322MalformedNumericManifest:
    def test_garbage_numbers_do_not_crash_form_building(self) -> None:
        manifest = SkillManifest(
            skill_id="test.aud22322",
            brief_template={
                "target_duration": {"ideal": "auto", "min": "fast", "max": None},
            },
            input_schema={
                "type": "object",
                "required": ["target_duration"],
                "properties": {
                    "target_duration": {
                        "type": "integer", "minimum": "fast", "maximum": "lots",
                    },
                },
            },
            ui_card={"form_fields": ("target_duration",)},
        )
        fields = build_form_fields(manifest)  # 不得抛 ValueError/TypeError
        target = next(f for f in fields if f.name == "target_duration")
        # 垃圾 ideal → 无默认；垃圾 min/max → 退回 1/100000 兜底（不炸）
        assert target.default is None
        assert target.min_value == 1.0
        assert target.max_value == 100000.0

    def test_valid_numbers_still_flow_through(self) -> None:
        manifest = SkillManifest(
            skill_id="test.aud22322ok",
            brief_template={"target_duration": {"ideal": 720, "min": 648, "max": 792}},
        )
        fields = build_form_fields(manifest)
        target = next(f for f in fields if f.name == "target_duration")
        assert target.default == 720
        assert target.min_value == 648.0
        assert target.max_value == 792.0

    def test_float_and_bool_ignored(self) -> None:
        manifest = SkillManifest(
            skill_id="test.aud22322b",
            brief_template={"target_duration": {"ideal": True, "min": 1.5, "max": 3}},
        )
        fields = build_form_fields(manifest)
        # ideal=True（bool）→ 不可信 → 不自动补字段
        assert not any(f.name == "target_duration" for f in fields)


# ================================================================
# AUD-22402 / AUD-22403 — skill_dry_run
# ================================================================


def _custom_plan() -> EditPlan:
    return EditPlan.create(
        target=PlanTarget(project_id="fx-project-001", timeline_id="fx-timeline-001"),
        base_revision=42,
        operations=(EditOperation(
            op_id="aud22402-op",
            kind=OperationKind.ENSURE_SUBTITLE_TRACK,
            params={"track_name": "t"},
            intent={"reason": "test"},
        ),),
        mode=PlanMode.PREVIEW,
        title="injected plan",
        rationale="injected fake executor",
    )


class TestAud22402InjectedFakeExecutor:
    def test_injected_fake_is_called_and_none_falls_back(self) -> None:
        calls: list[dict] = []

        def counting_fake(**kwargs) -> None:
            calls.append(kwargs)
            return None

        runner = SkillDryRunner(fake_executor=counting_fake)
        outcome = runner.run(_auto_caption_draft(), make_auto_caption_fixture())
        assert len(calls) == 1
        assert calls[0]["executor_kind"] == "editorial_builder"
        assert calls[0]["draft"].skill_id.startswith("draft.")
        assert outcome.success is True
        assert outcome.edit_plan is not None  # 内置分派兜底

    def test_injected_fake_plan_wins(self) -> None:
        plan = _custom_plan()
        runner = SkillDryRunner(fake_executor=lambda **kwargs: plan)
        outcome = runner.run(_auto_caption_draft(), make_auto_caption_fixture())
        assert outcome.edit_plan is plan
        assert outcome.success is True

    def test_injected_fake_exception_is_contained(self) -> None:
        def boom(**kwargs) -> None:
            raise RuntimeError("injected fake exploded")

        runner = SkillDryRunner(fake_executor=boom)
        outcome = runner.run(_auto_caption_draft(), make_auto_caption_fixture())
        assert outcome.success is True  # 降级回内置受控分派，run 不卡死

    def test_non_plan_return_is_ignored(self) -> None:
        runner = SkillDryRunner(fake_executor=lambda **kwargs: "not a plan")
        outcome = runner.run(_auto_caption_draft(), make_auto_caption_fixture())
        assert outcome.success is True
        assert outcome.edit_plan is not None


class TestAud22403StaleRevisionComparesBase:
    def test_matching_base_revision_passes(self) -> None:
        outcome = SkillDryRunner().run(
            _auto_caption_draft(), make_auto_caption_fixture(), base_revision=42,
        )
        assert outcome.success is True

    def test_mismatched_base_revision_blocks_with_stale(self) -> None:
        outcome = SkillDryRunner().run(
            _auto_caption_draft(), make_auto_caption_fixture(), base_revision=7,
        )
        assert outcome.success is False
        assert outcome.error_code == "STALE_REVISION"
        revision_check = next(
            c for c in outcome.checks if c.code == "REVISION_CURRENT"
        )
        assert revision_check.passed is False
        assert "7" in revision_check.message

    def test_workflow_template_base_revision_used(self) -> None:
        workflow = {
            "workflow_id": "wf-aud22403",
            "base_revision": 42,
            "policy": {"timeline_write": "approval_required"},
            "nodes": [{
                "node_id": "n1",
                "spec_id": "topos.media.selected_assets",
                "params": {"a": 1},
            }],
            "edges": [],
        }
        draft = new_draft_from_workflow(
            author="dev", workflow_definition=workflow, title="T",
        )
        current = DryRunFixture(
            timeline_snapshot=TimelineSnapshot(
                timeline_id="t", project_id="p", revision=42,
            ),
        )
        stale = DryRunFixture(
            timeline_snapshot=TimelineSnapshot(
                timeline_id="t", project_id="p", revision=41,
            ),
        )
        assert SkillDryRunner().run(draft, current).success is True
        stale_outcome = SkillDryRunner().run(draft, stale)
        assert stale_outcome.success is False
        assert stale_outcome.error_code == "STALE_REVISION"

    def test_sentinel_semantics_preserved(self) -> None:
        fixture = DryRunFixture(
            timeline_snapshot=TimelineSnapshot(
                timeline_id="t", project_id="p", revision=-1,
            ),
        )
        outcome = SkillDryRunner().run(_auto_caption_draft(), fixture)
        assert outcome.success is False
        assert outcome.error_code == "STALE_REVISION"


# ================================================================
# AUD-22409 — skill_release register_into 红线
# ================================================================


class TestAud22409RegisterIntoRedLine:
    def test_official_skill_id_rejected(self) -> None:
        registry = SkillRegistry()
        official_version_before = registry.require(
            OFFICIAL_VOICEOVER_POLISH.skill_id,
        ).version
        hijack = replace(OFFICIAL_VOICEOVER_POLISH, version="9.9.9")
        release = SkillRelease(
            release_id="r-aud22409",
            manifest=hijack,
            executor_kind_value="workflow_runner",
        )
        diags = SkillReleaseGate().register_into(registry, release)
        assert diags, "official skill_id must be rejected"
        assert registry.require(OFFICIAL_VOICEOVER_POLISH.skill_id).version \
            == official_version_before

    def test_draft_prefix_with_official_source_rejected(self) -> None:
        registry = SkillRegistry()
        impostor = replace(
            OFFICIAL_VOICEOVER_POLISH, skill_id="draft.impostor-1",
        )
        release = SkillRelease(
            release_id="r-aud22409b",
            manifest=impostor,
            executor_kind_value="workflow_runner",
        )
        diags = SkillReleaseGate().register_into(registry, release)
        assert diags
        assert registry.get("draft.impostor-1") is None

    def test_legitimate_local_dev_release_still_registers(self) -> None:
        workflow = {
            "workflow_id": "wf-aud22409",
            "policy": {"timeline_write": "approval_required"},
            "nodes": [],
            "edges": [],
        }
        draft = new_draft_from_workflow(
            author="dev", workflow_definition=workflow, title="T",
        )
        release = SkillRelease.from_draft(
            draft=draft, eval_suite_version="t", released_by="dev",
        )
        registry = SkillRegistry()
        diags = SkillReleaseGate().register_into(registry, release)
        assert diags == []
        assert registry.get(release.skill_id) is not None


# ================================================================
# AUD-22410 / AUD-22411 — skill_studio_drafts
# ================================================================


class TestAud22410ProducesPlanSemantics:
    def test_forbidden_policy_does_not_produce_plan(self) -> None:
        draft = new_draft_from_workflow(
            author="dev",
            workflow_definition={
                "workflow_id": "wf-f", "policy": {"timeline_write": "forbidden"},
                "nodes": [], "edges": [],
            },
            title="T",
        )
        assert draft.manifest.produces_plan is False

    def test_approval_policy_produces_plan(self) -> None:
        draft = new_draft_from_workflow(
            author="dev",
            workflow_definition={
                "workflow_id": "wf-a",
                "policy": {"timeline_write": "approval_required"},
                "nodes": [], "edges": [],
            },
            title="T",
        )
        assert draft.manifest.produces_plan is True

    def test_no_policy_does_not_produce_plan(self) -> None:
        draft = new_draft_from_workflow(
            author="dev",
            workflow_definition={"workflow_id": "wf-n", "nodes": [], "edges": []},
            title="T",
        )
        assert draft.manifest.produces_plan is False


class TestAud22411DeepCopiedWorkflowSnapshot:
    def test_caller_mutation_does_not_drift_draft_or_hash(self) -> None:
        workflow = {
            "workflow_id": "wf-aud22411",
            "policy": {"timeline_write": "approval_required", "nested": {"x": [1, 2]}},
            "nodes": [{"node_id": "n1", "params": {"p": 1}}],
            "edges": [],
        }
        draft = new_draft_from_workflow(
            author="dev", workflow_definition=workflow, title="T",
        )
        recorded_hash = draft.origin.workflow_hash
        # 调用方在创建草稿后原地改（顶层追加 + 嵌套突变）
        workflow["nodes"].append({"node_id": "n2"})
        workflow["policy"]["nested"]["x"].append(3)
        assert len(draft.workflow_template["nodes"]) == 1
        assert draft.workflow_template["policy"]["nested"]["x"] == [1, 2]
        assert draft.default_policy["nested"]["x"] == [1, 2]
        recomputed = hashlib.sha256(
            json.dumps(
                draft.workflow_template, sort_keys=True, ensure_ascii=False,
                default=str,
            ).encode("utf-8"),
        ).hexdigest()[:16]
        assert recomputed == recorded_hash

    def test_snapshot_not_aliased_to_caller(self) -> None:
        workflow = {
            "workflow_id": "wf-aud22411b",
            "policy": {"nested": {"x": [1]}},
            "nodes": [], "edges": [],
        }
        draft = new_draft_from_workflow(
            author="dev", workflow_definition=workflow, title="T",
        )
        assert draft.workflow_template["policy"] is not workflow["policy"]
        assert draft.default_policy is not workflow["policy"]


# ================================================================
# AUD-22414 / AUD-22415 — subagent_runtime
# ================================================================


def _echo_runner(request) -> SubagentResult:
    return SubagentResult(
        child_run_id=request.child_run_id,
        agent_id=request.agent_id,
        status="succeeded",
        text="ok",
        round=request.round,
        lineage=request.lineage,
    )


class TestAud22414BoundedAccounting:
    def _registry(self) -> SubagentRegistry:
        registry = SubagentRegistry()
        registry.register(SubagentDefinition(
            agent_id="oneshot", display_name="One", runner=_echo_runner,
        ))
        registry.register(SubagentDefinition(
            agent_id="multi", display_name="Multi", runner=_echo_runner,
            continuable=True,
        ))
        return registry

    def test_one_shot_child_keeps_only_bounded_record(self) -> None:
        supervisor = SubagentSupervisor(self._registry())
        result = supervisor.run(
            parent_run_id="p1", session_id="s", agent_id="oneshot", prompt="go",
        )
        assert result.status == "succeeded"
        # prompt 不驻留；只留有界继续元数据（AUD-22414）
        record = supervisor._continuations[result.child_run_id]  # noqa: SLF001
        assert not hasattr(record, "prompt")
        with pytest.raises(SubagentError, match="one-shot"):
            supervisor.continue_child(result.child_run_id, "again")

    def test_continuable_child_keeps_prompt_free_record(self) -> None:
        supervisor = SubagentSupervisor(self._registry())
        result = supervisor.run(
            parent_run_id="p1", session_id="s", agent_id="multi", prompt="secret-ish",
        )
        assert list(supervisor._continuations) == [result.child_run_id]  # noqa: SLF001
        record = supervisor._continuations[result.child_run_id]  # noqa: SLF001
        assert not hasattr(record, "prompt")
        assert record.round == 1
        follow = supervisor.continue_child(result.child_run_id, "more")
        assert follow.round == 2

    def test_max_rounds_error_preserved_after_final_round(self) -> None:
        supervisor = SubagentSupervisor(
            self._registry(), policy=SubagentPolicy(max_rounds=2),
        )
        result = supervisor.run(
            parent_run_id="p1", session_id="s", agent_id="multi", prompt="1",
        )
        supervisor.continue_child(result.child_run_id, "2")
        # 达到轮次上限后再继续 → 领域错误保持"maximum rounds"（锁定语义）
        with pytest.raises(SubagentError, match="maximum rounds"):
            supervisor.continue_child(result.child_run_id, "3")

    def test_continuation_lru_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.features.ai_harness.subagent_runtime as subagent_module

        monkeypatch.setattr(subagent_module, "_MAX_TRACKED_CHILDREN", 2)
        supervisor = SubagentSupervisor(self._registry())
        first = supervisor.run(
            parent_run_id="p1", session_id="s", agent_id="multi", prompt="1",
        )
        supervisor.run(parent_run_id="p1", session_id="s", agent_id="multi", prompt="2")
        supervisor.run(parent_run_id="p1", session_id="s", agent_id="multi", prompt="3")
        assert len(supervisor._continuations) == 2  # noqa: SLF001
        with pytest.raises(SubagentError, match="unknown child run"):
            supervisor.continue_child(first.child_run_id, "evicted")

    def test_parent_child_cap_still_enforced(self) -> None:
        supervisor = SubagentSupervisor(
            self._registry(), policy=SubagentPolicy(max_children_per_parent=2),
        )
        supervisor.run(parent_run_id="p1", session_id="s", agent_id="oneshot", prompt="1")
        supervisor.run(parent_run_id="p1", session_id="s", agent_id="oneshot", prompt="2")
        with pytest.raises(SubagentError, match="child limit"):
            supervisor.run(parent_run_id="p1", session_id="s", agent_id="oneshot", prompt="3")


class TestAud22415ContinueRoundsAtomic:
    def test_concurrent_followups_get_distinct_rounds(self) -> None:
        barrier = threading.Barrier(2, timeout=5.0)
        seen_rounds: list[int] = []
        rounds_lock = threading.Lock()

        def racing_runner(request) -> SubagentResult:
            if request.round >= 2:
                barrier.wait()
            with rounds_lock:
                seen_rounds.append(request.round)
            return _echo_runner(request)

        registry = SubagentRegistry()
        registry.register(SubagentDefinition(
            agent_id="racer", display_name="Racer",
            runner=racing_runner, continuable=True,
        ))
        supervisor = SubagentSupervisor(
            registry, policy=SubagentPolicy(max_rounds=8),
        )
        seed = supervisor.run(
            parent_run_id="p1", session_id="s", agent_id="racer", prompt="seed",
        )

        errors: list[Exception] = []

        def _cont() -> None:
            try:
                supervisor.continue_child(seed.child_run_id, "follow-up")
            except Exception as exc:  # noqa: BLE001 - 记录线程内失败
                errors.append(exc)

        threads = [
            threading.Thread(target=_cont, name=f"follow-{i}") for i in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        assert not any(t.is_alive() for t in threads)
        assert errors == []
        # 旧缺陷：两个并发 follow-up 同获 round=2；修复后轮次互异
        assert sorted(r for r in seen_rounds if r >= 2) == [2, 3]

    def test_round_cap_not_bypassed_by_race(self) -> None:
        registry = SubagentRegistry()
        registry.register(SubagentDefinition(
            agent_id="multi2", display_name="M", runner=_echo_runner, continuable=True,
        ))
        supervisor = SubagentSupervisor(
            registry, policy=SubagentPolicy(max_rounds=3),
        )
        result = supervisor.run(
            parent_run_id="p1", session_id="s", agent_id="multi2", prompt="1",
        )
        assert supervisor.continue_child(result.child_run_id, "2").round == 2
        assert supervisor.continue_child(result.child_run_id, "3").round == 3
        with pytest.raises(SubagentError):
            supervisor.continue_child(result.child_run_id, "4")
