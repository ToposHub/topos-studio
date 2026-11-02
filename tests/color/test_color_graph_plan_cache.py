"""B08 §5.4 债务收口：plan 编译缓存预算驱动淘汰（``BoundedPlanCache``）。

验收：总额超限按 LRU 淘汰最旧；命中（``[]`` 与 ``.get`` 两条读路径）
续命；``compile_graph(cache=...)`` 兼容；预算 ≤0 不缓存；renderer 持有
有界缓存（切换尺寸/多图不再无上限累积）。
"""

from __future__ import annotations

import pytest

from src.features.color.core.color_graph_compiler import (
    BoundedPlanCache,
    compile_graph,
)
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_snapshot import freeze_graph
from tests.color.fixtures.color_graph import f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers


class _FakePlan:
    def __init__(self, estimated_bytes: int, tag: str):
        self.tag = tag
        self.passes = [type("P", (), {"estimated_bytes": estimated_bytes})()]


class TestBoundedPlanCache:
    def test_evicts_oldest_over_budget(self):
        cache = BoundedPlanCache(byte_budget=200)
        cache["a"] = _FakePlan(100, "a")
        cache["b"] = _FakePlan(100, "b")
        assert set(cache) == {"a", "b"}
        cache["c"] = _FakePlan(100, "c")  # 超预算 → 淘汰 a
        assert set(cache) == {"b", "c"}
        assert cache.total_bytes == 200

    def test_hit_refreshes_recency(self):
        cache = BoundedPlanCache(byte_budget=200)
        cache["a"] = _FakePlan(100, "a")
        cache["b"] = _FakePlan(100, "b")
        assert cache["a"].tag == "a"        # [] 读路径续命
        cache["c"] = _FakePlan(100, "c")    # 淘汰 b（a 最近用过）
        assert set(cache) == {"a", "c"}

    def test_get_read_path_refreshes(self):
        cache = BoundedPlanCache(byte_budget=200)
        cache["a"] = _FakePlan(100, "a")
        cache["b"] = _FakePlan(100, "b")
        assert cache.get("a") is not None and cache.get("z") is None
        cache["c"] = _FakePlan(100, "c")
        assert set(cache) == {"a", "c"}, ".get 命中也必须续命"

    def test_overwrite_updates_accounting(self):
        cache = BoundedPlanCache(byte_budget=300)
        cache["a"] = _FakePlan(100, "a1")
        cache["a"] = _FakePlan(250, "a2")
        assert cache.total_bytes == 250
        assert cache["a"].tag == "a2"

    def test_zero_budget_disables_caching(self):
        cache = BoundedPlanCache(byte_budget=0)
        cache["a"] = _FakePlan(10, "a")
        assert set(cache) == set() and cache.total_bytes == 0

    def test_compile_graph_roundtrip_with_budget(self):
        graph = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        snapshot = freeze_graph(graph, session_id="s1")
        cache = BoundedPlanCache(byte_budget=1)  # 恒超限 → 每次重编译
        plan1 = compile_graph(snapshot, size=(16, 16), cache=cache)
        plan2 = compile_graph(snapshot, size=(16, 16), cache=cache)
        assert plan1 is not plan2
        big = BoundedPlanCache(byte_budget=10 * 1024 * 1024)
        p1 = compile_graph(snapshot, size=(16, 16), cache=big)
        p2 = compile_graph(snapshot, size=(16, 16), cache=big)
        assert p1 is p2, "预算内应命中"
        assert big.total_bytes > 0

    def test_renderer_holds_bounded_cache(self):
        """renderer 构造路径持有 BoundedPlanCache（__init__ 依赖 Qt/GL，
        以源码断言锁定装配，行为由上面各用例覆盖）。"""
        import inspect
        from src.features.color.core.async_renderer import AsyncRenderer
        assert "BoundedPlanCache()" in inspect.getsource(
            AsyncRenderer.__init__), (
            "renderer 必须持有有界 plan 缓存（B08 §5.4）")
