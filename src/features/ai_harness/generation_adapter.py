"""生成能力 Harness 适配器（plan §10.5 P3.3）。

把现有 :class:`GenerationService`（``media_generation_service``）接入
:class:`HarnessRuntime`，让图片 / 视频 / 音乐 / TTS 都是 provider task，返回候选
artifact（plan §10.5 P3.3）。

设计（plan §10.5 P3.3 / §10.1）：

- :class:`GenerationHarnessAdapter` 把 :class:`Invocation` 转成 ``GenerationRequest``，
  调 ``GenerationService.request_generation``，把 ``(JobHandle, list[Diagnostic])`` +
  ``GenerationResult.candidates`` 翻译进 :class:`RunRecord`。
- 共享一个 :class:`JobRegistry`（service 与 bridge），让 Job 进度经
  :class:`JobRunBridge` 流入 RunRecord。
- consent gate（``GenerationConsent.is_complete``）→ RunRecord ``WAITING_FOR_USER``，
  直到 host 确认；未确认不生成（plan §0 红线 1）。
- 取消经 :meth:`HarnessRuntime.is_cancelled` → ``poll_cancel_to_job`` 真实传播。
- 失败 / 取消不污染工程：候选不进 ``GenerationBin``（service 已保证），timeline
  不被写（生成只产候选 artifact，进 timeline 走另一条 approval 链）。

红线（plan §10.5 P3.3 / §1.3）：

- 生成前显示模型 / 上传 / 成本 / 许可证 / 安全（``GenerationConsent`` 必须完整）。
- 结果不自动进 timeline（候选进 GenerationBin，待用户预览 / 导入）。
- 取消 / 失败不污染工程（候选不入 bin；timeline 不变）。
- 外部输入不可信：prompt 经 hash 进 provenance（``GenerationRequest.prompt_hash``），
  不存原始 prompt（除非 retain_prompts）。
"""

from __future__ import annotations

import logging
from typing import Any

from src.features.ai_harness.invocation_models import (
    ArtifactRef,
    Invocation,
    RunEvent,
    RunEventKind,
    RunStatus,
    sanitize_for_storage,
)
from src.features.ai_harness.runtime import HarnessRuntime
from src.features.ai_harness.scheduler_adapter import (
    JobRunBridge,
    job_ref_from_handle,
    poll_cancel_to_job,
)
from src.features.edit.ai_editing.generation_models import (
    GenerationBudget,
    GenerationConsent,
    GenerationModality,
    GenerationRequest,
)


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# Invocation → GenerationRequest
# ---------------------------------------------------------------------- #


def invocation_to_generation_request(inv: Invocation) -> GenerationRequest:
    """把 :class:`Invocation` 转成 ``GenerationRequest``（plan §10.5 P3.3）。

    inputs 约定字段：modality / prompt / negative_prompt / duration_seconds /
    aspect_ratio / style / reference_asset_ids / consent / budget / provider_id /
    requested_by。
    """
    inputs = inv.inputs or {}
    modality_str = str(inputs.get("modality", "image"))
    try:
        modality = GenerationModality(modality_str)
    except ValueError:
        modality = GenerationModality.IMAGE

    # AUD-21907：reference_asset_ids 来自外部文本链路（chat/workflow/MCP），
    # 必须验证形状——字符串会被 tuple() 拆成单字符 id、静默污染图生视频/图生图
    # 的参考素材。非法形状在这里抛 ValueError，由 invoke 的 REQUEST_BUILD_FAILED
    # 异常边界收进 RunRecord（fail-closed，不静默丢弃也不猜意图）。
    reference_asset_ids = _validated_reference_asset_ids(
        inputs.get("reference_asset_ids", ()),
    )

    consent = inputs.get("consent")
    if not isinstance(consent, GenerationConsent):
        consent = _consent_from_inputs(inputs)

    budget = inputs.get("budget")
    if not isinstance(budget, GenerationBudget):
        budget = GenerationBudget()

    return GenerationRequest(
        request_id=inv.invocation_id,
        modality=modality,
        prompt=str(inputs.get("prompt", "")),
        consent=consent,
        budget=budget,
        negative_prompt=str(inputs.get("negative_prompt", "")),
        duration_seconds=(
            float(inputs["duration_seconds"]) if inputs.get("duration_seconds") else None
        ),
        aspect_ratio=str(inputs.get("aspect_ratio", "")),
        style=str(inputs.get("style", "")),
        reference_asset_ids=reference_asset_ids,
        requested_by=str(inputs.get("requested_by", f"harness:{inv.source}")),
    )


def _validated_reference_asset_ids(raw: Any) -> tuple[str, ...]:
    """校验并规整外部输入的 reference_asset_ids（AUD-21907）。

    只接受 list/tuple 且逐项为非空字符串；其他形状（含单个字符串）抛
    ``ValueError``——调用方（invoke）把该异常记为 REQUEST_BUILD_FAILED。
    """
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ValueError(
            "reference_asset_ids 必须是素材 id 字符串的列表/元组，"
            f"实际收到 {type(raw).__name__}"
        )
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            ids.append(item)
        else:
            raise ValueError(
                "reference_asset_ids 的每一项都必须是非空字符串，"
                f"实际收到 {item!r}"
            )
    return tuple(ids)


def _consent_from_inputs(inputs: dict[str, Any]) -> GenerationConsent:
    """从 inputs 的扁平字段构造 consent（plan §0 红线 1：consent 必须完整）。"""
    consent_data = inputs.get("consent_fields", {}) or {}
    return GenerationConsent(
        confirmed=bool(consent_data.get("confirmed", inputs.get("consent_confirmed", False))),
        provider_id=str(consent_data.get("provider_id", inputs.get("provider_id", ""))),
        cost_acknowledged=bool(
            consent_data.get("cost_acknowledged", inputs.get("cost_acknowledged", False))
        ),
        upload_acknowledged=bool(
            consent_data.get("upload_acknowledged", inputs.get("upload_acknowledged", False))
        ),
        copyright_acknowledged=bool(
            consent_data.get("copyright_acknowledged",
                             inputs.get("copyright_acknowledged", False))
        ),
        target_location=str(consent_data.get("target_location",
                                             inputs.get("target_location", ""))),
    )


# ---------------------------------------------------------------------- #
# GenerationHarnessAdapter
# ---------------------------------------------------------------------- #


class GenerationHarnessAdapter:
    """把 :class:`GenerationService` 接入 :class:`HarnessRuntime`（plan §10.5 P3.3）。

    用法（host 装配）::

        gen_svc = GenerationService(registry, jobs=shared_jobs)
        adapter = GenerationHarnessAdapter(gen_svc, runtime)
        run_id = adapter.invoke(invocation)

    红线：结果不自动进 timeline；取消 / 失败不污染工程。
    """

    def __init__(
        self,
        generation_service: Any,
        runtime: HarnessRuntime,
        *,
        bridge: JobRunBridge | None = None,
    ) -> None:
        self._svc = generation_service
        self._runtime = runtime
        # 共享 JobRegistry：service 拥有的 + bridge 绑定。
        self._jobs = getattr(generation_service, "job_registry", None) \
            or getattr(generation_service, "_jobs", None)
        self._bridge = bridge or JobRunBridge(runtime)
        # AUD-21905：run 取消必须传播到生成 job——invoke 返回后（真实 provider
        # 在 shared worker 里跑）不再有同步检查点，订阅 RUN_CANCEL_REQUESTED
        # 事件把 cancel token 实时翻译成 job.request_cancel()。
        self._unsubscribe_cancel = self._runtime.add_subscriber(
            self._on_run_cancel_requested
        )

    def close(self) -> None:
        """退订 runtime 事件（与 runtime 同生命周期的宿主可不调用）。"""
        unsubscribe = getattr(self, "_unsubscribe_cancel", None)
        if callable(unsubscribe):
            unsubscribe()
            self._unsubscribe_cancel = None

    def _on_run_cancel_requested(self, event: RunEvent) -> None:
        """把 run 的 cancel token 传播到其绑定的生成 job（AUD-21905）。"""
        if event.kind != RunEventKind.RUN_CANCEL_REQUESTED:
            return
        try:
            self._bridge.propagate_cancel(event.run_id)
        except Exception:  # noqa: BLE001 — 取消传播不得外抛打断发布方
            _LOG.exception(
                "cancel propagation failed for run %s", event.run_id,
            )

    # ---------------- 主入口 ---------------- #

    def invoke(self, inv: Invocation) -> str:
        """执行生成 invocation；返回 run_id。状态全写进 RunRecord。"""
        run_id = self._runtime.start(inv)

        # 1. policy gate：allow_generation 关闭 → 拒绝
        if not inv.policy.allow_generation:
            self._runtime.set_status(run_id, RunStatus.VALIDATING)
            self._runtime.record_diagnostic(run_id, {
                "severity": "error", "code": "GENERATION_DISABLED",
                "message": "本次运行策略未允许生成（allow_generation=False）",
            })
            self._runtime.finish(
                run_id, success=False,
                error_code="GENERATION_DISABLED",
                error_message="policy.allow_generation is False",
            )
            return run_id

        # 2. 构造 request
        try:
            request = invocation_to_generation_request(inv)
        except Exception as exc:  # noqa: BLE001
            self._runtime.set_status(run_id, RunStatus.VALIDATING)
            self._runtime.record_diagnostic(run_id, {
                "severity": "error", "code": "REQUEST_BUILD_FAILED",
                "message": str(exc),
            })
            self._runtime.finish(
                run_id, success=False,
                error_code="REQUEST_BUILD_FAILED", error_message=str(exc),
            )
            return run_id

        # 3. consent gate：未完整 → WAITING_FOR_USER（plan §0 红线 1）
        if not request.consent.is_complete:
            self._runtime.mark_running(run_id)
            self._runtime.mark_waiting_for_user(run_id, message=(
                f"生成 {request.modality.value} 需要确认：模型 / 上传 / 成本 / "
                f"许可证。provider={request.consent.provider_id or '(默认)'}"
            ))
            self._runtime.record_diagnostic(run_id, {
                "severity": "warning", "code": "CONSENT_INCOMPLETE",
                "message": "GenerationConsent 未完整，等待用户确认",
                "missing": _missing_consent_fields(request.consent),
                "prompt_hash": request.prompt_hash,
            })
            # 不继续执行；host 确认后重新 invoke（带完整 consent）
            return run_id

        # 4. 取消检查
        if self._runtime.is_cancelled(run_id):
            self._runtime.finish(run_id, success=False)
            return run_id

        # 5. 调 GenerationService
        self._runtime.mark_running(run_id)
        if self._jobs is not None:
            self._bridge.bind(run_id, self._jobs)
        try:
            run_async = False
            is_demo_request = getattr(self._svc, "is_demo_request", None)
            if callable(is_demo_request):
                run_async = not bool(is_demo_request(request))
            request_fn = (
                getattr(self._svc, "request_generation_async", None)
                if run_async else getattr(self._svc, "request_generation", None)
            )
            if not callable(request_fn):
                request_fn = self._svc.request_generation
            job_handle, diagnostics = request_fn(request)
        except Exception as exc:  # noqa: BLE001 — 异常归 RunRecord
            self._runtime.record_diagnostic(run_id, {
                "severity": "error", "code": "GENERATION_EXCEPTION",
                "message": str(exc),
            })
            self._runtime.finish(
                run_id, success=False,
                error_code="GENERATION_EXCEPTION", error_message=str(exc),
            )
            _LOG.exception("generation service failed for run %s", run_id)
            return run_id

        # 6. 诊断
        for diag in diagnostics or ():
            self._runtime.record_diagnostic(run_id, sanitize_for_storage(diag.to_dict())
                                            if hasattr(diag, "to_dict") else sanitize_for_storage(diag))

        # 7. job 失败 / 被拒（handle is None：consent/cost/provider gate 失败）
        if job_handle is None:
            self._runtime.finish(
                run_id, success=False,
                error_code="GENERATION_GATE_FAILED",
                error_message="生成被 gate 拒绝（consent/cost/provider，见 diagnostics）",
            )
            return run_id

        # 8. job → RunRecord
        self._bridge.track(run_id, job_handle.job_id, self._jobs)
        self._runtime.attach_job(run_id, job_ref_from_handle(job_handle))

        # 9. 取消传播（若 run 已 cancel）
        if self._runtime.is_cancelled(run_id):
            poll_cancel_to_job(self._runtime, run_id, job_handle)
            self._runtime.finish(run_id, success=False)
            return run_id

        # 10. 真实 provider 已进入 shared worker；demo provider 仍同步完成，
        # 保持离线测试和 deterministic UI 路径可立即返回。
        future = None
        if run_async:
            worker_pool = getattr(self._svc, "worker_pool", None)
            future = getattr(worker_pool, "future", lambda _job_id: None)(job_handle.job_id)
        if future is not None and not future.done():
            future.add_done_callback(
                lambda completed: self._on_generation_worker_done(
                    completed, run_id, request, job_handle,
                )
            )
        else:
            self._collect_generation_outcome(run_id, request, job_handle)

        return run_id

    def _on_generation_worker_done(
        self, future: Any, run_id: str, request: GenerationRequest, job_handle: Any,
    ) -> None:
        """Finish a remote generation run from the worker completion callback."""
        try:
            future.result()
            self._collect_generation_outcome(run_id, request, job_handle)
        except Exception as exc:  # noqa: BLE001 — surface callback failure in the run
            self._runtime.record_diagnostic(run_id, {
                "severity": "error",
                "code": "GENERATION_WORKER_CALLBACK_FAILED",
                "message": str(exc),
            })
            self._runtime.finish(
                run_id,
                success=False,
                error_code="GENERATION_WORKER_CALLBACK_FAILED",
                error_message=str(exc),
            )

    # ---------------- 结果收集 ---------------- #

    def _collect_generation_outcome(
        self, run_id: str, request: GenerationRequest, job_handle: Any,
    ) -> None:
        """把生成候选 + 成本归入 RunRecord（plan §10.5 P3.3）。"""
        # 候选从 GenerationBin 取（service 已把 result.candidates 推进 bin）
        gen_bin = getattr(self._svc, "generation_bin", None) \
            or getattr(self._svc, "_bin", None)
        candidates = ()
        if gen_bin is not None:
            try:
                candidates = gen_bin.by_request(request.request_id)
            except Exception:  # noqa: BLE001
                candidates = ()

        # job 状态判定成功 / 失败
        status = getattr(job_handle, "status", None)
        status_val = getattr(status, "value", str(status))
        success = status_val == "succeeded"

        # AUD-21906：job 的 cancelled 终态（job 面板直接按 job_id 取消）不得
        # 记成 FAILED/GENERATION_JOB_FAILED——先把 cancel token 置位（finish 的
        # cancel 分支随后收敛到 CANCELLED 终态），并留可见诊断区分取消与失败。
        if status_val == "cancelled":
            self._runtime.record_diagnostic(run_id, {
                "severity": "warning", "code": "GENERATION_JOB_CANCELLED",
                "job_id": getattr(job_handle, "job_id", ""),
                "message": "生成 job 被取消（run 终态记 CANCELLED，非失败）",
            })
            self._runtime.cancel(run_id, reason="generation_job_cancelled")
            self._runtime.finish(run_id, success=False)
            return

        # 候选 → artifact（不自动进 timeline；只产引用）
        for cand in candidates:
            self._runtime.record_artifact(run_id, ArtifactRef(
                artifact_id=cand.asset_id,
                artifact_type=f"generated_{cand.modality.value}",
                producer_node_id=f"generation:{request.consent.provider_id or 'default'}",
                summary=(
                    f"{cand.modality.value} 候选 · license={getattr(cand.license, 'value', '?')}"
                    f" · cost={cand.cost_units}"
                ),
            ))

        # 成本
        cost = float(getattr(job_handle, "result_manifest", {}).get("actual_cost_units", 0.0) or 0.0) \
            if getattr(job_handle, "result_manifest", None) else 0.0
        if cost > 0:
            self._runtime.record_cost(run_id, cost)

        # 许可证 / 安全诊断（plan §10.5：显示许可证/安全）
        for cand in candidates:
            license_val = getattr(cand.license, "value", str(cand.license))
            if license_val in ("unknown", "pending"):
                self._runtime.record_diagnostic(run_id, {
                    "severity": "warning", "code": "LICENSE_UNKNOWN",
                    "asset_id": cand.asset_id,
                    "message": f"候选 {cand.asset_id} 许可证未知，商用前需确认",
                })
            if getattr(cand.safety, "blocked", False):
                self._runtime.record_diagnostic(run_id, {
                    "severity": "error", "code": "SAFETY_BLOCKED",
                    "asset_id": cand.asset_id,
                    "message": f"候选 {cand.asset_id} 被安全检查拦截",
                })

        # job 进度更新
        self._bridge.sync(run_id, job_handle.job_id)

        # AUD-21905：收尾前最后一次取消检查——worker 回调里 finish(success=True)
        # 必须让位于已请求的取消（终态 CANCELLED），并把协作取消信号补传给 job。
        if self._runtime.is_cancelled(run_id):
            poll_cancel_to_job(self._runtime, run_id, job_handle)
            self._runtime.finish(run_id, success=False)
            return

        with_warnings = bool(candidates) and any(
            getattr(c.license, "value", "") in ("unknown", "pending") for c in candidates
        )
        self._runtime.finish(
            run_id, success=success, with_warnings=with_warnings,
            error_code="" if success else "GENERATION_JOB_FAILED",
            error_message="" if success else (
                getattr(job_handle, "error_message", "") or "生成 job 失败"
            ),
        )


def _missing_consent_fields(consent: GenerationConsent) -> list[str]:
    """列出 consent 缺的字段（供 UI 提示）。"""
    missing = []
    if not consent.provider_id:
        missing.append("provider_id")
    if not consent.cost_acknowledged:
        missing.append("cost_acknowledged")
    if not consent.upload_acknowledged:
        missing.append("upload_acknowledged")
    if not consent.copyright_acknowledged:
        missing.append("copyright_acknowledged")
    return missing


__all__ = [
    "GenerationHarnessAdapter",
    "invocation_to_generation_request",
]
