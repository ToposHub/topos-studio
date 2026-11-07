"""N06：Node Cache Auto/On/Off（per-node 输出缓存：cone 键 + 预算 LRU +
世代老化 + 命中事件）。

验收（计划 N06）：改上游/自身/资源/尺寸后失效，下游选择不误失效；
Off 无缓存；越预算可解释；删除/取消释放；不得以蓝条代替实际缓存命中。
"""
from __future__ import annotations

from dataclasses import replace as _replace

from types import SimpleNamespace

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


# ── KF-06：时域标志（legacy 载荷起源 + 下游传播 + 缓存绕过）────────────


class TestTemporalAnimatedFlag:
    def _animated_graph(self, *, origin: str):
        """source → A（带关键帧）→ C → output；origin=legacy|params。"""
        nodes = [_node("src", NodeType.SOURCE),
                 _corrector("a", 0.2),
                 _corrector("c", 0.1),
                 _node("out", NodeType.OUTPUT)]
        a = nodes[1]
        kf = {"exposure": {"keyframes": [
            {"frame": 0, "value": 0.0, "interpolation": "linear"},
            {"frame": 24, "value": 0.5, "interpolation": "linear"}]}}
        if origin == "legacy":
            nodes[1] = _replace(
                a, legacy_payload={**dict(a.legacy_payload),
                                   "grade_keyframes": kf})
        else:
            nodes[1] = _replace(
                a, params={**dict(a.params), "grade_keyframes": kf})
        edges = [ColorEdgeData("e1", "src", "image", "a", "image"),
                 ColorEdgeData("e2", "a", "image", "c", "image"),
                 ColorEdgeData("e3", "c", "image", "out", "image")]
        return ColorGraphData(nodes=tuple(nodes), edges=tuple(edges),
                              source_node_id="src", output_node_id="out")

    @pytest.mark.parametrize("origin", ["legacy", "params"])
    def test_animation_flags_own_and_downstream_not_upstream(self, origin):
        # legacy 起源轨道在 node.params 不可见——判据必须覆盖载荷域
        passes = _passes(self._animated_graph(origin=origin))
        assert passes["a"].key_frame_animated is True
        assert passes["c"].key_frame_animated is True   # 时域下游
        assert passes["out"].key_frame_animated is True
        assert passes["src"].key_frame_animated is False  # 上游不受波及

    def test_plain_graph_flags_all_false(self):
        passes = _passes(_branch_graph())
        assert not any(p.key_frame_animated for p in passes.values())

    def test_tombstone_only_track_still_temporal(self):
        # 墓碑-only（显式空轨道）非空 Mapping：保守按真处理
        graph = self._animated_graph(origin="params")
        nodes = list(graph.nodes)
        nodes[1] = _replace(nodes[1], params={
            **dict(nodes[1].params),
            "grade_keyframes": {"exposure": {"keyframes": [],
                                             "tombstone": True}}})
        graph = _replace(graph, nodes=tuple(nodes))
        assert _passes(graph)["a"].key_frame_animated is True


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

    def test_legacy_animated_forked_node_bypasses_cache(self):
        """KF-06：legacy 载荷起源动画节点（params 无轨道域）即便
        consumers=2（auto 可缓存）也逐帧重算——静像 seek 不命中陈旧纹理。"""
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            base = _fork_graph()
            nodes = list(base.nodes)
            a = next(n for n in nodes if n.node_id == "a")
            patched = _replace(
                a, legacy_payload={
                    **dict(a.legacy_payload),
                    "grade_keyframes": {"exposure": {"keyframes": [
                        {"frame": 0, "value": 0.0,
                         "interpolation": "linear"}]}}})
            graph = _replace(base, nodes=tuple(
                patched if n.node_id == "a" else n for n in nodes))
            counters: dict = {}
            for rid in ("r1", "r2"):
                result = self._run(ctx, graph, executor, rid,
                                   counters=counters)
                assert result.published
            assert counters.get("a") == 2  # 无缓存条目 → 每帧重算
            assert len(executor._node_cache) == 0
            executor.release_pool()
        finally:
            ctx.release()

    def test_downstream_of_animated_bypasses_cache(self):
        """KF-06：时域上游的下游节点（cache_mode=on 强制可缓存）同样
        绕过——其输入随帧变，缓存键无帧身份会命中陈旧合成。"""
        ctx = self._ctx()
        try:
            executor = ColorGraphExecutor(ctx)
            nodes = list(_branch_graph().nodes)
            a = next(n for n in nodes if n.node_id == "a")
            c = next(n for n in nodes if n.node_id == "c")
            animated_a = _replace(
                a, legacy_payload={
                    **dict(a.legacy_payload),
                    "grade_keyframes": {"exposure": {"keyframes": [
                        {"frame": 0, "value": 0.0,
                         "interpolation": "linear"}]}}})
            forced_c = _replace(c, params={**dict(c.params),
                                           "cache_mode": "on"})
            graph = _replace(_branch_graph(), nodes=tuple(
                animated_a if n.node_id == "a"
                else forced_c if n.node_id == "c" else n
                for n in nodes))
            counters: dict = {}
            for rid in ("r1", "r2"):
                result = self._run(ctx, graph, executor, rid,
                                   counters=counters)
                assert result.published
            assert counters.get("c") == 2  # on 策略被时域上游覆盖
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

    def test_budget_eviction_defers_working_set_textures(self):
        """执行中预算淘汰命中工作集 → 延迟归还（不变式：被下游持有的
        纹理不进池——否则后续 _alloc_texture 取走当渲染目标，与下游
        采样同纹理 = GL 反馈环/串写）。单元级直接钉住该不变式。"""
        from collections import OrderedDict

        class _FakeTex:
            def __init__(self, name):
                self.name = name
                self.size = (32, 32)
                self.dtype = "f2"
                self.released = False

            def release(self):
                self.released = True

        class _Pass:
            def __init__(self, node_id, cone):
                self.node_id = node_id
                self.op = "corrector"
                self.cone_key = cone
                self.consumers = 1
                self.params = {"cache_mode": "on"}
                self.key_frame_animated = False

        ex = ColorGraphExecutor.__new__(ColorGraphExecutor)
        ex._pool = {}
        ex._node_cache = OrderedDict()
        ex._node_cache_bytes = 0
        ex._node_cache_budget = 32 * 32 * 8  # 恰一条 f2
        ex._run_dtype = "f2"
        ex._exec_generation = 1
        ex.node_cache_stats = {"hit": 0, "stored": 0,
                               "oversize_rejected": 0, "evicted_budget": 0,
                               "swept_stale": 0, "policy_off": 0,
                               "auto_skipped": 0}
        tex_a, tex_b = _FakeTex("a"), _FakeTex("b")
        assert ex._node_cache_store(_Pass("a", "cone-a"), tex_a, 32, 32,
                                    ("s",), [])
        # B 入缓存淘汰 A——A 仍在工作集（node_outputs 持有）
        deferred: list = []
        working_set = {"a": tex_a}
        events: list = []
        assert ex._node_cache_store(_Pass("b", "cone-b"), tex_b, 32, 32,
                                    ("s",), events,
                                    deferred_releases=deferred,
                                    working_set=working_set)
        # A 延迟（未进池、未销毁）；B 在缓存
        assert deferred == [tex_a]
        assert not tex_a.released
        assert all(tex_a not in free for free in ex._pool.values())
        assert ex._node_cache_bytes <= ex._node_cache_budget
        deferred_evictions = [e for e in events
                              if e[1] == "evicted_deferred"]
        assert deferred_evictions and deferred_evictions[0][0] == "a"
        assert deferred_evictions[0][3] == "working_set"
        # 工作集外的淘汰仍立即还池（可解释：budget 事件）
        tex_c = _FakeTex("c")
        events2: list = []
        assert ex._node_cache_store(_Pass("c", "cone-c"), tex_c, 32, 32,
                                    ("s",), events2,
                                    deferred_releases=[],
                                    working_set={"a": tex_a})
        assert any(e[1] == "evicted" and e[3] == "budget"
                   for e in events2)

    def test_budget_eviction_working_set_output_consistency(self):
        """端到端：预算恰一条、双缓存节点串行图——淘汰发生在执行中，
        输出仍与无缓存运行位级一致（延迟归还不改变渲染结果）。"""
        ctx = self._ctx()
        try:
            size = (32, 32)
            # src → a → c → out；a/c 强制 On；预算恰一条 → 存 c 淘汰 a
            nodes = [_node("src", NodeType.SOURCE),
                     _corrector("a", 0.2, cache_mode="on"),
                     _corrector("c", 0.1, cache_mode="on"),
                     _node("out", NodeType.OUTPUT)]
            edges = [ColorEdgeData("e1", "src", "image", "a", "image"),
                     ColorEdgeData("e2", "a", "image", "c", "image"),
                     ColorEdgeData("e3", "c", "image", "out", "image")]
            graph = ColorGraphData(nodes=tuple(nodes), edges=tuple(edges),
                                   source_node_id="src",
                                   output_node_id="out")

            def _pixels(executor, rid):
                result = self._run(ctx, graph, executor, rid, size=size)
                assert result.published
                fbo = ctx.framebuffer(
                    color_attachments=[result.output_texture])
                fbo.use()
                data = np.frombuffer(
                    fbo.read(components=4, dtype="f2"),
                    dtype=np.float16).reshape(size[1], size[0], 4)
                executor.release_revision(rid)
                return data, result

            cached = ColorGraphExecutor(
                ctx, node_cache_budget_bytes=32 * 32 * 8)
            with_cache, result = _pixels(cached, "rc")
            assert any(e[1] == "evicted_deferred" and e[0] == "a"
                       for e in result.cache_events)
            cached.release_pool()
            fresh = ColorGraphExecutor(
                ctx, node_cache_budget_bytes=0)  # 预算 0 = 全拒收
            without_cache, _ = _pixels(fresh, "rf")
            fresh.release_pool()
            np.testing.assert_array_equal(with_cache, without_cache)
        finally:
            ctx.release()


class TestGraphCacheContextIdentity:
    """R5.3：节点缓存键含任务逐帧身份（静态节点不跨视频帧命中旧输出）。"""

    @staticmethod
    def _context(params, revision=7, mask_epoch=None):
        from src.features.color.core.async_renderer import AsyncRenderer
        snapshot = SimpleNamespace(
            source_content_revision=revision,
            color_context_cache_key=lambda: "ctx-key",
        )
        return AsyncRenderer._graph_cache_context(
            snapshot, params, mask_epoch or {})

    def test_video_task_frame_enters_key(self):
        base = {"color_output_target": "front"}
        # 播放期缓存参数携带冻结快照（revision 恒定）；任务边界写入的
        # 实际解码帧使静态节点的缓存键逐帧失效
        assert (self._context({**base, "stabilization_source_frame": 112})
                != self._context({**base, "stabilization_source_frame": 113}))
        assert (self._context({**base, "stabilization_source_frame": 113})
                == self._context({**base, "stabilization_source_frame": 113}))

    def test_still_render_context_is_stable(self):
        # 非视频渲染（无逐帧键）：暂停态缓存行为不变
        base = {"color_output_target": "front"}
        assert self._context(base) == self._context(dict(base))

    def test_mask_epoch_and_target_enter_key(self):
        base = {"color_output_target": "front"}
        assert self._context(base) != self._context(
            base, mask_epoch={"l1": 2})
        assert self._context(base) != self._context(
            {"color_output_target": "full_res"})
