"""TAEP Analysis Service（Phase 5）。

编排可插拔 ``AnalysisProvider``，建立以 fingerprint 为 key 的 ``AssetAnalysis``
存储，并把 provider 输出转换为 ``SemanticSegment`` 后进入 ``SemanticIndex``
（TAEP §12 P5）。

职责：

- 注册 provider（scene_cut_local 默认可用；transcript/tag/audio 云 provider 按需）。
- 对某 ``asset_id`` 运行指定 provider，聚合结果到 ``AssetAnalysis``。
- fingerprint 不匹配时使分析失效（素材被替换/修改后过期）。
- async job、取消、缓存失效与隐私策略（云上传需单独确认，P5 接 job API）。
- 把 transcript 原始/修订文本、speaker、置信度、语言、来源与用户修订分层。
- 提供 ``search_assets``、``get_asset_analysis`` 的分页与范围查询。

不直接写 timeline；分析结果只能提出剪辑候选（TAEP §5.4）。
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from src.features.edit.ai_editing.analysis_scheduler import (
    AnalysisJobScheduler,
    ResourceLane,
)

if TYPE_CHECKING:
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AnalysisProvider,
    AssetAnalysis,
    AssetFingerprint,
    analysis_to_segments,
)
from src.features.edit.ai_editing.analysis_request import (
    AnalysisRecipe,
    AnalysisRequest,
    RequestState,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind, SemanticSegment
from src.features.edit.ai_editing.jobs import (
    JobHandle,
    JobKind,
    JobRegistry,
    JobStatus,
)

logger = logging.getLogger(__name__)


class SceneCutProvider:
    """把现有 ``scene_cut`` 模块适配为 AnalysisProvider（复用既有 detector）。

    TAEP §5.4：scene_cuts 是 AssetAnalysis 最成熟的子模块，直接作为 provider。
    本 provider 不依赖云；本地运行。
    """

    provider_id = "scene_cut_local"
    supported_kinds = (SegmentKind.SHOT,)

    def analyze(
        self, asset_id: str, fingerprint: AssetFingerprint, *, fps: str,
        progress: Any | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        # 实际调用 SceneCutController/detector 由宿主注入（避免这里 import Qt）。
        # P5 提供“从已有 SceneCutAnalysis 转换”的入口（见 from_scene_cut_analysis）。
        # 默认返回空分析（无既有结果时）。
        analysis = AssetAnalysis(
            asset_id=asset_id, fingerprint=fingerprint, fps=fps,
            scene_cuts=(),
            provenance=AnalysisProvenance(provider=self.provider_id, model="builtin"),
        )
        return analysis, ()

    @staticmethod
    def from_scene_cut_analysis(
        scene_analysis: Any, asset_id: str, fingerprint: AssetFingerprint, fps: str,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        """把已有的 ``SceneCutAnalysis`` 转换为 AssetAnalysis + segments。

        复用既有 scene_cut 输出，避免重复分析（TAEP §5.4 fingerprint 缓存）。
        """
        cuts: list[dict[str, Any]] = []
        candidates = getattr(scene_analysis, "candidates", ()) or ()
        for cand in candidates:
            # 只取 accepted/pending 的 cut；rejected 跳过
            decision = getattr(cand, "decision", None)
            decision_val = decision.value if hasattr(decision, "value") else str(decision)
            if decision_val == "rejected":
                continue
            frame = int(getattr(cand, "source_frame", 0) or 0)
            cuts.append({
                "frame": frame,
                "confidence": float(getattr(cand, "confidence", 1.0) or 1.0),
                "kind": getattr(getattr(cand, "kind", None), "value", "cut"),
            })
        # 排序 + 去重
        cuts.sort(key=lambda c: c["frame"])
        analysis = AssetAnalysis(
            asset_id=asset_id, fingerprint=fingerprint, fps=fps,
            scene_cuts=tuple(cuts),
            provenance=AnalysisProvenance(
                provider="scene_cut_local",
                model=str(getattr(scene_analysis, "detector_version", "builtin")),
                created_at=str(getattr(scene_analysis, "created_at", "")),
            ),
        )
        segments = tuple(analysis_to_segments(analysis))
        return analysis, segments


# ---------------------------------------------------------------------- #
# AnalysisService
# ---------------------------------------------------------------------- #


@dataclass
class _AssetRecord:
    """单个 asset 的分析聚合 + segment 缓存。"""

    analysis: AssetAnalysis
    segments: tuple[SemanticSegment, ...] = ()


# ---------------------------------------------------------------------- #
# M4: recipe → provider_id 解析（§17.4 配方表）
# ---------------------------------------------------------------------- #
#
# 配方（AnalysisRecipe）= 固定组合的 provider 能力包，而非"完整/不完整"二元按钮。用户
# 可勾掉敏感/昂贵模块（人脸/OCR/云端）。此处给出每个 recipe 默认调用的 provider_id
# 顺序；``AnalysisService.providers_for_request`` 会为 UI 发现过滤未注册 provider（如
# 无 WhisperX 时的 ``transcript_local``），但执行前的 preflight 会拒绝缺少任何必需
# provider 的已批准 recipe，不能把缩减后的结果标记为分析完成。
#
# M4 首发：EDITORIAL_LOCAL = scene_cut + transcript（口播/访谈/粗剪）。
# OCR/视觉（VISUAL_SEMANTIC_LOCAL）留 M8；真实云 ASR（CLOUD_ENHANCED）留 M10。

RECIPE_PROVIDER_IDS: dict[AnalysisRecipe, tuple[str, ...]] = {
    AnalysisRecipe.FAST_LOCAL: ("scene_cut_local",),
    AnalysisRecipe.EDITORIAL_LOCAL: ("scene_cut_local", "transcript_local"),
    AnalysisRecipe.VISUAL_SEMANTIC_LOCAL: (
        "scene_cut_local", "transcript_local", "ocr_local", "visual_semantic_local",
    ),
    AnalysisRecipe.CLOUD_ENHANCED: ("scene_cut_local", "transcript_local"),         # 云 ASR 留 M10
}
"""每个 recipe 默认调用的 provider_id 列表（§17.4）。顺序即执行顺序。"""


def resolve_providers(recipe: AnalysisRecipe) -> tuple[str, ...]:
    """某 recipe 默认的 provider_id 列表（模块级便捷入口）。"""
    return RECIPE_PROVIDER_IDS.get(recipe, ())


def _notify_progress(
    callback: Callable[[float | None, str | None], Any] | None,
    fraction: float | None,
    message: str | None,
) -> None:
    """向宿主转发进度；UI 回调异常不得破坏分析任务。"""
    if callback is None:
        return
    try:
        callback(fraction, message)
    except Exception:  # noqa: BLE001
        # Progress is observability only. A broken status-bar callback must not
        # turn a successful transcription into a failed analysis job.
        pass


def _accepts_provider_options(analyze: Callable[..., Any]) -> bool:
    """Check whether a provider's ``analyze`` method accepts request options.

    A number of legacy providers intentionally keep the original protocol.
    Signature inspection lets the service add per-request settings without
    catching and hiding a real ``TypeError`` raised inside a provider.
    """
    try:
        parameters = inspect.signature(analyze).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "options"
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


class AnalysisService:
    """provider 编排 + 分析存储 + 失效判定（TAEP §12 P5）。"""

    def __init__(self, job_registry: JobRegistry | None = None) -> None:
        self._providers: dict[str, AnalysisProvider] = {}
        self._records: dict[str, _AssetRecord] = {}
        # Rebuildable visual vectors are kept outside project persistence and
        # attached only when a SemanticIndex is requested.
        self._embeddings: dict[str, tuple[float, ...]] = {}
        self._jobs = job_registry or JobRegistry()
        # M2: 类型化请求合同仓库（propose → confirm → execute 三步流）
        self._requests: dict[str, AnalysisRequest] = {}
        # AUD-12904：worker 线程（request_analysis → ingest / 请求状态收尾）与
        # UI 线程（search_assets / known_asset_ids / export_all / list_requests）
        # 并发访问以上共享字典。RLock 保护全部读写；枚举先在锁内快照再锁外遍历，
        # 与 AnalysisJobScheduler 的线程边界防护对称。
        self._state_lock = threading.RLock()
        # 注册内置 provider
        self.register(SceneCutProvider())

    @property
    def jobs(self) -> JobRegistry:
        return self._jobs

    # ---------------- provider 注册 ---------------- #

    def register(self, provider: AnalysisProvider) -> None:
        with self._state_lock:
            self._providers[provider.provider_id] = provider  # type: ignore[attr-defined]

    def unregister(self, provider_id: str) -> bool:
        """Remove a provider from future runs without deleting cached evidence."""

        with self._state_lock:
            return self._providers.pop(str(provider_id), None) is not None

    def available_providers(self) -> list[str]:
        with self._state_lock:
            return list(self._providers.keys())

    # ---------------- M4: recipe → provider 解析（§17.4）---------------- #

    def providers_for_request(
        self, request: AnalysisRequest,
    ) -> tuple[AnalysisProvider, ...]:
        """返回 request 实际要跑的 provider 列表（M4）。

        优先用 request 显式指定的 ``provider_ids``；否则按 ``recipe_id`` 解析默认组合
        （见 :data:`RECIPE_PROVIDER_IDS`）。这个发现接口会过滤未注册 provider；真正
        执行时 :meth:`missing_provider_ids` 的 preflight 会拒绝缺少任一必需 provider
        的 recipe，因而不能把缩减结果伪装为完成。
        """
        ids = request.provider_ids or RECIPE_PROVIDER_IDS.get(request.recipe_id, ())
        return tuple(self._providers[i] for i in ids if i in self._providers)

    def provider_ids_for_recipe(self, recipe: AnalysisRecipe) -> tuple[str, ...]:
        """某 recipe 默认对应的 provider_id 列表（含未注册的；供 UI/诊断展示）。"""
        return RECIPE_PROVIDER_IDS.get(recipe, ())

    def missing_provider_ids(self, request: AnalysisRequest) -> tuple[str, ...]:
        """Return required provider ids that are not registered for ``request``.

        Discovery may filter unavailable providers for UI presentation, but an
        approved recipe must never silently run a smaller recipe and be marked
        complete.  Callers use this preflight before scheduling.
        """
        ids = request.provider_ids or RECIPE_PROVIDER_IDS.get(request.recipe_id, ())
        return tuple(provider_id for provider_id in ids if provider_id not in self._providers)

    # ---------------- M2: AnalysisRequest 三步流（§17.3）---------------- #
    # 旧 ``request_analysis`` 是"直接执行"路径；新代码应走 propose → confirm →
    # execute，使"未确认不分析"这条红线落到代码。Assistant / Skill 只能 propose；
    # 用户从媒体池/确认卡 confirm 后 scheduler 才能 execute。

    def propose_analysis(self, request: AnalysisRequest) -> AnalysisRequest:
        """登记一份未确认的分析提议（``user_confirmed_at`` 为空）。

        Assistant / Skill / 媒体池 draft 都走这里：只描述"想分析什么"，不执行。
        返回的 request ``is_confirmed`` 为 False；``execute_analysis`` 会拒绝它。
        """
        if request.is_confirmed:
            # 防御：调用方不应直接构造已确认的 request；要求经 confirm_analysis。
            raise ValueError(
                "propose_analysis expects an unconfirmed request; "
                "use confirm_analysis to confirm a proposal"
            )
        with self._state_lock:
            self._requests[request.request_id] = request
        return request

    def confirm_analysis(self, request_id: str, *, confirmed_at: float | None = None) -> AnalysisRequest:
        """用户确认一份提议 → 置位 ``user_confirmed_at``，``state=CONFIRMED``。

        Args:
            request_id: ``propose_analysis`` 返回的 request_id。
            confirmed_at: 确认时间戳；None 用当前时间。
        """
        import time as _time

        with self._state_lock:
            req = self._requests.get(request_id)
            if req is None:
                raise KeyError(f"unknown AnalysisRequest {request_id!r}")
            if req.state != RequestState.PROPOSED:
                raise ValueError(
                    f"request {request_id!r} is in state {req.state.value}, cannot confirm"
                )
            confirmed = req.with_confirmation(
                confirmed_at if confirmed_at is not None else _time.time()
            )
            self._requests[request_id] = confirmed
        return confirmed

    def execute_analysis(
        self,
        request_id: str,
        *,
        fps: str = "24/1",
        scheduler: "AnalysisJobScheduler | None" = None,
        on_asset_done: "Callable[[str, bool], Any] | None" = None,
        on_request_done: "Callable[[str, bool], Any] | None" = None,
    ) -> tuple[JobHandle | None, str]:
        """执行一份**已确认**的分析请求。

        核心不变量（§17.3）：``user_confirmed_at`` 为空（未 confirm）的 request
        **必须拒绝**，绝不执行。这把"未经用户确认不得分析整批素材"落到代码层。

        Args:
            scheduler: M3 统一调度器。提供时，逐 asset 的分析 work 提交到
                ``ResourceLane.ANALYSIS_LOCAL``（或 ``_CLOUD``），由 scheduler 按优先级/
                并发出队（播放优先于分析）；execute 不阻塞，返回汇总 job 句柄。
                None 时（测试/无 app）走同步路径（向后兼容 M2 测试）。
            on_asset_done: 单个 asset 分析完成的回调 (asset_id, success)；scheduler
                出队执行后调用，供 controller 更新 per-asset badge 状态。
            on_request_done: scheduler 异步完成全部 asset 后的回调
                (request_id, success)。
        """
        with self._state_lock:
            req = self._requests.get(request_id)
        if req is None:
            return None, f"unknown AnalysisRequest {request_id!r}"
        if not req.is_confirmed:
            return None, (
                f"AnalysisRequest {request_id!r} is not confirmed "
                "(user_confirmed_at is None); scheduler refuses to execute"
            )
        if req.state != RequestState.CONFIRMED:
            return None, (
                f"request {request_id!r} is in state {req.state.value}, "
                f"expected {RequestState.CONFIRMED.value}"
            )

        # AUD-12906：预算 preflight——素材数超上限直接拒绝执行（§10.2 滥用
        # 保护："分析全部素材"不能扩到超大工程），request 迁移 FAILED 并明示原因。
        if req.budget.max_assets is not None and len(req.asset_ids) > req.budget.max_assets:
            with self._state_lock:
                self._requests[request_id] = req.with_state(RequestState.FAILED)
            return None, (
                f"AnalysisBudget exceeded: {len(req.asset_ids)} assets > "
                f"max_assets={req.budget.max_assets}; split the request or raise the budget"
            )

        # 标记 RUNNING；云请求的执行需调用方已获用户对上传范围/费用/保留的单独确认
        with self._state_lock:
            self._requests[request_id] = req.with_state(RequestState.RUNNING)

        missing_provider_ids = self.missing_provider_ids(req)
        if missing_provider_ids:
            with self._state_lock:
                self._requests[request_id] = req.with_state(RequestState.FAILED)
            return None, (
                "required analysis provider(s) unavailable for "
                f"{req.recipe_id.value!r}: {', '.join(missing_provider_ids)}"
            )

        # M4：按 recipe 解析 provider 列表（多 provider 编排，§17.4）。缺失项已在
        # 上面的 preflight 拒绝，这里只取得对应 provider 对象。
        providers = self.providers_for_request(req)
        if not providers:
            # recipe 解析后无可用 provider（既无显式 provider_ids，recipe 默认 provider
            # 也未注册）→ 报错而非静默跑空（§11.1 M4：模型/能力缺失必须明示）。
            with self._state_lock:
                self._requests[request_id] = req.with_state(RequestState.FAILED)
            return None, (
                f"no registered provider for recipe {req.recipe_id.value!r} "
                f"(registered: {sorted(self._providers.keys())})"
            )
        provider_ids = tuple(p.provider_id for p in providers)  # type: ignore[attr-defined]
        lane = ResourceLane.ANALYSIS_CLOUD if req.is_cloud else ResourceLane.ANALYSIS_LOCAL

        if scheduler is not None:
            # M3 异步路径：逐 asset 提交到 scheduler（按优先级/并发出队）
            return self._execute_via_scheduler(
                request_id, req, scheduler=scheduler, lane=lane, fps=fps,
                provider_ids=provider_ids, on_asset_done=on_asset_done,
                on_request_done=on_request_done,
            )

        # 同步路径（向后兼容 M2；测试/无 app 场景）
        errors: list[str] = []
        completed = 0
        # AUD-12906：执行期预算检查点（时长截止 + 成本累计）。
        started_at = time.monotonic()
        spent_cost_units = 0.0
        for asset_id in req.asset_ids:
            violation = self._budget_violation(req, started_at, spent_cost_units)
            if violation is not None:
                errors.append(f"{asset_id}: skipped ({violation})")
                continue
            fingerprint = AssetFingerprint()  # 真实指纹由 M3 导入期计算并传入
            job, err = self.request_analysis(
                asset_id, fingerprint, fps=fps, provider_ids=provider_ids,
                require_user_confirmation=req.is_cloud,
            )
            if err:
                errors.append(f"{asset_id}: {err}")
                if on_asset_done is not None:
                    on_asset_done(asset_id, False)
            elif job is not None and job.status.value == "succeeded":
                completed += 1
                spent_cost_units += _job_cost_units(job)
                if on_asset_done is not None:
                    on_asset_done(asset_id, True)
            else:
                detail = getattr(job, "error", "") if job is not None else "unknown"
                errors.append(f"{asset_id}: {detail or 'analysis job failed'}")
                if on_asset_done is not None:
                    on_asset_done(asset_id, False)

        new_state = RequestState.COMPLETED if not errors else RequestState.FAILED
        with self._state_lock:
            self._requests[request_id] = self._requests[request_id].with_state(new_state)
        if errors:
            return None, "; ".join(errors)
        # 返回一个汇总 job 句柄（便于 UI 显示完成）；逐 asset 的 job 已在 records 里
        summary_job = self._jobs.submit(
            JobKind.ANALYSIS, label=f"Analysis {request_id} ({completed} assets)"
        )
        summary_job.complete({"assets": completed, "recipe": req.recipe_id.value})
        return summary_job, ""

    def _execute_via_scheduler(
        self,
        request_id: str,
        req: AnalysisRequest,
        *,
        scheduler: "AnalysisJobScheduler",
        lane: "ResourceLane",
        fps: str,
        provider_ids: tuple[str, ...],
        on_asset_done: "Callable[[str, bool], Any] | None",
        on_request_done: "Callable[[str, bool], Any] | None",
    ) -> tuple[JobHandle | None, str]:
        """经 AnalysisJobScheduler 异步执行（M3）：逐 asset 提交到 lane，按优先级出队。

        M4：``provider_ids`` 是按 recipe 解析的 provider 列表（多 provider 顺序跑）。
        work 回调内同步调 request_analysis（仍由 provider 同步跑）；真实 worker 线程接入
        （SceneCutWorker / WhisperX worker 等）在 controller 层替换 work。scheduler 在此
        负责并发上限 + playback 优先级 + 统一取消。

        R2：每个 asset job 注册 ``on_cancel`` 回调，scheduler 在 pending 取消 /
        ``cancel_all`` 时回调本服务，把对应 asset 计入 errors（或显式标记取消），
        触发 ``_finalize_scheduled_request``——否则当某些 asset job 在 pending 阶段
        被取消（work 未跑）时，``done`` 永远到不了 ``total``，request 永久卡 RUNNING。

        AUD-12903：work 闭包遵守调度器契约——结束时把 asset job 驱动到终态并调
        ``scheduler.notify_finished``（释放 lane 容量、触发积压出队）；汇总
        ``summary_job`` 在 request 收尾时终结，不再永久停留 QUEUED。

        AUD-12905：完成/失败/取消计数与 finalize 判定在 ``counters_lock`` 内原子
        进行——并发 pool 线程的读-改-写不再丢失更新，也不会双重收尾。

        R06（媒体池复查）：取消是独立结局类（成功/失败/取消三分）——
        外层 asset job 已请求取消时，work 在 **provider 执行前**就拒绝
        启动（provider 不支持中断也一样不 ingest/不发布）；全部为取消
        的 request 收尾为 CANCELLED（不再与失败混淆）。
        """
        completed = {"count": 0}
        errors: list[str] = []
        cancelled = {"count": 0}
        total = len(req.asset_ids)
        counters_lock = threading.Lock()
        finalized = {"done": False}
        # AUD-12906：执行期预算检查点（时长截止 + 成本累计）。
        started_at = time.monotonic()
        spent_cost_units = {"units": 0.0}

        # 汇总句柄（异步；UI 据此显示"分析中"）。先创建再提交 asset job，
        # 收尾时由 _finalize_scheduled_request 终结（成功 complete / 失败 fail）。
        summary_job = self._jobs.submit(
            JobKind.ANALYSIS, label=f"Analysis {request_id} ({total} assets, scheduled)"
        )

        def _finalize_if_done() -> None:
            """全部 asset 完成/失败/取消 → 收尾 request 状态（至多一次）。"""
            with counters_lock:
                if finalized["done"]:
                    return
                done = completed["count"] + len(errors) + cancelled["count"]
                if done < total:
                    return
                finalized["done"] = True
                error_snapshot = list(errors)
                completed_snapshot = completed["count"]
                cancelled_snapshot = cancelled["count"]
            self._finalize_scheduled_request(
                request_id, error_snapshot, completed_snapshot,
                cancelled=cancelled_snapshot,
                summary_job=summary_job, recipe=req.recipe_id,
                on_request_done=on_request_done,
            )

        def _record_outcome(asset_id: str, success: bool, detail: str) -> None:
            with counters_lock:
                if success:
                    completed["count"] += 1
                else:
                    errors.append(f"{asset_id}: {detail or 'analysis job failed'}")

        def _settle_asset_job(
            asset_id: str, asset_job: JobHandle, success: bool, detail: str,
            *, job_cancelled: bool = False,
        ) -> None:
            """AUD-12903：work 结束后终结 asset_job 并释放 lane 容量。

            R06：取消结局走 complete()（CANCELLING → CANCELLED），与
            失败（FAILED）区分。
            """
            if asset_job.status.is_active:
                if success or job_cancelled:
                    asset_job.complete({"asset_id": asset_id,
                                        "cancelled": job_cancelled})
                else:
                    asset_job.fail("ANALYSIS_FAILED", detail or "analysis job failed")
                # CANCELLING 状态下 complete() 会转 CANCELLED（协作取消语义）。
            scheduler.notify_finished(asset_job.job_id)

        def _make_work(asset_id: str, asset_job: JobHandle) -> "Callable[[], Any]":
            def _work() -> None:
                success = False
                detail = ""
                job_cancelled = False
                try:
                    # R06：外层 asset job 已请求取消——取消边界在 **发布前**：
                    # 不启动 provider（即不 ingest，晚到结果不得入库）、
                    # 不计成功，按取消计数。不依赖 provider 支持中断。
                    if asset_job.status in (JobStatus.CANCELLING,
                                            JobStatus.CANCELLED):
                        job_cancelled = True
                        detail = "canceled before provider execution"
                    else:
                        violation = self._budget_violation(
                            req, started_at, spent_cost_units["units"],
                        )
                        if violation is not None:
                            detail = violation
                        else:
                            fingerprint = AssetFingerprint()
                            job, err = self.request_analysis(
                                asset_id, fingerprint, fps=fps,
                                provider_ids=provider_ids,
                                require_user_confirmation=req.is_cloud,
                            )
                            success = (
                                err == "" and job is not None
                                and job.status.value == "succeeded"
                            )
                            if not success:
                                detail = (
                                    err
                                    or (getattr(job, "error_message", "") if job is not None else "")
                                    or "analysis job failed"
                                )
                            else:
                                spent_cost_units["units"] += _job_cost_units(job)
                except Exception as exc:  # noqa: BLE001 — work 内异常不得卡死 request 状态机
                    detail = f"{type(exc).__name__}: {exc}"
                with counters_lock:
                    if job_cancelled:
                        cancelled["count"] += 1
                    elif success:
                        completed["count"] += 1
                    else:
                        errors.append(f"{asset_id}: {detail or 'analysis job failed'}")
                if on_asset_done is not None:
                    try:
                        on_asset_done(asset_id, success)
                    except Exception:  # noqa: BLE001 — 宿主回调异常不阻断收尾
                        logger.warning(
                            "on_asset_done callback failed for %s", asset_id,
                            exc_info=True,
                        )
                _finalize_if_done()
                _settle_asset_job(asset_id, asset_job, success, detail,
                                  job_cancelled=job_cancelled)
            return _work

        def _make_on_cancel(asset_id: str) -> "Callable[[], Any]":
            def _on_cancel() -> None:
                # R06：pending 阶段取消同样是"取消"结局（不与失败混淆）
                with counters_lock:
                    cancelled["count"] += 1
                if on_asset_done is not None:
                    try:
                        on_asset_done(asset_id, False)
                    except Exception:  # noqa: BLE001 — 取消路径回调异常不阻断清理
                        logger.warning(
                            "on_asset_done callback failed during cancel of %s",
                            asset_id, exc_info=True,
                        )
                _finalize_if_done()
            return _on_cancel

        for asset_id in req.asset_ids:
            asset_job = self._jobs.submit(
                JobKind.ANALYSIS, label=f"Analyze {asset_id} ({req.recipe_id.value})"
            )
            scheduler.submit(
                lane, asset_job,
                work=_make_work(asset_id, asset_job),
                on_cancel=_make_on_cancel(asset_id),
                tags=(asset_id,),  # P0-8：per-asset 取消定位标签
            )

        return summary_job, ""

    def _finalize_scheduled_request(
        self, request_id: str, errors: list[str], completed: int,
        *,
        cancelled: int = 0,
        summary_job: JobHandle | None = None,
        recipe: AnalysisRecipe | None = None,
        on_request_done: "Callable[[str, bool], Any] | None" = None,
    ) -> None:
        """scheduler 路径的 request 收尾（状态迁移 + 汇总 job 终结）。

        R06：成功/失败/取消三分——全部 asset 都被取消（无成功无失败）时
        request 收尾为 CANCELLED、汇总 job 终结为 CANCELLED，与单项结局
        一致（此前取消被计成失败，或 provider 已跑完照常发布成功）。
        """
        if errors:
            new_state = RequestState.FAILED
        elif cancelled and not completed:
            new_state = RequestState.CANCELLED
        else:
            new_state = RequestState.COMPLETED
        with self._state_lock:
            cur = self._requests.get(request_id)
            if cur is not None:
                self._requests[request_id] = cur.with_state(new_state)
        if summary_job is not None and summary_job.status.is_active:
            # AUD-12903：汇总 job 必须终结，否则永久 QUEUED 且 is_idle() 永假。
            if errors:
                summary_job.fail("ANALYSIS_FAILED", "; ".join(errors))
            elif new_state == RequestState.CANCELLED:
                # QUEUED/CANCELLING → CANCELLED（complete 在 CANCELLING 态
                # 协作转 CANCELLED；先 request_cancel 兜底其他活跃态）。
                summary_job.request_cancel()
                summary_job.complete({
                    "assets": completed, "cancelled": cancelled,
                    "recipe": recipe.value if recipe is not None else "",
                })
            else:
                summary_job.complete({
                    "assets": completed,
                    "cancelled": cancelled,
                    "recipe": recipe.value if recipe is not None else "",
                })
        if on_request_done is not None:
            try:
                on_request_done(request_id, not errors)
            except Exception:  # noqa: BLE001
                # 状态已经收尾；通知失败不应把一次成功分析改成失败。
                logger.warning(
                    "on_request_done callback failed for %s", request_id,
                    exc_info=True,
                )

    def _budget_violation(
        self, req: AnalysisRequest,
        started_at: float, spent_cost_units: float,
    ) -> str | None:
        """AUD-12906：执行期预算检查点；返回违例描述或 None。

        ``max_duration_seconds`` 以 wall-clock 截止时间衡量——超时后剩余 asset
        不再启动（§17.7 语义：超限转 PARTIAL/FAILED，不无限跑）。``max_cost_units``
        按 provider 写进 job result manifest 的 ``cost_units`` 累计；达到上限后
        不再启动新的（尤其云）分析。
        """
        budget = req.budget
        if (
            budget.max_duration_seconds is not None
            and (time.monotonic() - started_at) > budget.max_duration_seconds
        ):
            return f"budget exceeded: max_duration_seconds={budget.max_duration_seconds}"
        if (
            budget.max_cost_units is not None
            and spent_cost_units >= budget.max_cost_units
        ):
            return f"budget exceeded: max_cost_units={budget.max_cost_units}"
        return None

    def get_request(self, request_id: str) -> AnalysisRequest | None:
        """取一份请求的当前状态（UI 据此展示 PROPOSED/CONFIRMED/RUNNING/...）。"""
        with self._state_lock:
            return self._requests.get(request_id)

    def cancel_request(self, request_id: str) -> bool:
        """取消尚未开始执行的分析提议，避免 Agent UI 留下悬挂 request。"""
        with self._state_lock:
            req = self._requests.get(request_id)
            if req is None or req.state not in (
                RequestState.PROPOSED, RequestState.CONFIRMED,
            ):
                return False
            self._requests[request_id] = req.with_state(RequestState.CANCELLED)
        return True

    def list_requests(self, *, project_id: str | None = None) -> list[AnalysisRequest]:
        """列出请求（可选按工程过滤）。"""
        with self._state_lock:
            reqs = list(self._requests.values())
        if project_id is not None:
            reqs = [r for r in reqs if r.project_id == project_id]
        return reqs

    # ---------------- 写入（合并 provider 结果）---------------- #

    def ingest(
        self, asset_id: str, analysis: AssetAnalysis, segments: tuple[SemanticSegment, ...]
    ) -> None:
        """合并一个 provider 的分析结果到 asset 记录。

        不同 provider 各填 AssetAnalysis 的不同字段；此处做字段级合并
        （scene_cuts/transcript/tags/audio 非空覆盖）。
        """
        with self._state_lock:
            existing = self._records.get(asset_id)
            if existing is None:
                self._records[asset_id] = _AssetRecord(analysis=analysis, segments=segments)
                return
            # 字段级合并：取非空字段
            merged = _merge_analysis(existing.analysis, analysis)
            # 按 segment_id 去重合并（SemanticSegment 含 dict 不可哈希，故用 id 去重）
            seen_ids: set[str] = {s.segment_id for s in existing.segments}
            merged_list = list(existing.segments)
            for seg in segments:
                if seg.segment_id not in seen_ids:
                    seen_ids.add(seg.segment_id)
                    merged_list.append(seg)
            merged_list.sort(key=lambda s: (s.source_range.start, s.kind.value))
            self._records[asset_id] = _AssetRecord(analysis=merged, segments=tuple(merged_list))

    def ingest_embeddings(self, embeddings: Any) -> None:
        """Attach finite vectors only to already-ingested evidence segments."""
        if not embeddings:
            return
        with self._state_lock:
            known_segment_ids = {
                segment.segment_id
                for record in self._records.values()
                for segment in record.segments
            }
        import math
        for segment_id, vector in dict(embeddings).items():
            values = tuple(float(value) for value in vector)
            if segment_id not in known_segment_ids:
                continue
            if not values or not all(math.isfinite(value) for value in values):
                continue
            with self._state_lock:
                self._embeddings[segment_id] = values

    def get_embedding(self, segment_id: str) -> tuple[float, ...] | None:
        """Read one rebuildable vector without exposing mutable cache state."""
        with self._state_lock:
            return self._embeddings.get(segment_id)

    # ---------------- 读取 ---------------- #

    def get_analysis(self, asset_id: str) -> AssetAnalysis | None:
        with self._state_lock:
            rec = self._records.get(asset_id)
            return rec.analysis if rec else None

    def get_segments(self, asset_id: str) -> tuple[SemanticSegment, ...]:
        with self._state_lock:
            rec = self._records.get(asset_id)
            return rec.segments if rec else ()

    def get_status(self, asset_id: str) -> str:
        """供 snapshot_service 的 analysis_provider 用。"""
        with self._state_lock:
            rec = self._records.get(asset_id)
            if rec is None:
                return "unknown"
            return "ready" if rec.segments else "pending"

    def is_valid(self, asset_id: str, current_fingerprint: AssetFingerprint) -> bool:
        """分析是否仍有效（fingerprint 匹配）。"""
        with self._state_lock:
            rec = self._records.get(asset_id)
            if rec is None:
                return False
            return rec.analysis.is_valid_for(current_fingerprint)

    def invalidate(self, asset_id: str) -> None:
        """素材变更后使分析失效（TAEP §5.4）。"""
        with self._state_lock:
            record = self._records.pop(asset_id, None)
            if record is not None:
                for segment in record.segments:
                    self._embeddings.pop(segment.segment_id, None)
            providers_snapshot = tuple(self._providers.values())
        for provider in providers_snapshot:
            invalidate_asset = getattr(provider, "invalidate_asset", None)
            if callable(invalidate_asset):
                try:
                    invalidate_asset(asset_id)
                except Exception:  # noqa: BLE001
                    # Cache cleanup must not prevent the authoritative
                    # in-memory evidence invalidation above.
                    logger.warning(
                        "provider invalidate_asset failed for %s", asset_id,
                        exc_info=True,
                    )

    def known_asset_ids(self) -> tuple[str, ...]:
        """当前持有分析记录的全部 asset_id（无写入、稳定顺序）。

        供宿主桥（``MainWindowAiEditHostBridge``）构建 ``SemanticIndex`` 时枚举
        工程中哪些素材已有分析证据；不暴露内部 ``_records`` 引用。
        """
        with self._state_lock:
            return tuple(sorted(self._records.keys()))

    def export_all(self) -> dict[str, Any] | None:
        """Serialize the evidence cache for the project archive.

        The normalized segments are stored alongside ``AssetAnalysis`` so
        evidence references remain stable across a save/load cycle.  No
        provider is called while exporting and no absolute media path is
        introduced by this layer.
        """
        with self._state_lock:
            if not self._records:
                return None
            records = [
                (asset_id, record)
                for asset_id, record in sorted(self._records.items())
            ]
        return {
            "schema_version": "ai-analysis/1",
            "records": [
                {
                    "asset_id": asset_id,
                    "analysis": record.analysis.to_dict(),
                    "segments": [segment.to_dict() for segment in record.segments],
                }
                for asset_id, record in records
            ],
        }

    def restore_all(self, data: dict[str, Any] | None) -> int:
        """Restore serialized evidence entries; malformed records are skipped.

        Restoring cache data is intentionally non-fatal to project opening. A
        corrupt or old analysis entry is treated as a cache miss, not as a
        reason to block the edit project itself.
        """
        if not isinstance(data, dict):
            return 0
        restored = 0
        with self._state_lock:
            for raw in data.get("records", ()) or ():
                if not isinstance(raw, dict):
                    continue
                try:
                    analysis = AssetAnalysis.from_dict(dict(raw.get("analysis", {})))
                    asset_id = str(raw.get("asset_id") or analysis.asset_id)
                    if not asset_id or analysis.asset_id != asset_id:
                        continue
                    raw_segments = raw.get("segments") or ()
                    segments = tuple(
                        SemanticSegment.from_dict(dict(item)) for item in raw_segments
                    )
                    if not segments:
                        segments = tuple(analysis_to_segments(analysis))
                    self._records[asset_id] = _AssetRecord(
                        analysis=analysis,
                        segments=segments,
                    )
                    restored += 1
                except (KeyError, TypeError, ValueError):
                    continue
        return restored

    def invalidate_stale(
        self, current_fingerprints: dict[str, AssetFingerprint],
    ) -> tuple[str, ...]:
        """Drop entries whose source fingerprint no longer matches."""
        invalidated: list[str] = []
        with self._state_lock:
            candidates = [
                asset_id for asset_id in current_fingerprints
                if asset_id in self._records
            ]
            for asset_id in candidates:
                rec = self._records.get(asset_id)
                fingerprint = current_fingerprints[asset_id]
                if rec is None or rec.analysis.is_valid_for(fingerprint):
                    continue
                self._records.pop(asset_id, None)
                for segment in rec.segments:
                    self._embeddings.pop(segment.segment_id, None)
                invalidated.append(asset_id)
        return tuple(sorted(invalidated))

    def ingest_scene_cut_analysis(
        self, asset_id: str, scene_analysis: Any, *,
        fps: str = "24/1", fingerprint: AssetFingerprint | None = None,
    ) -> int:
        """把既有 ``SceneCutAnalysis``（来自 SceneCutController/detector）灌入证据仓库。

        这是 T1.3 的标准入口：用户从右键菜单触发场景检测后，composition root 把
        ``SceneCutController.detection_completed`` 的结果经本方法转成 SHOT segment
        并 ``ingest`` 进仓库（TAEP §5.4）。``SceneCutProvider.analyze`` 仍返回空
        （避免在 provider 同步路径里阻塞解码）；真实数据走这条被动接收路径。

        Args:
            asset_id: 目标素材。
            scene_analysis: ``SceneCutAnalysis``（含 candidates）。
            fps: 帧率（默认 24/1；可从 MediaAsset.metadata 取）。
            fingerprint: 素材指纹（None 时用空 AssetFingerprint，与 request_analysis 默认一致）。

        Returns:
            灌入的 SHOT segment 数量（0 = 空结果或无 accepted candidate）。
        """
        fp = fingerprint or AssetFingerprint()
        analysis, segments = SceneCutProvider.from_scene_cut_analysis(
            scene_analysis, asset_id, fp, fps,
        )
        n = len(segments)
        if n == 0:
            return 0
        self.ingest(asset_id, analysis, segments)
        return n

    # ---------------- 运行 provider（job 化）---------------- #

    def request_analysis(
        self, asset_id: str, fingerprint: AssetFingerprint, *, fps: str = "24/1",
        provider_id: str | None = None,
        provider_ids: tuple[str, ...] | list[str] | None = None,
        require_user_confirmation: bool = False,
        options: dict[str, Any] | None = None,
        on_progress: Callable[[float | None, str | None], Any] | None = None,
    ) -> tuple[JobHandle | None, str]:
        """提交分析 job（异步、可取消）。

        云 provider（``require_user_confirmation=True``）的上传必须由调用方先获得
        用户确认（TAEP §12 P5）。本地 provider 无需确认。

        Args:
            provider_id: 单 provider（向后兼容；旧调用入口）。
            provider_ids: M4 多 provider 顺序列表（recipe 解析结果）。优先于 ``provider_id``。
                逐 provider 跑后经 :meth:`ingest` 字段级合并（scene_cuts + transcript + ...），
                每个 provider 的 field-level provenance 独立保留。
            on_progress: 可选的宿主进度回调。``None`` 进度表示当前阶段无法估算，
                UI 应显示不确定进度条；回调异常不会影响分析任务。
            options: 可选的本次分析设置。只传给声明支持 ``options`` 的 provider，
                保持旧 provider 的兼容性。
        """
        # M4：解析 provider 列表（provider_ids 优先，否则用 provider_id 单值，否则首个注册的）
        if provider_ids is not None:
            ids = tuple(provider_ids)
        elif provider_id is not None:
            ids = (provider_id,)
        else:
            with self._state_lock:
                first = next(iter(self._providers.keys()), None)
            if first is None:
                return None, "no analysis provider registered"
            ids = (first,)

        providers: list[AnalysisProvider] = []
        for pid in ids:
            with self._state_lock:
                p = self._providers.get(pid)
            if p is None:
                return None, f"unknown provider {pid!r}"
            providers.append(p)

        if require_user_confirmation:
            # 调用方应已确认；这里仅记录 job kind
            job = self._jobs.submit(JobKind.ANALYSIS, label=f"Cloud analyze {asset_id}")
        else:
            label = f"Analyze {asset_id}" if len(providers) == 1 else (
                f"Analyze {asset_id} ({len(providers)} providers)"
            )
            job = self._jobs.submit(JobKind.ANALYSIS, label=label)

        # P5：实际执行由宿主 worker 接（避免在 service 内同步阻塞）。
        # 这里同步执行本地 provider（SceneCutProvider / WhisperXTranscriptProvider），
        # 便于测试与无云场景；云 provider 由 worker 异步驱动 job 状态。
        # M4：多 provider 顺序跑，逐个 ingest 合并（字段级，_merge_analysis）。
        # T1.4：执行期间 job 处于 RUNNING 并按 provider 进度上报（供 job 面板显示）。
        try:
            job.mark_running()
            _notify_progress(on_progress, 0.0, "分析任务已启动")
            ran: list[str] = []
            total_segs = 0
            n_providers = max(len(providers), 1)
            for idx, provider in enumerate(providers):
                # 每个 provider 占整体进度的均分段；base = 已完成 provider 的占比。
                base = idx / n_providers
                span = 1.0 / n_providers
                pid = provider.provider_id  # type: ignore[attr-defined]

                def _on_progress(frac: float | None, msg: Any = None, *,
                                 _base: float = base, _span: float = span,
                                 _pid: str = pid) -> None:
                    # frac 是该 provider 自身进度（0..1）；映射到整体进度。
                    mapped: float | None = None
                    if frac is not None:
                        mapped = _base + _span * max(0.0, min(1.0, float(frac)))
                        job.update_progress(mapped, f"{_pid}: {msg}" if msg else None)
                    _notify_progress(
                        on_progress,
                        mapped,
                        f"{_pid}: {msg}" if msg else _pid,
                    )

                analyze = provider.analyze  # type: ignore[attr-defined]
                kwargs: dict[str, Any] = {
                    "fps": fps,
                    "progress": _on_progress,
                }
                if options and _accepts_provider_options(analyze):
                    kwargs["options"] = dict(options)
                analysis, segments = analyze(asset_id, fingerprint, **kwargs)
                self.ingest(asset_id, analysis, segments)
                drain_embeddings = getattr(provider, "drain_embeddings", None)
                if callable(drain_embeddings):
                    self.ingest_embeddings(drain_embeddings(asset_id))
                ran.append(pid)
                total_segs += len(segments)
                job.update_progress((idx + 1) / n_providers)
                _notify_progress(on_progress, (idx + 1) / n_providers, f"{pid} 已完成")
            job.complete({"segments": total_segs, "providers": ran})
            _notify_progress(on_progress, 1.0, "分析完成")
        except Exception as exc:
            job.fail("ANALYSIS_FAILED", str(exc))
            _notify_progress(on_progress, None, f"分析失败：{exc}")
        return job, ""

    def search_assets(
        self, *, text: str | None = None, kind: SegmentKind | None = None,
        limit: int | None = None,
    ) -> list[SemanticSegment]:
        """跨 asset 的 segment 搜索（分页，避免把数小时转录塞进模型上下文）。"""
        with self._state_lock:
            records_snapshot = list(self._records.values())
        candidates: list[SemanticSegment] = []
        for rec in records_snapshot:
            for seg in rec.segments:
                if kind is not None and seg.kind != kind:
                    continue
                candidates.append(seg)
        if text is not None:
            # AUD-12907：整轮搜索只构建一个共享探针索引（search_evidence 逐
            # segment 独立打分，共享索引与"每段各建一个索引"结果等价），
            # 避免每段新建 SemanticIndex 的 O(n) 重对象构建。
            from src.features.edit.ai_editing.semantic_index import SemanticIndex
            probe = SemanticIndex()
            probe.add_many(candidates)
            matched_ids = {
                hit.segment.segment_id
                for hit in probe.search_evidence(text, limit=max(1, len(candidates)))
            }
            candidates = [seg for seg in candidates if seg.segment_id in matched_ids]
        results = candidates
        results.sort(key=lambda s: (s.asset_id, s.source_range.start))
        if limit is not None:
            results = results[:limit]
        return results


def _job_cost_units(job: JobHandle | None) -> float:
    """Read the provider-reported cost contribution of one finished job.

    Providers report spend via ``result_manifest["cost_units"]`` (the same
    convention ``JobRegistry.metrics`` aggregates).  Missing or non-numeric
    entries simply contribute 0.0 — budget accounting never crashes analysis.
    """
    if job is None:
        return 0.0
    manifest = getattr(job, "result_manifest", None)
    if not isinstance(manifest, dict):
        return 0.0
    value = manifest.get("cost_units", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _merge_analysis(base: AssetAnalysis, extra: AssetAnalysis) -> AssetAnalysis:
    """字段级合并两个 AssetAnalysis（extra 的非空字段覆盖 base）。

    M2（§18.2）：``field_provenance`` 也按字段合并——每个 provider 只覆盖它实际填了
    非空数据的那一类的来源，不互相覆盖。这样 transcript provider 不会覆盖 shot
    provider 的来源，反之亦然。
    """
    from dataclasses import replace

    merged_field_prov = _merge_field_provenance(base.field_provenance, extra.field_provenance)

    return replace(
        base,
        scene_cuts=extra.scene_cuts or base.scene_cuts,
        transcript=extra.transcript or base.transcript,
        tags=extra.tags or base.tags,
        ocr=extra.ocr or base.ocr,
        audio=extra.audio or base.audio,
        fingerprint=extra.fingerprint or base.fingerprint,
        user_verified=base.user_verified or extra.user_verified,
        field_provenance=merged_field_prov,
    )


def _merge_field_provenance(
    base: "FieldProvenance | None", extra: "FieldProvenance | None",
) -> "FieldProvenance | None":
    """合并两份 field-level provenance：extra 的非空字段来源覆盖 base。"""
    if base is None and extra is None:
        return None
    if base is None:
        return extra
    if extra is None:
        return base
    from src.features.edit.ai_editing.analysis_models import _ANALYSIS_FIELDS

    result = base
    for name in _ANALYSIS_FIELDS:
        extra_p = extra.for_field(name)
        if extra_p is not None:
            result = result.with_field(name, extra_p)
    return result
