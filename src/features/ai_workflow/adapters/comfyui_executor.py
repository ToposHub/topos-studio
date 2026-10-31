"""P6.3 ComfyUI Workflow executor —— GenerationProvider adapter（plan §14.1 step 2/4）。

> 用本地 mock HTTP/WebSocket server 固化官方 API-format workflow fixture；测试 submit、
> progress、cancel、history/output 映射，不依赖真实 GPU。
> 实现 ``ComfyUI Workflow`` executor 的 ``GenerationProvider`` adapter，结果必须进
> ``GenerationBin``，不得直接导入 timeline。

把 ComfyUI 工作流执行接入 :class:`GenerationProvider` 协议（plan §1.4.2 C1/C2）：

- :class:`FakeComfyServer` — mock Comfy server（不依赖真实 GPU/网络；§14.1 step 2）。
  生产环境替换为 :class:`RealComfyClient`（HTTP + WebSocket），接口一致。
- :class:`ComfyGenerationRequest` — executor 入参（snapshot + profile + consent +
  bindings + modality + prompt）。``consent.is_complete=False`` → 拒绝提交（红线 §0 #1）。
- :class:`ComfyWorkflowExecutor` — 实现 :class:`GenerationProvider` 协议：
    - ``submit_job``：preflight 通过后 → server.submit → :class:`ComfyJobRef`。
    - ``poll_progress``：拉 server 状态 → 更新 :class:`ComfyJobRef`。
    - ``fetch_outputs``：下载输出 → 校验 hash → :class:`GenerationResult`（候选进 bin）。
    - ``cancel_job``：传播取消。
    - ``generate``（Protocol 路径）：把 :class:`GenerationRequest` 转
      :class:`ComfyGenerationRequest`，跑完整 pipeline。

红线（plan §14.1 step 2/4 / §9.4 / §0 / §1.4.2）：

- **不绕开 preflight**：``submit_job`` 必须传 :class:`ComfyImportReport` 且 preflight
  通过；缺失依赖 → :class:`ComfyExecutorError`（PREFLIGHT_FAILED）。
- **consent gate**：未确认不提交（CONSENT_INCOMPLETE）。
- **结果进 GenerationBin**（候选，不直接进 timeline）：``fetch_outputs`` 返回
  :class:`GenerationResult`，候选由 :class:`GenerationService` 落到 bin。
- **取消传播**：``cancel_job`` → server.cancel + :class:`ComfyJobRef` → CANCELLED。
- **hash 校验**（§9.4 #5）：下载内容 hash ≠ server 报告 → HASH_MISMATCH，候选不入 bin。
- **provider_id 受控**：``comfyui-local`` / ``comfyui-cloud``。
- **provenance 完整**：``provider_id`` / ``model_id`` / ``model_version``（含 snapshot
  hash） / ``input_prompt_hash`` 必填。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from src.features.ai_workflow.adapters.comfyui_importer import (
    ComfyImportReport,
    ComfyPreflightChecker,
    ComfyServerCapabilities,
)
from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyConnectionProfile,
    ComfyInputBinding,
    ComfyJobRef,
    ComfyJobState,
    ComfyOutputArtifact,
    ComfyWorkflowSnapshot,
)
from src.features.edit.ai_editing.generation_models import (
    GenerationBudget,
    GeneratedAsset,
    GenerationConsent,
    GenerationModality,
    GenerationProvenance,
    GenerationRequest,
    GenerationResult,
    LicenseStatus,
)


# ---------------------------------------------------------------------- #
# provider_id 常量
# ---------------------------------------------------------------------- #

COMFYUI_LOCAL_PROVIDER_ID = "comfyui-local"
COMFYUI_CLOUD_PROVIDER_ID = "comfyui-cloud"

# AUD-23015: 已提交作业 LRU 追踪上限（幂等重放窗口）。
_MAX_TRACKED_JOBS = 256


# ---------------------------------------------------------------------- #
# ComfyExecutorError
# ---------------------------------------------------------------------- #


class ComfyExecutorErrorCode:
    """executor 错误码（plan §9.4 #9）。"""

    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    CONSENT_INCOMPLETE = "CONSENT_INCOMPLETE"
    SERVER_FAILED = "SERVER_FAILED"
    CANCELLED = "CANCELLED"
    HASH_MISMATCH = "HASH_MISMATCH"
    NO_OUTPUTS = "NO_OUTPUTS"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TIMEOUT = "TIMEOUT"
    COST_LIMIT_EXCEEDED = "COST_LIMIT_EXCEEDED"
    CONSENT_PROVIDER_MISMATCH = "CONSENT_PROVIDER_MISMATCH"

    ALL: Tuple[str, ...] = (
        PREFLIGHT_FAILED, CONSENT_INCOMPLETE, SERVER_FAILED,
        CANCELLED, HASH_MISMATCH, NO_OUTPUTS, SUBMIT_FAILED,
        IDEMPOTENCY_CONFLICT, TIMEOUT, COST_LIMIT_EXCEEDED,
        CONSENT_PROVIDER_MISMATCH,
    )


class ComfyExecutorError(Exception):
    """executor 异常（frozen 语义：``code`` 不变）。"""

    def __init__(self, *, code: str, message: str) -> None:
        if code not in ComfyExecutorErrorCode.ALL:
            raise ValueError(f"invalid ComfyExecutorErrorCode: {code!r}")
        super().__init__(f"[{code}] {message}")
        self._code = code
        self._message = message

    @property
    def code(self) -> str:
        return self._code

    @property
    def message(self) -> str:
        return self._message


# ---------------------------------------------------------------------- #
# ComfyGenerationRequest —— executor 入参
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComfyGenerationRequest:
    """executor 的入参（plan §14.1 step 4）。

    红线：``consent.is_complete=False`` → 构造失败（§0 #1）。

    Attributes:
        request_id: 唯一请求 ID。
        snapshot: ComfyUI workflow snapshot（只读）。
        profile: 连接 profile（决定 provider_id）。
        consent: 用户同意（必须完整）。
        modality: 生成类型（image/video/audio）。
        prompt: 用户 prompt（hash 进 provenance；不存原文到审计）。
        bindings: 输入绑定（覆盖 snapshot 字面量输入）。
    """

    request_id: str
    snapshot: ComfyWorkflowSnapshot
    profile: ComfyConnectionProfile
    consent: GenerationConsent
    modality: GenerationModality
    prompt: str
    bindings: Tuple[ComfyInputBinding, ...] = ()
    budget: GenerationBudget = field(default_factory=GenerationBudget)

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.consent.is_complete:
            raise ValueError(
                "consent must be complete (confirmed + cost/upload/copyright acknowledged) "
                "before submitting a ComfyUI workflow"
            )
        if not self.prompt:
            raise ValueError("prompt cannot be empty")

    @property
    def workflow_snapshot_hash(self) -> str:
        return self.snapshot.content_hash

    @property
    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:16]

    def submitted_payload(self) -> Dict[str, Any]:
        """返回提交给 Comfy server 的 API-format payload（应用 bindings 后）。"""
        import copy
        payload = copy.deepcopy(dict(self.snapshot.raw_payload))
        for b in self.bindings:
            # 找到对应节点输入并改写
            node = payload.get(b.slot.node_id)
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            if b.slot.input_name in inputs:
                # 类型转换：integer/float 转数字，其他转字符串
                val: Any = b.resolved_value
                if b.slot.artifact_type == "integer":
                    try:
                        val = int(b.resolved_value)
                    except ValueError:
                        pass
                elif b.slot.artifact_type == "float":
                    try:
                        val = float(b.resolved_value)
                    except ValueError:
                        pass
                inputs[b.slot.input_name] = val
        return payload


# ---------------------------------------------------------------------- #
# FakeComfyServer —— mock Comfy server（§14.1 step 2）
# ---------------------------------------------------------------------- #


@dataclass
class _FakeJob:
    """FakeComfyServer 内部 job 记录。"""

    external_job_id: str
    workflow_snapshot_hash: str
    payload: Dict[str, Any]
    state: str = ComfyJobState.QUEUED
    progress_fraction: float = 0.0
    progress_message: str = ""
    outputs: List[ComfyOutputArtifact] = field(default_factory=list)
    fail_reason: str = ""
    submit_time_iso: str = ""
    # 测试用：实际下载内容 hash（默认与报告一致；tamper 时不一致）
    actual_output_hashes: Dict[str, str] = field(default_factory=dict)
    actual_cost_units: float = 0.0


class FakeComfyServer:
    """mock Comfy server（plan §14.1 step 2）。

    不依赖真实 GPU/网络。固化 fixture（API-format workflow）。
    生产环境替换为 :class:`RealComfyClient`（HTTP + WebSocket），接口一致。

    用法（测试）：

    >>> server = FakeComfyServer(profile_id="local-default", available_models={...})
    >>> ref = server.submit(payload, snapshot_hash)
    >>> server.advance_to_running(ref.external_job_id)
    >>> ref, outputs = server.complete_with_outputs(ref.external_job_id, outputs=(...))
    """

    def __init__(
        self,
        *,
        profile_id: str,
        available_class_types: Optional[frozenset] = None,
        available_models: Optional[frozenset] = None,
        cost_units: float = 0.0,
    ) -> None:
        if cost_units < 0.0:
            raise ValueError("cost_units must be non-negative")
        self.profile_id = profile_id
        # 默认所有 class_type 都"server 已安装"（用 caps 单独传 preflight）
        self.available_class_types = available_class_types or frozenset()
        self.available_models = available_models or frozenset()
        self._jobs: Dict[str, _FakeJob] = {}
        self._cost_units = cost_units
        # 测试用：submit 后自动完成 + 产 1 张 image
        self.auto_complete_with_image: bool = False

    # ---------------- 基本查询 ---------------- #

    @property
    def job_count(self) -> int:
        return len(self._jobs)

    def get_job(self, external_job_id: str) -> ComfyJobRef:
        j = self._jobs[external_job_id]
        return ComfyJobRef(
            external_job_id=j.external_job_id,
            profile_id=self.profile_id,
            workflow_snapshot_hash=j.workflow_snapshot_hash,
            state=j.state,
            progress_fraction=j.progress_fraction,
            progress_message=j.progress_message,
            submit_time_iso=j.submit_time_iso,
        )

    # ---------------- 提交 ---------------- #

    def submit(
        self,
        *,
        payload: Mapping[str, Any],
        workflow_snapshot_hash: str,
    ) -> ComfyJobRef:
        external_job_id = f"comfy-{uuid.uuid4().hex[:12]}"
        submit_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        job = _FakeJob(
            external_job_id=external_job_id,
            workflow_snapshot_hash=workflow_snapshot_hash,
            payload=dict(payload),
            submit_time_iso=submit_iso,
            actual_cost_units=self._cost_units,
        )
        self._jobs[external_job_id] = job
        if self.auto_complete_with_image:
            # 自动推进：RUNNING → SUCCESS + image
            self.advance_to_running(external_job_id)
            img = ComfyOutputArtifact(
                node_id="9", output_index=0, artifact_kind="image",
                filename=f"topos_{external_job_id[-6:]}.png",
                content_hash="sha256:" + hashlib.sha256(
                    f"{external_job_id}".encode()
                ).hexdigest(),
                bytes_size=4096,
            )
            self.complete_with_outputs(external_job_id, outputs=(img,))
        return self.get_job(external_job_id)

    # ---------------- 进度 ---------------- #

    def advance_to_running(self, external_job_id: str) -> ComfyJobRef:
        j = self._jobs[external_job_id]
        j.state = ComfyJobState.RUNNING
        j.progress_fraction = 0.5
        j.progress_message = "executing workflow"
        return self.get_job(external_job_id)

    def complete_with_outputs(
        self,
        external_job_id: str,
        *,
        outputs: Tuple[ComfyOutputArtifact, ...] = (),
        tamper_hash: bool = False,
    ) -> Tuple[ComfyJobRef, List[ComfyOutputArtifact]]:
        j = self._jobs[external_job_id]
        j.state = ComfyJobState.SUCCESS
        j.progress_fraction = 1.0
        j.progress_message = "done"
        j.outputs = list(outputs)
        if tamper_hash:
            # 模拟下载内容与报告 hash 不一致（红线 §9.4 #5）
            for o in outputs:
                # 实际 hash 是 "tampered"，与报告 o.content_hash 不同
                j.actual_output_hashes[o.filename] = "sha256:tampered-content"
        else:
            for o in outputs:
                j.actual_output_hashes[o.filename] = o.content_hash
        return self.get_job(external_job_id), list(j.outputs)

    def fail(self, external_job_id: str, *, reason: str) -> ComfyJobRef:
        j = self._jobs[external_job_id]
        j.state = ComfyJobState.FAILED
        j.fail_reason = reason
        return self.get_job(external_job_id)

    def cancel(self, external_job_id: str) -> ComfyJobRef:
        j = self._jobs[external_job_id]
        # 已终态的不能取消
        if j.state in ComfyJobState.TERMINAL:
            return self.get_job(external_job_id)
        j.state = ComfyJobState.CANCELLED
        return self.get_job(external_job_id)

    def job_outputs(
        self, external_job_id: str,
    ) -> Tuple[List[ComfyOutputArtifact], Dict[str, str], float]:
        """返回 (outputs, 实际下载 hash 表, 实际成本)——server 协议的公共面。

        :class:`RealComfyClient` 实现同签名；executor 的 ``fetch_outputs`` 只经
        此方法取产物，不穿透 ``_jobs`` 私有结构。
        """
        j = self._jobs[external_job_id]
        return list(j.outputs), dict(j.actual_output_hashes), float(j.actual_cost_units)


# ---------------------------------------------------------------------- #
# ComfyWorkflowExecutor —— GenerationProvider adapter
# ---------------------------------------------------------------------- #


class ComfyWorkflowExecutor:
    """ComfyUI workflow 执行器，实现 :class:`GenerationProvider` 协议（C1/C2）。

    把 ComfyUI 工作流执行包装为 provider-neutral 生成 provider，使生成结果可经
    :class:`GenerationService` 进 :class:`GenerationBin`（候选，不直接进 timeline）。

    生产环境把 :class:`FakeComfyServer` 换成真实 HTTP/WS client（接口一致）。
    """

    def __init__(
        self,
        *,
        server: FakeComfyServer,
        server_capabilities: ComfyServerCapabilities,
        require_pinned_custom_nodes: bool = True,
        # AUD-23011：真实 ComfyUI 图像生成普遍 >30s（SDXL 常见 1-5 分钟），默认
        # 超时必须放宽到分钟级，否则非平凡生成会被主动取消并误报 TIMEOUT。
        # Fake/测试路径由测试显式传小值。
        poll_timeout_s: float = 600.0,
        poll_interval_s: float = 0.05,
    ) -> None:
        if poll_timeout_s <= 0.0:
            raise ValueError("poll_timeout_s must be positive")
        if poll_interval_s < 0.0:
            raise ValueError("poll_interval_s must be non-negative")
        self._server = server
        self._caps = server_capabilities
        self._preflight = ComfyPreflightChecker(
            require_pinned_custom_nodes=require_pinned_custom_nodes,
        )
        # bind_workflow 时填入（generate 路径用）
        # AUD-23019: bind/capabilities 写入与 worker 线程 generate 的读取
        # 共享这组字段，统一由 ``_bind_lock`` 同步（generate 入口一次性
        # 快照读取，避免读到撕裂的 snapshot/profile/bindings 组合）。
        self._bind_lock = threading.Lock()
        self._bound_snapshot: Optional[ComfyWorkflowSnapshot] = None
        self._bound_profile: Optional[ComfyConnectionProfile] = None
        self._bound_bindings: Tuple[ComfyInputBinding, ...] = ()
        self._poll_timeout_s = poll_timeout_s
        self._poll_interval_s = poll_interval_s
        # request_id -> (stable request fingerprint, external job reference)
        # AUD-23015: executor 与宿主同生命周期常驻，此表只增不减会无界
        # 增长。改为 LRU（OrderedDict + 上限）：幂等重放保护覆盖最近
        # ``_MAX_TRACKED_JOBS`` 条；被淘汰的极旧 request_id 再提交按新
        # 任务处理（幂等窗口有限 vs 内存有界的权衡，见审计建议）。
        self._submitted_jobs: "OrderedDict[str, Tuple[str, ComfyJobRef]]" = OrderedDict()

    # ---------------- provider_id（Protocol 字段）---------------- #

    # 默认 provider_id（满足 GenerationProvider.provider_id 类属性检查）
    provider_id: str = COMFYUI_LOCAL_PROVIDER_ID

    def provider_id_for(self, profile: ComfyConnectionProfile) -> str:
        """根据 profile 决定 provider_id（local / cloud）。"""
        if profile.is_cloud:
            return COMFYUI_CLOUD_PROVIDER_ID
        return COMFYUI_LOCAL_PROVIDER_ID

    @property
    def supported_modalities(self) -> Tuple[GenerationModality, ...]:
        return (
            GenerationModality.IMAGE,
            GenerationModality.VIDEO,
            GenerationModality.AUDIO if hasattr(GenerationModality, "AUDIO") else GenerationModality.IMAGE,
        )

    # ---------------- bind（generate 路径用）---------------- #

    def set_server_capabilities(self, caps: ComfyServerCapabilities) -> None:
        """替换 preflight 能力集（Real 路径：bind 时查询 /object_info 刷新）。

        AUD-23019: 与 worker 线程的 ``generate``/``submit_job`` 读取同步。
        """
        with self._bind_lock:
            self._caps = caps

    def bind_workflow(
        self,
        snapshot: ComfyWorkflowSnapshot,
        *,
        profile: ComfyConnectionProfile,
        bindings: Tuple[ComfyInputBinding, ...] = (),
    ) -> None:
        """绑定 workflow + profile（+ 可选输入绑定），供 :meth:`generate` 使用。

        AUD-23012：``bindings`` 使 generate 协议路径可以携带输入绑定（覆盖
        snapshot 字面量输入）；未传时保持空（snapshot 字面量原样提交）。
        AUD-23019: 三个绑定字段 + provider_id 原子地一起替换，防止
        worker 线程的 generate 读到撕裂的组合。
        """
        with self._bind_lock:
            self._bound_snapshot = snapshot
            self._bound_profile = profile
            self._bound_bindings = tuple(bindings)
            # 同步 provider_id（影响 Protocol 属性检查）
            object.__setattr__(self, "provider_id", self.provider_id_for(profile))

    # ---------------- Job 生命周期（显式 pipeline）---------------- #

    def submit_job(
        self,
        req: ComfyGenerationRequest,
        *,
        import_report: ComfyImportReport,
    ) -> ComfyJobRef:
        """preflight → submit。红线：preflight 未通过 → 异常。"""
        # AUD-23019: caps 与 bind 写线程同步后快照读取。
        with self._bind_lock:
            caps = self._caps
        preflight = self._preflight.check(import_report, capabilities=caps)
        if not preflight.allowed:
            raise ComfyExecutorError(
                code=ComfyExecutorErrorCode.PREFLIGHT_FAILED,
                message=f"preflight denied ({preflight.deny_code}); cannot submit",
            )
        fingerprint = self._request_fingerprint(req)
        previous = self._submitted_jobs.get(req.request_id)
        if previous is not None:
            self._submitted_jobs.move_to_end(req.request_id)
            previous_fingerprint, previous_ref = previous
            if previous_fingerprint != fingerprint:
                raise ComfyExecutorError(
                    code=ComfyExecutorErrorCode.IDEMPOTENCY_CONFLICT,
                    message="request_id was already used for a different workflow request",
                )
            # A retry of the same request observes the original external job;
            # it never submits a second paid/provider-side job.
            return self.poll_progress(previous_ref)
        try:
            ref = self._server.submit(
                payload=req.submitted_payload(),
                workflow_snapshot_hash=req.workflow_snapshot_hash,
            )
        except Exception as e:
            raise ComfyExecutorError(
                code=ComfyExecutorErrorCode.SUBMIT_FAILED,
                message=f"server submit failed: {e}",
            ) from e
        self._track_submitted(req.request_id, fingerprint, ref)
        return ref

    def _track_submitted(
        self, request_id: str, fingerprint: str, ref: ComfyJobRef,
    ) -> None:
        """记录已提交作业并维持 LRU 上限（AUD-23015）。

        executor 与宿主同生命周期常驻，追踪表只增不减会无界增长；
        超出 ``_MAX_TRACKED_JOBS`` 后淘汰最旧条目（被淘汰的极旧
        request_id 再提交按新任务处理——幂等窗口有限 vs 内存有界）。
        """
        self._submitted_jobs[request_id] = (fingerprint, ref)
        self._submitted_jobs.move_to_end(request_id)
        while len(self._submitted_jobs) > _MAX_TRACKED_JOBS:
            self._submitted_jobs.popitem(last=False)

    @staticmethod
    def _request_fingerprint(req: ComfyGenerationRequest) -> str:
        """Hash request identity without retaining the original prompt."""
        payload = {
            "snapshot": req.workflow_snapshot_hash,
            "prompt_hash": req.prompt_hash,
            "modality": req.modality.value,
            "profile_id": req.profile.profile_id,
            "payload": req.submitted_payload(),
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def poll_progress(self, ref: ComfyJobRef) -> ComfyJobRef:
        """拉 server 状态。"""
        return self._server.get_job(ref.external_job_id)

    def poll_until_terminal(
        self,
        ref: ComfyJobRef,
        *,
        timeout_s: float | None = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> ComfyJobRef:
        """Poll until provider terminal state; timeout/cancel propagates cancellation."""
        deadline = time.monotonic() + (self._poll_timeout_s if timeout_s is None else timeout_s)
        if deadline <= time.monotonic():
            raise ComfyExecutorError(
                code=ComfyExecutorErrorCode.TIMEOUT,
                message="comfy job polling timed out before it started",
            )
        current = ref
        while not current.is_terminal:
            if is_cancelled is not None and is_cancelled():
                self.cancel_job(current)
                raise ComfyExecutorError(
                    code=ComfyExecutorErrorCode.CANCELLED,
                    message="comfy job cancelled by user request",
                )
            current = self.poll_progress(current)
            if current.is_terminal:
                return current
            if time.monotonic() >= deadline:
                self.cancel_job(current)
                raise ComfyExecutorError(
                    code=ComfyExecutorErrorCode.TIMEOUT,
                    message="comfy job polling timed out; cancellation requested",
                )
            if self._poll_interval_s:
                time.sleep(self._poll_interval_s)
        return current

    def fetch_outputs(
        self,
        req: ComfyGenerationRequest,
        ref: ComfyJobRef,
    ) -> GenerationResult:
        """SUCCESS → 下载 + hash 校验 → :class:`GenerationResult`。"""
        latest = self._server.get_job(ref.external_job_id)
        if latest.state == ComfyJobState.FAILED:
            return GenerationResult(
                request_id=req.request_id,
                error_code=ComfyExecutorErrorCode.SERVER_FAILED,
                error_message="comfy job failed",
            )
        if latest.state == ComfyJobState.CANCELLED:
            return GenerationResult(
                request_id=req.request_id,
                error_code=ComfyExecutorErrorCode.CANCELLED,
                error_message="comfy job cancelled",
            )
        if latest.state != ComfyJobState.SUCCESS:
            return GenerationResult(
                request_id=req.request_id,
                error_code=ComfyExecutorErrorCode.SERVER_FAILED,
                error_message=f"job not ready (state={latest.state})",
            )
        # SUCCESS：取 outputs（经公共 job_outputs 协议；Fake/Real 同签名）。
        # AUD-23002：Real 客户端下载失败（瞬时网络错误等）会从 job_outputs 抛出，
        # 映射为 SERVER_FAILED 诊断返回（不固化"无产物"，client 侧保留重试态）。
        try:
            outputs, actual_output_hashes, actual_cost = self._server.job_outputs(
                ref.external_job_id,
            )
        except Exception as e:  # noqa: BLE001 — server 协议异常统一映射诊断码
            return GenerationResult(
                request_id=req.request_id,
                error_code=ComfyExecutorErrorCode.SERVER_FAILED,
                error_message=f"failed to fetch outputs from server: {e}",
            )
        actual_cost = float(actual_cost)
        if req.budget.max_cost_units is not None and actual_cost > req.budget.max_cost_units:
            return GenerationResult(
                request_id=req.request_id,
                actual_cost_units=actual_cost,
                error_code=ComfyExecutorErrorCode.COST_LIMIT_EXCEEDED,
                error_message="provider reported cost above the approved budget",
            )
        if not outputs:
            return GenerationResult(
                request_id=req.request_id,
                error_code=ComfyExecutorErrorCode.NO_OUTPUTS,
                error_message="job produced no outputs",
            )
        # hash 校验（红线 §9.4 #5）
        candidates: List[GeneratedAsset] = []
        max_candidates = max(0, int(req.budget.max_candidates))
        for idx, out in enumerate(outputs[:max_candidates]):
            # hash 校验（红线 §9.4 #5）。AUD-23013：实际 hash **缺失**（不在
            # actual_output_hashes 中，如下载失败后的 pending 占位）视为校验
            # 失败，不得回退为期望值让比较恒等通过。
            actual_hash = actual_output_hashes.get(out.filename)
            if actual_hash is None:
                return GenerationResult(
                    request_id=req.request_id,
                    error_code=ComfyExecutorErrorCode.HASH_MISMATCH,
                    error_message=(
                        f"output {out.filename!r} has no verified content hash "
                        "(download incomplete); refusing to admit candidate"
                    ),
                )
            if actual_hash != out.content_hash:
                # hash 不一致：拒绝该候选（不进 bin）
                return GenerationResult(
                    request_id=req.request_id,
                    error_code=ComfyExecutorErrorCode.HASH_MISMATCH,
                    error_message=(
                        f"output {out.filename!r} hash mismatch "
                        f"(expected {out.content_hash[:24]}…, got {actual_hash[:24]}…)"
                    ),
                )
            # 构造 GeneratedAsset（候选进 bin；不直接进 timeline）
            provenance = GenerationProvenance(
                provider_id=self.provider_id_for(req.profile),
                model_id=_infer_model_id(req.snapshot),
                model_version=f"comfy-workflow@{req.workflow_snapshot_hash[:24]}",
                requested_at=req.consent.confirmed_at,
                input_prompt_hash=req.prompt_hash,
                seed=None,
                reference_asset_ids=(),
                parent_skill_id="",
            )
            asset = GeneratedAsset(
                asset_id=f"gen-{req.request_id}-{idx + 1}",
                request_id=req.request_id,
                modality=req.modality,
                provenance=provenance,
                license=LicenseStatus.UNKNOWN,
                cost_units=0.0,
                preview_handle=self._preview_handle(req, out),
                expected_sha256=out.content_hash,
            )
            candidates.append(asset)
        return GenerationResult(
            request_id=req.request_id,
            candidates=tuple(candidates),
            actual_cost_units=actual_cost,
        )

    def cancel_job(self, ref: ComfyJobRef) -> ComfyJobRef:
        """取消传播（红线）。"""
        return self._server.cancel(ref.external_job_id)

    def _preview_handle(self, req: ComfyGenerationRequest, out: ComfyOutputArtifact) -> str:
        """候选预览句柄：Real 客户端给 http(s) /view URL（物化器可直接下载）；
        Fake 保留 comfyui:// 仅供测试。"""
        url_builder = getattr(self._server, "output_url", None)
        if callable(url_builder):
            try:
                return str(url_builder(req, out))
            except Exception:  # noqa: BLE001 — URL 构造失败退回 scheme 句柄
                pass
        return f"comfyui://{req.profile.profile_id}/{out.filename}"

    # ---------------- GenerationProvider.generate（标准协议路径）---------------- #

    def generate(
        self,
        request: GenerationRequest,
        *,
        progress: Optional[Callable[[float, str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> GenerationResult:
        """provider-neutral 入口（plan §14.1 step 4：结果进 GenerationBin）。

        ``is_cancelled`` 由 :class:`GenerationService` 按签名自动注入
        （与 HttpGenerationProvider 对齐）：轮询循环检查到取消 → 传播
        server 取消并返回 CANCELLED 诊断。"""
        # consent gate（红线 §0 #1）
        if not request.consent.is_complete:
            return GenerationResult(
                request_id=request.request_id,
                error_code=ComfyExecutorErrorCode.CONSENT_INCOMPLETE,
                error_message="consent not complete",
            )
        # AUD-23019: bind 状态在锁内一次性快照读取——worker 线程执行
        # generate 期间 UI 线程重绑定时，后续步骤使用同一个一致组合。
        with self._bind_lock:
            bound_snapshot = self._bound_snapshot
            bound_profile = self._bound_profile
            bound_bindings = self._bound_bindings
        if bound_snapshot is None or bound_profile is None:
            return GenerationResult(
                request_id=request.request_id,
                error_code=ComfyExecutorErrorCode.SUBMIT_FAILED,
                error_message="workflow/profile not bound; call bind_workflow first",
            )
        expected_provider = self.provider_id_for(bound_profile)
        if request.consent.provider_id and request.consent.provider_id != expected_provider:
            return GenerationResult(
                request_id=request.request_id,
                error_code=ComfyExecutorErrorCode.CONSENT_PROVIDER_MISMATCH,
                error_message="consent provider does not match the bound provider",
            )
        # 构造 ComfyGenerationRequest（再做一次 consent 校验，防御性）
        try:
            comfy_req = ComfyGenerationRequest(
                request_id=request.request_id,
                snapshot=bound_snapshot,
                profile=bound_profile,
                consent=request.consent,
                modality=request.modality,
                prompt=request.prompt,
                budget=request.budget,
                bindings=bound_bindings,
            )
        except ValueError as e:
            return GenerationResult(
                request_id=request.request_id,
                error_code=ComfyExecutorErrorCode.CONSENT_INCOMPLETE,
                error_message=str(e),
            )
        # preflight（用 bound snapshot 构造 report）。AUD-23012：把绑定的 slot
        # 与"声明必填但仍未绑定"的 slot 一并放进 report，使 MISSING_BINDING
        # 门禁在 generate 路径真实生效（此前恒为空转）。
        bound_slot_ids = {b.slot.slot_id for b in bound_bindings}
        unbound_required_slots = tuple(
            s.slot_id
            for s in bound_snapshot.input_slots
            if s.required and s.slot_id not in bound_slot_ids
        )
        report = ComfyImportReport(
            snapshot=bound_snapshot,
            required_bindings=bound_bindings,
            required_unbound_slot_ids=unbound_required_slots,
        )
        try:
            ref = self.submit_job(comfy_req, import_report=report)
        except ComfyExecutorError as e:
            return GenerationResult(
                request_id=request.request_id,
                error_code=e.code,
                error_message=e.message,
            )
        if progress is not None:
            progress(0.3, "comfy job submitted")
        # 等待 provider 终态；超时/取消会先传播取消再返回诊断。
        try:
            ref = self.poll_until_terminal(ref, is_cancelled=is_cancelled)
        except ComfyExecutorError as e:
            return GenerationResult(
                request_id=request.request_id,
                error_code=e.code,
                error_message=e.message,
            )
        if progress is not None:
            progress(0.6, f"comfy job {ref.state}")
        result = self.fetch_outputs(comfy_req, ref)
        if progress is not None:
            progress(1.0, "comfy outputs fetched")
        return result


def _infer_model_id(snapshot: ComfyWorkflowSnapshot) -> str:
    """从 snapshot 推断 model_id（取 checkpoint 名；多模型用第一个）。"""
    for m in snapshot.required_models:
        if m.model_type == "checkpoint":
            return m.model_name
    # 退化：取第一个模型
    if snapshot.required_models:
        return snapshot.required_models[0].model_name
    return "comfy-unknown-model"
