"""Batch D (wd5-g31) regression tests for Major audit findings.

Covers:
  * AUD-5201  — exec_module failure must not leave a half-initialized module
                in sys.modules (previous entry restored / broken entry gone)
  * AUD-5202  — unregister_plugin never deletes a node type currently held
                by another (overriding) plugin; the previous holder is
                restored when the overrider unregisters
  * AUD-5203  — register_plugin is atomic: a mid-loop failure rolls every
                registry back (no ghost node types, no plugin entry), and
                category lists never accumulate duplicates on retry
  * AUD-5305  — _RAMP_FRAGMENT mirror extend is the period-2 triangle wave
                (identity on [0,1]), not a period-1 fold
  * AUD-5306  — the ambiguous "m.a > 0 ? m.a : m.r" mask heuristic is gone
                from all six shaders; explicit channel selection (S3-A
                contract) replaces it
  * AUD-5307  — _FUSED_COLOR grade operator order matches _GRADE
                (blackpoint -> gain/offset -> gamma -> lift -> multiply/add
                -> channel mask * mask * mix); _FUSED (basic) gains the
                extended grade uniforms with identity initializers
  * AUD-5402  — regression lock: the cited "except Exception: pass →
                silent 0 padding" no longer exists (fixed by an earlier
                commit; entry reported INVALID)
  * AUD-5409  — compress oldest-eviction re-attaches children: no dangling
                parent ids, keep_types nodes stay reachable, undo/redo keep
                working after eviction
  * AUD-5410  — load_from_file actually restores actions through the
                callback registry; unrestorable nodes are pruned with the
                chain preserved instead of becoming action=None ghosts
  * AUD-5411  — save_to_file survives group actions (recursive sub-action
                serialization) and numpy-like scalars (json default hook);
                failures are logged at error level
  * AUD-5412  — undo/redo callback exceptions no longer stall the cursor:
                the applied prefix stays consistent and a retry never
                replays already-applied sub-actions
  * AUD-5500  — shutdown() closes every service-side clip (the
                (node_id, clip_id) snapshot is taken before clearing)
  * AUD-5501  — _compute_source_frame rejects inverted/empty source_in/out
                ranges instead of ZeroDivisionError / negative modulo
  * AUD-5502  — timeline state is read/written under _lock;
                get_timeline_context returns a defensive copy
  * AUD-5601  — set_expression/remove_expression plan ops go through the
                engine (dependency registration + cycle rejection +
                generation bump)
  * AUD-5602  — successful apply always bumps graph generation (pure
                keyframe/param plans included)
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

import pytest

from src.features.composite.core.node_plugin import (
    NodePlugin,
    NodePluginManager,
    PluginInfo,
)
from src.features.composite.core.shaders import (
    _COLOR_CORRECT_FRAGMENT,
    _DEEP_MERGE_FRAGMENT,
    _FUSED_COLOR_FRAGMENT,
    _FUSED_FRAGMENT,
    _GRADE_FRAGMENT,
    _HUECORRECT_FRAGMENT,
    _LUT3D_FRAGMENT,
    _RAMP_FRAGMENT,
    _SET_ALPHA_FRAGMENT,
)
from src.features.composite.core.undo_stack import (
    UndoAction,
    UndoStack,
    UndoTree,
    create_param_change_action,
)
from src.features.composite.core.video_context import VideoContext
from src.features.composite.edit_service import CompositeEditService


# ── helpers ──────────────────────────────────────────────────────────────

def glsl_mod(x: float, y: float) -> float:
    """GLSL mod(): result has the sign of y (unlike Python %)."""
    return x - y * math.floor(x / y)


def mirror_wave(t: float) -> float:
    """The AUD-5305 fixed ramp mirror: 1.0 - abs(mod(t, 2.0) - 1.0)."""
    return 1.0 - abs(glsl_mod(t, 2.0) - 1.0)


_OLD_HEURISTIC = re.compile(r"\.a\s*>\s*0(?:\.0)?\s*\?")


def strip_comments(source: str) -> str:
    """Remove GLSL block and line comments (keeps preprocessor lines)."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    return source


def _make_plugin(name: str, node_types: list[str], category: str = "Plugins"):
    """Build a minimal NodePlugin subclass providing the given node types."""
    classes = [type(f"_Node_{name}_{nt}", (), {"node_type": nt})
               for nt in node_types]

    class _Plugin(NodePlugin):
        @classmethod
        def plugin_info(cls) -> PluginInfo:
            return PluginInfo(name=name)

        @classmethod
        def node_types(cls):
            return list(classes)

        @classmethod
        def category(cls) -> str:
            return category

    return _Plugin


# ── AUD-5201: exec_module failure cleans sys.modules ────────────────────

class TestAud5201BrokenModuleCleanup:
    def _boom_file(self, tmp_path: Path) -> Path:
        plugin_file = tmp_path / "boom_plugin.py"
        plugin_file.write_text(
            "raise RuntimeError('boom during exec')\n", encoding="utf-8")
        return plugin_file

    def test_failed_exec_removes_module_from_sys_modules(self, tmp_path):
        module_name = "topos_node_plugin_boom_plugin"
        sys.modules.pop(module_name, None)
        try:
            manager = NodePluginManager()
            assert manager._load_plugin_file(self._boom_file(tmp_path)) is False
            assert module_name not in sys.modules, (
                "half-initialized module leaked into sys.modules")
        finally:
            sys.modules.pop(module_name, None)

    def test_failed_exec_restores_previous_entry(self, tmp_path):
        module_name = "topos_node_plugin_boom_plugin"
        sentinel = object()
        sys.modules[module_name] = sentinel
        try:
            manager = NodePluginManager()
            assert manager._load_plugin_file(self._boom_file(tmp_path)) is False
            assert sys.modules.get(module_name) is sentinel, (
                "pre-existing sys.modules entry was destroyed by the "
                "failed load")
        finally:
            sys.modules.pop(module_name, None)

    def test_successful_load_keeps_module_in_sys_modules(self, tmp_path):
        module_name = "topos_node_plugin_ok_plugin"
        sys.modules.pop(module_name, None)
        plugin_file = tmp_path / "ok_plugin.py"
        plugin_file.write_text(
            "from src.features.composite.core.node_plugin import NodePlugin\n"
            "class _OkNode:\n"
            "    node_type = 'ok_tmp_type'\n"
            "class OkPlugin(NodePlugin):\n"
            "    @classmethod\n"
            "    def plugin_info(cls):\n"
            "        from src.features.composite.core.node_plugin import PluginInfo\n"
            "        return PluginInfo(name='ok_plugin')\n"
            "    @classmethod\n"
            "    def node_types(cls):\n"
            "        return [_OkNode]\n"
            "    @classmethod\n"
            "    def category(cls):\n"
            "        return 'Tmp'\n",
            encoding="utf-8")
        try:
            manager = NodePluginManager()
            assert manager._load_plugin_file(plugin_file) is True
            assert module_name in sys.modules, (
                "successfully executed module must stay importable")
        finally:
            sys.modules.pop(module_name, None)


# ── AUD-5202: unregister respects overrides ─────────────────────────────

class TestAud5202UnregisterOverride:
    def test_unregistering_overridden_plugin_keeps_current_holder(self):
        manager = NodePluginManager()
        plugin_a = _make_plugin("plugin_a", ["shared_type"], category="CatA")
        plugin_b = _make_plugin("plugin_b", ["shared_type"], category="CatB")
        assert manager.register_plugin(plugin_a) is True
        assert manager.register_plugin(plugin_b) is True  # override
        holder = manager.node_types["shared_type"]

        # Unregister the FIRST (overridden) plugin: the node type belongs
        # to plugin_b now and must survive.
        assert manager.unregister_plugin("plugin_a") is True
        assert "shared_type" in manager.node_types
        assert manager.node_types["shared_type"] is holder
        assert "plugin_b" in manager.plugins

        # Unregister the overrider: type disappears entirely (no history).
        assert manager.unregister_plugin("plugin_b") is True
        assert "shared_type" not in manager.node_types

    def test_unregistering_overrider_restores_previous_holder(self):
        manager = NodePluginManager()
        plugin_a = _make_plugin("plugin_a", ["shared_type"], category="CatA")
        plugin_b = _make_plugin("plugin_b", ["shared_type"], category="CatB")
        assert manager.register_plugin(plugin_a) is True
        class_a = plugin_a.node_types()[0]
        assert manager.register_plugin(plugin_b) is True

        assert manager.unregister_plugin("plugin_b") is True
        # The previous holder (still-registered plugin_a) is restored.
        assert manager.node_types["shared_type"] is class_a
        assert manager.node_categories.get("CatA") == ["shared_type"]
        assert manager.node_categories.get("CatB") is None


# ── AUD-5203: atomic registration ────────────────────────────────────────

class TestAud5203AtomicRegister:
    def test_callback_failure_rolls_back_all_node_types(self):
        manager = NodePluginManager()

        def boom(node_type, node_class):
            if node_type == "second_type":
                raise RuntimeError("callback boom")

        manager.on_node_registered = boom
        plugin = _make_plugin("ghosty", ["first_type", "second_type"])

        assert manager.register_plugin(plugin) is False
        # No ghost node types and no half-registered plugin.
        assert "first_type" not in manager.node_types
        assert "second_type" not in manager.node_types
        assert "ghosty" not in manager.plugins
        assert manager.node_categories == {}

    def test_retry_after_failure_registers_without_duplicates(self):
        manager = NodePluginManager()
        calls: list[str] = []

        def boom(node_type, node_class):
            calls.append(node_type)
            if node_type == "second_type":
                raise RuntimeError("callback boom")

        manager.on_node_registered = boom
        plugin = _make_plugin("ghosty", ["first_type", "second_type"])
        assert manager.register_plugin(plugin) is False

        # Retry with a healthy callback: must succeed and must not
        # duplicate category entries.
        manager.on_node_registered = lambda nt, nc: calls.append(nt)
        assert manager.register_plugin(plugin) is True
        assert manager.node_types.keys() >= {"first_type", "second_type"}
        assert manager.node_categories["Plugins"] == [
            "first_type", "second_type"]
        assert "ghosty" in manager.plugins

    def test_missing_node_type_attribute_registers_nothing(self):
        manager = NodePluginManager()

        class _NoType:
            pass

        class _Plugin(NodePlugin):
            @classmethod
            def plugin_info(cls):
                return PluginInfo(name="no_type_plugin")

            @classmethod
            def node_types(cls):
                return [_NoType]

            @classmethod
            def category(cls):
                return "Plugins"

        assert manager.register_plugin(_Plugin) is False
        assert manager.node_types == {}
        assert manager.node_categories == {}
        assert "no_type_plugin" not in manager.plugins


# ── AUD-5305: ramp mirror extend mode ────────────────────────────────────

class TestAud5305RampMirror:
    def test_shader_uses_triangle_wave_mirror(self):
        code = strip_comments(_RAMP_FRAGMENT)
        assert "return 1.0 - abs(mod(t, 2.0) - 1.0);" in code
        # Old period-1 fold must be gone.
        assert "t_mod > 0.5" not in code

    def test_mirror_is_identity_inside_unit_interval(self):
        for t in (0.0, 0.001, 0.25, 0.5, 0.731, 0.999, 1.0):
            assert mirror_wave(t) == pytest.approx(t, abs=1e-9)

    def test_mirror_folds_outside_coordinates_correctly(self):
        cases = {
            1.25: 0.75,   # 0.25 past the end folds back
            -0.1: 0.1,    # mirrors off the start edge
            -1.5: 0.5,    # second period still folds
            2.3: 0.3,     # 0.3 past period 2 mirrors off the 0 edge
            3.75: 0.25,
        }
        for t, expected in cases.items():
            assert mirror_wave(t) == pytest.approx(expected, abs=1e-9)


# ── AUD-5306: explicit mask channel selection ────────────────────────────

class TestAud5306MaskHeuristicGone:
    SIX_SHADERS = {
        "_COLOR_CORRECT_FRAGMENT": _COLOR_CORRECT_FRAGMENT,
        "_SET_ALPHA_FRAGMENT": _SET_ALPHA_FRAGMENT,
        "_HUECORRECT_FRAGMENT": _HUECORRECT_FRAGMENT,
        "_FUSED_COLOR_FRAGMENT": _FUSED_COLOR_FRAGMENT,
        "_LUT3D_FRAGMENT": _LUT3D_FRAGMENT,
        "_DEEP_MERGE_FRAGMENT": _DEEP_MERGE_FRAGMENT,
    }

    def test_ambiguous_heuristic_absent_everywhere(self):
        for name, source in self.SIX_SHADERS.items():
            assert not _OLD_HEURISTIC.search(strip_comments(source)), (
                f"{name}: the ambiguous 'a if a>0 else r' mask heuristic "
                "is back")

    def test_five_shaders_declare_explicit_channel_selector(self):
        for name in ("_SET_ALPHA_FRAGMENT", "_HUECORRECT_FRAGMENT",
                     "_FUSED_COLOR_FRAGMENT", "_LUT3D_FRAGMENT",
                     "_DEEP_MERGE_FRAGMENT"):
            code = strip_comments(self.SIX_SHADERS[name])
            assert re.search(r"uniform\s+int\s+u_mask_channel\s*;", code), (
                f"{name}: missing u_mask_channel declaration")
            assert "u_mask_channel == 4" in code, (
                f"{name}: missing explicit channel selection chain")

    def test_color_correct_uses_deterministic_luma(self):
        # The shader's uniform set is pinned to the ColorCorrectNode
        # parameter surface by test_fix_nodes1_aud2, so no selector uniform
        # is added there — the heuristic is replaced by deterministic
        # Rec.709 luma (the merge contract's default channel).
        code = strip_comments(_COLOR_CORRECT_FRAGMENT)
        assert "u_mask_channel" not in code
        assert "dot(mask_sample.rgb, vec3(0.2126, 0.7152, 0.0722))" in code

    def test_default_luma_fixes_opaque_grayscale_masks(self):
        # A grayscale matte loaded as RGBA has a=1, r=g=b=v. The old
        # heuristic returned alpha (1.0) — the mask did nothing. The luma
        # default returns v, matching merge-node behaviour.
        def old_heuristic(m):
            return m[3] if m[3] > 0.0 else m[0]

        def luma_default(m):
            return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]

        grey = (0.25, 0.25, 0.25, 1.0)
        assert old_heuristic(grey) == 1.0
        assert luma_default(grey) == pytest.approx(0.25)

        # White fallback mask (what evaluators bind when no mask is
        # connected) keeps full effect under the new default.
        white = (1.0, 1.0, 1.0, 1.0)
        assert luma_default(white) == pytest.approx(1.0)


# ── AUD-5307: fused grade operator order ─────────────────────────────────

class TestAud5307FusedGradeOrder:
    def test_fused_color_grade_order_matches_grade(self):
        code = strip_comments(_FUSED_COLOR_FRAGMENT)
        # Order: blackpoint -> gain/offset -> gamma -> lift (in loop),
        # multiply/add AFTER the loop, channel mask * mask * mix last.
        idx_blackpoint = code.index("(c - u_grade_blackpoint)")
        idx_gain = code.index("c = c * u_grade_gain[i] + u_grade_offset[i];")
        idx_gamma = code.index("pow(max(c, 0.0001), 1.0 / max(u_grade_gamma[i], 0.001))")
        idx_lift = code.index("c = c + u_grade_lift[i];")
        idx_mult = code.index("graded = graded * u_grade_multiply + u_grade_add;")
        idx_mix = code.index("u_grade_channel_mask * (mask_val * u_grade_mix)")
        assert idx_blackpoint < idx_gain < idx_gamma < idx_lift < idx_mult < idx_mix
        # The old in-loop multiply/add (before gain) must be gone.
        assert "c = c * u_grade_multiply + u_grade_add;" not in code

    def test_grade_reference_order(self):
        # _GRADE applies multiply/add after lift, outside the per-channel
        # loop — the order the fused stages are now aligned with.
        code = strip_comments(_GRADE_FRAGMENT)
        idx_lift = code.index("g = g + u_lift[i];")
        idx_mult = code.index("result = result * u_multiply + u_add;")
        assert idx_lift < idx_mult

    def test_fused_basic_gains_extended_uniforms_with_identity_defaults(self):
        code = strip_comments(_FUSED_FRAGMENT)
        for decl in ("uniform float u_grade_blackpoint = 0.0;",
                     "uniform float u_grade_whitepoint = 1.0;",
                     "uniform float u_grade_multiply = 1.0;",
                     "uniform float u_grade_add = 0.0;"):
            assert decl in code, f"_FUSED_FRAGMENT lost {decl}"
        # Same operator order as _GRADE inside the stage.
        idx_blackpoint = code.index("(c - u_grade_blackpoint)")
        idx_lift = code.index("c = c + u_grade_lift[i];")
        idx_mult = code.index("graded = graded * u_grade_multiply + u_grade_add;")
        assert idx_blackpoint < idx_lift < idx_mult


# ── AUD-5402: regression lock (entry INVALID — fixed by earlier commit) ──

class TestAud5402NoSilentPaddingSwallow:
    def test_no_broad_silent_except_in_tile_padding_computer(self):
        source = Path(
            "src/features/composite/core/tile_padding_computer.py"
        ).read_text(encoding="utf-8")
        assert not re.search(
            r"except\s+Exception\s*:\s*\n\s*pass", source), (
            "broad except with silent pass is back in "
            "tile_padding_computer.py")


# ── AUD-5409: eviction keeps chains complete ─────────────────────────────

class TestAud5409EvictionChainIntegrity:
    def _tree_with_chain(self):
        """root -> p1 -> a1(add_node keep) -> p2 -> p3 (current)."""
        tree = UndoTree()
        for name in ("p1", "a1", "p2", "p3"):
            action = UndoAction(
                "add_node" if name == "a1" else "param_change",
                {"name": name}, lambda d: None, lambda d: None, name)
            tree.push(action)
        return tree

    def _ids(self, tree: UndoTree, name: str) -> str:
        for nid, node in tree._nodes.items():
            if node.description == name:
                return nid
        raise AssertionError(f"node {name} not found")

    def test_evicting_ancestor_reattaches_children(self):
        tree = self._tree_with_chain()
        root_id = tree._root_node_id
        p1, a1, p2, p3 = (self._ids(tree, n) for n in ("p1", "a1", "p2", "p3"))

        tree.compress(keep_types=["add_node"], max_nodes=3)

        # p1 (oldest non-keep) evicted; its child re-attached to root.
        assert p1 not in tree._nodes
        assert tree._nodes[a1].parent_id == root_id
        assert tree._nodes[p2].parent_id == a1
        assert tree._nodes[p3].parent_id == p2
        assert tree._current_node_id == p3
        # Path to root has no hole.
        path = tree._get_path_to_root(p3)
        assert path[-1] == root_id
        assert set(path) == {p3, p2, a1, root_id}
        # keep_types node stays reachable in the history list.
        descriptions = [tree._nodes[nid].description
                        for nid, _, _ in tree.get_history_list()]
        assert "a1" in descriptions

    def test_repeated_eviction_keeps_undo_redo_working(self):
        tree = self._tree_with_chain()
        p3 = self._ids(tree, "p3")
        tree.compress(keep_types=["add_node"], max_nodes=2)

        # Chain: root -> a1 -> p3 (p1 and p2 evicted, children re-linked).
        assert tree._get_path_to_root(tree._current_node_id)[-1] == \
            tree._root_node_id
        # Undo to bottom and redo back — no ghost stops.
        undone = []
        while tree.undo() is not None:
            undone.append(True)
        assert len(undone) == 2  # p3, a1
        assert tree.get_current_node().is_root()
        redone = []
        while tree.redo() is not None:
            redone.append(True)
        assert len(redone) == 2
        assert tree._current_node_id == p3


# ── AUD-5410: load_from_file restores actions ────────────────────────────

class TestAud5410LoadRestoresActions:
    def _save_history(self, tmp_path) -> str:
        stack = UndoStack()
        data = {"value": 0}

        def undo_fn(d):
            data["value"] = d["old"]

        def redo_fn(d):
            data["value"] = d["new"]

        stack.register_callbacks("param_change", undo_fn, redo_fn)
        for i in range(3):
            stack.push(create_param_change_action(
                "node_001", "radius", float(i), float(i + 1),
                undo_fn, redo_fn))
        path = str(tmp_path / "history.json")
        assert stack.save_to_file(path) is True
        return path

    def test_loaded_actions_execute_through_callbacks(self, qapp, tmp_path):
        path = self._save_history(tmp_path)
        data = {"value": 3}
        stack = UndoStack()
        stack.register_callbacks(
            "param_change",
            lambda d: data.__setitem__("value", d["old"]),
            lambda d: data.__setitem__("value", d["new"]))
        assert stack.load_from_file(path) is True

        history = stack.get_history_list()
        assert len(history) == 4  # root + 3 actions
        # Undo actually executes the restored action (previously the
        # restored nodes had action=None: the cursor moved but nothing ran).
        action = stack.undo()
        assert action is not None and action.action_type == "param_change"
        assert data["value"] == 2.0
        assert stack.redo() is not None
        assert data["value"] == 3.0

    def test_unrestorable_nodes_are_pruned_with_chain_intact(
            self, qapp, tmp_path):
        path = self._save_history(tmp_path)
        stack = UndoStack()  # no callbacks registered
        assert stack.load_from_file(path) is True
        # All three action nodes were pruned; only the root remains.
        assert stack._tree.get_nodes_count() == 0
        assert stack.can_undo() is False
        assert stack.can_redo() is False

    def test_from_dict_parks_payload(self):
        node = UndoTree.from_dict({
            "nodes": {
                "n1": {"id": "n1", "action": {"action_type": "param_change",
                                              "data": {"old": 1}},
                       "timestamp": "2026-01-01T00:00:00"}},
            "root_node_id": "root",
            "current_node_id": "n1",
        })._nodes["n1"]
        assert node.pending_action == {"action_type": "param_change",
                                       "data": {"old": 1}}
        assert node.action is None


# ── AUD-5411: save survives groups and non-JSON scalars ──────────────────

class _NpLikeFloat:
    """Duck-typed numpy scalar: json.dump's default hook must coerce it."""

    def __init__(self, value: float) -> None:
        self._value = value

    def item(self) -> float:
        return self._value


class TestAud5411SaveWithGroups:
    def test_group_history_saves_and_round_trips(self, qapp, tmp_path):
        stack = UndoStack()
        log: list[str] = []

        def undo_fn(d):
            log.append("undo:" + str(d["old"]))

        def redo_fn(d):
            log.append("redo:" + str(d["new"]))

        stack.register_callbacks("param_change", undo_fn, redo_fn)
        stack.begin_group()
        for old, new in ((0, 1), (1, 2)):
            stack.push(create_param_change_action(
                "n", "radius", old, new, undo_fn, redo_fn))
        stack.end_group("two edits")

        path = str(tmp_path / "grouped.json")
        # Previously json.dump raised TypeError (UndoAction objects inside
        # data) which the broad except swallowed into a silent False.
        assert stack.save_to_file(path) is True

        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        group_actions = [
            node["action"]["data"]["actions"]
            for node in payload["nodes"].values()
            if (node.get("action") or {}).get("action_type") == "group"]
        assert group_actions and all(
            isinstance(sub, dict) for sub in group_actions[0])

        # Round trip: the group's sub-actions come back executable.
        stack2 = UndoStack()
        stack2.register_callbacks("param_change", undo_fn, redo_fn)
        assert stack2.load_from_file(path) is True
        log.clear()
        assert stack2.undo() is not None
        assert log == ["undo:1", "undo:0"]  # reversed group order

    def test_numpy_like_scalar_is_coerced(self, qapp, tmp_path):
        stack = UndoStack()
        stack.push(create_param_change_action(
            "n", "radius", _NpLikeFloat(0.25), _NpLikeFloat(0.75),
            lambda d: None, lambda d: None))
        path = str(tmp_path / "np.json")
        assert stack.save_to_file(path) is True
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        values = [node["action"]["data"]
                  for node in payload["nodes"].values()
                  if node.get("action")]
        assert values and values[0]["old"] == 0.25
        assert values[0]["new"] == 0.75


# ── AUD-5412: callback failure never stalls or replays ───────────────────

class TestAud5412CallbackFailureProtection:
    def test_undo_callback_failure_advances_cursor(self):
        tree = UndoTree()

        def bad_undo(d):
            raise RuntimeError("undo boom")

        action = UndoAction("param_change", {}, bad_undo,
                            lambda d: None, "bad")
        tree.push(action)

        result = tree.undo()  # must not raise
        assert result is action
        assert tree.get_current_node().is_root()

    def test_redo_callback_failure_advances_cursor(self):
        tree = UndoTree()
        calls: list[str] = []

        def bad_redo(d):
            raise RuntimeError("redo boom")

        action = UndoAction("param_change", {},
                            lambda d: calls.append("undo"), bad_redo, "a")
        tree.push(action)
        tree.undo()
        at_root = tree._current_node_id
        result = tree.redo()  # must not raise
        assert result is action
        assert tree._current_node_id != at_root  # cursor advanced

    def test_group_undo_failure_applies_prefix_once(self, qapp):
        stack = UndoStack()
        log: list[str] = []

        def make(name: str, fail: bool) -> UndoAction:
            def undo_fn(d):
                log.append("undo:" + name)
                if fail:
                    raise RuntimeError("boom " + name)

            def redo_fn(d):
                log.append("redo:" + name)

            return UndoAction("param_change", {"name": name}, undo_fn,
                              redo_fn, name)

        stack.begin_group()
        stack.push(make("a", False))
        stack.push(make("b", True))   # fails mid-group
        stack.push(make("c", False))
        stack.end_group("grp")

        stack.undo()  # must not raise; sub-actions applied once each
        assert log.count("undo:c") == 1
        assert log.count("undo:b") == 1
        assert log.count("undo:a") == 1
        assert stack.can_undo() is False  # cursor reached the root

        # A retry must not replay the already-applied prefix.
        stack.undo()
        assert log.count("undo:c") == 1


# ── AUD-5500: shutdown closes service clips ──────────────────────────────

class _FakeClipService:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def open_clip(self, path: str) -> str:
        return f"clip::{path}"

    def close_clip(self, clip_id: str) -> bool:
        self.closed.append(clip_id)
        return True

    def get_stats(self) -> dict:
        return {}


class TestAud5500ShutdownClosesClips:
    def test_shutdown_closes_every_service_clip(self):
        service = _FakeClipService()
        ctx = VideoContext(service=service)
        ctx.open_clip("/a.mp4", "node_1")
        ctx.open_clip("/a.mp4", "node_2")   # shared clip, two references
        ctx.open_clip("/b.mp4", "node_3")

        ctx.shutdown()

        # One close per node reference (service side is refcounted).
        # Previously: close_clip_for_node popped from the already-cleared
        # dict, returned False immediately and NO clip was ever closed.
        assert service.closed == [
            "clip::/a.mp4", "clip::/a.mp4", "clip::/b.mp4"]
        assert ctx._node_clips == {}


# ── AUD-5501: source range validation ────────────────────────────────────

class TestAud5501SourceRangeGuard:
    def _ctx(self) -> VideoContext:
        return VideoContext(service=_FakeClipService())

    def test_empty_range_with_loop_returns_invalid(self):
        ctx = self._ctx()
        # source_out == source_in - 1 -> duration 0. Previously the loop
        # path raised ZeroDivisionError (killing prefetch workers).
        assert ctx._compute_source_frame(
            timeline_frame=0, fps=24.0, source_in=10, source_out=9,
            time_offset=0.0, speed=1.0, loop=True, hold=False,
            total_frames=100) == -1

    def test_inverted_range_returns_invalid(self):
        ctx = self._ctx()
        assert ctx._compute_source_frame(
            timeline_frame=5, fps=24.0, source_in=10, source_out=5,
            time_offset=0.0, speed=1.0, loop=True, hold=False,
            total_frames=100) == -1

    def test_zero_total_frames_returns_invalid(self):
        ctx = self._ctx()
        assert ctx._compute_source_frame(
            timeline_frame=0, fps=24.0, source_in=0, source_out=-1,
            time_offset=0.0, speed=1.0, loop=True, hold=False,
            total_frames=0) == -1

    def test_normal_loop_mapping_still_works(self):
        ctx = self._ctx()
        assert ctx._compute_source_frame(
            timeline_frame=25, fps=24.0, source_in=0, source_out=9,
            time_offset=0.0, speed=1.0, loop=True, hold=False,
            total_frames=100) == 5  # (25 - 9 - 1) % 10 == 5


# ── AUD-5502: timeline state under lock ──────────────────────────────────

class _FakeFrameService:
    """Minimal VideoFrameProviderService stand-in (records requests)."""

    def __init__(self, total_frames: int = 240, fps: float = 24.0) -> None:
        from src.shared.video.frame_provider_service import ClipInfo
        self.clip_info = ClipInfo(
            clip_id="c1", path="/v.mp4", width=64, height=64, fps=fps,
            total_frames=total_frames, duration=total_frames / fps)
        self.requested: list[tuple[str, int, str]] = []

    def get_clip_info(self, clip_id: str):
        return self.clip_info

    def request_frame(self, clip_id: str, frame: int, quality: str):
        self.requested.append((clip_id, frame, quality))
        return None


class TestAud5502TimelineLocking:
    def test_get_timeline_context_returns_defensive_copy(self):
        ctx = VideoContext(service=_FakeClipService())
        snapshot = ctx.get_timeline_context()
        snapshot.current_frame = 999
        assert ctx.get_timeline_context().current_frame == 0

    def test_set_timeline_frame_is_visible_to_reads(self):
        ctx = VideoContext(service=_FakeClipService())
        ctx.set_timeline_frame(50)
        assert ctx.get_timeline_context().current_frame == 50
        assert ctx._timeline.current_frame == 50

    def test_exporting_flips_quality_coherently(self):
        ctx = VideoContext(service=_FakeClipService())
        assert ctx.get_quality_for_render_mode("preview") == "proxy"
        ctx.set_exporting(True)
        assert ctx.get_quality_for_render_mode("preview") == "master"
        ctx.set_exporting(False)
        assert ctx.get_quality_for_render_mode("preview") == "proxy"

    def test_request_path_reads_locked_timeline_frame(self):
        service = _FakeFrameService()
        ctx = VideoContext(service=service)
        ctx.set_timeline_frame(7)
        ctx.request_frame_for_node("c1", timeline_frame=None, fps=None)
        # Identity mapping: timeline 7 -> source 7 (fps falls back to clip).
        assert service.requested and service.requested[0][1] == 7

    def test_stats_expose_timeline_state(self):
        ctx = VideoContext(service=_FakeClipService())
        ctx.set_timeline_frame(12)
        stats = ctx.get_stats()
        assert stats["timeline_frame"] == 12


# ── AUD-5601: expression ops go through the engine ───────────────────────

class TestAud5601ExpressionOpsThroughEngine:
    def _graph(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode
        graph = NodeGraph()
        graph.add_node(TransformNode(node_id="t"))
        return graph

    def test_set_expression_registers_dependency_and_bumps_generation(self):
        graph = self._graph()
        plan = {"plan_id": "expr", "operations": [{
            "op": "set_expression", "node_id": "t",
            "param_name": "scale_x", "expression": "2.0 * 3.0"}]}
        before = graph.generation
        result = CompositeEditService(graph).apply(plan)
        assert result.applied, result.error

        node = graph.get_node("t")
        assert node.get_expression("scale_x") == "2.0 * 3.0"
        # The engine path registered the expression in the dependency
        # manager (the old Node-layer bypass left it empty).
        assert ("t", "scale_x") in graph._expression_manager._expressions
        assert graph.generation > before

    def test_remove_expression_clears_manager_entry(self):
        graph = self._graph()
        assert CompositeEditService(graph).apply({
            "plan_id": "set", "operations": [{
                "op": "set_expression", "node_id": "t",
                "param_name": "scale_x", "expression": "2.0 * 3.0"}]}).applied
        result = CompositeEditService(graph).apply({
            "plan_id": "rm", "operations": [{
                "op": "remove_expression", "node_id": "t",
                "param_name": "scale_x"}]})
        assert result.applied, result.error
        assert graph.get_node("t").get_expression("scale_x") is None
        assert ("t", "scale_x") not in \
            graph._expression_manager._expressions

    def test_set_expression_plan_routes_through_engine(self):
        graph = self._graph()
        calls: list[tuple[str, str, str]] = []
        original = graph.set_node_expression

        def spy(node_id, param_name, expression):
            calls.append((node_id, param_name, expression))
            return original(node_id, param_name, expression)

        graph.set_node_expression = spy
        result = CompositeEditService(graph).apply({
            "plan_id": "expr", "operations": [{
                "op": "set_expression", "node_id": "t",
                "param_name": "scale_x", "expression": "2.0 * 3.0"}]})
        assert result.applied, result.error
        # The plan path must call the ENGINE API, not Node.set_expression
        # (the bypass skipped dependency registration, dirty propagation
        # and the generation bump).
        assert calls == [("t", "scale_x", "2.0 * 3.0")]

    def test_cyclic_expressions_in_one_plan_are_rejected(self):
        # AUD-R2-013 (wd10-blk): was an xfail(strict=True) trip-lock while the
        # engine consulted ExpressionManager.detect_circular_dependency — a
        # dead DFS that marks the start key visited before it can ever be
        # revisited, so NO cycle (not even a self-reference) was detected.
        # NodeGraph._expression_would_cycle now performs the real walk; the
        # detector works, so a cyclic plan must be rejected.
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.output_node import OutputNode
        graph = NodeGraph()
        graph.add_node(OutputNode(node_id="a"))
        graph.add_node(OutputNode(node_id="b"))

        plan = {"plan_id": "cyclic", "operations": [
            {"op": "set_expression", "node_id": "a",
             "param_name": "width", "expression": "b.width"},
            {"op": "set_expression", "node_id": "b",
             "param_name": "width", "expression": "a.width"},
        ]}
        result = CompositeEditService(graph).apply(plan)
        assert not result.applied


# ── AUD-5602: apply bumps generation ─────────────────────────────────────

class TestAud5602GenerationBump:
    def _graph(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode
        graph = NodeGraph()
        graph.add_node(TransformNode(node_id="t"))
        return graph

    def test_pure_keyframe_plan_bumps_generation(self):
        graph = self._graph()
        before = graph.generation
        result = CompositeEditService(graph).apply({
            "plan_id": "kf", "operations": [{
                "op": "add_keyframe", "node_id": "t",
                "param_name": "scale_x", "frame": 0, "value": 1.5}]})
        assert result.applied, result.error
        # Previously generation_after == generation_before for pure
        # keyframe plans: the base_generation optimistic lock and the
        # graph_revision cache token both went stale.
        assert result.generation_after > before

    def test_parameter_plan_bumps_generation(self):
        graph = self._graph()
        before = graph.generation
        result = CompositeEditService(graph).apply({
            "plan_id": "param", "operations": [{
                "op": "set_parameter", "node_id": "t",
                "param_name": "scale_x", "value": 2.0}]})
        assert result.applied, result.error
        assert result.generation_after > before

    def test_empty_plan_does_not_bump(self):
        graph = self._graph()
        before = graph.generation
        result = CompositeEditService(graph).apply(
            {"plan_id": "noop", "operations": []})
        assert result.applied
        assert result.generation_after == before
