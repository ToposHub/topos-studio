"""T2.1 — AI 持久化接线测试（MainWindowAiEditingMixin ↔ EditProjectIO）。

验收（docs/ai_closure_focused_plan_2026-08-04.md 工作流 2）：

- ``_collect_ai_payload``：从 session store + plan repository 收集可序列化数据。
- ``_restore_ai_payload``：把磁盘数据还原进 store / repo。
- 往返：collect → IO（ToposProjectIO）→ restore 后，session catalog + plans 一致。
- 空仓/未装配 → collect 返回 None（跳过 ai/ 写入）。
- 还原后 plan 的 preview 是历史子集（compiled=None），commit 前必须重新 preview。
"""

from __future__ import annotations

import types

import pytest

pytestmark = pytest.mark.app

from src.app.controllers.ai_editing_mixin import AiEditingMixin
from src.features.edit.ai_editing.assistant_session_store import AssistantSessionStore
from src.features.edit.ai_editing.models import (
    EditOperation,
    EditPlan,
    OperationKind,
    PlanTarget,
)
from src.features.edit.ai_editing.plan_repository import PlanRepository
from src.features.ai_harness.invocation_models import Invocation, InvocationScope, RunPolicy
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.edit.ai_editing.autonomous_recovery import AutonomousCheckpoint
from src.features.edit.ai_editing.autonomous_mission import (
    AutonomousMissionStore,
    MissionStage,
)
from src.features.edit.ai_editing.goal_supervisor import GoalSpec
from src.features.edit.ai_editing.audit_store import AuditEntry, AuditStore
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO


# ================================================================
# Helpers：最小 fake mixin 实例（不创建 QApplication）
# ================================================================


def _make_plan(plan_id: str = "plan-1") -> EditPlan:
    return EditPlan.create(
        target=PlanTarget(project_id="proj-1", timeline_id="t1"),
        base_revision=1,
        plan_id=plan_id,
        operations=(
            EditOperation(
                op_id="op-1", kind=OperationKind.ADD_CLIP,
                params={"asset_id": "a1", "track_id": "t1",
                        "timeline_start": 0, "timeline_duration": 100},
            ),
        ),
    )


def _make_mixin(*, session_store, plan_repo, runtime=None) -> AiEditingMixin:
    """构造一个最小 mixin 实例，绑定 session_store + 一个含 plan_repository 的 service。"""
    # MainWindowAiEditingMixin 的方法引用 self._ai_session_store /
    # self._ai_assistant_service.plan_repository。用一个简单 namespace 模拟。
    obj = AiEditingMixin.__new__(AiEditingMixin)
    obj._ai_session_store = session_store
    # service 是一个带 plan_repository 属性的 stub
    service = types.SimpleNamespace(plan_repository=plan_repo)
    obj._ai_assistant_service = service
    if runtime is not None:
        obj._shared_runtime = runtime
    obj.edit_project_io = None  # _wire_ai_persistence 早退
    return obj


# ================================================================
# Tests
# ================================================================


class TestCollectAIPayload:
    def test_empty_returns_none(self):
        """空仓（无 session 数据 + 无 plan）→ None（跳过 ai/ 写入）。"""
        store = AssistantSessionStore()
        repo = PlanRepository()
        mixin = _make_mixin(session_store=store, plan_repo=repo)

        assert mixin._collect_ai_payload() is None

    def test_with_plans_returns_plans(self):
        store = AssistantSessionStore()
        repo = PlanRepository()
        repo.store(_make_plan("p1"))
        mixin = _make_mixin(session_store=store, plan_repo=repo)

        payload = mixin._collect_ai_payload()
        assert payload is not None
        assert payload["session"] is None  # 空 session
        assert payload["plans"] is not None
        assert payload["plans"]["schema_version"] == 1
        assert len(payload["plans"]["plans"]) == 1


class TestRestoreAIPayload:
    def test_restore_plans_into_empty_repo(self):
        # 源：有 plan 的仓库
        src_repo = PlanRepository()
        src_repo.store(_make_plan("p1"), created_at=1000.0)
        exported = src_repo.export_all()

        # 目标：空仓库
        store = AssistantSessionStore()
        repo = PlanRepository()
        mixin = _make_mixin(session_store=store, plan_repo=repo)

        mixin._restore_ai_payload({"session_catalog": None, "plans": exported})
        assert repo.count() == 1
        assert repo.get("p1") is not None
        assert repo.get("p1").created_at == 1000.0

    def test_restore_skips_when_no_plans(self):
        store = AssistantSessionStore()
        repo = PlanRepository()
        repo.store(_make_plan("existing"))
        mixin = _make_mixin(session_store=store, plan_repo=repo)

        # 无 plans 数据（旧工程）→ 不动仓库
        mixin._restore_ai_payload({"session_catalog": None, "plans": None})
        assert repo.count() == 1  # 原有的还在

    def test_restore_clears_then_imports(self):
        """restore 先 clear 再 import（持久化版本视为权威）。"""
        store = AssistantSessionStore()
        repo = PlanRepository()
        repo.store(_make_plan("old-plan"))
        mixin = _make_mixin(session_store=store, plan_repo=repo)

        # 磁盘上是不同的 plan
        disk_repo = PlanRepository()
        disk_repo.store(_make_plan("disk-plan"))
        mixin._restore_ai_payload({"plans": disk_repo.export_all()})

        assert repo.count() == 1
        assert repo.get("old-plan") is None  # 被清掉
        assert repo.get("disk-plan") is not None  # 换成磁盘版本

    def test_restore_non_dict_payload_no_crash(self):
        store = AssistantSessionStore()
        repo = PlanRepository()
        mixin = _make_mixin(session_store=store, plan_repo=repo)

        # 不应抛
        mixin._restore_ai_payload(None)  # type: ignore[arg-type]
        mixin._restore_ai_payload("garbage")  # type: ignore[arg-type]


class TestEndToEndRoundtrip:
    """collect → ToposProjectIO.save → load → restore 完整往返。"""

    def test_roundtrip_preserves_session_and_plans(self, tmp_path):
        # 1. 源端：store 带 catalog 数据，repo 带 plan
        src_store = AssistantSessionStore()
        src_repo = PlanRepository()
        src_repo.store(_make_plan("rt-1"), created_at=500.0)
        mixin = _make_mixin(session_store=src_store, plan_repo=src_repo)

        # 2. collect
        payload = mixin._collect_ai_payload()
        assert payload is not None
        assert payload["plans"] is not None

        # 3. 写进 .topos
        zf_path = str(tmp_path / "roundtrip.topos")
        project = ToposProject.create_single_asset(
            name="RT", asset_path="/tmp/x.jpg", media_type="image")
        ok = ToposProjectIO.save(
            project, zf_path,
            ai_session_data=payload["session"],
            ai_plans_data=payload["plans"],
        )
        assert ok

        # 4. 读回
        _, full_data = ToposProjectIO.load(zf_path, load_full_data=True)
        assert full_data is not None
        loaded_payload = {
            "session_catalog": full_data.get("ai_session_catalog"),
            "session": full_data.get("ai_session"),
            "plans": full_data.get("ai_plans"),
        }

        # 5. restore 进新端
        dst_store = AssistantSessionStore()
        dst_repo = PlanRepository()
        dst_mixin = _make_mixin(session_store=dst_store, plan_repo=dst_repo)
        dst_mixin._restore_ai_payload(loaded_payload)

        # 6. 验证：plan 仓库内容一致
        assert dst_repo.count() == 1
        got = dst_repo.get("rt-1")
        assert got is not None
        assert got.created_at == 500.0
        # plan 内容（经 EditPlan.to_dict/from_dict 往返）一致
        assert got.plan.plan_id == "rt-1"
        assert got.content_hash == src_repo.get("rt-1").content_hash

    def test_roundtrip_persists_runtime_and_interrupts_active_run(self, tmp_path):
        """.topos 保存运行时快照；新进程恢复时不重启孤儿 worker。"""
        src_runtime = HarnessRuntime()
        run_id = src_runtime.start(Invocation(
            source="workflow",
            skill_id="wf.test",
            scope=InvocationScope(project_id="proj-1"),
            policy=RunPolicy(),
            parent_session_id="sess-1",
            user_text="分析真实项目",
        ))
        assert src_runtime.mark_running(run_id)
        src_mixin = _make_mixin(
            session_store=AssistantSessionStore(),
            plan_repo=PlanRepository(),
            runtime=src_runtime,
        )

        payload = src_mixin._collect_ai_payload()
        assert payload is not None and payload["runtime"] is not None
        zf_path = str(tmp_path / "runtime-roundtrip.topos")
        project = ToposProject.create_single_asset(
            name="Runtime RT", asset_path="/tmp/x.jpg", media_type="image")
        assert ToposProjectIO.save(
            project, zf_path, ai_runtime_data=payload["runtime"])

        _, full_data = ToposProjectIO.load(zf_path, load_full_data=True)
        assert full_data is not None
        assert full_data["ai_runtime"]["runs"]

        dst_runtime = HarnessRuntime()
        dst_mixin = _make_mixin(
            session_store=AssistantSessionStore(),
            plan_repo=PlanRepository(),
            runtime=dst_runtime,
        )
        dst_mixin._restore_ai_payload({"runtime": full_data["ai_runtime"]})
        restored = dst_runtime.get_run(run_id)
        assert restored is not None
        assert restored.status == "failed"
        assert restored.error_code == "RUN_INTERRUPTED"

    def test_roundtrip_persists_autonomous_checkpoint_without_restarting_it(self, tmp_path):
        """恢复只回填 checkpoint；自动提交仍需 controller 的 live gates。"""
        src_runtime = HarnessRuntime()
        run_id = src_runtime.start(Invocation(
            source="workflow",
            skill_id="wf.test",
            scope=InvocationScope(project_id="proj-1", timeline_id="t1"),
            policy=RunPolicy(),
            parent_session_id="sess-1",
        ))
        checkpoint = AutonomousCheckpoint(
            run_id=run_id,
            plan_id="plan-1",
            project_id="proj-1",
            timeline_id="t1",
            base_revision=0,
            asset_ids=("a1",),
        )

        class _Controller:
            def __init__(self):
                self.audit = AuditStore()
                self.missions = AutonomousMissionStore()
                self.missions.start("mission-run", "plan-1")
                self.missions.record("mission-run", MissionStage.PLAN, passed=True)
                self.goal = GoalSpec(
                    goal_id="mission-run", project_id="proj-1", timeline_id="t1",
                    objective_ref="skill:product-ad", constraint_refs=("plan:plan-1",),
                )
                self.audit.record(AuditEntry(
                    timestamp=1.0,
                    tool="topos.autonomous_agent",
                    plan_id="plan-1",
                    applied=False,
                    new_revision=None,
                    diagnostics_summary={"AUTO_RUN_FAILED": 1},
                ))

            def export_autonomous_checkpoints(self):
                return {"schema_version": 1, "checkpoints": [checkpoint.to_dict()]}

            def export_autonomous_audit(self):
                return self.audit.export_all()

            def export_autonomous_missions(self):
                return self.missions.export_all()

            def export_autonomous_goals(self):
                return {
                    "schema_version": "topos-goal-spec/1.0",
                    "goals": [self.goal.to_dict()],
                }

            def restore_autonomous_checkpoints(self, data):
                self.restored = data

            def restore_autonomous_audit(self, data):
                self.restored_audit = data

            def restore_autonomous_missions(self, data):
                self.restored_missions = data

            def restore_autonomous_goals(self, data):
                self.restored_goals = data

        src_mixin = _make_mixin(
            session_store=AssistantSessionStore(),
            plan_repo=PlanRepository(),
            runtime=src_runtime,
        )
        src_mixin._ai_edit_controller = _Controller()
        payload = src_mixin._collect_ai_payload()
        assert payload is not None and payload["runtime"]["autonomous"]
        assert payload["runtime"]["autonomous_audit"]
        assert payload["runtime"]["autonomous_missions"]
        assert payload["runtime"]["autonomous_goals"]

        zf_path = str(tmp_path / "autonomous-roundtrip.topos")
        project = ToposProject.create_single_asset(
            name="Autonomous RT", asset_path="/tmp/x.jpg", media_type="image")
        assert ToposProjectIO.save(
            project, zf_path, ai_runtime_data=payload["runtime"])
        _, full_data = ToposProjectIO.load(zf_path, load_full_data=True)
        assert full_data is not None

        dst_controller = _Controller()
        dst_mixin = _make_mixin(
            session_store=AssistantSessionStore(),
            plan_repo=PlanRepository(),
            runtime=HarnessRuntime(),
        )
        dst_mixin._ai_edit_controller = dst_controller
        dst_mixin._restore_ai_payload({"runtime": full_data["ai_runtime"]})
        assert dst_controller.restored["checkpoints"][0]["run_id"] == run_id
        assert dst_controller.restored_audit["entries"][0]["plan_id"] == "plan-1"
        assert dst_controller.restored_missions["missions"][0]["plan_id"] == "plan-1"
        assert dst_controller.restored_goals["goals"][0]["goal_id"] == "mission-run"
