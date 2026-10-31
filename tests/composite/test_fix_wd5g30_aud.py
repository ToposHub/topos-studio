"""Batch D (wd5-g30) regression tests for Major audit findings.

Covers:
  * AUD-4603  — bake/unbake round-trip must not freeze i18n titles as
                permanent user_label renames
  * AUD-48008 — Y-only (luminance) EXR must decode to grey, not black
  * AUD-4900  — compiler projection is record-only; dry-run compile never
                mutates live Merge nodes
  * AUD-4901  — per-layer opacity projection: top layer owns the merge fg,
                lower layers never dim the whole group above them
  * AUD-4906  — duplicate_layer of a Precomp deep-copies the child stack
  * AUD-4907  — nested-stack layers resolve their owning stack for
                property/move/duplicate/parent operations
  * AUD-5000  — node_graph_cycle is actually reported (ValueError path)
  * AUD-5004  — audio assets classify as audio and are rejected for layer
                drops instead of becoming broken image_input layers
  * AUD-5008  — registry-only (plugin) node types get a synthesized
                capability instead of None/placeholder
  * AUD-5100  — remove_node_and_reconnect cleans expressions/bindings/parents
  * AUD-5101  — remove_node_and_reconnect marks downstream dirty while the
                edges still exist
  * AUD-5102  — evaluate() snapshots the node map under the graph lock
  * AUD-5103  — to_dict/validate/migrate_* hold the graph lock
  * AUD-5104  — from_dict survives malformed node/connection entries
  * AUD-5105  — from_dict copies + normalizes groups/viewport/viewer state
  * AUD-5200  — the application-level plugin directory resolves to
                <repo>/plugins/node_plugins
"""

import copy
import os
import sys
import threading
import unittest
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.bake_service import (
    BAKED_TITLE_SUFFIX,
    BAKE_ORIGINAL_USER_LABEL_PARAM,
    apply_bake_rewrite,
    apply_unbake_rewrite,
    plan_bake,
    plan_unbake,
)
from src.features.composite.core.node_engine import (
    NodeGraph, NODE_REGISTRY, Connection, register_node_type,
)
from src.features.composite.core.layer_model import Layer, LayerStack
from src.features.composite.core.layer_stack_compiler import LayerStackCompiler
from src.features.composite.core.layer_stack_controller import (
    LayerStackController,
)
from src.features.composite.core.layer_stack_validator import (
    LayerStackValidator,
)
from src.features.composite.core.media_drop_payload import (
    MediaDropPayload, MEDIA_TYPE_AUDIO, MEDIA_TYPE_IMAGE,
    MIME_TYPE_MEDIA_POOL_ASSETS, _classify_media_type, resolve_drop_payloads,
)
from src.features.composite.core import node_capabilities
from src.features.composite.core import node_plugin
from src.features.composite.nodes.base_node import Node, Port, ParamDef
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.invert_node import InvertNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.transform_node import TransformNode

pytestmark = pytest.mark.composite_core


# ── Shared fixtures ───────────────────────────────────────────────────────────


def _bake_graph():
    """constant → invert → blur, consumed by a stand-in viewer node."""
    graph = NodeGraph()
    src = ConstantNode("src")
    inv = InvertNode("inv")
    blur = BlurNode("blur")
    viewer = BlurNode("viewer")
    for node in (src, inv, blur, viewer):
        graph.add_node(node)
    graph.connect("src", "result", "inv", "image")
    graph.connect("inv", "result", "blur", "image")
    graph.connect("blur", "result", "viewer", "image")
    return graph


def _stack_graph(num_layers: int):
    """Output + N (source, transform) layer chains registered on a stack."""
    graph = NodeGraph()
    out = OutputNode(node_id="out")
    graph.add_node(out)
    stack = LayerStack(stack_id="main", output_node_id="out")
    layers = {}
    for i in range(num_layers):
        src = ConstantNode(node_id=f"src{i}")
        t = TransformNode(node_id=f"t{i}")
        graph.add_node(src)
        graph.add_node(t)
        graph.connect(f"src{i}", "result", f"t{i}", "image")
        lid = f"L{i}"
        layers[lid] = Layer(layer_id=lid, name=f"Layer {i}",
                            root_node_id=f"t{i}", source_node_id=f"src{i}")
        stack.layer_ids.append(lid)
    graph.set_layer_stack(stack)
    for layer in layers.values():
        graph.upsert_layer(stack.stack_id, layer)
    return graph, stack, layers


def _apply_changes(graph, changes):
    """Apply CompileChange records the way the controller does."""
    from src.features.composite.core.layer_stack_compiler import MERGE_NODE_TYPE
    from src.features.composite.nodes.blend_node import MergeNode
    for ch in changes:
        if ch.kind == "create_node":
            if ch.node_type == MERGE_NODE_TYPE:
                node = MergeNode(node_id=ch.node_id)
            else:
                node = ConstantNode(node_id=ch.node_id)
            graph.add_node(node)
        elif ch.kind == "remove_node":
            graph.remove_node(ch.node_id)
        elif ch.kind == "connect":
            if ch.src_port_name == "__parent__":
                node = graph.get_node(ch.src_node_id)
                if node is not None:
                    node.parent_id = ch.dst_port_name or None
            else:
                graph.connect(ch.src_node_id, ch.src_port_name,
                              ch.dst_node_id, ch.dst_port_name)
        elif ch.kind == "set_parameter":
            graph.set_node_parameter(ch.node_id, ch.param_name, ch.value)
        elif ch.kind == "disconnect":
            graph.disconnect(ch.src_node_id, ch.src_port_name,
                             ch.dst_node_id, ch.dst_port_name)


class _FakeMimeData:
    """Minimal QMimeData stand-in (mirrors test_media_drop_payload)."""

    def __init__(self, formats=None, text="", urls=()):
        self._formats = formats or {}
        self._text = text
        self._urls = list(urls)

    def data(self, mime):
        payload = self._formats.get(mime)
        if payload is None:
            return None

        class _QBA:
            def __init__(self, raw: bytes):
                self._raw = raw

            def size(self):
                return len(self._raw)

            def __bytes__(self):
                return self._raw

        return _QBA(payload.encode("utf-8"))

    def text(self):
        return self._text

    def urls(self):
        return self._urls


# ── AUD-4603: bake/unbake user_label round-trip ───────────────────────────────


class TestAud4603BakeUserLabel(unittest.TestCase):

    def test_unbake_clears_user_label_for_never_renamed_node(self):
        graph = _bake_graph()
        target = graph.get_node("blur")
        self.assertIsNone(target.user_label)  # never renamed
        target.instance_label = "2"           # display "Blur2" != node_title
        plan = plan_bake(graph, "blur", "/cache", 1, 3)
        apply_bake_rewrite(graph, plan)
        self.assertTrue(plan.original_user_label is None)
        self.assertTrue(target.user_label.endswith(BAKED_TITLE_SUFFIX))

        unplan = plan_unbake(graph, plan.bake_node_id)
        apply_unbake_rewrite(graph, unplan)
        # The pre-fix code froze "Blur2" into user_label; the fix restores
        # None so i18n / instance numbering keep owning the title.
        self.assertIsNone(target.user_label)

    def test_unbake_restores_explicit_user_label(self):
        graph = _bake_graph()
        target = graph.get_node("blur")
        target.user_label = "My Blur"
        plan = plan_bake(graph, "blur", "/cache", 1, 3)
        apply_bake_rewrite(graph, plan)

        unplan = plan_unbake(graph, plan.bake_node_id)
        self.assertEqual(unplan.original_user_label, "My Blur")
        apply_unbake_rewrite(graph, unplan)
        self.assertEqual(target.user_label, "My Blur")

    def test_bake_node_records_label_param(self):
        graph = _bake_graph()
        target = graph.get_node("blur")
        target.user_label = "My Blur"
        plan = plan_bake(graph, "blur", "/cache", 1, 3)
        apply_bake_rewrite(graph, plan)
        bake_node = graph.get_node(plan.bake_node_id)
        self.assertEqual(
            bake_node.parameters[BAKE_ORIGINAL_USER_LABEL_PARAM], "My Blur")
        # "" is the persisted sentinel for "no user label".
        target2_graph = _bake_graph()
        plan2 = plan_bake(target2_graph, "blur", "/cache", 1, 3)
        apply_bake_rewrite(target2_graph, plan2)
        self.assertEqual(target2_graph.get_node(
            plan2.bake_node_id).parameters[BAKE_ORIGINAL_USER_LABEL_PARAM], "")

    def test_legacy_bake_without_param_recovers_none_label(self):
        """Pre-fix bakes (no recorded param) with an auto title must still
        restore None rather than freezing the translated title."""
        graph = _bake_graph()
        target = graph.get_node("blur")
        target.instance_label = "3"
        plan = plan_bake(graph, "blur", "/cache", 1, 3)
        apply_bake_rewrite(graph, plan)
        # Simulate a legacy bake node: the provenance param is absent.
        del graph.get_node(plan.bake_node_id).parameters[
            BAKE_ORIGINAL_USER_LABEL_PARAM]
        unplan = plan_unbake(graph, plan.bake_node_id)
        self.assertIsNone(unplan.original_user_label)
        apply_unbake_rewrite(graph, unplan)
        self.assertIsNone(target.user_label)


# ── AUD-48008: Y-only EXR decode ──────────────────────────────────────────────


class _FakePixelType:
    HALF = 1
    FLOAT = 2
    UINT = 3

    def __init__(self, value):
        self.value = value


class _FakeImathModule(SimpleNamespace):
    PixelType = _FakePixelType


class _FakeHeaderChannel:
    def __init__(self, ptype=_FakePixelType.FLOAT):
        self.type = ptype


class _FakeDataWindow:
    def __init__(self, width, height):
        self.min = SimpleNamespace(x=0, y=0)
        self.max = SimpleNamespace(x=width - 1, y=height - 1)


class _FakeInputFile:
    """2.x-style InputFile serving a Y-only EXR."""

    def __init__(self, path, y_bytes, width, height):
        self._y = y_bytes
        self._w = width
        self._h = height

    def header(self):
        return {
            "dataWindow": _FakeDataWindow(self._w, self._h),
            "channels": {"Y": _FakeHeaderChannel()},
        }

    def channel(self, name, pixel_type):
        if name == "Y":
            return self._y
        raise KeyError(name)


class TestAud48008YOnlyExr(unittest.TestCase):

    def _inject_openexr(self, file_obj):
        import numpy as np
        exr_mod = SimpleNamespace(
            InputFile=lambda path: file_obj,
            MultiPartInputFile=None,
        )
        # ``None`` attribute presence matters: hasattr check drives dispatch.
        imath_mod = _FakeImathModule()
        sys.modules["OpenEXR"] = exr_mod
        sys.modules["Imath"] = imath_mod
        self.addCleanup(sys.modules.pop, "OpenEXR", None)
        self.addCleanup(sys.modules.pop, "Imath", None)
        return np

    def test_y_only_exr_broadcasts_luminance(self):
        import numpy as np
        w, h = 4, 3
        y = (np.arange(w * h, dtype=np.float32) * 0.25).reshape(h, w)
        fake = _FakeInputFile("y_only.exr", y.tobytes(), w, h)
        self._inject_openexr(fake)

        from src.features.composite.core.image_loader import _decode_exr
        result = _decode_exr("y_only.exr", mtime=0.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.components, 3)
        # R == G == B == Y: grey image, not the pre-fix all-black fill.
        np.testing.assert_allclose(result.data[..., 0], y)
        np.testing.assert_allclose(result.data[..., 1], y)
        np.testing.assert_allclose(result.data[..., 2], y)
        self.assertTrue((result.data != 0).any())

    def test_y_backfills_partially_missing_channels(self):
        import numpy as np
        w, h = 2, 2
        y = np.full((h, w), 0.5, dtype=np.float32)

        class _PartialFile(_FakeInputFile):
            def __init__(self):
                super().__init__("partial.exr", y.tobytes(), w, h)
                self._r = np.full((h, w), 1.0, dtype=np.float32).tobytes()

            def header(self):
                return {
                    "dataWindow": _FakeDataWindow(w, h),
                    "channels": {"R": _FakeHeaderChannel(),
                                 "Y": _FakeHeaderChannel()},
                }

            def channel(self, name, pixel_type):
                if name == "R":
                    return self._r
                if name == "Y":
                    return self._y
                raise KeyError(name)

        self._inject_openexr(_PartialFile())
        from src.features.composite.core.image_loader import _decode_exr
        result = _decode_exr("partial.exr", mtime=0.0)
        np.testing.assert_allclose(result.data[..., 0], 1.0)  # R kept
        np.testing.assert_allclose(result.data[..., 1], 0.5)  # G <- Y
        np.testing.assert_allclose(result.data[..., 2], 0.5)  # B <- Y


# ── AUD-4900: compiler projection is record-only ─────────────────────────────


class TestAud4900DryRunCompile(unittest.TestCase):

    def test_compile_does_not_mutate_existing_merge_params(self):
        graph, stack, layers = _stack_graph(2)
        compiler = LayerStackCompiler(graph)
        first = compiler.compile(stack, layers)
        _apply_changes(graph, first.changes)
        stack.generated_merge_node_ids = list(first.merge_node_ids)
        merge = graph.get_node(first.merge_node_ids[0])
        self.assertEqual(merge.parameters["opacity"], 1.0)

        # Drift the layer state, then run a *dry* compile (no apply).
        layers["L0"].opacity = 0.25
        layers["L0"].blend_mode = "screen"
        result = compiler.compile(stack, layers)

        # The live node is untouched by the compile itself...
        self.assertEqual(merge.parameters["opacity"], 1.0)
        self.assertEqual(merge.parameters["blend_mode"], "over")
        # ...and the projection is carried as set_parameter changes.
        params = {(c.node_id, c.param_name): c.value
                  for c in result.changes if c.kind == "set_parameter"}
        self.assertEqual(params[(first.merge_node_ids[0], "opacity")], 0.25)
        self.assertEqual(
            params[(first.merge_node_ids[0], "blend_mode")], "screen")

    def test_steady_state_compile_emits_no_param_changes(self):
        graph, stack, layers = _stack_graph(3)
        compiler = LayerStackCompiler(graph)
        first = compiler.compile(stack, layers)
        _apply_changes(graph, first.changes)
        stack.generated_merge_node_ids = list(first.merge_node_ids)

        second = compiler.compile(stack, layers)
        param_changes = [c for c in second.changes
                         if c.kind == "set_parameter"]
        self.assertEqual(param_changes, [],
                         "steady-state compile must be change-free so the "
                         "sync dry-run does not report false drift")

    def test_controller_sync_dry_run_leaves_graph_untouched(self):
        graph, stack, layers = _stack_graph(2)
        ctrl = LayerStackController(graph, undo_stack=None)
        ctrl.rebuild()
        merge_id = ctrl.main_stack().generated_merge_node_ids[0]
        before = copy.deepcopy(
            graph.get_node(merge_id).parameters)
        # Sync (dry-run) after a layer change: the merge must not move.
        layers["L0"].opacity = 0.4
        result = ctrl.sync_from_node_graph(repair=False)
        self.assertEqual(graph.get_node(merge_id).parameters, before)
        self.assertTrue(any("differs" in w for w in result.warnings))


# ── AUD-4901: opacity projection targets ─────────────────────────────────────


class TestAud4901OpacityProjection(unittest.TestCase):

    def test_top_layer_opacity_projects_to_first_merge(self):
        graph, stack, layers = _stack_graph(3)
        layers["L0"].opacity = 0.25
        result = LayerStackCompiler(graph).compile(stack, layers)
        _apply_changes(graph, result.changes)
        m0, m1 = result.merge_node_ids
        # M0's foreground is root(L0): its opacity modulates only L0.
        self.assertEqual(graph.get_node(m0).parameters["opacity"], 0.25)
        # The mid layer must NOT dim the group above it via M1.
        self.assertEqual(graph.get_node(m1).parameters["opacity"], 1.0)

    def test_mid_layer_opacity_does_not_dim_group_above(self):
        graph, stack, layers = _stack_graph(3)
        layers["L1"].opacity = 0.5  # mid layer
        result = LayerStackCompiler(graph).compile(stack, layers)
        _apply_changes(graph, result.changes)
        m0, m1 = result.merge_node_ids
        # Pre-fix behaviour wrote 0.5 onto M1 whose fg is (A over B) —
        # dimming A as a side effect.  Now no merge carries it...
        self.assertEqual(graph.get_node(m1).parameters["opacity"], 1.0)
        self.assertEqual(graph.get_node(m0).parameters["opacity"], 1.0)
        # ...and the unrepresentable value is surfaced, not swallowed.
        self.assertTrue(any("opacity" in w for w in result.warnings))

    def test_bottom_layer_opacity_reported_not_dropped(self):
        graph, stack, layers = _stack_graph(2)
        layers["L1"].opacity = 0.7  # bottom layer, no owning merge
        result = LayerStackCompiler(graph).compile(stack, layers)
        _apply_changes(graph, result.changes)
        m0 = result.merge_node_ids[0]
        self.assertEqual(graph.get_node(m0).parameters["opacity"], 1.0)
        self.assertTrue(any("opacity" in w for w in result.warnings))

    def test_single_layer_no_merge_no_crash(self):
        graph, stack, layers = _stack_graph(1)
        layers["L0"].opacity = 0.3
        result = LayerStackCompiler(graph).compile(stack, layers)
        self.assertTrue(result.ok)
        self.assertEqual(result.merge_node_ids, [])
        self.assertTrue(any("opacity" in w for w in result.warnings))

    def test_root_opacity_fallback_when_supported(self):
        class _OpacityRootNode(ConstantNode):
            supports_layer_opacity = True

        graph, stack, layers = _stack_graph(2)
        # Swap the bottom layer's root for a node that declares opacity.
        root = _OpacityRootNode(node_id="opaque_root")
        graph.add_node(root)
        layers["L1"].root_node_id = "opaque_root"
        layers["L1"].opacity = 0.6
        result = LayerStackCompiler(graph).compile(stack, layers)
        _apply_changes(graph, result.changes)
        self.assertEqual(
            graph.get_node("opaque_root").parameters["opacity"], 0.6)
        self.assertFalse(any("opacity" in w for w in result.warnings))

    def test_live_top_layer_still_owns_first_merge(self):
        """Disabled top layer: the new live top's opacity drives M0."""
        graph, stack, layers = _stack_graph(3)
        layers["L0"].enabled = False
        layers["L1"].opacity = 0.25
        layers["L1"].blend_mode = "screen"
        result = LayerStackCompiler(graph).compile(stack, layers)
        _apply_changes(graph, result.changes)
        merge = graph.get_node(result.merge_node_ids[0])
        self.assertEqual(merge.parameters["opacity"], 0.25)
        self.assertEqual(merge.parameters["blend_mode"], "screen")


# ── AUD-4906: Precomp duplication deep-copies the child stack ─────────────────


def _precomp_with_child():
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    ctrl = LayerStackController(graph, undo_stack=None)
    precomp_layer_id = ctrl.create_precomp_layer("Nested")
    precomp_layer = graph.get_layer(precomp_layer_id)
    child_id = graph.get_node(precomp_layer.root_node_id).parameters[
        "child_stack_id"]
    for i in range(2):
        src = ConstantNode(node_id=f"csrc{i}")
        graph.add_node(src)
        ctrl.add_media_layer(f"Inner {i}", f"csrc{i}", stack_id=child_id)
    return graph, ctrl, precomp_layer_id, child_id


class TestAud4906DuplicatePrecomp(unittest.TestCase):

    def test_duplicate_precomp_gets_independent_child_stack(self):
        graph, ctrl, precomp_layer_id, child_id = _precomp_with_child()
        new_layer_id = ctrl.duplicate_layer(precomp_layer_id)
        self.assertIsNotNone(new_layer_id)
        old_root = graph.get_node(
            graph.get_layer(precomp_layer_id).root_node_id)
        new_root = graph.get_node(
            graph.get_layer(new_layer_id).root_node_id)
        new_child_id = new_root.parameters["child_stack_id"]
        self.assertNotEqual(new_child_id, child_id)
        self.assertIsNotNone(graph.get_layer_stack(new_child_id))
        # The duplicated stack carries its own layers with fresh ids.
        new_stack = graph.get_layer_stack(new_child_id)
        self.assertEqual(len(new_stack.layer_ids), 2)
        old_stack = graph.get_layer_stack(child_id)
        self.assertFalse(set(old_stack.layer_ids)
                         & set(new_stack.layer_ids))

    def test_deleting_original_keeps_duplicate_child_stack(self):
        graph, ctrl, precomp_layer_id, child_id = _precomp_with_child()
        new_layer_id = ctrl.duplicate_layer(precomp_layer_id)
        new_child_id = graph.get_node(graph.get_node(
            graph.get_layer(new_layer_id).root_node_id).id).parameters[
                "child_stack_id"]
        # Removing the original precomp reclaims ITS child stack only.
        ctrl.remove_layer(precomp_layer_id)
        self.assertIsNone(graph.get_layer_stack(child_id))
        self.assertIsNotNone(graph.get_layer_stack(new_child_id))
        for lid in graph.get_layer_stack(new_child_id).layer_ids:
            self.assertIsNotNone(graph.get_layer(lid))

    def test_nested_precomp_duplicate_recurses(self):
        from src.features.composite.nodes.precomp_node import PrecompNode
        graph, ctrl, precomp_layer_id, child_id = _precomp_with_child()
        # Build a nested precomp layer inside the child stack directly
        # (create_precomp_layer only targets the main stack).
        deep_node = PrecompNode(node_id="deep_precomp")
        deep_node.set_parameter("child_stack_id", "deep_stack")
        deep_node.set_parameter("precomp_name", "Deeper")
        graph.add_node(deep_node)
        deep_stack = LayerStack(stack_id="deep_stack", name="Deeper",
                                output_node_id="deep_precomp")
        graph.set_layer_stack(deep_stack)
        deep_layer = Layer(layer_id="deep_layer", name="Deeper",
                           root_node_id="deep_precomp",
                           layer_type="precomp")
        graph.get_layer_stack(child_id).layer_ids.insert(0, "deep_layer")
        graph.upsert_layer(child_id, deep_layer)
        deep_src = ConstantNode(node_id="deep_src")
        graph.add_node(deep_src)
        ctrl.add_media_layer("Deep", "deep_src", stack_id="deep_stack")
        ctrl._recompile(graph.get_layer_stack(child_id))

        new_layer_id = ctrl.duplicate_layer(precomp_layer_id)
        new_child_id = graph.get_node(graph.get_layer(
            new_layer_id).root_node_id).parameters["child_stack_id"]
        new_stack = graph.get_layer_stack(new_child_id)
        nested_types = [
            graph.get_node(graph.get_layer(lid).root_node_id).node_type
            for lid in new_stack.layer_ids
        ]
        self.assertIn("precomp", nested_types)
        # The nested precomp inside the copy points at its own deep stack.
        copied_deep_layer_ids = [
            lid for lid in new_stack.layer_ids
            if graph.get_node(graph.get_layer(lid).root_node_id).node_type
            == "precomp"
        ]
        copied_deep_root = graph.get_node(
            graph.get_layer(copied_deep_layer_ids[0]).root_node_id)
        copied_deep_id = copied_deep_root.parameters["child_stack_id"]
        self.assertNotEqual(copied_deep_id, "deep_stack")
        self.assertIsNotNone(graph.get_layer_stack(copied_deep_id))


# ── AUD-4907: nested-stack layer operations ──────────────────────────────────


class TestAud4907NestedStackOps(unittest.TestCase):

    def _nested(self):
        graph, ctrl, precomp_layer_id, child_id = _precomp_with_child()
        child_layer_ids = list(graph.get_layer_stack(child_id).layer_ids)
        return graph, ctrl, child_id, child_layer_ids

    def test_set_parent_within_nested_stack(self):
        graph, ctrl, child_id, lids = self._nested()
        self.assertTrue(ctrl.set_layer_parent(lids[1], lids[0]))
        self.assertEqual(
            graph.get_layer(lids[1]).parent_layer_id, lids[0])

    def test_move_layer_within_nested_stack(self):
        graph, ctrl, child_id, lids = self._nested()
        self.assertTrue(ctrl.move_layer(lids[0], 1))
        self.assertEqual(graph.get_layer_stack(child_id).layer_ids[0],
                         lids[1])

    def test_duplicate_layer_within_nested_stack(self):
        graph, ctrl, child_id, lids = self._nested()
        new_id = ctrl.duplicate_layer(lids[0])
        self.assertIsNotNone(new_id)
        self.assertIn(new_id, graph.get_layer_stack(child_id).layer_ids)

    def test_track_matte_within_nested_stack(self):
        graph, ctrl, child_id, lids = self._nested()
        self.assertTrue(ctrl.set_layer_track_matte(lids[0], lids[1]))
        self.assertEqual(
            graph.get_layer(lids[0]).track_matte_layer_id, lids[1])

    def test_set_property_uses_owning_stack(self):
        graph, ctrl, child_id, lids = self._nested()
        self.assertTrue(ctrl.set_layer_property(lids[0], "opacity", 0.5))
        self.assertEqual(graph.get_layer(lids[0]).opacity, 0.5)


# ── AUD-5000: validator cycle detection ───────────────────────────────────────


class TestAud5000ValidatorCycle(unittest.TestCase):

    def test_cycle_is_reported_as_error(self):
        graph, stack, layers = _stack_graph(2)
        # connect() refuses cycles — graft one the way a corrupt file or a
        # low-level editor would: directly into the connection list.
        graph.connections.append(Connection("t0", "result", "src0", "image"))
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.has("node_graph_cycle"))
        self.assertFalse(report.ok)

    def test_acyclic_graph_stays_clean(self):
        graph, stack, layers = _stack_graph(2)
        result = LayerStackCompiler(graph).compile(stack, layers)
        _apply_changes(graph, result.changes)
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertFalse(report.has("node_graph_cycle"))
        self.assertTrue(report.ok)

    def test_unexpected_sort_failure_is_diagnosed(self):
        graph, stack, layers = _stack_graph(1)

        def _boom():
            raise RuntimeError("api broke")

        graph.topological_sort = _boom
        report = LayerStackValidator(graph).validate(stack, layers)
        self.assertTrue(report.has("topology_check_failed"))


# ── AUD-5004: audio classification / rejection ────────────────────────────────


class TestAud5004Audio(unittest.TestCase):

    def test_audio_alias_classifies_as_audio(self):
        self.assertEqual(_classify_media_type("audio", ""), MEDIA_TYPE_AUDIO)
        self.assertEqual(_classify_media_type("sound", ""), MEDIA_TYPE_AUDIO)
        self.assertEqual(_classify_media_type("music", ""), MEDIA_TYPE_AUDIO)

    def test_audio_extension_classifies_as_audio(self):
        self.assertEqual(_classify_media_type(None, "x/narration.wav"),
                         MEDIA_TYPE_AUDIO)
        self.assertEqual(_classify_media_type(None, "x/beat.MP3"),
                         MEDIA_TYPE_AUDIO)

    def test_resolve_drop_payloads_skips_audio_assets(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_MEDIA_POOL_ASSETS: '["a_audio", "a_img"]',
        })

        def lookup(aid):
            if aid == "a_audio":
                return {"asset_id": aid, "path": "/m/voice.wav",
                        "media_type": "audio"}
            return {"asset_id": aid, "path": "/m/frame.exr",
                    "media_type": "image"}

        payloads = resolve_drop_payloads(mime, asset_lookup=lookup)
        self.assertEqual([p.asset_id for p in payloads], ["a_img"])

    def test_resolve_drop_payloads_skips_audio_paths(self):
        mime = _FakeMimeData(
            formats={},
            text="",
            urls=[SimpleNamespace(
                isLocalFile=lambda: True,
                toLocalFile=lambda: "/m/song.flac")])
        payloads = resolve_drop_payloads(mime)
        self.assertEqual(payloads, [])

    def test_audio_node_type_raises_instead_of_image_input(self):
        payload = MediaDropPayload(asset_id="a", path="/m/a.wav",
                                   media_type=MEDIA_TYPE_AUDIO)
        with self.assertRaises(ValueError):
            payload.node_type  # noqa: B018 — property raise is the contract

    def test_image_classification_unchanged(self):
        self.assertEqual(_classify_media_type(None, "/m/x.exr"),
                         MEDIA_TYPE_IMAGE)


# ── AUD-5008: registry-only capability synthesis ──────────────────────────────


class _SynthTestNode(Node):
    node_type = "aud5008_synth"
    node_title = "AUD5008 Synth"

    def __init__(self, node_id=None):
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]


class _SynthPlaceholderNode(_SynthTestNode):
    node_type = "aud5008_placeholder"
    IS_PLACEHOLDER = True


class TestAud5008RegistryCapability(unittest.TestCase):

    def setUp(self):
        from src.features.composite.core.node_engine import NODE_CATEGORIES
        register_node_type("aud5008_synth", _SynthTestNode)
        register_node_type("aud5008_placeholder", _SynthPlaceholderNode)

        def _cleanup(node_type):
            NODE_REGISTRY.pop(node_type, None)
            for cat, entries in list(NODE_CATEGORIES.items()):
                remaining = [e for e in entries if e[0] != node_type]
                if len(remaining) != len(entries):
                    NODE_CATEGORIES[cat] = remaining

        self.addCleanup(_cleanup, "aud5008_synth")
        self.addCleanup(_cleanup, "aud5008_placeholder")

    def test_registry_only_type_gets_capability(self):
        cap = node_capabilities.get_node_capability("aud5008_synth")
        self.assertIsNotNone(cap)
        self.assertEqual(cap.action_type, "effect")
        self.assertFalse(cap.is_placeholder)
        self.assertFalse(node_capabilities.is_placeholder_node_type(
            "aud5008_synth"))

    def test_registry_only_placeholder_honoured(self):
        cap = node_capabilities.get_node_capability("aud5008_placeholder")
        self.assertIsNotNone(cap)
        self.assertTrue(cap.is_placeholder)
        self.assertTrue(node_capabilities.is_placeholder_node_type(
            "aud5008_placeholder"))

    def test_unknown_type_still_none(self):
        self.assertIsNone(
            node_capabilities.get_node_capability("aud5008_missing"))


# ── AUD-5100 / AUD-5101: remove_node_and_reconnect cleanup ───────────────────


class TestAud5100RemoveReconnectCleanup(unittest.TestCase):

    def _graph(self):
        graph = NodeGraph()
        src = ConstantNode("src")
        mid = BlurNode("mid")
        out = OutputNode("out")
        for node in (src, mid, out):
            graph.add_node(node)
        graph.connect("src", "result", "mid", "image")
        graph.connect("mid", "result", "out", "image")
        return graph, out

    def test_bindings_pointing_at_removed_node_are_cleared(self):
        graph, out = self._graph()
        out.parameter_bindings["width"] = {
            "node_id": "mid", "param_name": "width"}
        self.assertTrue(graph.remove_node_and_reconnect("mid"))
        self.assertEqual(out.parameter_bindings, {})

    def test_children_reparented_to_none(self):
        graph, out = self._graph()
        out.parent_id = "mid"
        self.assertTrue(graph.remove_node_and_reconnect("mid"))
        self.assertIsNone(out.parent_id)

    def test_expression_cleanup_matches_remove_node(self):
        """The reconnection path must run the same expression teardown as
        remove_node: expressions *owned* by the removed node are dropped.
        (Dependencies pointing at a removed id are intentionally retained
        by ExpressionManager so a restored id becomes valid again.)"""
        graph, out = self._graph()
        ok, err = graph.set_node_expression("mid", "width", "out.width")
        self.assertTrue(ok, msg=str(err))
        mgr = graph._expression_manager
        self.assertIn(("mid", "width"), mgr._expressions)
        self.assertTrue(graph.remove_node_and_reconnect("mid"))
        self.assertNotIn(("mid", "width"), mgr._expressions)

    def test_downstream_marked_dirty_aud5101(self):
        graph, out = self._graph()
        # Clean the downstream flag the way a completed evaluation would.
        out._dirty = False
        self.assertTrue(graph.remove_node_and_reconnect("mid"))
        # The reconnect path used to strip the edges *before* dirtying, so
        # nothing downstream was ever marked.
        self.assertTrue(out._dirty)


# ── AUD-5102: evaluate() node-map snapshot ───────────────────────────────────


class TestAud5102EvaluateSnapshot(unittest.TestCase):

    def test_node_deleted_during_sort_does_not_crash(self):
        graph = NodeGraph()
        src = ConstantNode("src")
        mid = BlurNode("mid")
        out = OutputNode("out")
        for node in (src, mid, out):
            graph.add_node(node)
        graph.connect("src", "result", "mid", "image")
        graph.connect("mid", "result", "out", "image")

        original_sort = graph.topological_sort

        def sort_then_concurrent_delete():
            order = original_sort()
            # Simulate a UI-thread deletion between the sort and the loop:
            # the (now stale) order still contains "mid".
            graph.remove_node("mid")
            return order

        graph.topological_sort = sort_then_concurrent_delete
        try:
            # Pre-fix: self.nodes["mid"] raised KeyError out of evaluate().
            graph.evaluate(None, None)
        finally:
            graph.topological_sort = original_sort  # type: ignore[assignment]


# ── AUD-5103: locked public readers/writers ──────────────────────────────────


class TestAud5103LockDiscipline(unittest.TestCase):

    def test_to_dict_survives_concurrent_adders(self):
        graph = NodeGraph()
        graph.add_node(OutputNode(node_id="out"))
        stop = threading.Event()
        errors: list = []

        def adder():
            i = 0
            try:
                while not stop.is_set():
                    graph.add_node(ConstantNode(node_id=f"race{i}"))
                    i += 1
            except Exception as exc:  # pragma: no cover - failure report
                errors.append(exc)

        def serializer():
            try:
                while not stop.is_set():
                    graph.to_dict()
            except Exception as exc:  # RuntimeError pre-fix
                errors.append(exc)

        threads = [threading.Thread(target=adder),
                   threading.Thread(target=serializer),
                   threading.Thread(target=serializer)]
        for t in threads:
            t.start()
        import time
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [])

    def test_validate_reports_cycle(self):
        graph = NodeGraph()
        a = BlurNode("a")
        b = BlurNode("b")
        graph.add_node(a)
        graph.add_node(b)
        graph.connect("a", "result", "b", "image")
        graph.connections.append(Connection("b", "result", "a", "image"))
        errors = graph.validate()
        self.assertTrue(any("cycle" in e.lower() for e in errors))

    def test_migrate_output_format_mode_is_idempotent(self):
        graph = NodeGraph()
        graph.add_node(OutputNode(node_id="out"))
        self.assertTrue(graph.migrate_output_format_mode())
        self.assertFalse(graph.migrate_output_format_mode())


# ── AUD-5104: from_dict malformed entries ────────────────────────────────────


class TestAud5104FromDictValidation(unittest.TestCase):

    def _base(self):
        return {
            "composite_schema_version": 11,
            "fps": 24.0,
            "duration_frames": 10,
            "nodes": [
                {"id": "src", "type": "constant"},
                {"id": "out", "type": "output"},
            ],
            "connections": [
                {"src_node_id": "src", "src_port_name": "result",
                 "dst_node_id": "out", "dst_port_name": "image"},
            ],
        }

    def test_malformed_connection_entries_are_skipped(self):
        data = self._base()
        data["connections"] = [
            "not-a-dict",
            {"src_node_id": "src"},                      # missing fields
            {"src_node_id": "src", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},  # valid
            {"src_node_id": "src", "src_port_name": "result",
             "dst_node_id": "ghost", "dst_port_name": "image"},  # bad node
        ]
        graph = NodeGraph.from_dict(copy.deepcopy(data))
        # Only the valid connection survives; the load does not raise.
        self.assertEqual(len(graph.connections), 1)
        self.assertIsNotNone(graph.get_node("src"))
        self.assertIsNotNone(graph.get_node("out"))

    def test_non_dict_node_entry_is_skipped(self):
        data = self._base()
        data["nodes"] = ["garbage", {"id": "out", "type": "output"}]
        graph = NodeGraph.from_dict(copy.deepcopy(data))
        self.assertIsNone(graph.get_node("src"))
        self.assertIsNotNone(graph.get_node("out"))

    def test_non_list_containers_ignored(self):
        data = self._base()
        data["nodes"] = {"id": "src", "type": "constant"}
        data["connections"] = 42
        graph = NodeGraph.from_dict(data)
        self.assertEqual(graph.nodes, {})
        self.assertEqual(graph.connections, [])


# ── AUD-5105: groups / viewport / viewer state hydration ─────────────────────


class TestAud5105StateHydration(unittest.TestCase):

    def test_groups_are_copied_and_normalized(self):
        groups = {
            "g1": {"title": "G", "node_ids": ["src", 3]},
        }
        data = {
            "nodes": [{"id": "src", "type": "constant"},
                      {"id": "out", "type": "output"}],
            "groups": groups,
        }
        graph = NodeGraph.from_dict(copy.deepcopy(data))
        # Caller-side mutation must not leak into graph state.
        groups["g1"]["title"] = "MUTATED"
        self.assertEqual(graph._node_groups["g1"]["title"], "G")
        # node_ids normalized to strings.
        self.assertEqual(graph._node_groups["g1"]["node_ids"], ["src", "3"])
        self.assertIsNotNone(graph.get_group_for_node("src"))

    def test_group_missing_node_ids_is_repaired(self):
        data = {
            "nodes": [{"id": "src", "type": "constant"}],
            "groups": {"broken": {"title": "no ids"}},
        }
        graph = NodeGraph.from_dict(data)
        # get_group_for_node used to KeyError on the missing 'node_ids'.
        self.assertIsNone(graph.get_group_for_node("src"))

    def test_viewport_state_is_copied_not_aliased(self):
        state = {"zoom": 1.0}
        data = {
            "nodes": [{"id": "out", "type": "output"}],
            "viewport_state": state,
            "viewer_state": {"viewing_node_id": "out"},
        }
        graph = NodeGraph.from_dict(data)
        state["zoom"] = 99.0
        self.assertEqual(graph._viewport_state["zoom"], 1.0)
        self.assertEqual(graph._viewer_state["viewing_node_id"], "out")

    def test_wrong_typed_ui_state_ignored(self):
        data = {
            "nodes": [{"id": "out", "type": "output"}],
            "viewport_state": ["not", "a", "dict"],
            "viewer_state": None,
        }
        graph = NodeGraph.from_dict(data)
        self.assertEqual(graph._viewport_state, {})
        self.assertEqual(graph._viewer_state, {})


# ── AUD-5200: plugin directory resolution ────────────────────────────────────


class TestAud5200PluginDirs(unittest.TestCase):

    def test_app_plugin_dir_resolves_to_repo_root(self):
        from pathlib import Path
        app_dir = node_plugin.NodePluginManager.PLUGIN_DIRS[1]
        expected = Path(
            node_plugin.__file__).resolve().parents[4] / "plugins" / \
            "node_plugins"
        self.assertEqual(app_dir, expected)
        # The directory exists in the repo and carries the bundled plugins.
        self.assertTrue(app_dir.exists(),
                        f"application plugin dir missing: {app_dir}")


if __name__ == "__main__":
    unittest.main()
