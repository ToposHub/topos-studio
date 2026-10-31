"""P6.4 Composite Subgraph / Render / Import adapter（plan §9.3 / M8）。

> 一个 ``Composite Subgraph`` 节点引用已有 ``CompositeGraphData``，输入为已批准的媒体
> 或时间线片段，输出为 render job / 生成媒体 artifact。它不共享 GPU texture，也不允许
> AI workflow 直接修改 Composite 内部节点。

把 Composite 渲染能力接入 AI Workflow（plan §9.3 / §1.4.1 / §1.4.3）：

- :class:`CompositeGraphRef` — 已保存合成图的引用（``composite_graph_id`` + schema/version +
  content_hash；**不持有** GPU texture / OpenGL context）。
- :class:`CompositeRenderJobRef` / :class:`CompositeRenderJobState` — 合成渲染 Job 引用。
- :class:`CompositeSubgraphRequest` / :class:`CompositeSubgraphResult` — 渲染入参/出参。
- :class:`FakeCompositeRenderService` — mock render service（不依赖真实 GPU/OpenGL）。
  生产环境替换为真实 Composite render service，接口一致。
- :class:`CompositeSubgraphAdapter` — 把渲染流程包装为 workflow 节点可调用的 adapter：
  ``submit_render`` / ``poll_render`` / ``fetch_result`` / ``cancel_render``。
- :func:`register_composite_node_specs` — 把 ``Composite Subgraph`` / ``Render Composite`` /
  ``Import Render Result`` 三个节点 spec 注册到 :class:`NodeSpecRegistry`。

红线（plan §9.3 / §1.4.1 / §1.4.3 / §0 / §10.3.6）：

- **不跨图传 texture**：节点输入是 ``asset_ref`` / ``asset_set`` / ``selection_set``，
  **不是** GPU texture；输出是 ``render_job_ref`` / ``asset_ref``，**不是** texture。
  本模块**不 import** moderngl / PyQt6 / composite.core。
- **render 生命周期由 Composite 管**：adapter 只持有 job ID；不持有 OpenGL context；
  不直接修改 Composite 内部节点。
- **取消不泄漏 / 不半写入**（§10.3.6）：``cancel_render`` 转发取消；已终态 job 的 cancel
  是 no-op；失败/取消不产生半写入 timeline（adapter 不写 timeline，只产候选 asset_ref）。
- **不直接写 timeline**：Composite 节点 ``timeline_write_required=False``；导入媒体池仍
  走单独 approval 链（生成节点没有插轨权限）。
- **graph 版本校验**：引用过期的合成图（content_hash 不匹配）→ GRAPH_STALE，拒绝渲染。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from src.features.ai_workflow.core.node_specs import (
    NodeCategory,
    NodeExecutorId,
    NodePortSpec,
    NodeSpecRegistry,
    WorkflowNodeSpec,
)


# ---------------------------------------------------------------------- #
# CompositeGraphRef
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class CompositeGraphRef:
    """已保存合成图的引用（plan §9.3 #2）。

    Attributes:
        composite_graph_id: 合成图唯一 ID（与 EditProject.compositions key 对齐）。
        schema_version: 合成图 schema 版本（用于迁移）。
        content_hash: 合成图内容 hash（``sha256:...``）；过期校验用。
        display_name: 显示名（Inspector 用）。
    """

    composite_graph_id: str
    schema_version: str = "1"
    content_hash: str = ""
    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.composite_graph_id:
            raise ValueError("composite_graph_id cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "composite_graph_id": self.composite_graph_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "display_name": self.display_name,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CompositeGraphRef":
        return cls(
            composite_graph_id=d["composite_graph_id"],
            schema_version=d.get("schema_version", "1"),
            content_hash=d.get("content_hash", ""),
            display_name=d.get("display_name", ""),
        )


# ---------------------------------------------------------------------- #
# CompositeRenderJobState / CompositeRenderJobRef
# ---------------------------------------------------------------------- #


class CompositeRenderJobState:
    """合成渲染 Job 状态（与 ComfyJobState 解耦）。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    ALL: Tuple[str, ...] = (QUEUED, RUNNING, SUCCESS, FAILED, CANCELLED)
    TERMINAL: Tuple[str, ...] = (SUCCESS, FAILED, CANCELLED)


class CompositeRenderQuality:
    """渲染质量（plan §9.3 #3）。"""

    PROXY = "proxy"     # 代理质量（快速预览）
    FULL = "full"       # 完整质量（交付）
    FINAL = "final"     # 最终交付（含 color mgmt）

    ALL: Tuple[str, ...] = (PROXY, FULL, FINAL)


class CompositeRenderRange:
    """渲染范围（plan §9.3 #3）。"""

    WHOLE = "whole"             # 整个合成长度
    SELECTION = "selection"     # 仅选中区间
    CUSTOM = "custom"           # 自定义 in/out 点

    ALL: Tuple[str, ...] = (WHOLE, SELECTION, CUSTOM)


class CompositeImportStrategy:
    """渲染结果的导入策略（plan §9.3 #4 / M8）。"""

    NONE = "none"                              # 不导入（仅留 render_job_ref）
    IMPORT_AS_GENERATION = "import_as_generation"  # 进生成素材箱（候选）
    IMPORT_TO_MEDIA_POOL = "import_to_media_pool"  # 直接进媒体池（仍需 approval）

    ALL: Tuple[str, ...] = (NONE, IMPORT_AS_GENERATION, IMPORT_TO_MEDIA_POOL)


@dataclass(frozen=True)
class CompositeRenderJobRef:
    """合成渲染 Job 引用（plan §9.3 #3）。

    红线：**不持有** GPU texture / OpenGL context / Qt 对象。只持有 job ID + 状态。
    """

    render_job_id: str
    composite_graph_id: str
    graph_content_hash: str
    quality: str = CompositeRenderQuality.PROXY
    range_spec: str = CompositeRenderRange.WHOLE
    state: str = CompositeRenderJobState.QUEUED
    progress_fraction: float = 0.0
    progress_message: str = ""
    submit_time_iso: str = ""

    def __post_init__(self) -> None:
        if not self.render_job_id:
            raise ValueError("render_job_id cannot be empty")
        if not self.composite_graph_id:
            raise ValueError("composite_graph_id cannot be empty")
        if self.state not in CompositeRenderJobState.ALL:
            raise ValueError(f"invalid state: {self.state!r}")
        if not (0.0 <= self.progress_fraction <= 1.0):
            raise ValueError(
                f"progress_fraction must be in [0,1]: {self.progress_fraction}"
            )

    @property
    def is_terminal(self) -> bool:
        return self.state in CompositeRenderJobState.TERMINAL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "render_job_id": self.render_job_id,
            "composite_graph_id": self.composite_graph_id,
            "graph_content_hash": self.graph_content_hash,
            "quality": self.quality,
            "range_spec": self.range_spec,
            "state": self.state,
            "progress_fraction": self.progress_fraction,
            "progress_message": self.progress_message,
            "submit_time_iso": self.submit_time_iso,
        }


# ---------------------------------------------------------------------- #
# CompositeSubgraphError
# ---------------------------------------------------------------------- #


class CompositeSubgraphErrorCode:
    """Composite Subgraph 错误码。"""

    GRAPH_NOT_FOUND = "GRAPH_NOT_FOUND"
    GRAPH_STALE = "GRAPH_STALE"           # content_hash 不匹配
    RENDER_FAILED = "RENDER_FAILED"
    CANCELLED = "CANCELLED"
    NO_OUTPUT = "NO_OUTPUT"
    SUBMIT_FAILED = "SUBMIT_FAILED"

    ALL: Tuple[str, ...] = (
        GRAPH_NOT_FOUND, GRAPH_STALE, RENDER_FAILED,
        CANCELLED, NO_OUTPUT, SUBMIT_FAILED,
    )


class CompositeSubgraphError(Exception):
    """Composite Subgraph adapter 异常。"""

    def __init__(self, *, code: str, message: str) -> None:
        if code not in CompositeSubgraphErrorCode.ALL:
            raise ValueError(f"invalid CompositeSubgraphErrorCode: {code!r}")
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
# CompositeSubgraphRequest / CompositeSubgraphResult
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class CompositeSubgraphRequest:
    """渲染入参（plan §9.3）。

    红线：``input_asset_ids`` 是 asset_ref 字符串，**不是** texture。

    Attributes:
        request_id: 唯一请求 ID。
        graph_ref: 引用的合成图。
        input_asset_ids: 输入素材 ID（asset_ref）。
        selection_set_id: 选择集 ID（可选；plan §3.1 selection_set）。
        quality: 渲染质量。
        range_spec: 渲染范围。
        range_in_frame: 自定义范围起帧（range_spec=CUSTOM 时用）。
        range_out_frame: 自定义范围止帧。
        import_strategy: 结果导入策略（不直接进 timeline）。
    """

    request_id: str
    graph_ref: CompositeGraphRef
    input_asset_ids: Tuple[str, ...] = ()
    selection_set_id: str = ""
    quality: str = CompositeRenderQuality.PROXY
    range_spec: str = CompositeRenderRange.SELECTION
    range_in_frame: int = 0
    range_out_frame: int = 0
    import_strategy: str = CompositeImportStrategy.IMPORT_AS_GENERATION

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        # 至少要有输入（asset_refs / selection_set）或显式 whole_timeline
        if not self.input_asset_ids and not self.selection_set_id:
            if self.range_spec != CompositeRenderRange.WHOLE:
                raise ValueError(
                    "input is empty: provide input_asset_ids / selection_set_id, "
                    "or set range_spec=WHOLE to render the whole timeline"
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "graph_ref": self.graph_ref.to_dict(),
            "input_asset_ids": list(self.input_asset_ids),
            "selection_set_id": self.selection_set_id,
            "quality": self.quality,
            "range_spec": self.range_spec,
            "range_in_frame": self.range_in_frame,
            "range_out_frame": self.range_out_frame,
            "import_strategy": self.import_strategy,
        }


@dataclass(frozen=True)
class CompositeSubgraphResult:
    """渲染出参（plan §9.3 #4/#5）。"""

    request_id: str
    succeeded: bool
    output_asset_id: str = ""
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "succeeded": self.succeeded,
            "output_asset_id": self.output_asset_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


# ---------------------------------------------------------------------- #
# FakeCompositeRenderService —— mock render service
# ---------------------------------------------------------------------- #


@dataclass
class _FakeRenderJob:
    """FakeCompositeRenderService 内部 job 记录。"""

    render_job_id: str
    composite_graph_id: str
    graph_content_hash: str
    quality: str
    range_spec: str
    state: str = CompositeRenderJobState.QUEUED
    progress_fraction: float = 0.0
    progress_message: str = ""
    output_asset_id: str = ""
    fail_reason: str = ""
    submit_time_iso: str = ""


class FakeCompositeRenderService:
    """mock Composite render service（plan §9.3 #3）。

    不依赖真实 GPU / OpenGL / moderngl。生产环境替换为真实 Composite render service，
    接口一致（``submit`` / ``advance_to_running`` / ``complete_with_output`` / ``fail`` /
    ``cancel`` / ``get_job`` / ``get_output``）。

    Args:
        available_graph_hashes: 已注册的合成图 content_hash 集合（graph_id → hash 由调用方
            自行映射；这里只验证 hash 是否存在 + 是否匹配）。
        graph_id_to_hash: graph_id → content_hash 映射（用于 GRAPH_STALE 校验）。
    """

    def __init__(
        self,
        *,
        available_graph_hashes: Optional[FrozenSet[str]] = None,
        graph_id_to_hash: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._available = available_graph_hashes or frozenset()
        self._graph_id_to_hash: Dict[str, str] = dict(graph_id_to_hash or {})
        self._jobs: Dict[str, _FakeRenderJob] = {}

    @property
    def job_count(self) -> int:
        return len(self._jobs)

    def get_job(self, render_job_id: str) -> CompositeRenderJobRef:
        j = self._jobs[render_job_id]
        return self._to_ref(j)

    def get_output(self, render_job_id: str) -> str:
        """SUCCESS job 的输出 asset_id（公开结果查询面；AUD-23108）。

        adapter 的 ``fetch_result`` 只经此公开方法取输出，不穿透 ``_jobs``
        私有结构——生产 render service 只需实现同一公开签名即可替换 Fake。
        未知 job id 抛 KeyError（与 ``get_job`` 一致）。
        """
        j = self._jobs[render_job_id]
        return j.output_asset_id

    # ---------------- submit ---------------- #

    def submit(self, req: CompositeSubgraphRequest) -> CompositeRenderJobRef:
        graph_id = req.graph_ref.composite_graph_id
        req_hash = req.graph_ref.content_hash
        # 1. 优先用 graph_id_to_hash 映射做精确校验（GRAPH_STALE）
        if graph_id in self._graph_id_to_hash:
            server_hash = self._graph_id_to_hash[graph_id]
            if server_hash != req_hash:
                raise CompositeSubgraphError(
                    code=CompositeSubgraphErrorCode.GRAPH_STALE,
                    message=(
                        f"composite graph {graph_id!r} is stale "
                        f"(expected hash {server_hash[:16]}…, "
                        f"got {req_hash[:16]}…)"
                    ),
                )
        else:
            # 2. 没有 id→hash 映射时，用 available_hash 集合做存在性校验
            if req_hash not in self._available:
                raise CompositeSubgraphError(
                    code=CompositeSubgraphErrorCode.GRAPH_NOT_FOUND,
                    message=(
                        f"composite graph {graph_id!r} "
                        f"(hash={req_hash[:16]}…) not found on server"
                    ),
                )
        render_job_id = f"render-{uuid.uuid4().hex[:12]}"
        submit_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        job = _FakeRenderJob(
            render_job_id=render_job_id,
            composite_graph_id=graph_id,
            graph_content_hash=req_hash,
            quality=req.quality,
            range_spec=req.range_spec,
            submit_time_iso=submit_iso,
        )
        self._jobs[render_job_id] = job
        return self._to_ref(job)

    # ---------------- 进度 ---------------- #

    def advance_to_running(self, render_job_id: str) -> CompositeRenderJobRef:
        j = self._jobs[render_job_id]
        j.state = CompositeRenderJobState.RUNNING
        j.progress_fraction = 0.5
        j.progress_message = "rendering composite"
        return self._to_ref(j)

    def complete_with_output(
        self, render_job_id: str, *, output_asset_id: str,
    ) -> Tuple[CompositeRenderJobRef, str]:
        j = self._jobs[render_job_id]
        j.state = CompositeRenderJobState.SUCCESS
        j.progress_fraction = 1.0
        j.progress_message = "done"
        j.output_asset_id = output_asset_id
        return self._to_ref(j), output_asset_id

    def fail(self, render_job_id: str, *, reason: str) -> CompositeRenderJobRef:
        j = self._jobs[render_job_id]
        j.state = CompositeRenderJobState.FAILED
        j.fail_reason = reason
        return self._to_ref(j)

    def cancel(self, render_job_id: str) -> CompositeRenderJobRef:
        j = self._jobs[render_job_id]
        if j.state in CompositeRenderJobState.TERMINAL:
            return self._to_ref(j)  # no-op
        j.state = CompositeRenderJobState.CANCELLED
        return self._to_ref(j)

    @staticmethod
    def _to_ref(j: _FakeRenderJob) -> CompositeRenderJobRef:
        return CompositeRenderJobRef(
            render_job_id=j.render_job_id,
            composite_graph_id=j.composite_graph_id,
            graph_content_hash=j.graph_content_hash,
            quality=j.quality,
            range_spec=j.range_spec,
            state=j.state,
            progress_fraction=j.progress_fraction,
            progress_message=j.progress_message,
            submit_time_iso=j.submit_time_iso,
        )


# ---------------------------------------------------------------------- #
# CompositeSubgraphAdapter
# ---------------------------------------------------------------------- #


class CompositeSubgraphAdapter:
    """把 Composite 渲染流程包装为 workflow 节点可调用的 adapter（plan §9.3）。

    生产环境把 :class:`FakeCompositeRenderService` 换成真实 Composite render service。
    """

    def __init__(self, *, render_service: Any) -> None:
        # render_service duck-types FakeCompositeRenderService 的公开面
        # （submit / cancel / get_job / get_output）
        self._svc = render_service

    def submit_render(self, req: CompositeSubgraphRequest) -> CompositeRenderJobRef:
        try:
            return self._svc.submit(req)
        except CompositeSubgraphError:
            raise
        except Exception as e:
            raise CompositeSubgraphError(
                code=CompositeSubgraphErrorCode.SUBMIT_FAILED,
                message=f"render submit failed: {e}",
            ) from e

    def poll_render(self, ref: CompositeRenderJobRef) -> CompositeRenderJobRef:
        return self._svc.get_job(ref.render_job_id)

    def fetch_result(
        self, req: CompositeSubgraphRequest, ref: CompositeRenderJobRef,
    ) -> CompositeSubgraphResult:
        latest = self._svc.get_job(ref.render_job_id)
        if latest.state == CompositeRenderJobState.FAILED:
            return CompositeSubgraphResult(
                request_id=req.request_id, succeeded=False,
                error_code=CompositeSubgraphErrorCode.RENDER_FAILED,
                error_message="composite render failed",
            )
        if latest.state == CompositeRenderJobState.CANCELLED:
            return CompositeSubgraphResult(
                request_id=req.request_id, succeeded=False,
                error_code=CompositeSubgraphErrorCode.CANCELLED,
                error_message="composite render cancelled",
            )
        if latest.state != CompositeRenderJobState.SUCCESS:
            return CompositeSubgraphResult(
                request_id=req.request_id, succeeded=False,
                error_code=CompositeSubgraphErrorCode.RENDER_FAILED,
                error_message=f"render not ready (state={latest.state})",
            )
        # SUCCESS：取 output_asset_id。AUD-23108：只走服务的**公开**结果接口
        # ``get_output``（FakeCompositeRenderService 与生产 render service 的
        # 鸭子类型契约面），不再穿透 ``_jobs`` 私有字典。
        out = self._svc.get_output(ref.render_job_id)
        if not out:
            return CompositeSubgraphResult(
                request_id=req.request_id, succeeded=False,
                error_code=CompositeSubgraphErrorCode.NO_OUTPUT,
                error_message="render produced no output",
            )
        return CompositeSubgraphResult(
            request_id=req.request_id, succeeded=True,
            output_asset_id=out,
        )

    def cancel_render(self, ref: CompositeRenderJobRef) -> CompositeRenderJobRef:
        return self._svc.cancel(ref.render_job_id)


# ---------------------------------------------------------------------- #
# Composite Subgraph / Render / Import 节点 spec
# ---------------------------------------------------------------------- #


def _input_port(name: str, atype: str, **kw: Any) -> NodePortSpec:
    return NodePortSpec(name=name, artifact_type=atype, direction="input", **kw)


def _output_port(name: str, atype: str, **kw: Any) -> NodePortSpec:
    return NodePortSpec(name=name, artifact_type=atype, direction="output", **kw)


def _composite_node_specs() -> Tuple[WorkflowNodeSpec, ...]:
    """Composite 相关节点 spec（plan §4.2 M6-M10）。

    红线：
    - 输入/输出端口**不含** texture / TextureResult（plan §1.4.1）。
    - 所有 Composite 节点 ``timeline_write_required=False``（不直接写 timeline）。
    - ``executor_id=RENDER``（受控；不含任意代码执行）。
    """
    return (
        # Composite Subgraph：引用已保存合成图，输入 asset_ref，输出 render_job_ref
        WorkflowNodeSpec(
            spec_id="topos.composite.subgraph",
            display_name="Composite Subgraph", category=NodeCategory.GENERATION,
            description="引用已保存合成图，输入 asset_ref，输出 render_job_ref（不传 texture）",
            inputs=(
                _input_port("assets", "asset_set"),
                _input_port("composite_graph", "composite_graph_ref"),
                _input_port("selection", "selection_set", required=False),
            ),
            outputs=(_output_port("render_job", "render_job_ref"),),
            required_capabilities=(),
            executor_id=NodeExecutorId.RENDER, resource_lane="render",
            estimated_cost=0.5,
            params_schema={"type": "object", "properties": {
                "quality": {"type": "string", "enum": list(CompositeRenderQuality.ALL)},
                "range_spec": {"type": "string", "enum": list(CompositeRenderRange.ALL)},
                "import_strategy": {
                    "type": "string",
                    "enum": list(CompositeImportStrategy.ALL),
                },
            }},
            default_params={
                "quality": CompositeRenderQuality.PROXY,
                "range_spec": CompositeRenderRange.WHOLE,
                "import_strategy": CompositeImportStrategy.IMPORT_AS_GENERATION,
            },
            cancelable=True, retry_policy="fail",
        ),
        # Render Composite：执行渲染（把 render_job_ref 推进到 SUCCESS）
        WorkflowNodeSpec(
            spec_id="topos.composite.render",
            display_name="Render Composite", category=NodeCategory.OUTPUT,
            description="执行合成渲染 Job（不直接写 timeline；输出 render_job_ref / asset_ref）",
            inputs=(
                _input_port("render_job", "render_job_ref"),
                _input_port("composite_graph", "composite_graph_ref", required=False),
            ),
            outputs=(_output_port("render_job", "render_job_ref"),
                     _output_port("rendered_asset", "asset_ref")),
            required_capabilities=(),
            executor_id=NodeExecutorId.RENDER, resource_lane="render",
            estimated_cost=1.0,
            cancelable=True, retry_policy="fail",
        ),
        # Import Render Result：渲染结果进媒体池候选（仍需 approval 才进 timeline）
        WorkflowNodeSpec(
            spec_id="topos.composite.import_render_result",
            display_name="Import Render Result", category=NodeCategory.OUTPUT,
            description="渲染结果导入媒体池候选（不直接写 timeline；需后续 approval）",
            inputs=(_input_port("rendered_asset", "asset_ref"),),
            outputs=(_output_port("asset", "asset_ref"),
                     _output_port("report", "report")),
            required_capabilities=(),
            executor_id=NodeExecutorId.READ_ONLY, resource_lane="control",
            cancelable=False, retry_policy="fail",
        ),
    )


def register_composite_node_specs(registry: NodeSpecRegistry) -> None:
    """注册 Composite 相关节点 spec 到 :class:`NodeSpecRegistry`。"""
    for spec in _composite_node_specs():
        registry.register(spec)
