"""P4.4 开发者 Skill Studio —— 发布门禁与 SkillRelease（plan §10.6）。

> 发布产物是 ``SkillDefinition + graph template + manifest + eval version +
> content hash``。本地开发 Skill 醒目标"未验证"；第三方不得执行任意 Python、
> shell、文件系统或 UI 自动化。

设计要点：

- **发布产物** :class:`SkillRelease`：frozen + 可序列化；携带 manifest + graph
  template + eval_suite_version + content_hash + 发布者 + 时间戳。
- **发布门禁** :class:`SkillReleaseGate.publish`：三道门
  1. **版本门**：draft.version 必须 ≥ 1.0.0（dev 0.x 不得发布）；
  2. **eval 门**：必须跑 :class:`SkillEvalSuite` 且全部 PASS（plan §10.6 "发布前
     必须通过"）；
  3. **executor 门**：draft.executor_kind 不得为任意代码（二次防御；红线）。
- **source 分级**：local_dev 发布后仍 ``unverified=True``（plan §10.6 红线）；
  发布不改变 source（草稿是 local_dev，发布后仍 local_dev）。
- **registry 接线** :meth:`SkillReleaseGate.register_into`：把 release 注册进
  :class:`SkillRegistry`；local_dev 仍 unverified；草稿 skill_id 是 ``draft.*``
  前缀，绝不覆盖 official skill。

红线（plan §10.6 / §1.4 / §5.3）：

- 发布产物不含 secret / 绝对路径（继承 manifest 脱敏）。
- 第三方 / local_dev skill 的 executor_kind 受控；不携带任意 Python/shell 代码。
- 发布幂等：同草稿同版本 → 同 content_hash（视为同产物）。
- 发布不绕过 :class:`SkillRegistry` 的工具 allowlist 校验（FORBIDDEN_CAPABILITIES
  仍由 registry 拒绝）。

与既有模块的关系：

- 复用 :class:`SkillDraft`（P4.1）/ :class:`SkillEvalSuite`（P4.3）。
- 复用 :class:`SkillRegistry`（M9）做注册。
- 复用 :func:`compute_content_hash` / :func:`with_signature`（M9 manifest）。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.features.ai_harness.skill_eval_suite import SkillEvalReport, SkillEvalSuite
from src.features.ai_harness.skill_studio_drafts import SkillDraft
from src.features.edit.ai_editing.skill_manifest import SkillManifest, SkillSource, with_signature

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.features.ai_harness.skill_studio_drafts import ExecutorKind


# ---------------------------------------------------------------------- #
# PublishDenyCode —— 发布拒绝原因
# ---------------------------------------------------------------------- #


class PublishDenyCode:
    """发布门禁拒绝原因编码（plan §10.6）。"""

    DEV_VERSION_NOT_PUBLISHABLE = "DEV_VERSION_NOT_PUBLISHABLE"  # 0.x dev 不得发布
    EVAL_FAILED = "EVAL_FAILED"                                # eval 套件未全 PASS
    ARBITRARY_CODE_EXECUTOR = "ARBITRARY_CODE_EXECUTOR"        # executor_kind 任意代码
    CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"            # 内容 hash 不一致（篡改）

    ALL: Tuple[str, ...] = (
        DEV_VERSION_NOT_PUBLISHABLE, EVAL_FAILED,
        ARBITRARY_CODE_EXECUTOR, CONTENT_HASH_MISMATCH,
    )


# ---------------------------------------------------------------------- #
# SkillRelease —— 发布产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillRelease:
    """发布的 Skill 产物（plan §10.6 "SkillDefinition + graph template + manifest
    + eval version + content hash"）。

    frozen + 可序列化；由 :class:`SkillReleaseGate.publish` 产出。
    红线：local_dev 发布后仍 ``unverified=True``。
    """

    release_id: str                        # uuid4 hex
    manifest: SkillManifest                # 发布版 manifest（version ≥ 1.0.0）
    graph_template: Optional[Dict[str, Any]] = None  # workflow 来源时的源 graph
    workflow_hash: str = ""                # workflow 来源 hash（来源追溯）
    executor_kind_value: str = ""          # 受控 executor kind 字符串值
    eval_suite_version: str = ""           # eval 套件版本（发布时通过的）
    eval_passed: bool = False              # 发布时 eval 是否全 PASS
    content_hash: str = ""                 # 整个发布产物的内容 hash
    released_by: str = ""
    released_at: float = 0.0

    @property
    def skill_id(self) -> str:
        return self.manifest.skill_id

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def unverified(self) -> bool:
        """红线：local_dev 发布后仍 unverified。"""
        return self.manifest.unverified

    @property
    def source(self) -> SkillSource:
        return self.manifest.source

    @property
    def executor_kind(self) -> "ExecutorKind":
        """受控 executor kind（红线：is_arbitrary_code 恒为 False）。"""
        # 二次防御：构造失败（未知值）→ 视为任意代码（保守拒绝）
        from src.features.ai_harness.skill_studio_drafts import ExecutorKind
        try:
            return ExecutorKind(self.executor_kind_value)
        except ValueError:
            # 未知值视为危险（不应发生，但防御性处理）
            class _Dangerous:
                value = self.executor_kind_value
                is_arbitrary_code = True
            return _Dangerous()  # type: ignore[return-value]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "release_id": self.release_id,
            "skill_id": self.skill_id,
            "version": self.version,
            "manifest": self.manifest.to_dict(),
            "graph_template": self.graph_template,
            "workflow_hash": self.workflow_hash,
            "executor_kind": self.executor_kind_value,
            "eval_suite_version": self.eval_suite_version,
            "eval_passed": self.eval_passed,
            "content_hash": self.content_hash,
            "released_by": self.released_by,
            "released_at": self.released_at,
            "unverified": self.unverified,
        }

    @staticmethod
    def from_draft(
        *,
        draft: SkillDraft,
        eval_suite_version: str,
        released_by: str,
        eval_passed: bool = False,
        released_at: float = 0.0,
    ) -> "SkillRelease":
        """从已通过门禁的草稿构造发布产物。

        红线：草稿 source 保持（local_dev 仍 local_dev）；version 已由门禁保证 ≥ 1.0.0。
        content_hash 由 *发布产物整体* 计算（manifest + graph + executor + eval）。
        """
        # 重算 manifest 签名（确保 content_hash 反映最终内容）
        manifest = with_signature(draft.manifest, signed_by=f"local_dev:{released_by}")
        content_hash = _compute_release_hash(
            manifest=manifest,
            graph_template=draft.workflow_template,
            executor_kind_value=draft.executor_kind.value,
            eval_suite_version=eval_suite_version,
        )
        wf_hash = draft.origin.workflow_hash if draft.origin.kind == "workflow_definition" else ""
        return SkillRelease(
            release_id=uuid.uuid4().hex,
            manifest=manifest,
            graph_template=draft.workflow_template,
            workflow_hash=wf_hash,
            executor_kind_value=draft.executor_kind.value,
            eval_suite_version=eval_suite_version,
            eval_passed=eval_passed,
            content_hash=content_hash,
            released_by=released_by,
            released_at=released_at or time.time(),
        )


# ---------------------------------------------------------------------- #
# PublishResult —— 门禁返回
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PublishResult:
    """发布门禁的返回：成功 → release；失败 → deny_code + eval_report。"""

    success: bool
    release: Optional[SkillRelease] = None
    deny_code: str = ""
    deny_message: str = ""
    eval_report: Optional[SkillEvalReport] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "release": self.release.to_dict() if self.release else None,
            "deny_code": self.deny_code,
            "deny_message": self.deny_message,
            "eval_report": self.eval_report.to_dict() if self.eval_report else None,
        }


# ---------------------------------------------------------------------- #
# SkillReleaseGate —— 发布门禁
# ---------------------------------------------------------------------- #


class SkillReleaseGate:
    """发布门禁（plan §10.6 "发布前必须通过 eval"）。

    三道门：

    1. **版本门**：draft.version 必须 ≥ 1.0.0（dev 0.x 不得发布）；
    2. **eval 门**：必须跑默认 eval 套件且全部 PASS；
    3. **executor 门**：draft.executor_kind 不得为任意代码（二次防御）。

    用法::

        gate = SkillReleaseGate()
        result = gate.publish(draft, released_by="dev-alice")
        if result.success:
            gate.register_into(registry, result.release)
    """

    def __init__(self, *, eval_suite: Optional[SkillEvalSuite] = None) -> None:
        self._eval_suite = eval_suite or SkillEvalSuite.default()

    def publish(
        self, draft: SkillDraft, *, released_by: str = "", now: float = 0.0,
    ) -> PublishResult:
        """跑门禁并发布。返回 :class:`PublishResult`。"""
        ts = now or time.time()

        # ---- 门 1：版本 ≥ 1.0.0 ----
        if draft.version.is_dev:
            return PublishResult(
                success=False,
                deny_code=PublishDenyCode.DEV_VERSION_NOT_PUBLISHABLE,
                deny_message=(
                    f"draft version {draft.version} is dev (< 1.0.0); "
                    f"bump to ≥ 1.0.0 before publishing"
                ),
            )

        # ---- 门 2：executor_kind 受控（二次防御；红线）----
        if draft.executor_kind.is_arbitrary_code:
            return PublishResult(
                success=False,
                deny_code=PublishDenyCode.ARBITRARY_CODE_EXECUTOR,
                deny_message=(
                    f"executor_kind {draft.executor_kind.value!r} is arbitrary-code; "
                    f"third-party skills must not execute arbitrary Python/shell/fs/ui"
                ),
            )

        # ---- 门 3：eval 套件全 PASS ----
        report = self._eval_suite.run(draft)
        if not report.passed:
            failures = report.failures()
            failure_names = [f.name for f in failures]
            return PublishResult(
                success=False,
                deny_code=PublishDenyCode.EVAL_FAILED,
                deny_message=(
                    f"eval suite not all passed: {report.failed_count}/{report.total} "
                    f"failed ({failure_names})"
                ),
                eval_report=report,
            )

        # ---- 通过门禁 → 构造发布产物 ----
        release = SkillRelease.from_draft(
            draft=draft,
            eval_suite_version=self._eval_suite.suite_version,
            eval_passed=True,
            released_by=released_by,
            released_at=ts,
        )
        return PublishResult(success=True, release=release, eval_report=report)

    def register_into(self, registry: Any, release: SkillRelease) -> List[Any]:
        """把发布产物注册进 :class:`SkillRegistry`。

        红线（编码为校验，而非仅写在 docstring —— AUD-22409）：

        - 草稿 skill_id 必须 ``draft.*`` 前缀（绝不覆盖 official skill；
          ``SkillRelease`` 是公开 frozen dataclass，可绕过 ``from_draft`` 用
          任意 manifest 构造，故在注册前强制校验）；
        - source 必须为 local_dev（发布不改变来源，也不得冒充官方）；
        - local_dev 仍 unverified（registry 不改 source）；
        - 工具 allowlist 仍由 registry 校验（FORBIDDEN_CAPABILITIES 仍拒绝）。

        返回 registry.register 的诊断列表（空 = 成功）；红线不满足时返回
        拒绝诊断且**不**触碰 registry。
        """
        from src.features.edit.ai_editing.diagnostics import (
            DiagnosticSeverity,
            TaepDiagnosticCode,
            make_diagnostic,
        )

        skill_id = str(release.skill_id)
        if not skill_id.startswith("draft."):
            reason = (
                f"release skill_id {skill_id!r} must use the 'draft.' prefix; "
                f"registering a release must never override an official skill"
            )
            return [make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.PERMISSION_DENIED,
                reason=reason,
                details={"skill_id": skill_id, "red_line": "draft_prefix_required"},
            )]
        if release.source != SkillSource.LOCAL_DEV:
            reason = (
                f"release source must be local_dev (got {release.source!r}); "
                f"published skills stay unverified and never pose as official"
            )
            return [make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.PERMISSION_DENIED,
                reason=reason,
                details={"skill_id": skill_id, "source": str(release.source)},
            )]
        diags = registry.register(release.manifest, enabled=True)
        return list(diags or [])


# ---------------------------------------------------------------------- #
# content hash 计算
# ---------------------------------------------------------------------- #


def _compute_release_hash(
    *,
    manifest: SkillManifest,
    graph_template: Optional[Dict[str, Any]],
    executor_kind_value: str,
    eval_suite_version: str,
) -> str:
    """计算发布产物整体的稳定内容 hash（sha256 hex）。

    payload = manifest.content_hash + canonical graph + executor_kind + eval_version。
    """
    import hashlib
    import json
    payload = {
        "manifest_hash": manifest.content_hash,
        "graph_template": graph_template,
        "executor_kind": executor_kind_value,
        "eval_suite_version": eval_suite_version,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "PublishDenyCode",
    "SkillRelease",
    "PublishResult",
    "SkillReleaseGate",
]
