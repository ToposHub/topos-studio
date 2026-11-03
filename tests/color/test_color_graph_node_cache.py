"""N06：Node Cache Auto/On/Off（per-node 输出缓存：cone 键 + 预算 LRU +
世代老化 + 命中事件）。

验收（计划 N06）：改上游/自身/资源/尺寸后失效，下游选择不误失效；
Off 无缓存；越预算可解释；删除/取消释放；不得以蓝条代替实际缓存命中。
"""
from __future__ import annotations

from dataclasses import replace as _replace

import numpy as np
import pytest

pytest.importorskip("moderngl")

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
)
from src.shared.project.color_graph import (
    ColorEdgeData, ColorGraphData, ColorNodeData, NodeType,
)


def _node(node_id: str, node_type: NodeType, *, params=None,
          legacy=None) -> ColorNodeData:
    return ColorNodeData(node_id=node_id, node_type=node_type.value,
                         params=params or {}, legacy_payload=legacy or {})


def _corrector(node_id: str, exposure: float = 0.0, *,
               cache_mode: str | None = None) -> ColorNodeData:
    legacy = {"id": node_id,
              "grading_params": {"exposure": exposure}}
    params = {"cache_mode": cache_mode} if cache_mode else {}
    return _node(node_id, NodeType.CORRECTOR, params=params, legacy=legacy)


def _fork_graph(a_cache: str | None = None, c_exposure: float = 0.1):
    """source → A ─base→ PM → output；A ─branch→ PM（A consumers=2）。"""
    nodes = [
        _node("src", NodeType.SOURCE),
        _corrector("a", 0.2, cache_mode=a_cache),
        _corrector("c", c_exposure),
        _node("pm", NodeType.PARALLEL_MIXER),
        _node("out", NodeType.OUTPUT),
    ]
    edges = [
        ColorEdgeData("e1", "src", "image", "a", "image"),
        ColorEdgeData("e2", "a", "image", "pm", "base"),
        ColorEdgeData("e3", "a", "image", "pm", "branch"),
        ColorEdgeData("e4", "pm", "image", "out", "image"),
    ]
    # c 不在输出锥内（构造上游/独立分支用变体图）
    return ColorGraphData(nodes=tuple(nodes), edges=tuple(edges),
                          source_node_id="src", output_node_id="out")


def _serial_graph(exposure: float = 0.2) -> ColorGraphData:
    nodes = [_node("src", NodeType.SOURCE),
             _corrector("a", exposure),
             _node("out", NodeType.OUTPUT)]
    edges = [ColorEdgeData("e1", "src", "image", "a", "image"),
             ColorEdgeData("e2", "a", "image", "out", "image")]
    return ColorGraphData(nodes=tuple(nodes), edges=tuple(edges),
                          source_node_id="src", output_node_id="out")


def _branch_graph(c_exposure: float = 0.1) -> ColorGraphData:
    """source → A → C（串行；改 C 只动 C 键——cone 独立性对照）。"""
    nodes = [_node("src", NodeType.SOURCE),
             _corrector("a", 0.2),
             _corrector("c", c_exposure),
             _node("out", NodeType.OUTPUT)]
    edges = [ColorEdgeData("e1", "src", "image", "a", "image"),
             ColorEdgeData("e2", "a", "image", "c", "image"),
             ColorEdgeData("e3", "c", "image", "out", "image")]
    return ColorGraphData(nodes=tuple(nodes), edges=tuple(edges),
                          source_node_id="src", output_node_id="out")


def _passes(graph):
    plan = compile_graph(freeze_graph(graph, session_id="s"),
                         size=(32, 32))
    return {p.node_id: p for p in plan.passes}


# ── cone 键语义 ─────────────────────────────────────────────────────────


class TestConeKeys:
    def test_upstream_change_invalidates_downstream_only_chain(self):
        base = _passes(_branch_graph())
        changed = _passes(_branch_graph(c_exposure=0.9))
        assert base["c"].cone_key != changed["c"].cone_key
        assert base["a"].cone_key == changed["a"].cone_key
        assert base["src"].cone_key == changed["src"].cone_key

    def test_self_change_only_own_key(self):
        graph = _branch_graph()
        nodes = list(graph.nodes)
        a = next(n for n in nodes if n.node_id == "a")
        patched = _replace(a, legacy_payload={
            **dict(a.legacy_payload),
            "grading_params": {"exposure": 0.7}})
        graph2 = _replace(graph, nodes=tuple(
            patched if n.node_id == "a" else n for n in nodes))
        base, changed = _passes(graph), _passes(graph2)
        assert base["a"].cone_key != changed["a"].cone_key
        assert base["c"].cone_key != changed["c"].cone_key  # 上游变→下游变
        # 反向：只改 c 时 a 不变（上面已证）；此处证 a 改不波及 src
        assert base["src"].cone_key == changed["src"].cone_key

    def test_policy_keys_excluded_from_identity(self):
        graph = _branch_graph()
        nodes = list(graph.nodes)
        a = next(n for n in nodes if n.node_id == "a")
        patched = _replace(a, params={**dict(a.params),
                                      "cache_mode": "on"})
        graph2 = _replace(graph, nodes=tuple(
            patched if n.node_id == "a" else n for n in nodes))
        # 切缓存策略/锁/色不改变结果缓存身份（不误失效）
        assert _passes(graph)["a"].cone_key == _passes(graph2)["a"].cone_key

    def test_resource_change_changes_key(self):
        graph = _branch_graph()
        nodes = list(graph.nodes)
        a = next(n for n in nodes if n.node_id == "a")
        patched = _replace(a, params={
            **dict(a.params),
            "resources": {"lut_ref": "topos-res://lut_x",
                          "lut_strength": 0.5}})
        graph2 = _replace(graph, nodes=tuple(
            patched if n.node_id == "a" else n for n in nodes))
        assert _passes(graph)["a"].cone_key != _passes(graph2)["a"].cone_key


# ── 执行器缓存行为 ──────────────────────────────────────────────────────


class TestExecutorNodeCache:
    def _ctx(self):
        import moderngl
        return moderngl.create_standalone_context(require=330)

    def _run(self, ctx, graph, executor, rid, *, cache_context=("s",),
             size=(32, 32), counters=None):
        plan = compile_graph(freeze_graph(graph, session_id="s"),
                             size=size)
        src = ctx.texture((size[0], size[1]), 4, dtype="f2")

        def provider(node_id):
            if counters is not None:
                counters[node_id] = counters.get(node_id, 0) + 1
            return graph_node_layer_provider(graph)(node_id)
        try:
            return executor.execute_plan(
                plan, src, request_id=rid, source_size=size,
                layer_param_provider=provider,
                cache_context=cache_context)
        finally:
            src.release()

    def test_forked_auto_node_hits_on_second_run(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _fork_graph()
            counters: dict = {}
            first = self._run(ctx, graph, executor, "r1", counters=counters)
            assert first.published
            assert counters.get("a") == 1
            kinds = {e[1] for e in first.cache_events}
            assert "stored" in kinds  # A（consumers=2）已入缓存
            second = self._run(ctx, graph, executor, "r2", counters=counters)
            assert second.published
            hits = {e[0] for e in second.cache_events if e[1] == "hit"}
            assert "a" in hits
            assert counters.get("a") == 1  # 第二帧 A 未重算
            assert second.executed_passes < first.executed_passes
            executor.release_pool()
        finally:
            ctx.release()

    def test_off_policy_never_caches(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _fork_graph(a_cache="off")
            counters: dict = {}
            for rid in ("r1", "r2"):
                result = self._run(ctx, graph, executor, rid,
                                   counters=counters)
                assert result.published
            assert counters.get("a") == 2  # 每帧重算
            assert executor.node_cache_stats["policy_off"] >= 2
            assert len(executor._node_cache) == 0  # Off 无缓存条目
            executor.release_pool()
        finally:
            ctx.release()

    def test_on_policy_caches_single_consumer(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _serial_graph()
            nodes = list(graph.nodes)
            a = next(n for n in nodes if n.node_id == "a")
            graph = _replace(graph, nodes=tuple(
                _replace(a, params={**dict(a.params), "cache_mode": "on"})
                if n.node_id == "a" else n for n in nodes))
            # 串行图 A 是最终 pass 前一节——非 final，可缓存
            counters: dict = {}
            self._run(ctx, graph, executor, "r1", counters=counters)
            self._run(ctx, graph, executor, "r2", counters=counters)
            assert counters.get("a") == 1  # On：单消费者也缓存
            executor.release_pool()
        finally:
            ctx.release()

    def test_auto_skips_single_consumer(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _serial_graph()  # A consumers=1，auto → 不缓存
            counters: dict = {}
            for rid in ("r1", "r2"):
                self._run(ctx, graph, executor, rid, counters=counters)
            assert counters.get("a") == 2
            assert executor.node_cache_stats["auto_skipped"] >= 2
            executor.release_pool()
        finally:
            ctx.release()

    def test_source_content_context_invalidates(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _fork_graph()
            counters: dict = {}
            self._run(ctx, graph, executor, "r1",
                      cache_context=("rev0",), counters=counters)
            self._run(ctx, graph, executor, "r2",
                      cache_context=("rev1",), counters=counters)
            assert counters.get("a") == 2  # 源内容变→全失效（不读旧帧）
            executor.release_pool()
        finally:
            ctx.release()

    def test_upstream_param_change_invalidates_entry(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            counters: dict = {}
            self._run(ctx, _branch_graph(), executor, "r1",
                      counters=counters)
            self._run(ctx, _branch_graph(), executor, "r2",
                      counters=counters)
            assert counters.get("a") == 2  # auto + 单消费者：不缓存，逐帧重算
            # 用 on 强制缓存验证失效链
            graph = _branch_graph()
            nodes = list(graph.nodes)
            a = next(n for n in nodes if n.node_id == "a")
            graph_on = _replace(graph, nodes=tuple(
                _replace(a, params={**dict(a.params), "cache_mode": "on"})
                if n.node_id == "a" else n for n in nodes))
            self._run(ctx, graph_on, executor, "r3", counters=counters)
            self._run(ctx, graph_on, executor, "r4", counters=counters)
            assert counters.get("a") == 3  # r1/r2 重算 + r3 重算入缓存，r4 命中
            # 改 c（下游）不影响 a 命中
            graph_c = _replace(graph_on, nodes=tuple(
                _replace(n, legacy_payload={
                    **dict(n.legacy_payload),
                    "grading_params": {"exposure": 0.9}})
                if n.node_id == "c" else n for n in graph_on.nodes))
            before = counters.get("a")
            self._run(ctx, graph_c, executor, "r5", counters=counters)
            assert counters.get("a") == before  # a 仍命中
            # 改 a 自身 → a 失效重算
            graph_a = _replace(graph_on, nodes=tuple(
                _replace(a, legacy_payload={
                    **dict(a.legacy_payload),
                    "grading_params": {"exposure": 0.8}})
                if n.node_id == "a" else n for n in graph_on.nodes))
            self._run(ctx, graph_a, executor, "r6", counters=counters)
            assert counters.get("a") == before + 1
            executor.release_pool()
        finally:
            ctx.release()

    def test_budget_oversize_rejected_and_evictions_explained(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(
                ctx, node_cache_budget_bytes=32 * 32 * 8)  # 恰一条 f2
            graph = _fork_graph()
            result = self._run(ctx, graph, executor, "r1")
            kinds = {e[1] for e in result.cache_events}
            assert "stored" in kinds
            # 第二条（c/pm 不可入——cone 外/最终 pass；用另一图再存一条
            # 触发预算淘汰）
            graph2 = _branch_graph()
            nodes = list(graph2.nodes)
            a = next(n for n in nodes if n.node_id == "a")
            # 键与首图不同（exposure 不同）→ 必然新存一条触发预算淘汰
            graph2_on = _replace(graph2, nodes=tuple(
                _replace(a, params={**dict(a.params), "cache_mode": "on"},
                         legacy_payload={
                             **dict(a.legacy_payload),
                             "grading_params": {"exposure": 0.25}})
                if n.node_id == "a" else n for n in nodes))
            result2 = self._run(ctx, graph2_on, executor, "r2")
            assert (executor.node_cache_stats["evicted_budget"] >= 1
                    or "oversize" in {e[1] for e in result2.cache_events})
            assert executor._node_cache_bytes <= executor._node_cache_budget
            executor.release_pool()
        finally:
            ctx.release()

    def test_stale_entry_ages_out_and_release_pool_frees(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _branch_graph()
            nodes = list(graph.nodes)
            a = next(n for n in nodes if n.node_id == "a")
            graph_on = _replace(graph, nodes=tuple(
                _replace(a, params={**dict(a.params), "cache_mode": "on"})
                if n.node_id == "a" else n for n in nodes))
            self._run(ctx, graph_on, executor, "seed")
            assert len(executor._node_cache) == 1
            # 连续执行不含该节点的其他图 → 世代老化淘汰（删除场景）
            other = _serial_graph()
            for i in range(executor._NODE_CACHE_IDLE_GENERATIONS + 1):
                self._run(ctx, other, executor, f"other-{i}")
            assert len(executor._node_cache) == 0
            assert executor.node_cache_stats["swept_stale"] >= 1
            # 再存一条后 release_pool 全释放
            self._run(ctx, graph_on, executor, "seed2")
            assert len(executor._node_cache) == 1
            executor.release_pool()
            assert len(executor._node_cache) == 0
        finally:
            ctx.release()

    def test_final_pass_never_cached_and_hit_does_not_break_output(self):
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _fork_graph()
            outs = []
            for rid in ("r1", "r2"):
                result = self._run(ctx, graph, executor, rid)
                assert result.published
                fbo = ctx.framebuffer(
                    color_attachments=[result.output_texture])
                fbo.use()
                data = np.frombuffer(
                    fbo.read(components=4, dtype="f2"),
                    dtype=np.float16).reshape(32, 32, 4).astype(np.float32)
                outs.append(data)
                executor.release_revision(rid)
            # 命中帧与重算帧位级一致（缓存不改变输出）
            np.testing.assert_array_equal(outs[0], outs[1])
            # 最终 pass（pm）不在缓存
            assert all(key[1] != "pm" for key in executor._node_cache)
            executor.release_pool()
        finally:
            ctx.release()
