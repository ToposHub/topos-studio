"""P4.3 开发者 Skill Studio —— 评测套件 eval（plan §10.6）。

> 为每个 Skill 编写 eval：成功、模型缺失、证据不足、拒绝上传、取消、
> revision stale、无 approval 六类最低用例；发布前必须通过。

设计要点：

- **六类最低用例**（plan §10.6）：``success`` / ``model_missing`` /
  ``evidence_insufficient`` / ``upload_rejected`` / ``cancel`` / ``no_approval``
  （+ 可选 ``revision_stale``）。
- **eval 语义**：失败场景的用例 ``expected_failure=True``——即用例本身期望
  dry-run 失败（产出特定 error_code），才算 eval PASS；这是 eval 的正确语义，
  不是误报。``success`` / ``no_approval`` 期望 dry-run 成功。
- **装配方式**：每个 :class:`SkillEvalCase` 携带一个 ``fixture_factory``（返回
  :class:`DryRunFixture`）+ 期望终态；:meth:`SkillEvalCase.evaluate` 跑
  :class:`SkillDryRunner` 并断言期望。
- **取消 / revision_stale / upload_rejected** 由 fixture + runner 的受控注入
  触发（不联网 / 不写盘）。
- **汇总** :class:`SkillEvalReport`（passed/total/per-case），供 P4.4 发布门禁
  （``SkillReleaseGate``）消费。

红线（plan §10.6 / §1.4）：

- eval 全程不联网 / 不写时间线 / 不执行任意代码（继承 dry-run 红线）。
- eval 用例不依赖私人素材（fixture 全部脱敏）。
- eval 报告不含 secret / 绝对路径 / prompt 全文（继承 RunRecord 脱敏）。

与既有模块的关系：

- 复用 :class:`SkillDryRunner` / :class:`DryRunFixture`（P4.2）。
- 复用 :class:`SkillDraft`（P4.1）作为输入。
- ``run_default_suite`` 自动为草稿装配默认 6 类用例（每个 skill 必跑）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

from src.features.ai_harness.skill_dry_run import (
    DryRunErrorCode,
    DryRunFixture,
    DryRunOutcome,
    SkillDryRunner,
    TimelineSnapshot,
    make_auto_caption_fixture,
    make_voiceover_polish_fixture,
)
from src.features.ai_harness.skill_studio_drafts import SkillDraft


# ---------------------------------------------------------------------- #
# EvalCaseKind
# ---------------------------------------------------------------------- #


class EvalCaseKind:
    """eval 用例类别（plan §10.6 "六类最低用例"）。

    用 ``str`` 常量集合（``ALL``）+ 校验，便于序列化与跨进程。
    类本身不可迭代；请使用 ``EvalCaseKind.ALL``（tuple[str, ...]）。
    """

    SUCCESS = "success"                              # 正常成功：dry-run 产 PREVIEW plan
    MODEL_MISSING = "model_missing"                  # 缺模型：dry-run 失败 MODEL_MISSING
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"  # 缺证据：失败 EVIDENCE_INSUFFICIENT
    UPLOAD_REJECTED = "upload_rejected"              # 拒绝上传：policy 禁 → 无 cloud 事件
    CANCEL = "cancel"                                # 取消：终态 CANCELLED + partial
    NO_APPROVAL = "no_approval"                      # 无 approval：dry-run 无 approval 事件
    REVISION_STALE = "revision_stale"                # revision stale：失败 STALE_REVISION

    ALL: Tuple[str, ...] = (
        SUCCESS, MODEL_MISSING, EVIDENCE_INSUFFICIENT,
        UPLOAD_REJECTED, CANCEL, NO_APPROVAL, REVISION_STALE,
    )

    #: 这些 kind 的用例本身期望 dry-run **失败**（产出特定 error_code 才算 PASS）。
    #: ``success`` / ``no_approval`` / ``upload_rejected`` / ``cancel`` 期望 dry-run
    #: 不一定失败（cancel 期望终态 CANCELLED；upload_rejected 期望无 cloud 事件）。
    EXPECTED_FAILURE: Tuple[str, ...] = (
        MODEL_MISSING, EVIDENCE_INSUFFICIENT, REVISION_STALE,
    )


#: 便捷导出（test 直接引用）
EXPECTED_FAILURE_CASES: Tuple[str, ...] = EvalCaseKind.EXPECTED_FAILURE


#: 每种 kind 期望的 dry-run error_code（""=期望成功）。
_KIND_EXPECTED_ERROR: Dict[str, str] = {
    EvalCaseKind.SUCCESS: "",
    EvalCaseKind.MODEL_MISSING: DryRunErrorCode.MODEL_MISSING,
    EvalCaseKind.EVIDENCE_INSUFFICIENT: DryRunErrorCode.EVIDENCE_INSUFFICIENT,
    EvalCaseKind.REVISION_STALE: "STALE_REVISION",
    # upload_rejected / cancel / no_approval 不靠 error_code 判定（见下）
}


# ---------------------------------------------------------------------- #
# SkillEvalCase
# ---------------------------------------------------------------------- #


FixtureFactory = Callable[[], DryRunFixture]


@dataclass(frozen=True)
class SkillEvalCase:
    """单个 eval 用例（plan §10.6）。

    Attributes:
        kind: :class:`EvalCaseKind`。
        name: 用例名（如 "voiceover_success"）。
        fixture_factory: 返回 :class:`DryRunFixture` 的工厂（脱敏；可注入失败场景）。
        cancel_after_start: 取消用例：dry-run 启动后立即请求取消。
        description: 用例描述（不含 prompt / 素材原文）。
    """

    kind: str
    name: str
    fixture_factory: FixtureFactory
    cancel_after_start: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EvalCaseKind.ALL:
            raise ValueError(f"unknown eval case kind: {self.kind!r}")

    def evaluate(self, draft: SkillDraft, *, runner: SkillDryRunner | None = None) -> "EvalCaseResult":
        """运行本用例，返回 :class:`EvalCaseResult`。

        判定逻辑：

        - expected-failure kind：``outcome.error_code == 期望码`` → PASS；
        - success：``outcome.success`` → PASS；
        - no_approval：``outcome.success`` 且无 approval 事件 → PASS；
        - upload_rejected：无 cloud_upload/network 事件 → PASS（红线）；
        - cancel：终态 CANCELLED 或 partial-marker → PASS。
        """
        r = runner or SkillDryRunner()
        start = time.time()
        fixture = self.fixture_factory()
        outcome = r.run(draft, fixture, cancel_before_executor=self.cancel_after_start)
        duration_ms = int((time.time() - start) * 1000)
        passed, reason = _judge(self.kind, outcome, self.cancel_after_start)
        return EvalCaseResult(
            kind=self.kind, name=self.name, passed=passed,
            duration_ms=duration_ms, reason=reason,
            outcome=outcome,
            cancelled=self.cancel_after_start and outcome.run_record.status == "cancelled",
        )


# ---------------------------------------------------------------------- #
# EvalCaseResult
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalCaseResult:
    """单个 eval 用例的运行结果。"""

    kind: str
    name: str
    passed: bool
    duration_ms: int = 0
    reason: str = ""                  # 通过/失败原因（脱敏）
    outcome: DryRunOutcome | None = None
    cancelled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "name": self.name, "passed": self.passed,
            "duration_ms": self.duration_ms, "reason": self.reason,
            "cancelled": self.cancelled,
            "outcome": self.outcome.to_dict() if self.outcome else None,
        }


# ---------------------------------------------------------------------- #
# SkillEvalReport（汇总）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillEvalReport:
    """草稿的 eval 套件运行汇总（plan §10.6 "发布前必须通过"）。"""

    draft_id: str
    results: Tuple[EvalCaseResult, ...] = ()
    suite_version: str = "v1"
    run_at: float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def passed(self) -> bool:
        """整体是否通过（所有用例 PASS）。"""
        return self.total > 0 and all(r.passed for r in self.results)

    @property
    def total_duration_ms(self) -> int:
        return sum(r.duration_ms for r in self.results)

    def failures(self) -> Tuple[EvalCaseResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "suite_version": self.suite_version,
            "run_at": self.run_at,
            "passed": self.passed,
            "total": self.total,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "total_duration_ms": self.total_duration_ms,
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------- #
# SkillEvalSuite（默认 6 类装配）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillEvalSuite:
    """eval 套件：一组 :class:`SkillEvalCase`。

    ``default()`` 装配 plan §10.6 的六类最低用例（用默认 fixture 工厂）。
    """

    cases: Tuple[SkillEvalCase, ...] = ()
    suite_version: str = "v1"

    @staticmethod
    def default() -> "SkillEvalSuite":
        """装配默认六类用例（success/model_missing/evidence_insufficient/
        upload_rejected/cancel/no_approval）+ revision_stale。"""
        cases: List[SkillEvalCase] = [
            SkillEvalCase(
                kind=EvalCaseKind.SUCCESS, name="success",
                fixture_factory=_default_success_fixture,
                description="正常成功：dry-run 产 PREVIEW EditPlan",
            ),
            SkillEvalCase(
                kind=EvalCaseKind.MODEL_MISSING, name="model_missing",
                fixture_factory=_empty_capability_fixture,
                description="缺模型：dry-run 失败 MODEL_MISSING",
            ),
            SkillEvalCase(
                kind=EvalCaseKind.EVIDENCE_INSUFFICIENT, name="evidence_insufficient",
                fixture_factory=_empty_evidence_fixture,
                description="缺证据：dry-run 失败 EVIDENCE_INSUFFICIENT",
            ),
            SkillEvalCase(
                kind=EvalCaseKind.UPLOAD_REJECTED, name="upload_rejected",
                fixture_factory=_default_success_fixture,
                description="拒绝上传：policy 禁止 → 无 cloud_upload 事件",
            ),
            SkillEvalCase(
                kind=EvalCaseKind.CANCEL, name="cancel",
                fixture_factory=_default_success_fixture,
                cancel_after_start=True,
                description="取消：dry-run 启动后立即取消 → 终态 CANCELLED",
            ),
            SkillEvalCase(
                kind=EvalCaseKind.NO_APPROVAL, name="no_approval",
                fixture_factory=_default_success_fixture,
                description="无 approval：dry-run 无 approval.granted 事件",
            ),
            SkillEvalCase(
                kind=EvalCaseKind.REVISION_STALE, name="revision_stale",
                fixture_factory=_stale_revision_fixture,
                description="revision stale：dry-run 失败 STALE_REVISION",
            ),
        ]
        return SkillEvalSuite(cases=tuple(cases), suite_version="v1")

    def run(self, draft: SkillDraft, *, runner: SkillDryRunner | None = None) -> SkillEvalReport:
        """运行套件中所有用例，返回 :class:`SkillEvalReport`。"""
        r = runner or SkillDryRunner()
        results = tuple(c.evaluate(draft, runner=r) for c in self.cases)
        return SkillEvalReport(
            draft_id=draft.draft_id, results=results,
            suite_version=self.suite_version,
        )


def run_default_suite(
    draft: SkillDraft, *, runner: SkillDryRunner | None = None,
) -> SkillEvalReport:
    """便捷入口：装配默认套件并运行（plan §10.6 发布前必跑）。"""
    return SkillEvalSuite.default().run(draft, runner=runner)


# ---------------------------------------------------------------------- #
# 判定逻辑
# ---------------------------------------------------------------------- #


def _judge(
    kind: str, outcome: DryRunOutcome, cancel_after_start: bool,
) -> Tuple[bool, str]:
    """判定单个用例是否 PASS（按 kind 的期望）。"""
    if kind == EvalCaseKind.SUCCESS:
        if outcome.success and outcome.edit_plan is not None:
            return True, "dry-run succeeded with PREVIEW plan"
        return False, f"expected success, got error={outcome.error_code}"

    if kind == EvalCaseKind.MODEL_MISSING:
        if outcome.error_code == DryRunErrorCode.MODEL_MISSING:
            return True, "correctly failed with MODEL_MISSING"
        return False, f"expected MODEL_MISSING, got {outcome.error_code}"

    if kind == EvalCaseKind.EVIDENCE_INSUFFICIENT:
        if outcome.error_code == DryRunErrorCode.EVIDENCE_INSUFFICIENT:
            return True, "correctly failed with EVIDENCE_INSUFFICIENT"
        return False, f"expected EVIDENCE_INSUFFICIENT, got {outcome.error_code}"

    if kind == EvalCaseKind.REVISION_STALE:
        if outcome.error_code == "STALE_REVISION":
            return True, "correctly failed with STALE_REVISION"
        return False, f"expected STALE_REVISION, got {outcome.error_code}"

    if kind == EvalCaseKind.UPLOAD_REJECTED:
        kinds = {e.kind for e in outcome.run_record.events}
        if "cloud_upload" not in kinds and "network_request" not in kinds:
            return True, "no cloud_upload/network events (policy rejected)"
        return False, "unexpected cloud_upload/network event"

    if kind == EvalCaseKind.NO_APPROVAL:
        kinds = {e.kind for e in outcome.run_record.events}
        if "approval.granted" not in kinds and "approval.consumed" not in kinds:
            return True, "no approval events (PREVIEW only)"
        return False, "unexpected approval event in dry-run"

    if kind == EvalCaseKind.CANCEL:
        status = outcome.run_record.status
        # cancel 用例：dry-runner 取消后终态应为 cancelled；取消原因记录。
        if status == "cancelled":
            return True, f"cancelled as expected (reason={outcome.run_record.cancel_reason})"
        return False, f"expected cancelled, got status={status}"

    return False, f"unknown kind: {kind}"


# ---------------------------------------------------------------------- #
# 默认 fixture 工厂（脱敏）
# ---------------------------------------------------------------------- #


def _default_success_fixture() -> DryRunFixture:
    """默认成功 fixture：含 transcript 证据 + asr/llm 能力。"""
    return make_voiceover_polish_fixture()


def _empty_capability_fixture() -> DryRunFixture:
    """缺模型 fixture：available_capabilities 为空。"""
    base = make_auto_caption_fixture()
    return DryRunFixture(
        evidence_refs=base.evidence_refs,
        timeline_snapshot=base.timeline_snapshot,
        available_capabilities=set(),
    )


def _empty_evidence_fixture() -> DryRunFixture:
    """缺证据 fixture：evidence_refs 为空（capabilities 齐全，确保只触发证据缺失）。"""
    base = make_auto_caption_fixture()
    return DryRunFixture(
        evidence_refs=(),
        timeline_snapshot=base.timeline_snapshot,
        # 齐全能力：避免误触发 MODEL_MISSING（只测证据缺失这一条）
        available_capabilities={"asr", "llm", "demucs", "tts", "translate", "ocr"},
    )


def _stale_revision_fixture() -> DryRunFixture:
    """revision stale fixture：revision=-1（与 draft base_revision 不一致）。"""
    base = make_auto_caption_fixture()
    stale = TimelineSnapshot(
        timeline_id=base.timeline_snapshot.timeline_id,
        project_id=base.timeline_snapshot.project_id,
        revision=-1,  # sentinel: stale
        track_count=base.timeline_snapshot.track_count,
        fps=base.timeline_snapshot.fps,
    )
    return DryRunFixture(
        evidence_refs=base.evidence_refs,
        timeline_snapshot=stale,
        # 齐全能力：确保只触发 STALE_REVISION（不误触发 MODEL_MISSING）
        available_capabilities={"asr", "llm", "demucs", "tts", "translate", "ocr"},
    )


__all__ = [
    "EvalCaseKind",
    "EXPECTED_FAILURE_CASES",
    "SkillEvalCase",
    "EvalCaseResult",
    "SkillEvalReport",
    "SkillEvalSuite",
    "run_default_suite",
]
