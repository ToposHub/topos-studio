"""EF-13 batch regression tests — Suggestion-level audit fixes.

Covers (one test group per AUD id):
  AUD-4702  graph_migration.migrate_graph version/nodes validation
  AUD-4707  asset_dependency collect skips disabled nodes
  AUD-48002 graph_preflight tolerates malformed group mapping payloads
  AUD-48006 graph_snapshot NodeSnapshot/ConnectionSnapshot frozen=True
  AUD-48009 image_loader.cancel_all keeps list mutations under the mutex
            (regression guard for the AUD-48007 re-lock rebuild)
  AUD-48010 image_loader prunes finished cancelled workers on entry points
  AUD-48015 layer_provenance._trace_chain is iterative (no RecursionError)
  AUD-4910  layer_stack_controller._new_layer_id dead membership test gone
  AUD-5001  layer_stack_validator reads graph.nodes under the graph lock
  AUD-5002  layer_stack_validator track-matte check uses a set (was O(n^2))
  AUD-5110  node_engine toposort deque + adjacency-indexed cycle/dirty walks
  AUD-5204  node_plugin docstring no longer claims a nonexistent file lock
  AUD-5210  runtime_context clip_effects stored as tuple + ownership contract
  AUD-5213  runtime_diagnostics.publish_many single critical section

Every test is a pure-data / mock path: no GL context, no modal dialogs,
no real decode threads.
"""

import inspect
import itertools
import sys
import os
import threading
import types
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.graph_migration import (
    COMPOSITE_SCHEMA_VERSION,
    migrate_graph,
)
from src.features.composite.core.asset_dependency import (
    collect_asset_dependencies,
)
from src.features.composite.core.graph_preflight import (
    _flatten_group_connections,
    run_preflight_data,
)
from src.features.composite.core.graph_snapshot import (
    ConnectionSnapshot,
    NodeSnapshot,
)
from src.features.composite.core.image_loader import ImageLoader
from src.features.composite.core.layer_provenance import (
    LayerProvenanceResolver,
)
from src.features.composite.core.layer_stack_controller import (
    LayerStackController,
)
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.layer_model import Layer, LayerStack
from src.features.composite.core.layer_stack_validator import (
    LayerStackValidator,
)
from src.features.composite.core.node_plugin import NodePluginManager
from src.features.composite.core.runtime_context import (
    CompositeRuntimeContext,
)
from src.features.composite.core.runtime_diagnostics import (
    DiagnosticBus,
    RuntimeDiagnostic,
)

pytestmark = pytest.mark.composite_core


# ── AUD-4702: migrate_graph version / nodes validation ──────────────────

class TestAud4702MigrateGraphValidation:

    def test_string_version_does_not_crash_and_migrates(self):
        # "9" is a corrupt marker; per the module contract it is treated as
        # v1 and the full chain runs.  Old code raised TypeError on the
        # str>=int comparison.
        data = migrate_graph({
            "composite_schema_version": "9",
            "nodes": [],
            "connections": [],
        })
        assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION

    def test_float_version_does_not_get_stamped_without_migration(self):
        # 10.5 used to fall through the chain (no migration registered for a
        # float key) and was STILL stamped as the current version.
        data = migrate_graph({
            "composite_schema_version": 10.5,
            "nodes": [],
        })
        assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION

    def test_missing_migration_fn_does_not_stamp_as_current(self, monkeypatch):
        import src.features.composite.core.graph_migration as gm
        broken = {k: v for k, v in gm._MIGRATIONS.items() if k != 3}
        monkeypatch.setattr(gm, "_MIGRATIONS", broken)
        data = migrate_graph({
            "composite_schema_version": 2,
            "nodes": [],
        })
        # Without a registered migration the data must NOT be stamped as
        # current — the stale version marker stays.
        assert data["composite_schema_version"] != COMPOSITE_SCHEMA_VERSION

    def test_normal_chain_still_stamps_current_version(self):
        data = migrate_graph({"composite_schema_version": 1, "nodes": []})
        assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION

    def test_null_nodes_does_not_crash(self):
        data = migrate_graph({"nodes": None, "connections": None})
        assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION

    def test_non_dict_node_members_are_skipped(self):
        data = migrate_graph({
            "nodes": ["junk", {"id": "a", "type": "constant"}],
        })
        assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION


# ── AUD-4707: disabled nodes are not collectable dependencies ────────────

class TestAud4707DisabledNodesSkipped:

    def _graph_data(self):
        return {"nodes": [
            {"id": "img_enabled_empty", "type": "image_input",
             "disabled": False, "parameters": {"file_path": ""}},
            {"id": "img_disabled_empty", "type": "image_input",
             "disabled": True, "parameters": {"file_path": ""}},
            {"id": "img_disabled_path", "type": "image_input",
             "disabled": True,
             "parameters": {"file_path": "X:/renders/shot.exr"}},
            {"id": "img_enabled_path", "type": "image_input",
             "disabled": False,
             "parameters": {"file_path": "X:/renders/kept.exr"}},
        ]}

    def test_disabled_nodes_are_not_collected(self):
        report = collect_asset_dependencies(self._graph_data(), check_fs=False)
        node_ids = {r.node_id for r in report.references}
        assert "img_disabled_empty" not in node_ids
        assert "img_disabled_path" not in node_ids
        # Enabled nodes are still surfaced (empty required path stays
        # visible as missing_path — the deliberate IES-style exposure).
        assert "img_enabled_empty" in node_ids
        assert "img_enabled_path" in node_ids

    def test_enabled_empty_path_still_reported_missing(self):
        report = collect_asset_dependencies(self._graph_data(), check_fs=False)
        by_node = {r.node_id: r for r in report.references}
        assert by_node["img_enabled_empty"].status == "missing_path"

    def test_disabled_empty_path_does_not_block_collect_plan(
            self, tmp_path):
        # End-to-end guard: a disabled node with an empty required path must
        # not make AssetCollectService.plan raise.  The enabled node points
        # at a real file so the plan can complete; reverting the AUD-4707
        # skip resurrects the disabled node's missing_path and plan() raises.
        from src.features.composite.core.asset_dependency import (
            AssetCollectService,
        )
        real_asset = tmp_path / "shot.exr"
        real_asset.write_bytes(b"fake-exr-bytes")
        graph = MagicMock()
        graph.to_dict.return_value = {"nodes": [
            {"id": "img_disabled_empty", "type": "image_input",
             "disabled": True, "parameters": {"file_path": ""}},
            {"id": "img_enabled_ok", "type": "image_input",
             "disabled": False,
             "parameters": {"file_path": str(real_asset)}},
        ]}
        service = AssetCollectService(graph)
        target = tmp_path / "collect_out"
        plan = service.plan(str(target), store_relative_to_project=False)
        collected_nodes = {ref[0] for copy in plan.copies
                           for ref in copy.references}
        assert "img_disabled_empty" not in collected_nodes
        assert "img_enabled_ok" in collected_nodes


# ── AUD-48002: malformed group mapping payloads never crash preflight ────

def _malformed_group_graph_data():
    return {
        "composite_schema_version": COMPOSITE_SCHEMA_VERSION,
        "nodes": [
            {"id": "m1", "type": "merge"},
            # g2's input_mappings is a corrupt non-dict payload.
            {"id": "g2", "type": "group", "input_mappings": "junk",
             "output_mappings": {}, "member_node_ids": ["m1"]},
            # g1 has a VALID outputs mapping pointing into g2 — the boundary
            # walk follows it and previously crashed on g2's payload.
            {"id": "g1", "type": "group", "input_mappings": {},
             "output_mappings": {"out": ("g2", "in")},
             "member_node_ids": ["m1"]},
        ],
        "connections": [
            {"src_node_id": "m1", "src_port_name": "result",
             "dst_node_id": "g2", "dst_port_name": "in"},
        ],
    }


class TestAud48002GroupMappingTolerance:

    def test_preflight_reports_invalid_mapping_without_crashing(self):
        report = run_preflight_data(_malformed_group_graph_data(),
                                    check_fs=False)
        codes = {issue.code for issue in report.issues}
        assert "group.invalid_mapping" in codes

    def test_flatten_group_connections_survives_corrupt_payload(self):
        data = _malformed_group_graph_data()
        flattened = _flatten_group_connections(
            data["nodes"], data["connections"])
        # The connection is kept as-is (unresolvable boundary), not a crash.
        assert any(c["dst_node_id"] == "g2" for c in flattened)


# ── AUD-48006: snapshot dataclasses are frozen ───────────────────────────

class TestAud48006FrozenSnapshots:

    def test_node_snapshot_is_frozen(self):
        ns = NodeSnapshot(node_id="a", node_type="merge", parameters={},
                          disabled=False, input_ports=[], output_ports=[])
        with pytest.raises(FrozenInstanceError):
            ns.disabled = True

    def test_connection_snapshot_is_frozen(self):
        cs = ConnectionSnapshot(src_node_id="a", src_port_name="result",
                                dst_node_id="b", dst_port_name="A")
        with pytest.raises(FrozenInstanceError):
            cs.src_node_id = "x"

    def test_snapshots_still_construct_with_defaults(self):
        ns = NodeSnapshot(node_id="a", node_type="merge", parameters={},
                          disabled=False, input_ports=[], output_ports=[])
        assert ns.port_revisions == {}
        assert ns.state == "dirty"

    def test_graph_snapshot_is_frozen(self):
        """AUD-48006 收口（复查会话补冻）：GraphSnapshot 同样冻结。

        EF-13 执行期 NodeSnapshot/ConnectionSnapshot 已冻结，GraphSnapshot
        被 composite_page 注入路径的原地重绑定阻塞为 PARTIAL；复查会话把
        注入改为 dataclasses.replace 替换快照后补冻。
        """
        from src.features.composite.core.graph_snapshot import GraphSnapshot
        gs = GraphSnapshot(generation=1)
        with pytest.raises(FrozenInstanceError):
            gs.eval_order = ["x"]

    def test_injection_returns_replacement_not_mutation(self):
        """注入路径返回替换快照，入参不被原地修改（AUD-48006 冻结前提）。"""
        import inspect
        from src.features.composite.ui import composite_page
        src = inspect.getsource(
            composite_page.CompositePage._inject_viewer3d_scene_snapshot)
        assert "dataclasses.replace" in src or "_dc_replace" in src
        assert "snapshot.connections =" not in src
        assert "snapshot.eval_order =" not in src
        assert "snapshot.nodes[" not in src


# ── AUD-48009 / AUD-48010: ImageLoader cancelled-worker bookkeeping ──────

class TestAud4809And48010ImageLoader:

    def test_cancel_all_keeps_running_worker_alive(self):
        """AUD-48009 regression guard: the post-wait rebuild happens under
        the mutex and running workers stay strongly referenced (the
        AUD-48007 keep-alive contract).  A bare clear() outside the lock
        would drop the running worker."""
        loader = ImageLoader()
        running = MagicMock()
        running.isRunning.return_value = True
        finished = MagicMock()
        finished.isRunning.return_value = False
        loader._active_workers["n2"] = running
        loader._cancelled_workers.append(finished)

        loader.cancel_all()

        assert loader._active_workers == {}
        assert running in loader._zombie_workers
        assert finished.wait.called

    def test_request_decode_prunes_finished_cancelled_workers(self):
        """AUD-48010: cancelled workers never deliver, so finished ones must
        be swept on the request/cancel entry points."""
        loader = ImageLoader()
        finished = MagicMock()
        finished.isRunning.return_value = False
        loader._cancelled_workers.append(finished)

        loader.request_decode("n1", "")  # empty path → cancel-only path

        assert loader._cancelled_workers == []

    def test_cancel_for_node_prunes_finished_cancelled_workers(self):
        loader = ImageLoader()
        finished = MagicMock()
        finished.isRunning.return_value = False
        loader._cancelled_workers.append(finished)

        loader.cancel_for_node("n1")

        assert loader._cancelled_workers == []


# ── AUD-48015: _trace_chain is iterative ─────────────────────────────────

class _FakeNode:
    def __init__(self, node_type: str) -> None:
        self.node_type = node_type


class _FakeConn:
    def __init__(self, src: str, dst: str) -> None:
        self.src_node_id = src
        self.dst_node_id = dst


class _FakeGraph:
    """Minimal read-only graph surface for LayerProvenanceResolver."""

    def __init__(self, nodes, connections):
        self._nodes = nodes
        self.connections = connections

    def get_node(self, node_id):
        return self._nodes.get(node_id)


def _chain_graph(depth: int) -> _FakeGraph:
    nodes = {f"n{i}": _FakeNode("merge") for i in range(depth)}
    conns = [_FakeConn(f"n{i - 1}", f"n{i}") for i in range(1, depth)]
    return _FakeGraph(nodes, conns)


class TestAud48015IterativeTraceChain:

    def _resolver(self, graph):
        return LayerProvenanceResolver(graph)

    def _stack(self):
        return types.SimpleNamespace(generated_merge_node_ids=[],
                                     output_node_id="")

    def test_deep_chain_does_not_hit_recursion_limit(self):
        depth = 3000  # recursive visit() raised RecursionError far earlier
        chain = self._resolver(_chain_graph(depth))._trace_chain(
            f"n{depth - 1}", None, self._stack())
        assert len(chain) == depth
        assert chain[0] == f"n{depth - 1}"
        assert chain[-1] == "n0"

    def test_boundary_type_stops_traversal(self):
        graph = _FakeGraph(
            {"n0": _FakeNode("merge"), "n1": _FakeNode("precomp"),
             "n2": _FakeNode("merge")},
            [_FakeConn("n0", "n1"), _FakeConn("n1", "n2")])
        chain = self._resolver(graph)._trace_chain("n2", None, self._stack())
        assert chain == ["n2", "n1"]

    def test_source_id_stops_traversal(self):
        graph = _FakeGraph(
            {"n0": _FakeNode("merge"), "n1": _FakeNode("merge"),
             "n2": _FakeNode("merge")},
            [_FakeConn("n0", "n1"), _FakeConn("n1", "n2")])
        chain = self._resolver(graph)._trace_chain("n2", "n1",
                                                   self._stack())
        assert chain == ["n2", "n1"]

    def test_diamond_visited_once(self):
        graph = _FakeGraph(
            {nid: _FakeNode("merge")
             for nid in ("s", "a", "b", "o")},
            [_FakeConn("s", "a"), _FakeConn("s", "b"),
             _FakeConn("a", "o"), _FakeConn("b", "o")])
        chain = self._resolver(graph)._trace_chain("o", None, self._stack())
        assert sorted(chain) == ["a", "b", "o", "s"]
        assert chain[0] == "o"


# ── AUD-4910: _new_layer_id dead membership test removed ─────────────────

class TestAud4910NewLayerId:

    def test_dead_get_all_layers_membership_test_removed(self):
        # The removed test compared a str against Layer objects — always
        # False.  Its presence is a doc/code bug, asserted at source level.
        src = inspect.getsource(LayerStackController._new_layer_id)
        assert "get_all_layers" not in src

    def test_collision_with_existing_layer_id_retries(self, monkeypatch):
        graph = NodeGraph()
        ctrl = LayerStackController(graph, undo_stack=None)
        graph._layers["layer_abc123"] = object()  # existing layer registry

        hexes = itertools.cycle(["abc123", "def456", "789012"])
        monkeypatch.setattr(
            "uuid.uuid4",
            lambda: types.SimpleNamespace(hex=next(hexes)))

        new_id = ctrl._new_layer_id()
        assert new_id != "layer_abc123"
        assert new_id == "layer_def456"

    def test_generated_ids_are_unique(self):
        graph = NodeGraph()
        ctrl = LayerStackController(graph, undo_stack=None)
        ids = {ctrl._new_layer_id() for _ in range(16)}
        assert len(ids) == 16


# ── AUD-5001 / AUD-5002: LayerStackValidator ─────────────────────────────

class TestAud5001And5002Validator:

    def _fixture(self):
        from src.features.composite.nodes.output_node import OutputNode
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.transform_node import TransformNode
        graph = NodeGraph()
        graph.add_node(OutputNode(node_id="out"))
        graph.add_node(ConstantNode(node_id="src0"))
        graph.add_node(TransformNode(node_id="t0"))
        stack = LayerStack(stack_id="main", output_node_id="out")
        layer = Layer(layer_id="L0", name="L0", root_node_id="t0",
                      source_node_id="src0")
        stack.layer_ids.append("L0")
        return graph, stack, {"L0": layer}

    def test_validate_runs_on_locked_real_graph(self):
        # AUD-5001: the node-count read goes through the graph RLock; a real
        # NodeGraph provides it and the check must pass cleanly.
        graph, stack, layers = self._fixture()
        report = LayerStackValidator(graph).validate(stack, layers)
        assert report.ok, [i.message for i in report.issues]

    def test_track_matte_inside_stack_is_valid(self):
        graph, stack, layers = self._fixture()
        stack.layer_ids.append("L1")
        layers["L1"] = Layer(layer_id="L1", name="L1", root_node_id="t0",
                             track_matte_layer_id="L0")
        report = LayerStackValidator(graph).validate(stack, layers)
        assert not report.has("dangling_track_matte")

    def test_track_matte_outside_stack_is_flagged(self):
        graph, stack, layers = self._fixture()
        stack.layer_ids.append("L1")
        layers["L1"] = Layer(layer_id="L1", name="L1", root_node_id="t0",
                             track_matte_layer_id="L_ghost")
        report = LayerStackValidator(graph).validate(stack, layers)
        assert report.has("dangling_track_matte")

    def test_layer_fields_membership_uses_set(self):
        # AUD-5002: the per-layer membership test must run against a set,
        # not the raw layer_ids list (O(n^2) on large stacks).
        src = inspect.getsource(LayerStackValidator._check_layer_fields)
        assert "stack_layer_id_set" in src

    def test_node_count_read_takes_graph_lock(self):
        # AUD-5001: the defensive count read must acquire the graph lock.
        src = inspect.getsource(LayerStackValidator._check_acyclic_chain)
        assert "_lock" in src


# ── AUD-5110: node_engine complexity fixes ───────────────────────────────

class TestAud5110NodeEngineComplexity:

    def _diamond(self):
        g = NodeGraph()
        for nid in ("s", "a", "b", "o"):
            g.add_node(MagicMockNode(nid))
        g.connect("s", "result", "a", "image")
        g.connect("s", "result", "b", "image")
        g.connect("a", "result", "o", "image")
        g.connect("b", "result", "o", "image")
        return g

    def test_topological_sort_deps_first(self):
        g = self._diamond()
        order = g.topological_sort()
        assert set(order) == {"s", "a", "b", "o"}
        assert order.index("s") < order.index("a") < order.index("o")
        assert order.index("s") < order.index("b") < order.index("o")

    def test_topological_sort_raises_on_cycle(self):
        g = self._diamond()
        # connect() refuses to create cycles, so corrupt the graph the way a
        # manual rewire / external edit would: inject the back edge directly.
        from src.features.composite.nodes.base_node import Connection
        g.connections.append(
            Connection("o", "result", "s", "image"))
        with pytest.raises(ValueError):
            g.topological_sort()

    def test_snapshot_topological_sort_matches_contract(self):
        from src.features.composite.core.node_engine import NodeGraph as NG
        conns = [
            types.SimpleNamespace(src_node_id="s", dst_node_id="a"),
            types.SimpleNamespace(src_node_id="a", dst_node_id="o"),
        ]
        order = NG._snapshot_topological_sort(["s", "a", "o"], conns)
        assert order == ["s", "a", "o"]

    def test_would_create_cycle_detection(self):
        g = self._diamond()
        # adding o→s closes a cycle (s reaches o)
        assert g._would_create_cycle("o", "s") is True
        # adding s→a is fine (a does not reach s)
        assert g._would_create_cycle("s", "a") is False
        assert g._would_create_cycle("a", "a") is True

    def test_mark_dirty_downstream_marks_only_downstream(self):
        g = self._diamond()
        b_node = g.nodes["b"]
        b_node._dirty = False
        g._mark_dirty_downstream("a")
        assert g.nodes["a"]._dirty is True
        assert g.nodes["o"]._dirty is True
        assert b_node._dirty is False

    def test_toposort_uses_deque_not_pop0(self):
        for fn_name in ("topological_sort", "_snapshot_topological_sort"):
            src = inspect.getsource(getattr(NodeGraph, fn_name))
            assert "popleft" in src, fn_name
            assert ".pop(0)" not in src, fn_name

    def test_cycle_and_dirty_walks_index_connections_once(self):
        for fn_name in ("_would_create_cycle", "_mark_dirty_downstream"):
            src = inspect.getsource(getattr(NodeGraph, fn_name))
            assert "adjacency" in src, fn_name


class MagicMockNode:
    """Minimal node stand-in for graph topology tests (no GL, no Qt)."""

    def __init__(self, node_id: str) -> None:
        from src.features.composite.nodes.base_node import Port
        self.id = node_id
        self.node_type = "merge"
        self._dirty = True
        self.inputs = [Port(name="image", direction="input",
                            data_type="image")]
        self.outputs = [Port(name="result", direction="output",
                             data_type="image")]

    def get_input_port(self, name: str):
        for port in self.inputs:
            if port.name == name:
                return port
        return None

    def get_output_port(self, name: str):
        for port in self.outputs:
            if port.name == name:
                return port
        return None


# ── AUD-5204: docstring no longer claims a nonexistent file lock ─────────

class TestAud5204DocstringReality:

    def test_no_file_lock_claim(self):
        doc = NodePluginManager.__doc__ or ""
        assert "file lock" not in doc

    def test_single_thread_guidance_retained(self):
        doc = NodePluginManager.__doc__ or ""
        assert "main thread" in doc


# ── AUD-5210: clip_effects boundary contract ─────────────────────────────

class TestAud5210ClipEffectsContract:

    def _ctx(self, clip_effects):
        return CompositeRuntimeContext.for_timeline_clip(
            project_id="p", graph_id="g", timeline_id="t", clip_id="c",
            timeline_frame=1, clip_local_frame=0, source_frame=0,
            clip_effects=clip_effects)

    def test_list_is_normalized_to_tuple(self):
        effect = object()
        ctx = self._ctx([effect])
        assert isinstance(ctx.clip_effects, tuple)
        assert ctx.clip_effects == (effect,)

    def test_default_is_empty_tuple(self):
        ctx = CompositeRuntimeContext()
        assert ctx.clip_effects == ()

    def test_context_is_frozen(self):
        ctx = CompositeRuntimeContext()
        with pytest.raises(FrozenInstanceError):
            ctx.quality = "master"

    def test_ownership_contract_documented(self):
        src = inspect.getsource(CompositeRuntimeContext)
        assert "AUD-5210" in src
        assert "read-only" in src


# ── AUD-5213: publish_many single critical section ───────────────────────

class _CountingLock:
    """Lock proxy counting critical-section entries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquired = 0

    def __enter__(self):
        self._lock.acquire()
        self.acquired += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


class TestAud5213PublishMany:

    def _rec(self, code: str) -> RuntimeDiagnostic:
        return RuntimeDiagnostic(code=code, severity="info",
                                 message=code)

    def test_batch_is_published_in_order(self):
        bus = DiagnosticBus()
        batch = [self._rec("a"), self._rec("b"), self._rec("c")]
        bus.publish_many(batch)
        assert bus.snapshot() == tuple(batch)

    def test_invalid_record_publishes_nothing(self):
        bus = DiagnosticBus()
        with pytest.raises(TypeError):
            bus.publish_many([self._rec("good"), "not-a-record"])
        # All-or-nothing: the prefix must not leak into the store.
        assert bus.snapshot() == ()

    def test_single_critical_section_per_batch(self):
        bus = DiagnosticBus()
        counting = _CountingLock()
        bus._lock = counting
        bus.publish_many([self._rec("a"), self._rec("b"), self._rec("c")])
        assert counting.acquired == 1
