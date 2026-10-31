"""TAEP M11 — Media Generation Service（§3.1 / §8.2 / §11.1 M11 / §11.2 M11 step 1）。

``GenerationService`` 是生成媒体的中央编排器，把请求经"用户同意 → 成本上限 gate → 异步
Job → provider → 生成素材箱"闭环，保证 §11.1 M11 退出条件：

- **生成取消/失败不污染工程**（§11.1 M11 退出条件）：失败/取消的候选不入素材箱，工程不变。
- **候选入媒体池后才可选择**（§11.1 M11 退出条件）：候选先进 ``GenerationBin``，用户显式
  import 后才成 ``MediaAsset``（经 ``MediaPoolImporter`` 注入点）。
- **可取消 Job**（§0 第三条 / §1.2 #7）：生成是长任务，进 ``JobRegistry``（``JobKind.GENERATION``），
  UI 只消费状态事件。
- **未经用户确认不得生成付费素材**（§0 第一条）：``GenerationConsent.is_complete`` 是硬 gate。
- **成本上限**（§10.2 / §11.2 M11 step 1）：``budget.max_cost_units`` 是硬上限；超限拒绝。

红线（§8.2 / §11.2 M11 / §0 / §1.2）：

- **不直接写 timeline**（§11.1 M11："生成结果直接入 timeline"是绝对不要做）：Agent 若建议
  放入 timeline，只能生成包含生成资产 ID 的 EditPlan，仍经 Preview/Diff/Approval/Undo
  （§11.2 M11 step 3）——本服务只产候选，timeline 提议是 G3 留后续。
- **provider adapter 可换**（§3.1）：经 ``ProviderRegistry`` 查找，不绑 SDK。
- **provenance 完整**（§1.2 #6）：候选携带 ``GenerationProvenance``；导入事件可追溯。
- **不记 secret / 绝对路径**（§0 第四条）：凭证经 ``credential_ref``；provenance 只记 hash。

v1（用户确认 G0-G2）：``request_generation`` 保留同步测试入口；生产调用
``request_generation_async``，由共享 ``JobWorkerPool`` 执行 provider，状态和进度仍回到
同一个 ``JobRegistry``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import threading
from typing import Any

from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.generation_bin import (
    GenerationBin,
    ImportEvent,
    MediaPoolImporter,
)
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset,
    GeneratedAssetState,
    GenerationModality,
    GenerationRequest,
    GenerationResult,
)
from src.features.edit.ai_editing.generation_provider import (
    GenerationProvider,
    ProviderRegistry,
)
from src.features.edit.ai_editing.jobs import (
    JobEvent,
    JobHandle,
    JobKind,
    JobMetrics,
    JobRegistry,
    JobStatus,
    JobWorkerPool,
)


# ---------------------------------------------------------------------- #
# 宿主侧加锁包装（AUD-13903）
# ---------------------------------------------------------------------- #


class _LockedJobRegistry(JobRegistry):
    """带宿主侧锁的 :class:`JobRegistry`（AUD-13903）。

    依赖方自述（jobs.py:329-330）："P4 不做锁（单 worker 场景足够）；多 worker
    由宿主加锁"。``GenerationService`` 正是宿主：本子类把事件序号自增、事件表
    追加与 job 字典读写串行化，避免并发 ``_record_event`` 产生重复序号（
    ``events_since`` 游标漏事件）或 UI 线程枚举与 worker 写入竞争。

    仅覆盖 service 自建的 registry；外部注入的 registry 无法事后加锁（已提交
    JobHandle 的 ``_event_sink`` 绑定在原对象上），此时 service 会把 worker 池
    限制为单 worker（见 :attr:`GenerationService.worker_pool`）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._registry_lock = threading.RLock()

    def submit(self, kind: JobKind, label: str = "") -> JobHandle:  # type: ignore[override]
        with self._registry_lock:
            return super().submit(kind, label)

    def _record_event(self, job: JobHandle, kind: str, message: str = "") -> None:
        with self._registry_lock:
            return super()._record_event(job, kind, message)

    def events_since(self, sequence: int = 0) -> tuple["JobEvent", ...]:  # type: ignore[override]
        with self._registry_lock:
            return super().events_since(sequence)

    def metrics(self, kind: JobKind | None = None) -> "JobMetrics":  # type: ignore[override]
        with self._registry_lock:
            return super().metrics(kind)

    def get(self, job_id: str) -> JobHandle | None:  # type: ignore[override]
        with self._registry_lock:
            return super().get(job_id)

    def cancel(self, job_id: str) -> bool:  # type: ignore[override]
        with self._registry_lock:
            return super().cancel(job_id)

    def list_active(self) -> list[JobHandle]:  # type: ignore[override]
        with self._registry_lock:
            return super().list_active()

    def list_all(self) -> list[JobHandle]:  # type: ignore[override]
        with self._registry_lock:
            return super().list_all()

    def cleanup_terminal(
        self, keep: int = 50, *, max_age_seconds: float | None = None,
    ) -> None:  # type: ignore[override]
        with self._registry_lock:
            return super().cleanup_terminal(keep, max_age_seconds=max_age_seconds)

    def prune_events(self, max_events: int = 500) -> None:  # type: ignore[override]
        with self._registry_lock:
            return super().prune_events(max_events)

    def to_dict(self) -> dict[str, Any]:  # type: ignore[override]
        with self._registry_lock:
            return super().to_dict()

    def restore_dict(
        self, data: dict[str, Any], *, terminate_active: bool = True,
    ) -> bool:  # type: ignore[override]
        with self._registry_lock:
            return super().restore_dict(data, terminate_active=terminate_active)


class _LockedGenerationBin(GenerationBin):
    """带宿主侧锁的 :class:`GenerationBin`（AUD-13903）。

    ``GenerationBin`` 自身无锁；worker 线程 ``add_candidates`` 写 ``_assets`` 与
    UI 线程 ``by_request()/all_assets()`` 枚举并发时会触发
    ``RuntimeError: dictionary changed size during iteration``。本子类把全部公开
    读写串行化。可把一个宿主已构造的 bin 的状态嫁接进来（状态只有
    ``_assets``/``_import_events`` 两个容器，无回调绑定，嫁接是完整的）。
    """

    def __init__(self, inner: GenerationBin | None = None) -> None:
        super().__init__()
        self._bin_lock = threading.RLock()
        if inner is not None:
            # 嫁接宿主 bin 的状态：宿主保留的原引用与本包装共享同一容器，
            # service 对外暴露的是本包装（带锁视图）。
            self._assets = inner._assets
            self._import_events = inner._import_events

    @property
    def count(self) -> int:  # type: ignore[override]
        with self._bin_lock:
            return super().count

    def get(self, asset_id: str) -> GeneratedAsset | None:  # type: ignore[override]
        with self._bin_lock:
            return super().get(asset_id)

    def require(self, asset_id: str) -> GeneratedAsset:  # type: ignore[override]
        with self._bin_lock:
            return super().require(asset_id)

    def all_assets(self) -> tuple[GeneratedAsset, ...]:  # type: ignore[override]
        with self._bin_lock:
            return super().all_assets()

    def by_state(self, state: GeneratedAssetState) -> tuple[GeneratedAsset, ...]:  # type: ignore[override]
        with self._bin_lock:
            return super().by_state(state)

    def by_request(self, request_id: str) -> tuple[GeneratedAsset, ...]:  # type: ignore[override]
        with self._bin_lock:
            return super().by_request(request_id)

    def pending_preview(self) -> tuple[GeneratedAsset, ...]:  # type: ignore[override]
        with self._bin_lock:
            return super().pending_preview()

    def importable(self) -> tuple[GeneratedAsset, ...]:  # type: ignore[override]
        with self._bin_lock:
            return super().importable()

    def add_candidates(self, candidates: tuple[GeneratedAsset, ...]) -> None:  # type: ignore[override]
        with self._bin_lock:
            return super().add_candidates(candidates)

    def mark_previewed(self, asset_id: str) -> GeneratedAsset:  # type: ignore[override]
        with self._bin_lock:
            return super().mark_previewed(asset_id)

    def set_preview_handle(self, asset_id: str, preview_handle: str) -> GeneratedAsset:  # type: ignore[override]
        with self._bin_lock:
            return super().set_preview_handle(asset_id, preview_handle)

    def discard(self, asset_id: str) -> GeneratedAsset | None:  # type: ignore[override]
        with self._bin_lock:
            return super().discard(asset_id)

    def import_to_media_pool(
        self,
        asset_id: str,
        *,
        importer: MediaPoolImporter,
        approved_by: str = "",
    ) -> tuple[str | None, list[Diagnostic]]:  # type: ignore[override]
        with self._bin_lock:
            return super().import_to_media_pool(
                asset_id, importer=importer, approved_by=approved_by,
            )

    def import_events(self) -> tuple[ImportEvent, ...]:  # type: ignore[override]
        with self._bin_lock:
            return super().import_events()

    def provenance_report(self) -> dict[str, Any]:  # type: ignore[override]
        with self._bin_lock:
            return super().provenance_report()

    def clear_discarded(self) -> int:  # type: ignore[override]
        with self._bin_lock:
            return super().clear_discarded()


# ---------------------------------------------------------------------- #
# GenerationService —— 中央编排器
# ---------------------------------------------------------------------- #


class GenerationService:
    """生成媒体编排服务（§3.1 ``media_generation_service.py`` / §8.2 / §11.2 M11）。

    用法::

        svc = GenerationService(registry=providers, bin=gen_bin, jobs=job_registry)
        job, diags = svc.request_generation(request)
        if job and job.status == JobStatus.SUCCEEDED:
            candidates = gen_bin.by_request(request.request_id)

    红线：同意/成本 gate 未过 → 不创建 Job（不静默）；失败/取消 → 候选不入素材箱。
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        bin: GenerationBin | None = None,
        jobs: JobRegistry | None = None,
        cost_ledger: "CostLedger | None" = None,
        workers: JobWorkerPool | None = None,
    ) -> None:
        self._registry = registry
        # AUD-13903：service 是 jobs.py P4 契约里的"宿主"，负责为无锁的
        # GenerationBin 提供锁（宿主传入的 bin 嫁接其状态，共享容器）。
        self._bin = _LockedGenerationBin(bin) if bin is not None else _LockedGenerationBin()
        # 外部注入的 JobRegistry 无法事后加锁（_event_sink 绑定原对象）；
        # 自建时用带锁子类（见 worker_pool 的单 worker 回退）。
        self._jobs = jobs if jobs is not None else _LockedJobRegistry()
        self._cost_ledger = cost_ledger
        self._workers = workers
        self._pool_lock = threading.RLock()
        # AUD-13902：取消/关闭与 worker 的"终态判定 + 候选入箱"临界区互斥，
        # 关闭"取消后候选仍进入素材箱"的 TOCTOU 窗口（§11.1 红线）。
        self._finalize_lock = threading.RLock()

    # ---------------- 查询 ---------------- #

    @property
    def generation_bin(self) -> GenerationBin:
        return self._bin

    @property
    def job_registry(self) -> JobRegistry:
        return self._jobs

    def preferred_provider(self, modality: GenerationModality) -> GenerationProvider | None:
        """返回指定 modality 的首选 provider，供宿主展示 consent 摘要。"""
        return self._registry.preferred_for(modality)

    def is_demo_request(self, request: GenerationRequest) -> bool:
        """Return whether the resolved provider is the deterministic demo path."""
        provider = self._resolve_provider(request)
        return bool(not isinstance(provider, list) and getattr(provider, "is_demo", False))

    @property
    def worker_pool(self) -> JobWorkerPool:
        """Lazily create the shared non-Qt worker pool for remote work.

        AUD-13903：并发首调不再重复构建线程池（旧实现 check-then-act 会泄漏
        输家的 executor）。jobs.py P4 契约声明 JobRegistry 不加锁、多 worker 由
        宿主负责：自建 registry 已是 ``_LockedJobRegistry``，可安全使用池默认
        并发；外部注入的 registry 无法加锁，退回单 worker 直到依赖方内置锁。
        """
        with self._pool_lock:
            if self._workers is None:
                if isinstance(self._jobs, _LockedJobRegistry):
                    self._workers = JobWorkerPool(self._jobs)
                else:
                    self._workers = JobWorkerPool(self._jobs, max_workers=1)
            return self._workers

    # ---------------- 请求生成（主入口）---------------- #

    def request_generation(
        self, request: GenerationRequest,
    ) -> tuple[JobHandle | None, list[Diagnostic]]:
        """处理生成请求：同意/成本 gate → Job → provider → 素材箱。

        Returns:
            (job, diagnostics)：gate 未过时 job=None + 诊断；成功时 job.status=SUCCEEDED，
            候选已入素材箱（diagnostics 可含 WARNING 级月度成本告警，AUD-13900）。
            失败时 job.status=FAILED + error_code。
        """
        provider, prepare_diags = self._prepare_generation(request)
        if isinstance(provider, list):
            return None, provider

        # 5. 创建 Job（§0 第三条：长任务进 JobRegistry，可取消）
        job = self._jobs.submit(
            JobKind.GENERATION,
            label=f"生成 {request.modality.value} ({request.request_id})",
        )

        execute_diags = self._execute_generation(request, provider, job)
        return job, [*prepare_diags, *execute_diags]

    def request_generation_async(
        self, request: GenerationRequest,
    ) -> tuple[JobHandle | None, list[Diagnostic]]:
        """Submit remote generation to the shared worker without blocking UI.

        AUD-13900：prepare 阶段的 WARNING 级诊断（月度成本告警）随返回值
        送达调用方；仅 ERROR 级阻断 job 提交。
        """
        provider, prepare_diags = self._prepare_generation(request)
        if isinstance(provider, list):
            return None, provider
        job = self._jobs.submit(
            JobKind.GENERATION,
            label=f"生成 {request.modality.value} ({request.request_id})",
        )
        self.worker_pool.submit(
            job,
            lambda handle: self._execute_generation(request, provider, handle),
        )
        return job, list(prepare_diags)

    def resume_generation_async(
        self,
        request: GenerationRequest,
        checkpoint: dict[str, Any],
    ) -> tuple[JobHandle | None, list[Diagnostic]]:
        """Explicitly resume a provider job from a persisted checkpoint.

        Resumption is never automatic after restart: the host must present the
        checkpoint and re-submit the same consented request.  The provider id,
        request id and remote job id are checked before a poll-only worker is
        created, preventing a checkpoint from being used to cross scopes.
        """
        provider, diagnostics = self._prepare_generation(request)
        if isinstance(provider, list):
            return None, provider
        if not isinstance(checkpoint, dict):
            return None, [make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.SCHEMA_INVALID,
                details={"reason": "generation checkpoint must be an object"},
            )]
        if (
            str(checkpoint.get("provider_id", "")) != str(provider.provider_id)
            or str(checkpoint.get("request_id", "")) != str(request.request_id)
            or not str(checkpoint.get("remote_job_id", ""))
        ):
            return None, [make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.PERMISSION_DENIED,
                reason="generation checkpoint does not match request/provider",
            )]
        job = self._jobs.submit(
            JobKind.GENERATION,
            label=f"恢复生成 {request.modality.value} ({request.request_id})",
        )
        remote_job_id = str(checkpoint["remote_job_id"])
        self.worker_pool.submit(
            job,
            lambda handle: self._execute_generation(
                request,
                provider,
                handle,
                resume_remote_job_id=remote_job_id,
            ),
        )
        # AUD-13900：prepare 的 WARNING 级诊断（月度成本告警）随返回送达。
        return job, [d for d in diagnostics if not d.is_error]

    def close(self) -> None:
        """Stop owned workers during host teardown; completed jobs remain queryable.

        AUD-13904：worker 池以 ``cancel_futures=True`` shutdown 后，未启动的
        future 永不执行、其 JobHandle 无人调用 complete/fail，会永久停留
        QUEUED（``is_active`` 恒真、metrics 虚高）。关闭时显式将所有非终态
        Job 判为 FAILED（``JOB_SHUTDOWN``），不留僵尸状态。
        """
        with self._pool_lock:
            workers, self._workers = self._workers, None
        if workers is not None:
            workers.shutdown(wait=False)
        # AUD-13902：判死循环与 worker 的"终态判定 + 入箱"临界区互斥——
        # worker 在锁内 complete 的 Job 不会出现在 list_active()，也不会
        # 被 fail() 改写回 FAILED。
        with self._finalize_lock:
            for job in self._jobs.list_active():
                job.fail(
                    "JOB_SHUTDOWN",
                    "generation service closed before the job finished",
                )

    def _prepare_generation(
        self, request: GenerationRequest,
    ) -> tuple[GenerationProvider | list[Diagnostic], list[Diagnostic]]:
        """gate（同意/成本）→ (provider 或阻断诊断, 全部诊断)。

        AUD-13900：``_check_cost`` 现在会产出 **WARNING 级**的月度成本告警
        诊断（提醒非阻断，§10.2）。因此本方法的早退判定从"有诊断即阻断"
        收紧为"有 **ERROR 级** 诊断才阻断"——warning 随第二个返回值流向
        调用方（job 照常提交），告警链路不再被丢弃。
        """
        consent_diags = self._check_consent(request)
        if consent_diags:
            return consent_diags, consent_diags
        provider = self._resolve_provider(request)
        if isinstance(provider, list):
            return provider, provider
        cost_diags = self._check_cost(request, provider)
        blocking = [d for d in cost_diags if d.severity == DiagnosticSeverity.ERROR]
        if blocking:
            return blocking, cost_diags
        return provider, cost_diags

    def _execute_generation(
        self,
        request: GenerationRequest,
        provider: GenerationProvider,
        job: JobHandle,
        *,
        resume_remote_job_id: str | None = None,
    ) -> list[Diagnostic]:
        """Run provider and materialize only a successful, non-cancelled result."""
        if job.status == JobStatus.QUEUED:
            job.mark_running()
        try:
            generate = provider.generate
            parameters = inspect.signature(generate).parameters
            if "progress" in parameters:
                kwargs: dict[str, Any] = {
                    "progress": lambda fraction, message: job.update_progress(fraction, message),
                }
                if "checkpoint" in parameters:
                    kwargs["checkpoint"] = job.set_checkpoint
                if "resume_remote_job_id" in parameters and resume_remote_job_id:
                    kwargs["resume_remote_job_id"] = resume_remote_job_id
                if "is_cancelled" in parameters:
                    kwargs["is_cancelled"] = lambda: job.status == JobStatus.CANCELLING
                result = generate(request, **kwargs)
            else:
                result = generate(request)
        except Exception as exc:  # noqa: BLE001 — 生成失败必须可见，不吞
            job.fail("GENERATION_ERROR", f"{type(exc).__name__}: {exc}")
            return [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                op_kind="generation",
                details={"reason": f"provider raised: {exc}"},
            )]

        if job.status.is_terminal:
            # close() 已将该 Job 判为 FAILED（JOB_SHUTDOWN）等终态：不再改写
            # 状态，也不把候选写入素材箱（§11.1 红线：取消/失败不污染工程）。
            return []

        if job.status == JobStatus.CANCELLING:
            # AUD-13902：取消收尾在 ``_finalize_lock`` 内完成（与 ``cancel``/
            # ``close`` 互斥）；锁内复查防止取消请求恰好在判定后到达。
            with self._finalize_lock:
                if job.status == JobStatus.CANCELLING:
                    job.complete({})
            return []

        if not result.succeeded:
            job.fail(
                result.error_code or "GENERATION_FAILED",
                result.error_message or "provider returned no candidates",
            )
            return [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                op_kind="generation",
                details={"reason": result.error_message or result.error_code},
            )]

        blocked = next((candidate for candidate in result.candidates if candidate.safety.blocked), None)
        if blocked is not None:
            job.fail("CONTENT_SAFETY_BLOCKED", "provider content safety check failed")
            return [make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.QUALITY_GATE_FAILED,
                detail="provider content safety check failed",
            )]

        if (
            request.budget.max_cost_units is not None
            and result.actual_cost_units > request.budget.max_cost_units
        ):
            job.fail("GENERATION_BUDGET_EXCEEDED", "provider reported cost above request budget")
            return [make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.QUOTA_EXCEEDED,
                detail=(
                    f"actual cost {result.actual_cost_units} exceeds "
                    f"budget {request.budget.max_cost_units}"
                ),
            )]

        result_issues = self._validate_provider_result(request, provider, result)
        if result_issues:
            job.fail("PROVIDER_RESULT_INVALID", "provider returned invalid generation metadata")
            return [make_diagnostic(
                DiagnosticSeverity.ERROR,
                TaepDiagnosticCode.SCHEMA_INVALID,
                details={"reason": "provider_result_invalid", "issues": result_issues},
            )]

        if self._cost_ledger is not None and result.actual_cost_units > 0:
            self._cost_ledger.add(
                provider_id=request.consent.provider_id or provider.provider_id,
                modality=request.modality,
                cost_units=result.actual_cost_units,
            )

        # AUD-13902：终态/取消判定与候选入箱 + job complete 在同一临界区
        # 内原子完成（``cancel``/``close`` 同持 ``_finalize_lock``）。取消要么
        # 发生在入箱前（此处判 CANCELLING，候选丢弃），要么发生在 complete
        # 之后（Job 已终态，取消请求为 no-op）——不再存在"已取消却入箱"
        # 的窗口（§11.1 红线：取消/失败不污染工程）。
        with self._finalize_lock:
            if job.status.is_terminal:
                # close() 已将该 Job 判为 FAILED（JOB_SHUTDOWN）等终态：
                # 不改写状态，也不把候选写入素材箱。
                return []

            if job.status == JobStatus.CANCELLING:
                job.complete({})
                return []

            self._bin.add_candidates(result.candidates)
            job.complete(result_manifest={
                "request_id": request.request_id,
                "candidate_ids": [c.asset_id for c in result.candidates],
                "actual_cost_units": result.actual_cost_units,
            })
        return []

    @staticmethod
    def _validate_provider_result(
        request: GenerationRequest,
        provider: GenerationProvider,
        result: GenerationResult,
    ) -> list[str]:
        """Validate provider-owned metadata before candidates enter the bin."""
        issues: list[str] = []
        if result.request_id != request.request_id:
            issues.append("result_request_id_mismatch")
        if result.actual_cost_units < 0:
            issues.append("negative_actual_cost")
        if len(result.candidates) > request.budget.max_candidates:
            issues.append("candidate_count_exceeds_budget")
        seen_ids: set[str] = set()
        for candidate in result.candidates:
            if candidate.request_id != request.request_id:
                issues.append("candidate_request_id_mismatch")
            if candidate.asset_id in seen_ids:
                issues.append("duplicate_candidate_id")
            seen_ids.add(candidate.asset_id)
            provenance = candidate.provenance
            if provenance.provider_id != provider.provider_id:
                issues.append("provenance_provider_mismatch")
            if not provenance.model_id:
                issues.append("missing_model_id")
            if not provenance.model_version:
                issues.append("missing_model_version")
            if not provenance.input_prompt_hash:
                issues.append("missing_prompt_hash")
        return list(dict.fromkeys(issues))[:8]

    # ---------------- 取消 ---------------- #

    def cancel(self, job_id: str) -> bool:
        """请求取消生成 Job（§0 第三条：可取消）。

        返回值语义（AUD-13905，与 ``JobRegistry.cancel`` 实际行为一致）：
        ``True`` 表示 job 存在且取消请求已下发；``False`` 仅表示 job_id 未知。
        **返回值不代表取消一定生效**——对已终态（SUCCEEDED/FAILED/CANCELLED）
        的 job，``request_cancel`` 是 no-op，已交付结果保留，但返回值仍为
        ``True``；调用方要判断"是否真的中断了进行中的生成"，应随后读取
        job 状态而非依赖本返回值。

        AUD-13902：取消与 worker 的"终态判定 + 候选入箱"临界区
        （``_finalize_lock``）互斥——取消要么发生在入箱前（候选被丢弃、
        Job 收尾为 CANCELLED），要么发生在 complete 之后（Job 已终态，
        ``request_cancel`` 为 no-op，已交付结果保留），不再存在
        "已取消却多了候选"的 TOCTOU 窗口。
        """
        with self._finalize_lock:
            return self._jobs.cancel(job_id)

    # ---------------- 内部：gate ---------------- #

    def _resolve_provider(
        self, request: GenerationRequest,
    ) -> "GenerationProvider | list[Diagnostic]":
        """解析 provider（§3.1 "adapter 可换" + §2.3 "防偷换 provider"）。

        consent.provider_id 非空时，必须能解析为支持 ``request.modality`` 的 provider；
        否则返回 PERMISSION_DENIED（防偷换：用户确认 A 后 registry 换成 B 仍走 B）。
        consent.provider_id 为空时退化为 ``preferred_for(modality)``（兼容旧调用方）。
        """
        wanted = request.consent.provider_id
        if wanted:
            candidate = self._registry.get(wanted)
            if candidate is None or request.modality not in tuple(candidate.supported_modalities):
                return [make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                    reason=(
                        f"provider 偷换：consent 确认 provider {wanted!r} 但它不支持 "
                        f"modality {request.modality.value}（§2.3：每次确认 provider）。"
                    ),
                )]
            return candidate
        # 退化：preferred_for（无 consent.provider_id 约束）
        provider = self._registry.preferred_for(request.modality)
        if provider is None:
            return [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                op_kind=f"generation:{request.modality.value}",
                details={"reason": f"no provider registered for {request.modality.value}"},
            )]
        return provider

    def _check_consent(self, request: GenerationRequest) -> list[Diagnostic]:
        """同意 gate（§0 第一条 / §2.3）。未完整确认 → 拒绝。"""
        c = request.consent
        if not c.is_complete:
            missing: list[str] = []
            if not c.confirmed:
                missing.append("confirmed")
            if not c.cost_acknowledged:
                missing.append("cost_acknowledged")
            if not c.upload_acknowledged:
                missing.append("upload_acknowledged")
            if not c.copyright_acknowledged:
                missing.append("copyright_acknowledged")
            return [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                reason=(
                    f"generation requires complete consent (missing: {missing}). "
                    "未经用户确认不得生成付费素材（§0 红线第一条）。"
                ),
            )]
        # provider_id 一致性由 _resolve_provider 在查找时校验（§2.3："每次确认
        # provider，防偷换"）：consent.provider_id 非空时必须解析为支持该 modality 的
        # provider，否则拒绝。此处只校验同意完整性，不重复。
        return []

    def _check_cost(
        self, request: GenerationRequest, provider: GenerationProvider,
    ) -> list[Diagnostic]:
        """成本上限 gate（§10.2 / §11.2 M11 step 1）。

        v1：``budget.max_cost_units`` 是请求级硬上限。若已有 cost_ledger 且本月累计超
        ``monthly_cost_alert``，发 warning（但不硬拒——alert 是提醒非阻断）。
        """
        budget = request.budget
        # 请求级硬上限：负值或零值（显式设 0）→ 拒绝
        if budget.max_cost_units is not None and budget.max_cost_units <= 0:
            return [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.QUOTA_EXCEEDED,
                detail=f"budget.max_cost_units must be > 0, got {budget.max_cost_units}",
            )]
        # cost_ledger 累计检查（AUD-13900：warning 级，不硬拒）
        if self._cost_ledger is not None:
            alert = self._cost_ledger.check_alert(provider.provider_id)
            if alert is not None:
                # alert 是提醒而非阻断：产出 WARNING 诊断随调用链返回
                # （_prepare_generation 只对 ERROR 早退），调用方/UI 可据此
                # 提示用户月度成本已超阈值（§10.2），不再计算后被丢弃。
                return [make_diagnostic(
                    DiagnosticSeverity.WARNING,
                    TaepDiagnosticCode.QUOTA_EXCEEDED,
                    detail=alert,
                )]
        return []


# ---------------------------------------------------------------------- #
# CostLedger —— 成本累计（§10.2 "每月/月度 token 和货币限额"）
# ---------------------------------------------------------------------- #


@dataclass
class CostLedger:
    """provider 维度的成本累计账本（§10.2）。

    累计每个 provider 的实际生成费用，供 ``monthly_cost_alert`` 检查。
    v1：in-memory，会话级（工程关闭清除；不持久化，§0 第四条）。

    AUD-13901：``add`` 的读-改-写在默认 2 worker 的 ``JobWorkerPool`` 并发
    完成下会丢失累计（输家覆盖赢家），全部读写（add/total_for/
    grand_total/check_alert/reset）现持内部锁串行化。锁为 RLock——
    ``check_alert`` 可安全复用 ``total_for``。
    """

    monthly_cost_alert: float | None = None  # 月度成本告警阈值（货币单位）
    _totals: dict[str, float] = None  # type: ignore[assignment]  # provider_id → 累计成本
    _lock: threading.RLock = field(  # AUD-13901：并发累计的串行化点
        default_factory=threading.RLock, init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if self._totals is None:
            self._totals = {}

    def add(
        self, *, provider_id: str, modality: GenerationModality, cost_units: float,
    ) -> None:
        """累加一次生成的实际费用（锁内读-改-写，AUD-13901）。"""
        with self._lock:
            self._totals[provider_id] = self._totals.get(provider_id, 0.0) + cost_units

    def total_for(self, provider_id: str) -> float:
        with self._lock:
            return self._totals.get(provider_id, 0.0)

    def grand_total(self) -> float:
        with self._lock:
            return sum(self._totals.values())

    def check_alert(self, provider_id: str) -> str | None:
        """检查是否超月度告警阈值。超 → 返回告警消息（None = 未超）。"""
        if self.monthly_cost_alert is None:
            return None
        total = self.total_for(provider_id)
        if total >= self.monthly_cost_alert:
            return (
                f"provider {provider_id} 累计成本 {total:.2f} 已达月度告警阈值 "
                f"{self.monthly_cost_alert:.2f}"
            )
        return None

    def reset(self) -> None:
        """清零（月初重置 / 测试用）。"""
        with self._lock:
            self._totals.clear()


__all__ = ["GenerationService", "CostLedger"]
