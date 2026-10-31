"""P6.4 测试：Composite Subgraph / Render / Import adapter（plan §9.3 / M8）。

> 一个 ``Composite Subgraph`` 节点引用已有 ``CompositeGraphData``，输入为已批准的媒体
> 或时间线片段，输出为 render job / 生成媒体 artifact。它不共享 GPU texture，也不允许
> AI workflow 直接修改 Composite 内部节点。

验证 :class:`CompositeSubgraphAdapter` 的核心契约（plan §9.3 / §1.4.1 / §1.4.3）：

- ``CompositeGraphRef``：已保存合成图的引用（``composite_graph_id`` + schema/version）。
- ``CompositeRenderJobRef``：合成渲染 Job 引用（不持有 GPU texture / OpenGL 对象）。
- ``CompositeSubgraphRequest``：渲染入参（asset_ref / selection_set 输入，**不是** texture）。
- :class:`CompositeSubgraphAdapter`：
    - ``submit_render``：调用 Composite render service 创建 render job → ``CompositeRenderJobRef``。
    - ``poll_render``：拉 render job 状态。
    - ``fetch_result``：render SUCCESS → 输出 asset_ref（导入媒体池候选）。
    - ``cancel_render``：取消传播。
- :class:`FakeCompositeRenderService`：mock render service（不依赖真实 GPU / OpenGL）。

红线（plan §9.3 / §1.4.1 / §1.4.3 / §0）：

- **不跨图传 texture**：Composite Subgraph 节点输入是 ``asset_ref`` / ``selection_set``，
  不是 GPU texture；输出是 ``render_job_ref`` / ``asset_ref``，不是 texture。
- **render 生命周期由 Composite 管**：AI workflow 不持有 OpenGL context；不直接修改
  Composite 内部节点。
- **取消不泄漏**：取消 render job → 释放 GPU/临时资源（adapter 转发取消；不污染 timeline）。
- **不直接写 timeline**：导入媒体池仍走单独 approval 链（生成节点没有插轨权限）。
- **CompositeGraphRef schema/version 校验**：引用过期的合成图 → 拒绝渲染。
- 不依赖 moderngl / PyQt6（adapter 是纯 Python）。
"""
from __future__ import annotations

import pytest

from src.features.ai_workflow.adapters.composite_adapter import (
    CompositeGraphRef,
    CompositeImportStrategy,
    CompositeRenderJobRef,
    CompositeRenderJobState,
    CompositeRenderQuality,
    CompositeRenderRange,
    CompositeSubgraphAdapter,
    CompositeSubgraphError,
    CompositeSubgraphErrorCode,
    CompositeSubgraphRequest,
    FakeCompositeRenderService,
    register_composite_node_specs,
)
from src.features.ai_workflow.core.node_specs import (
    NodeExecutorId,
    NodeSpecRegistry,
)


# ---------------------------------------------------------------------- #
# 共享 fixture / helper（AUD-23561：集中重复定义）
# ---------------------------------------------------------------------- #


def _graph_ref() -> CompositeGraphRef:
    """两个测试类共用的合法 graph 引用。"""
    return CompositeGraphRef(
        composite_graph_id="comp-001", schema_version="1",
        content_hash="sha256:abc",
    )


def _render_service() -> FakeCompositeRenderService:
    return FakeCompositeRenderService(
        available_graph_hashes=frozenset({"sha256:abc"}),
    )


@pytest.fixture()
def composite_registry() -> NodeSpecRegistry:
    """注册好 Composite spec 的 registry（规格注册测试共用）。"""
    reg = NodeSpecRegistry()
    register_composite_node_specs(reg)
    return reg


# ---------------------------------------------------------------------- #
# CompositeGraphRef
# ---------------------------------------------------------------------- #


class TestCompositeGraphRef:
    def test_construction(self) -> None:
        ref = CompositeGraphRef(
            composite_graph_id="comp-001",
            schema_version="1",
            content_hash="sha256:abc",
            display_name="My Composite",
        )
        assert ref.composite_graph_id == "comp-001"
        assert ref.schema_version == "1"
        assert ref.content_hash == "sha256:abc"

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="composite_graph_id"):
            CompositeGraphRef(
                composite_graph_id="",
                schema_version="1", content_hash="sha256:abc",
            )

    def test_to_dict_roundtrip(self) -> None:
        ref = CompositeGraphRef(
            composite_graph_id="comp-001", schema_version="1",
            content_hash="sha256:abc", display_name="X",
        )
        ref2 = CompositeGraphRef.from_dict(ref.to_dict())
        assert ref2 == ref

    def test_does_not_hold_texture_or_opengl_objects(self) -> None:
        # 红线：CompositeGraphRef 只引用 graph_id，不持有任何 GPU/Qt 对象
        ref = CompositeGraphRef(
            composite_graph_id="c", schema_version="1", content_hash="h",
        )
        d = ref.to_dict()
        for k in d:
            assert "texture" not in k.lower()
            assert "opengl" not in k.lower()
            assert "context" not in k.lower()


# ---------------------------------------------------------------------- #
# CompositeRenderJobRef / CompositeRenderJobState
# ---------------------------------------------------------------------- #


class TestCompositeRenderJobRef:
    def test_state_controlled(self) -> None:
        assert CompositeRenderJobState.QUEUED == "queued"
        assert CompositeRenderJobState.RUNNING == "running"
        assert CompositeRenderJobState.SUCCESS == "success"
        assert CompositeRenderJobState.FAILED == "failed"
        assert CompositeRenderJobState.CANCELLED == "cancelled"

    def test_construction(self) -> None:
        ref = CompositeRenderJobRef(
            render_job_id="render-001",
            composite_graph_id="comp-001",
            graph_content_hash="sha256:abc",
            quality=CompositeRenderQuality.PROXY,
            range_spec=CompositeRenderRange.WHOLE,
        )
        assert ref.render_job_id == "render-001"
        assert ref.state == CompositeRenderJobState.QUEUED
        assert ref.is_terminal is False

    def test_terminal_states(self) -> None:
        for s in (CompositeRenderJobState.SUCCESS,
                  CompositeRenderJobState.FAILED,
                  CompositeRenderJobState.CANCELLED):
            ref = CompositeRenderJobRef(
                render_job_id="r", composite_graph_id="c",
                graph_content_hash="h", state=s,
            )
            assert ref.is_terminal is True

    def test_does_not_hold_gpu_objects(self) -> None:
        # 红线：RenderJobRef 只持有 job ID + 状态，不持有 texture / OpenGL context
        ref = CompositeRenderJobRef(
            render_job_id="r", composite_graph_id="c", graph_content_hash="h",
        )
        d = ref.to_dict()
        for k in d:
            assert "texture" not in k.lower()
            assert "opengl" not in k.lower()


# ---------------------------------------------------------------------- #
# CompositeSubgraphRequest
# ---------------------------------------------------------------------- #


class TestCompositeSubgraphRequest:
    def test_construction_with_asset_ref(self) -> None:
        req = CompositeSubgraphRequest(
            request_id="sub-001",
            graph_ref=CompositeGraphRef(
                composite_graph_id="comp-001", schema_version="1",
                content_hash="sha256:abc",
            ),
            input_asset_ids=("asset/clip-1", "asset/clip-2"),
            quality=CompositeRenderQuality.FULL,
            range_spec=CompositeRenderRange.WHOLE,
            import_strategy=CompositeImportStrategy.IMPORT_AS_GENERATION,
        )
        assert req.request_id == "sub-001"
        assert req.input_asset_ids == ("asset/clip-1", "asset/clip-2")
        assert req.graph_ref.composite_graph_id == "comp-001"

    def test_input_must_be_asset_refs_not_textures(self) -> None:
        """红线：输入是 asset_ref 字符串 ID，不是 texture（AUD-23560）。

        可证伪的观测面：请求跨边界的唯一出口是 ``to_dict()`` wire format。
        - 合法 asset_ref → 序列化后仍是字符串 ID 列表；
        - texture 形状对象（不可 JSON 序列化）无法穿过 wire format——构造期
          校验或序列化 TypeError 都是可接受的防线，"静默放行并原样携带"会被
          本测试暴露。
        """
        import json

        req = CompositeSubgraphRequest(
            request_id="sub-001",
            graph_ref=CompositeGraphRef(
                composite_graph_id="c", schema_version="1", content_hash="h",
            ),
            input_asset_ids=("asset/1",),
        )
        assert all(isinstance(a, str) for a in req.input_asset_ids)
        # wire format：ID 原样可序列化（协议边界只携带字符串 ID）
        wire = json.loads(json.dumps(req.to_dict()))
        assert wire["input_asset_ids"] == ["asset/1"]

        # 负例：texture 形状对象（模拟 moderngl.Texture 等不可序列化对象）
        texture_like = object()
        try:
            bad_req = CompositeSubgraphRequest(
                request_id="sub-texture",
                graph_ref=CompositeGraphRef(
                    composite_graph_id="c", schema_version="1", content_hash="h",
                ),
                input_asset_ids=(texture_like,),
            )
        except ValueError:
            # 实现侧在构造期拒绝非字符串输入（更强的防线）——同样满足红线
            pass
        else:
            # 构造虽放行，wire format 也必须无法携带 texture 对象
            with pytest.raises(TypeError):
                json.dumps(bad_req.to_dict())

    def test_request_id_required(self) -> None:
        with pytest.raises(ValueError, match="request_id"):
            CompositeSubgraphRequest(
                request_id="",
                graph_ref=CompositeGraphRef(
                    composite_graph_id="c", schema_version="1", content_hash="h",
                ),
            )

    def test_at_least_one_input_or_whole_timeline(self) -> None:
        # 至少要有输入（asset_refs / selection_set）或显式 whole_timeline
        with pytest.raises(ValueError, match="input"):
            CompositeSubgraphRequest(
                request_id="x",
                graph_ref=CompositeGraphRef(
                    composite_graph_id="c", schema_version="1", content_hash="h",
                ),
                input_asset_ids=(),  # 空 + whole_timeline=False
            )
        # 显式 whole_timeline=True 可以空输入
        req = CompositeSubgraphRequest(
            request_id="x",
            graph_ref=CompositeGraphRef(
                composite_graph_id="c", schema_version="1", content_hash="h",
            ),
            input_asset_ids=(),
            range_spec=CompositeRenderRange.WHOLE,
        )
        assert req.range_spec == CompositeRenderRange.WHOLE


# ---------------------------------------------------------------------- #
# FakeCompositeRenderService
# ---------------------------------------------------------------------- #


class TestFakeCompositeRenderService:
    def test_submit_returns_job_ref(self) -> None:
        svc = FakeCompositeRenderService(available_graph_hashes=frozenset({"sha256:abc"}))
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = svc.submit(req)
        assert isinstance(ref, CompositeRenderJobRef)
        assert ref.state == CompositeRenderJobState.QUEUED
        assert svc.job_count == 1

    def test_submit_rejects_unknown_graph(self) -> None:
        # 红线：引用不存在的合成图 → 拒绝
        svc = FakeCompositeRenderService(available_graph_hashes=frozenset())
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        with pytest.raises(CompositeSubgraphError) as ei:
            svc.submit(req)
        assert ei.value.code == CompositeSubgraphErrorCode.GRAPH_NOT_FOUND

    def test_submit_rejects_stale_graph_version(self) -> None:
        # 红线：引用过期的合成图（content_hash 不匹配）→ 拒绝
        # 用 graph_id_to_hash 映射，使 server 知道 comp-001 的正确 hash
        svc = FakeCompositeRenderService(
            available_graph_hashes=frozenset({"sha256:abc"}),
            graph_id_to_hash={"comp-001": "sha256:abc"},
        )
        # graph_ref 的 hash 与 server 不匹配
        req = CompositeSubgraphRequest(
            request_id="r",
            graph_ref=CompositeGraphRef(
                composite_graph_id="comp-001", schema_version="1",
                content_hash="sha256:different",
            ),
            input_asset_ids=("asset/1",),
        )
        with pytest.raises(CompositeSubgraphError) as ei:
            svc.submit(req)
        assert ei.value.code == CompositeSubgraphErrorCode.GRAPH_STALE

    def test_advance_and_complete(self) -> None:
        svc = FakeCompositeRenderService(available_graph_hashes=frozenset({"sha256:abc"}))
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = svc.submit(req)
        svc.advance_to_running(ref.render_job_id)
        ref2, output_asset_id = svc.complete_with_output(
            ref.render_job_id, output_asset_id="asset/render-001",
        )
        assert ref2.state == CompositeRenderJobState.SUCCESS
        assert output_asset_id == "asset/render-001"

    def test_cancel(self) -> None:
        svc = FakeCompositeRenderService(available_graph_hashes=frozenset({"sha256:abc"}))
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = svc.submit(req)
        cancelled = svc.cancel(ref.render_job_id)
        assert cancelled.state == CompositeRenderJobState.CANCELLED

    def test_cancel_terminal_job_no_op(self) -> None:
        svc = FakeCompositeRenderService(available_graph_hashes=frozenset({"sha256:abc"}))
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = svc.submit(req)
        svc.complete_with_output(ref.render_job_id, output_asset_id="asset/x")
        # 已 SUCCESS 的 job 不能再 cancel（no-op）
        ref2 = svc.cancel(ref.render_job_id)
        assert ref2.state == CompositeRenderJobState.SUCCESS


# ---------------------------------------------------------------------- #
# CompositeSubgraphAdapter
# ---------------------------------------------------------------------- #


class TestCompositeSubgraphAdapter:
    def test_submit_render(self) -> None:
        svc = _render_service()
        adapter = CompositeSubgraphAdapter(render_service=svc)
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = adapter.submit_render(req)
        assert ref.state in (CompositeRenderJobState.QUEUED,
                             CompositeRenderJobState.RUNNING)

    def test_poll_render(self) -> None:
        svc = _render_service()
        adapter = CompositeSubgraphAdapter(render_service=svc)
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = adapter.submit_render(req)
        svc.advance_to_running(ref.render_job_id)
        ref2 = adapter.poll_render(ref)
        assert ref2.state == CompositeRenderJobState.RUNNING

    def test_fetch_result_success_returns_asset_ref(self) -> None:
        svc = _render_service()
        adapter = CompositeSubgraphAdapter(render_service=svc)
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = adapter.submit_render(req)
        svc.advance_to_running(ref.render_job_id)
        svc.complete_with_output(ref.render_job_id, output_asset_id="asset/render-001")
        result = adapter.fetch_result(req, ref)
        assert result.succeeded
        assert result.output_asset_id == "asset/render-001"

    def test_fetch_result_failed(self) -> None:
        svc = _render_service()
        adapter = CompositeSubgraphAdapter(render_service=svc)
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = adapter.submit_render(req)
        svc.fail(ref.render_job_id, reason="GPU OOM")
        result = adapter.fetch_result(req, ref)
        assert not result.succeeded
        assert result.error_code == CompositeSubgraphErrorCode.RENDER_FAILED

    def test_cancel_render_propagates(self) -> None:
        svc = _render_service()
        adapter = CompositeSubgraphAdapter(render_service=svc)
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = adapter.submit_render(req)
        cancelled = adapter.cancel_render(ref)
        assert cancelled.state == CompositeRenderJobState.CANCELLED
        # server 侧也取消
        assert svc.get_job(ref.render_job_id).state == CompositeRenderJobState.CANCELLED

    def test_cancel_after_terminal_is_no_op_on_timeline(self) -> None:
        # 红线（§10.3.6）：取消后不产生半写入 timeline
        svc = _render_service()
        adapter = CompositeSubgraphAdapter(render_service=svc)
        req = CompositeSubgraphRequest(
            request_id="r", graph_ref=_graph_ref(),
            input_asset_ids=("asset/1",),
        )
        ref = adapter.submit_render(req)
        svc.fail(ref.render_job_id, reason="err")
        # 已 FAILED，cancel 是 no-op，不应抛 / 不应改状态
        ref2 = adapter.cancel_render(ref)
        assert ref2.state == CompositeRenderJobState.FAILED
        # fetch 也应返回失败（不产生半写入）
        result = adapter.fetch_result(req, ref2)
        assert not result.succeeded
        assert result.output_asset_id == ""  # 无输出


# ---------------------------------------------------------------------- #
# Composite Subgraph 节点 spec（注册到 NodeSpecRegistry）
# ---------------------------------------------------------------------- #


class TestCompositeNodeSpecs:
    def test_register_composite_specs(self, composite_registry: NodeSpecRegistry) -> None:
        reg = composite_registry

        # Composite Subgraph
        sub = reg.get("topos.composite.subgraph")
        assert sub is not None
        assert sub.executor_id == NodeExecutorId.RENDER
        # 输入是 asset_ref / selection_set，不是 texture
        input_types = {p.artifact_type for p in sub.input_ports}
        assert "asset_ref" in input_types or "asset_set" in input_types
        # 输出是 render_job_ref / asset_ref，不是 texture
        output_types = {p.artifact_type for p in sub.output_ports}
        assert "render_job_ref" in output_types or "asset_ref" in output_types
        # 红线：输出端口不应是 texture / TextureResult
        for t in output_types:
            assert "texture" not in t.lower()

    def test_render_composite_spec_registered(self, composite_registry: NodeSpecRegistry) -> None:
        spec = composite_registry.get("topos.composite.render")
        assert spec is not None
        assert spec.executor_id == NodeExecutorId.RENDER
        assert spec.resource_lane == "render"

    def test_import_render_result_spec_registered(self, composite_registry: NodeSpecRegistry) -> None:
        spec = composite_registry.get("topos.composite.import_render_result")
        assert spec is not None
        # 输出是 asset_ref（导入媒体池候选），不直接写 timeline
        out_types = {p.artifact_type for p in spec.output_ports}
        assert "asset_ref" in out_types

    def test_specs_do_not_touch_timeline_directly(self, composite_registry: NodeSpecRegistry) -> None:
        # 红线：Composite 节点不直接写 timeline
        for sid in ("topos.composite.subgraph", "topos.composite.render",
                    "topos.composite.import_render_result"):
            s = composite_registry.require(sid)
            # subgraph / render 节点不直接 timeline_write（只有 apply_approved_plan 才写）
            assert s.timeline_write_required is False
