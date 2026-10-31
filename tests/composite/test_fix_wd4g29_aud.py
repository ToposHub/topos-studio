"""Batch D (wd4-g29) regression tests for Major audit findings.

Covers:
  * AUD-11500 — pass-fusion CONSERVATIVE size gate (unknown size = refuse)
  * AUD-11510 — ExpressionManager.propagate_change context parity
  * AUD-11511 — ExpressionManager locking (+ parser AST cache lock)
  * AUD-11530 — PresetManager path-safe names
  * AUD-11531 — PresetManager rename write-first ordering
  * AUD-11601/11602/11603/11604 — serialization_formats contracts
  * AUD-4400 — restore_graph failure containment
  * AUD-4401 — Media Pool poster identity caching
  * AUD-4500/4501 — composite plan immutability + validation gaps
  * AUD-4600/4601/4602 — bake rewire failure handling / multi-output guard
"""

import json
import os
import sys
import threading
import unittest
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox

pytestmark = pytest.mark.composite_core


# ── AUD-11500: pass fusion size gate ─────────────────────────────────────────


class _FakeNodePlan:
    def __init__(self, bbox):
        self.estimated_bbox = bbox


class _FakePlan:
    def __init__(self, node_plans):
        self.node_plans = node_plans


class _FakeNodeSnapshot:
    def __init__(self, node_type="", parameters=None, width=0, height=0):
        self.node_type = node_type
        self.parameters = parameters or {}
        if width or height:
            self.parameters = dict(self.parameters)
            self.parameters.setdefault("width", width)
            self.parameters.setdefault("height", height)


class _FakeSnapshot:
    def __init__(self, nodes=None, color_width=0, color_height=0):
        self.nodes = nodes or {}
        self.color_width = color_width
        self.color_height = color_height


class _Tex:
    def __init__(self, w, h):
        self.width, self.height = w, h


class TestPassFusionSizeGate:
    def test_cache_dimensions_used_first(self):
        from src.features.composite.utils.pass_fusion import (
            _get_texture_dimensions,
        )
        size = _get_texture_dimensions(
            "n", None, {"n": {"result": _Tex(64, 32)}}, _FakeNodePlan(None))
        assert size == (64, 32)

    def test_estimated_bbox_fallback(self):
        from src.features.composite.utils.pass_fusion import (
            _get_texture_dimensions,
        )
        plan_node = _FakeNodePlan(BBox(0, 0, 320, 240))
        size = _get_texture_dimensions("n", None, {}, plan_node)
        assert size == (320, 240)

    def test_empty_bbox_is_unknown(self):
        from src.features.composite.utils.pass_fusion import (
            _get_texture_dimensions,
        )
        size = _get_texture_dimensions(
            "n", None, {}, _FakeNodePlan(BBox(0, 0, 0, 0)))
        assert size is None

    def test_constant_source_params_resolve(self):
        from src.features.composite.utils.pass_fusion import (
            _get_texture_dimensions,
        )
        snap = _FakeSnapshot(nodes={
            "src": _FakeNodeSnapshot("constant", {"width": 640, "height": 480})})
        size = _get_texture_dimensions(
            "src", snap, {}, None, snap.nodes["src"])
        assert size == (640, 480)

    def test_image_input_without_dims_is_unknown(self):
        from src.features.composite.utils.pass_fusion import (
            _get_texture_dimensions,
        )
        snap = _FakeSnapshot(nodes={"src": _FakeNodeSnapshot("image_input")})
        size = _get_texture_dimensions(
            "src", snap, {}, None, snap.nodes["src"])
        assert size is None

    def test_color_input_uses_snapshot_dims(self):
        from src.features.composite.utils.pass_fusion import (
            _get_texture_dimensions,
        )
        snap = _FakeSnapshot(nodes={"c": _FakeNodeSnapshot("color_input")},
                             color_width=3840, color_height=2160)
        size = _get_texture_dimensions(
            "c", snap, {}, None, snap.nodes["c"])
        assert size == (3840, 2160)

    def test_unknown_size_alone_is_tolerated(self):
        """No size info at all → linear-chain invariant keeps it fusible."""
        from src.features.composite.utils.pass_fusion import (
            _check_size_compatibility,
        )
        plan = _FakePlan({"a": _FakeNodePlan(None), "b": _FakeNodePlan(None)})
        sizes, compatible = _check_size_compatibility(
            ["a", "b"], _FakeSnapshot(), {}, plan)
        assert compatible is True
        assert sizes == {}

    def test_known_mismatch_is_incompatible(self):
        from src.features.composite.utils.pass_fusion import (
            _check_size_compatibility,
        )
        plan = _FakePlan({
            "a": _FakeNodePlan(BBox(0, 0, 100, 100)),
            "b": _FakeNodePlan(BBox(0, 0, 200, 100)),
        })
        _, compatible = _check_size_compatibility(
            ["a", "b"], _FakeSnapshot(), {}, plan)
        assert compatible is False

    def test_cache_mismatch_is_incompatible(self):
        """A cached texture that differs from the plan bbox must reject."""
        from src.features.composite.utils.pass_fusion import (
            _check_size_compatibility,
        )
        plan = _FakePlan({"a": _FakeNodePlan(BBox(0, 0, 100, 100))})
        cache = {"b": {"result": _Tex(300, 100)}}
        _, compatible = _check_size_compatibility(
            ["a", "b"], _FakeSnapshot(), cache, plan)
        assert compatible is False

    def test_known_matching_sizes_are_compatible(self):
        from src.features.composite.utils.pass_fusion import (
            _check_size_compatibility,
        )
        plan = _FakePlan({
            "a": _FakeNodePlan(BBox(0, 0, 100, 100)),
            "b": _FakeNodePlan(BBox(5, 5, 100, 100)),
        })
        _, compatible = _check_size_compatibility(
            ["a", "b"], _FakeSnapshot(), {}, plan)
        assert compatible is True

    def test_mixed_known_and_unknown_uses_known_reference(self):
        """Unknown nodes inherit the chain size; known ones must agree."""
        from src.features.composite.utils.pass_fusion import (
            _check_size_compatibility,
        )
        snap = _FakeSnapshot(nodes={
            "src": _FakeNodeSnapshot("constant", {"width": 128, "height": 64})})
        plan = _FakePlan({
            "src": _FakeNodePlan(None),
            "fx": _FakeNodePlan(BBox(0, 0, 128, 64)),
        })
        _, compatible = _check_size_compatibility(
            ["src", "fx"], snap, {}, plan)
        assert compatible is True
        # …but a known disagreeing size still rejects the chain.
        plan_disagree = _FakePlan({
            "src": _FakeNodePlan(None),
            "fx": _FakeNodePlan(BBox(0, 0, 256, 64)),
        })
        _, compatible = _check_size_compatibility(
            ["src", "fx"], snap, {}, plan_disagree)
        assert compatible is False


# ── AUD-11510 / AUD-11511: expression manager ────────────────────────────────


class TestPropagateChangeContextParity:
    def _manager_with_expr(self, expr):
        from src.features.composite.utils.expression_parser import (
            ExpressionManager,
        )
        mgr = ExpressionManager()
        assert mgr.set_expression("n1", "v", expr).success
        return mgr

    def test_propagate_change_uses_time_and_fps(self):
        mgr = self._manager_with_expr("src.p + time * 10")
        params = {"n1": {"v": 0.0}, "src": {"p": 0.0}}
        out = mgr.propagate_change("src", "p", params, {}, frame=12, fps=2.0)
        # time = 12 / 2.0 = 6.0 → 0 + 60
        assert out["n1"]["v"] == pytest.approx(60.0)

    def test_propagate_change_matches_evaluate_all(self):
        mgr = self._manager_with_expr("src.p + time * 10")
        params = {"n1": {"v": 0.0}, "src": {"p": 5.0}}
        via_propagate = mgr.propagate_change(
            "src", "p", params, {}, frame=24, fps=24.0)
        via_all = mgr.evaluate_all(params, {}, 24, fps=24.0)
        assert via_propagate["n1"]["v"] == pytest.approx(via_all["n1"]["v"])

    def test_propagate_change_filters_non_finite_and_records_error(self):
        mgr = self._manager_with_expr("src.p / 0")  # parser yields +inf
        params = {"n1": {"v": 1.5}, "src": {"p": 1.0}}
        out = mgr.propagate_change("src", "p", params, {}, frame=1)
        assert out["n1"]["v"] == 1.5  # previous value kept
        assert mgr.get_last_error("n1", "v") is not None

    def test_propagate_change_default_signature_still_works(self):
        mgr = self._manager_with_expr("src.p + frame")
        params = {"n1": {"v": 0.0}, "src": {"p": 0.0}}
        out = mgr.propagate_change("src", "p", params, {}, 3)
        assert out["n1"]["v"] == pytest.approx(3.0)


class TestExpressionManagerLocking:
    def test_manager_has_reentrant_lock(self):
        from src.features.composite.utils.expression_parser import (
            ExpressionManager,
        )
        mgr = ExpressionManager()
        assert type(mgr._lock).__name__ == "_RLock" or "RLock" in type(
            mgr._lock).__name__
        # Re-entrant acquisition (evaluate_all → _compute_eval_order) must
        # not deadlock.
        with mgr._lock:
            with mgr._lock:
                pass

    def test_concurrent_edit_and_evaluate(self):
        from src.features.composite.utils.expression_parser import (
            ExpressionManager,
        )
        mgr = ExpressionManager()
        mgr.set_expression("a", "x", "frame + 1")
        errors: list = []

        def editor():
            for i in range(150):
                mgr.set_expression(f"n{i}", "p", "frame + 1")
                mgr.remove_expression(f"n{i}", "p")

        def evaluator():
            try:
                for _ in range(150):
                    mgr.evaluate_all({"a": {"x": 0.0}}, {}, 1)
            except Exception as exc:  # pragma: no cover - failure evidence
                errors.append(exc)

        t1 = threading.Thread(target=editor)
        t2 = threading.Thread(target=evaluator)
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert errors == []

    def test_dependency_accessors_return_copies(self):
        from src.features.composite.utils.expression_parser import (
            ExpressionManager,
        )
        mgr = ExpressionManager()
        mgr.set_expression("n1", "v", "src.p + 1")
        deps = mgr.get_dependencies("n1", "v")
        deps.add(("bogus", "x"))
        assert mgr.get_dependencies("n1", "v") == {("src", "p")}
        dependents = mgr.get_dependents("src", "p")
        dependents.add(("bogus", "y"))
        assert mgr.get_dependents("src", "p") == {("n1", "v")}

    def test_parser_ast_cache_thread_safe(self):
        from src.features.composite.utils.expression_parser import (
            ExpressionParser,
        )
        parser = ExpressionParser()
        errors: list = []

        def worker(base):
            try:
                for i in range(400):
                    parser.validate(f"{base} + {i} * frame")
            except Exception as exc:  # pragma: no cover - failure evidence
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(b,))
                   for b in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ── AUD-11530 / AUD-11531: preset manager ────────────────────────────────────


def _valid_nodes_data():
    return {"nodes": [{"id": "n1", "type": "blur"}], "connections": []}


class TestPresetNameSafety:
    def test_save_rejects_traversal_name(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        mgr = PresetManager(presets_dir=str(tmp_path))
        assert mgr.save_preset("../evil", _valid_nodes_data()) is False
        assert not (tmp_path.parent / "evil.json").exists()

    def test_save_rejects_separator_name(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        mgr = PresetManager(presets_dir=str(tmp_path))
        assert mgr.save_preset("a/b", _valid_nodes_data()) is False
        assert not (tmp_path / "a").exists()

    def test_delete_rejects_traversal_name(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        victim = tmp_path.parent / "victim.json"
        victim.write_text("{}")
        mgr = PresetManager(presets_dir=str(tmp_path))
        assert mgr.delete_preset("../../victim") is False
        assert victim.exists()

    def test_rename_rejects_unsafe_new_name(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        mgr = PresetManager(presets_dir=str(tmp_path))
        mgr.save_preset("Good", _valid_nodes_data())
        assert mgr.rename_preset("Good", "../escape") is False
        assert (tmp_path / "Good.json").exists()
        assert mgr.preset_exists("Good")

    def test_import_falls_back_to_stem_for_evil_name(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        external = tmp_path / "external.json"
        external.write_text(json.dumps({
            "name": "../../evil",
            "nodes": [{"id": "n1"}],
            "connections": [],
        }))
        mgr = PresetManager(presets_dir=str(tmp_path))
        name = mgr.import_preset(str(external))
        assert name == "external"
        assert (tmp_path / "external.json").exists()
        assert not (tmp_path.parent / "evil.json").exists()

    def test_import_rejects_when_stem_also_unsafe(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        sub = tmp_path / "sub"
        sub.mkdir()
        external = sub / "..evil.json"  # stem contains traversal dots
        external.write_text(json.dumps({
            "nodes": [], "connections": [],
        }))
        mgr = PresetManager(presets_dir=str(tmp_path))
        assert mgr.import_preset(str(external)) is None

    def test_import_handles_non_string_name(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        external = tmp_path / "ext2.json"
        external.write_text(json.dumps({
            "name": 12345,
            "nodes": [{"id": "n1"}],
            "connections": [],
        }))
        mgr = PresetManager(presets_dir=str(tmp_path))
        assert mgr.import_preset(str(external)) == "ext2"

    def test_update_description_rejects_unsafe_name(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        mgr = PresetManager(presets_dir=str(tmp_path))
        mgr.save_preset("Good", _valid_nodes_data())
        assert mgr.update_preset_description("../evil", "d") is False

    def test_cjk_preset_name_still_allowed(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        mgr = PresetManager(presets_dir=str(tmp_path))
        assert mgr.save_preset("模糊预设 1", _valid_nodes_data()) is True
        assert (tmp_path / "模糊预设 1.json").exists()


class TestPresetRenameDurability:
    def test_failed_write_keeps_old_file_and_memory_consistent(self, tmp_path):
        """Write failure must not lose the old file nor split memory state."""
        from src.features.composite.utils.preset_manager import PresetManager
        mgr = PresetManager(presets_dir=str(tmp_path))
        mgr.save_preset("Old", _valid_nodes_data(), "desc")
        # Block the new file: a directory occupies NewName.json
        (tmp_path / "NewName.json").mkdir()
        assert mgr.rename_preset("Old", "NewName") is False
        # Old file survives on disk and in memory, name field rolled back.
        assert (tmp_path / "Old.json").exists()
        assert mgr.preset_exists("Old")
        assert mgr.load_preset("Old")["name"] == "Old"
        assert mgr.load_preset("Old")["description"] == "desc"

    def test_successful_rename_order(self, tmp_path):
        from src.features.composite.utils.preset_manager import PresetManager
        mgr = PresetManager(presets_dir=str(tmp_path))
        mgr.save_preset("Old", _valid_nodes_data())
        assert mgr.rename_preset("Old", "New") is True
        assert (tmp_path / "Old.json").exists() is False
        assert (tmp_path / "New.json").exists() is True
        assert mgr.preset_exists("New") and not mgr.preset_exists("Old")


# ── AUD-11601..11604: serialization formats ─────────────────────────────────


class TestSerializePurity:
    def test_serialize_does_not_mutate_input(self):
        from src.features.composite.utils.serialization_formats import serialize
        data = {"nodes": []}
        serialize(data)
        assert "_format_version" not in data

    def test_full_data_envelope_holds_a_copy(self):
        from src.features.composite.utils.serialization_formats import (
            IncrementalSaver,
        )
        saver = IncrementalSaver()
        new = {"nodes": [{"id": "n1"}], "connections": []}
        delta = saver.compute_delta(new)
        assert delta["delta_type"] == "full"
        # Mutating the caller dict must not leak into the envelope.
        new["nodes"].append({"id": "n2"})
        assert delta["full_data"]["nodes"] == [{"id": "n1"}]


class TestDeserializeTopLevelType:
    def test_list_payload_raises_value_error(self):
        from src.features.composite.utils.serialization_formats import (
            deserialize,
        )
        with pytest.raises(ValueError, match="dict"):
            deserialize(b"[1, 2, 3]")

    def test_string_payload_raises_value_error(self):
        from src.features.composite.utils.serialization_formats import (
            deserialize,
        )
        with pytest.raises(ValueError):
            deserialize(b'"hello"')

    def test_dict_payload_still_works(self):
        from src.features.composite.utils.serialization_formats import (
            deserialize,
        )
        assert deserialize(b'{"a": 1}') == {"a": 1}


class TestIncrementalBaseAdvancement:
    def test_save_incremental_advances_base(self):
        from src.features.composite.utils.serialization_formats import (
            IncrementalSaver, save_incremental,
        )
        saver = IncrementalSaver()
        base = {"nodes": [{"id": "n1", "parameters": {"gain": 1.0}}],
                "connections": []}
        saver.set_base_snapshot(base, generation=1)
        v2 = {"nodes": [{"id": "n1", "parameters": {"gain": 2.0}}],
              "connections": []}
        save_incremental(v2, saver)
        assert saver.base_generation == 2
        assert saver.has_base_snapshot
        # The next delta is computed against the advanced base: no changes.
        assert saver.compute_delta(v2)["delta"].get("no_changes") is True

    def test_version_mismatch_refuses_stale_delta(self):
        from src.features.composite.utils.serialization_formats import (
            IncrementalSaver, load_incremental, save_incremental,
            serialize,
        )
        saver = IncrementalSaver()
        base = {"nodes": [{"id": "n1", "parameters": {"gain": 1.0}}],
                "connections": []}
        saver.set_base_snapshot(base, generation=1)
        v2 = {"nodes": [{"id": "n1", "parameters": {"gain": 2.0}}],
              "connections": []}
        raw = serialize(saver.compute_delta(v2))  # base_version = 1
        # The saver advances past generation 1 (e.g. a later save happened).
        saver.set_base_snapshot(v2, generation=7)
        with pytest.raises(ValueError, match="mismatch"):
            load_incremental(raw, saver=saver)

    def test_matching_version_loads(self):
        from src.features.composite.utils.serialization_formats import (
            IncrementalSaver, load_incremental, serialize,
        )
        saver = IncrementalSaver()
        base = {"nodes": [{"id": "n1", "parameters": {"gain": 1.0}}],
                "connections": []}
        saver.set_base_snapshot(base, generation=3)
        v2 = {"nodes": [{"id": "n1", "parameters": {"gain": 2.0}}],
              "connections": []}
        raw = serialize(saver.compute_delta(v2))  # base_version = 3
        loaded = load_incremental(raw, saver=saver)
        assert loaded["nodes"][0]["parameters"]["gain"] == 2.0

    def test_explicit_base_snapshot_skips_generation_check(self):
        from src.features.composite.utils.serialization_formats import (
            IncrementalSaver, load_incremental, save_incremental,
        )
        saver = IncrementalSaver()
        base = {"nodes": [{"id": "n1", "parameters": {"gain": 1.0}}],
                "connections": []}
        saver.set_base_snapshot(base, generation=1)
        v2 = {"nodes": [{"id": "n1", "parameters": {"gain": 2.0}}],
              "connections": []}
        raw = save_incremental(v2, saver)  # saver advances to gen 2
        loaded = load_incremental(raw, base_snapshot=base, saver=saver)
        assert loaded["nodes"][0]["parameters"]["gain"] == 2.0


class TestGraphDeltaValidation:
    def _saver(self):
        from src.features.composite.utils.serialization_formats import (
            IncrementalSaver,
        )
        return IncrementalSaver()

    def test_nodes_dict_raises_with_context(self):
        saver = self._saver()
        base = {"nodes": {"n1": {}}}
        with pytest.raises(ValueError, match="must be a list"):
            saver._compute_graph_delta(base, {"nodes": []})

    def test_non_dict_node_raises(self):
        saver = self._saver()
        with pytest.raises(ValueError, match="index 0"):
            saver._compute_graph_delta(
                {"nodes": []}, {"nodes": ["oops"]})

    def test_missing_id_raises(self):
        saver = self._saver()
        with pytest.raises(ValueError, match="missing or invalid 'id'"):
            saver._compute_graph_delta(
                {"nodes": []}, {"nodes": [{"type": "blur"}]})

    def test_duplicate_id_raises(self):
        saver = self._saver()
        dup = {"nodes": [{"id": "a"}, {"id": "a"}]}
        with pytest.raises(ValueError, match="duplicate node id"):
            saver._compute_graph_delta({"nodes": []}, dup)

    def test_valid_data_still_deltas(self):
        saver = self._saver()
        base = {"nodes": [{"id": "a"}], "connections": []}
        new = {"nodes": [{"id": "a"}, {"id": "b"}], "connections": []}
        delta = saver._compute_graph_delta(base, new)
        assert delta["nodes_added"] == [{"id": "b"}]


# ── AUD-4400 / AUD-4401: main window adapter ─────────────────────────────────


def _make_adapter(page):
    from unittest.mock import MagicMock
    from src.features.composite.adapters.main_window_adapter import (
        MainWindowAdapter,
    )
    adapter = MainWindowAdapter.__new__(MainWindowAdapter)
    adapter._mw = MagicMock()
    adapter._runtime = SimpleNamespace(
        page=page,
        viewer=MagicMock(),
        is_available=lambda: True,
    )
    adapter._last_presented_preview_request_id = 0
    return adapter


_VALID_GRAPH = {
    "nodes": [
        {"id": "src", "type": "constant", "position": [0, 0],
         "parameters": {"width": 4, "height": 4, "color_r": 1.0}},
    ],
    "connections": [],
}


class TestRestoreGraphFailureContainment:
    def test_corrupt_connection_data_falls_back_to_reset(self):
        from unittest.mock import MagicMock
        page = MagicMock()
        adapter = _make_adapter(page)
        # AUD-5104 (wd5-g30) made from_dict *tolerant* of malformed
        # connection entries (skipped with a warning instead of raising),
        # so a broken connection no longer triggers this containment path.
        # The containment itself is unchanged and still required for any
        # other deserialization failure — use a corrupt scalar field that
        # from_dict still rejects (fps must be float-convertible).
        corrupt = {
            "nodes": [{"id": "src", "type": "constant",
                       "parameters": {"width": 4, "height": 4}}],
            "connections": [{"src_node_id": "ghost"}],  # now skipped
            "fps": "not-a-number",                      # still raises
        }
        adapter.restore_graph(corrupt, "")  # must not raise
        page.reset_to_empty_workspace.assert_called_once()

    def test_malformed_connections_are_skipped_not_fatal(self):
        """AUD-5104: per-entry tolerance — a graph whose only defect is a
        malformed connection entry loads with the valid parts intact
        instead of resetting the workspace."""
        from unittest.mock import MagicMock
        from src.features.composite.core.node_engine import NodeGraph
        page = MagicMock()
        adapter = _make_adapter(page)
        corrupt = {
            "nodes": [{"id": "src", "type": "constant",
                       "parameters": {"width": 4, "height": 4}}],
            "connections": [{"src_node_id": "ghost"}],  # missing keys
        }
        adapter.restore_graph(corrupt, "")
        page.reset_to_empty_workspace.assert_not_called()
        assert isinstance(page._node_graph, NodeGraph)
        assert page._node_graph.get_node("src") is not None
        assert page._node_graph.connections == []

    def test_empty_nodes_resets_page(self):
        from unittest.mock import MagicMock
        page = MagicMock()
        adapter = _make_adapter(page)
        adapter.restore_graph({"nodes": [], "connections": []}, "")
        page.reset_to_empty_workspace.assert_called_once()

    def test_valid_graph_restores_normally(self):
        from src.features.composite.core.node_engine import NodeGraph
        from unittest.mock import MagicMock
        page = MagicMock()
        adapter = _make_adapter(page)
        adapter.restore_graph(_VALID_GRAPH, "")
        page.reset_to_empty_workspace.assert_not_called()
        assert isinstance(page._node_graph, NodeGraph)
        page._node_editor.build_from_graph.assert_called_once()

    def test_fallback_when_page_reset_missing(self):
        """No reset helper → fresh empty graph replaces the stale one."""
        from src.features.composite.core.node_engine import NodeGraph
        from unittest.mock import MagicMock
        page = MagicMock(spec=["_node_graph", "_node_editor"])
        page._node_graph = "STALE"
        adapter = _make_adapter(page)
        adapter.restore_graph({"nodes": [], "connections": []}, "")
        assert isinstance(page._node_graph, NodeGraph)
        page._node_editor.build_from_graph.assert_called_once()


class TestMediaPoolPosterIdentityCaching:
    def _poster_adapter(self):
        from unittest.mock import MagicMock
        adapter = _make_adapter(MagicMock())
        adapter._comp_clip_asset_id = ""
        adapter._comp_clip_asset_id_valid = False
        adapter._comp_clip_asset_id_time = 0.0
        adapter._runtime.page.get_composite_data = MagicMock(
            return_value={"comp_asset_id": "asset-1"})
        return adapter

    def test_identity_cached_within_ttl(self):
        adapter = self._poster_adapter()
        page = adapter._runtime.page
        assert adapter._resolve_comp_clip_asset_id(page) == "asset-1"
        assert adapter._resolve_comp_clip_asset_id(page) == "asset-1"
        assert page.get_composite_data.call_count == 1

    def test_graph_change_invalidates_cache(self):
        adapter = self._poster_adapter()
        page = adapter._runtime.page
        adapter._resolve_comp_clip_asset_id(page)
        adapter._on_composite_graph_changed_for_poster("cid", {})
        assert adapter._resolve_comp_clip_asset_id(page) == "asset-1"
        assert page.get_composite_data.call_count == 2

    def test_user_edit_invalidates_cache(self):
        adapter = self._poster_adapter()
        page = adapter._runtime.page
        adapter._resolve_comp_clip_asset_id(page)
        adapter._on_user_edit_started_for_poster("cid")
        adapter._resolve_comp_clip_asset_id(page)
        assert page.get_composite_data.call_count == 2

    def test_publish_uses_cache_across_frames(self, qapp):
        import numpy as np
        adapter = self._poster_adapter()
        media_pool = adapter._mw.media_pool_panel
        frame = np.zeros((2, 2, 3), dtype=np.float32)
        adapter._publish_comp_clip_media_pool_preview(frame, 2, 2)
        adapter._publish_comp_clip_media_pool_preview(frame, 2, 2)
        assert media_pool.set_comp_clip_preview.call_count == 2
        assert adapter._runtime.page.get_composite_data.call_count == 1


# ── AUD-4500 / AUD-4501: composite plan ──────────────────────────────────────


class TestCompositePlanImmutability:
    def test_parameters_mutation_cannot_change_hash(self):
        from src.features.composite.ai.composite_plan import (
            CompositePlan, NodeGraphOperation,
        )
        params = {"gain": 1.0}
        op = NodeGraphOperation(op_id="1", kind="add_node",
                                node_type="grade", parameters=params)
        plan = CompositePlan(
            plan_id="p", brief_id="b", project_id="pr", timeline_id="t",
            base_generation=0, operations=(op,))
        before = plan.plan_hash()
        params["gain"] = 99.0  # caller mutates its dict afterwards
        assert plan.plan_hash() == before

    def test_parameters_is_read_only(self):
        from src.features.composite.ai.composite_plan import NodeGraphOperation
        op = NodeGraphOperation(op_id="1", kind="set_param",
                                node_id="n1", parameters={"a": 1})
        with pytest.raises(TypeError):
            op.parameters["a"] = 2

    def test_brief_output_spec_frozen(self):
        from src.features.composite.ai.composite_plan import CompositeBrief
        spec = {"width": 1920}
        brief = CompositeBrief(
            brief_id="b", project_id="p", timeline_id="t",
            shot_scope=("c1",), desired_effect="hud_overlay",
            output_spec=spec)
        spec["width"] = 640
        assert brief.to_dict()["output_spec"]["width"] == 1920
        with pytest.raises(TypeError):
            brief.output_spec["width"] = 1

    def test_to_dict_and_equality_survive_freezing(self):
        from src.features.composite.ai.composite_plan import (
            CompositePlan, NodeGraphOperation,
        )
        a = NodeGraphOperation(op_id="1", kind="add_node",
                               node_type="blur", parameters={"x": 1})
        b = NodeGraphOperation(op_id="1", kind="add_node",
                               node_type="blur", parameters={"x": 1})
        assert a == b
        plan = CompositePlan(
            plan_id="p", brief_id="b", project_id="pr", timeline_id="t",
            base_generation=0, operations=(a,))
        assert plan.to_dict()["operations"][0]["parameters"] == {"x": 1}


class TestValidateCompositePlanGaps:
    def _plan(self, *ops):
        from src.features.composite.ai.composite_plan import CompositePlan
        return CompositePlan(
            plan_id="p", brief_id="b", project_id="pr", timeline_id="t",
            base_generation=0, operations=ops)

    def test_empty_node_type_is_blocked(self):
        from src.features.composite.ai.composite_plan import (
            NodeGraphOperation, validate_composite_plan,
        )
        plan = self._plan(NodeGraphOperation(
            op_id="1", kind="add_node", node_type=""))
        report = validate_composite_plan(plan, available_node_types=("blur",))
        assert any(f.code == "MISSING_NODE_TYPE" for f in report.errors)

    def test_empty_node_id_blocked_for_target_ops(self):
        from src.features.composite.ai.composite_plan import (
            NODE_OP_REMOVE_NODE, NODE_OP_SET_PARAM, NodeGraphOperation,
            validate_composite_plan,
        )
        for kind in (NODE_OP_SET_PARAM, NODE_OP_REMOVE_NODE):
            plan = self._plan(NodeGraphOperation(
                op_id="1", kind=kind, node_id=""))
            report = validate_composite_plan(plan)
            assert any(f.code == "MISSING_NODE_ID" for f in report.errors), kind

    def test_add_node_without_node_id_is_fine(self):
        from src.features.composite.ai.composite_plan import (
            NodeGraphOperation, validate_composite_plan,
        )
        plan = self._plan(NodeGraphOperation(
            op_id="1", kind="add_node", node_type="blur"))
        report = validate_composite_plan(
            plan, available_node_types=("blur",), gpu_available=True)
        assert not report.errors

    def test_unknown_node_reference_blocked_when_registry_given(self):
        from src.features.composite.ai.composite_plan import (
            NodeGraphOperation, validate_composite_plan,
        )
        plan = self._plan(NodeGraphOperation(
            op_id="1", kind="set_param", node_id="ghost", parameters={"a": 1}))
        report = validate_composite_plan(plan, existing_node_ids={"real"})
        assert any(f.code == "UNKNOWN_NODE_REFERENCE" for f in report.errors)

    def test_existing_node_reference_passes(self):
        from src.features.composite.ai.composite_plan import (
            NodeGraphOperation, validate_composite_plan,
        )
        plan = self._plan(NodeGraphOperation(
            op_id="1", kind="set_param", node_id="real", parameters={"a": 1}))
        report = validate_composite_plan(plan, existing_node_ids={"real"})
        assert not report.errors


# ── AUD-4600 / AUD-4601 / AUD-4602: bake service ────────────────────────────


def _bake_graph():
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.blur_node import BlurNode
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.invert_node import InvertNode
    graph = NodeGraph()
    for node in (ConstantNode("src"), InvertNode("inv"), BlurNode("blur")):
        graph.add_node(node)
    graph.connect("src", "output", "inv", "image")
    graph.connect("inv", "result", "blur", "image")
    return graph


class TestBakeRewireFailure:
    def test_failed_rewire_raises_and_keeps_target_enabled(
            self, tmp_path, monkeypatch):
        from src.features.composite import bake_service
        graph = _bake_graph()
        plan = bake_service.plan_bake(graph, "inv", str(tmp_path), 1, 3)
        # connect() returns False (no exception) when validation fails —
        # only the bake→consumer edge is made to fail; the restore of the
        # original target edge still goes through.
        real_connect = graph.connect

        def failing_connect(src, src_port, dst, dst_port):
            if src == plan.bake_node_id:
                return False
            return real_connect(src, src_port, dst, dst_port)

        monkeypatch.setattr(graph, "connect", failing_connect)
        with pytest.raises(RuntimeError, match="rewire failed"):
            bake_service.apply_bake_rewrite(graph, plan)
        # Target stays enabled, no half-rewritten undo payload is captured.
        assert graph.get_node("inv").disabled is False
        assert plan.after_graph == {}
        # The consumer is still wired to the original target.
        sources = [c.src_node_id for c in graph.connections
                   if c.dst_node_id == "blur"]
        assert sources == ["inv"]

    def test_unbake_failed_rewire_keeps_bake_node(self, tmp_path, monkeypatch):
        from src.features.composite import bake_service
        graph = _bake_graph()
        plan = bake_service.plan_bake(graph, "inv", str(tmp_path), 1, 3)
        bake_service.apply_bake_rewrite(graph, plan)

        real_connect = graph.connect

        def failing_connect(src, src_port, dst, dst_port):
            if src == "inv":  # the restored target connection fails
                return False
            return real_connect(src, src_port, dst, dst_port)

        unbake = bake_service.plan_unbake(graph, plan.bake_node_id)
        monkeypatch.setattr(graph, "connect", failing_connect)
        with pytest.raises(RuntimeError, match="unbake rewire failed"):
            bake_service.apply_unbake_rewrite(graph, unbake)
        # Bake node survives with its connections; consumer still fed by it.
        assert graph.get_node(plan.bake_node_id) is not None
        sources = [c.src_node_id for c in graph.connections
                   if c.dst_node_id == "blur"]
        assert sources == [plan.bake_node_id]
        assert graph.get_node("inv").disabled is True

    def test_multi_output_target_rejected_at_plan_time(self):
        from src.features.composite import bake_service

        class _Port:
            def __init__(self, name):
                self.name = name

        class _Node:
            outputs = [_Port("result"), _Port("matte")]
            display_title = "Keyer"

        class _Conn:
            def __init__(self, src, src_port, dst, dst_port):
                self.src_node_id, self.src_port_name = src, src_port
                self.dst_node_id, self.dst_port_name = dst, dst_port

        class _Graph:
            outputs_target = _Node()
            connections = [
                _Conn("tgt", "result", "c1", "image"),
                _Conn("tgt", "matte", "c2", "image"),
            ]

            def get_node(self, node_id):
                return self.outputs_target if node_id == "tgt" else None

            def to_dict(self):
                return {}

        with pytest.raises(ValueError, match="matte"):
            bake_service.plan_bake(_Graph(), "tgt", "/cache", 1, 3)

    def test_secondary_consumer_appearing_after_plan_rejected(
            self, tmp_path):
        """Graph changed between plan and apply → apply refuses (AUD-4602)."""
        from src.features.composite import bake_service
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.blur_node import BlurNode
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.invert_node import InvertNode
        graph = NodeGraph()
        for node in (ConstantNode("src"), InvertNode("inv"),
                     BlurNode("blur"), BlurNode("blur2")):
            graph.add_node(node)
        graph.connect("src", "output", "inv", "image")
        graph.connect("inv", "result", "blur", "image")
        plan = bake_service.plan_bake(graph, "inv", str(tmp_path), 1, 3)
        # A second consumer attaches to a non-primary port after planning.
        # InvertNode has one output port; emulate a secondary port via a
        # connection whose port name differs from the primary.
        real_outputs = graph.get_node("inv").outputs
        try:
            from src.features.composite.nodes.base_node import Port
            graph.get_node("inv").outputs = list(real_outputs) + [
                Port("matte", "output", "image")]
            graph.connect("inv", "matte", "blur2", "image")
            with pytest.raises(RuntimeError, match="non-primary"):
                bake_service.apply_bake_rewrite(graph, plan)
        finally:
            graph.get_node("inv").outputs = real_outputs
        # Nothing was mutated by the refused rewrite.
        assert graph.get_node("inv").disabled is False
        assert plan.after_graph == {}

    def test_successful_bake_unbake_roundtrip_after_fixes(self, tmp_path):
        from src.features.composite import bake_service
        graph = _bake_graph()
        plan = bake_service.plan_bake(graph, "inv", str(tmp_path), 1, 3)
        bake_service.apply_bake_rewrite(graph, plan)
        sources = [c.src_node_id for c in graph.connections
                   if c.dst_node_id == "blur"]
        assert sources == [plan.bake_node_id]  # exactly one source (no dup)
        unbake = bake_service.plan_unbake(graph, plan.bake_node_id)
        bake_service.apply_unbake_rewrite(graph, unbake)
        sources = [c.src_node_id for c in graph.connections
                   if c.dst_node_id == "blur"]
        assert sources == ["inv"]
        assert graph.get_node(plan.bake_node_id) is None


if __name__ == "__main__":
    unittest.main()
