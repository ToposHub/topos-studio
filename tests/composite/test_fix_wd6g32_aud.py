"""Batch D (wd6-g32) regression tests for Major audit findings.

Covers (edit_service domain):
  * AUD-5603 — plan-internal remove_node followed by references to the
              removed node is rejected at validate (keyframe/param ops,
              set_output_target, group membership, duplicate_node)
  * AUD-5604 — add_node on a registry None placeholder (deferred plugin)
              raises CompositeEditError at validate, never a raw TypeError
  * AUD-5605 — set_parameter / set_param / add_node.parameters enforce the
              value key and string types for str/file/hdri/channel params
  * AUD-5606 — add_node.position validated at validate time (length-2
              finite numeric sequence only)
  * AUD-5607 — group/ungroup existence pre-checked at validate with named
              diagnostics; plan-internal group add/remove tracked
  * AUD-5608 — CompositeEditPlan.from_dict raises CompositeEditError for
              malformed operations / base_generation (no TypeError escapes)

Covers (evaluator domain, headless via fakes — no GL context needed):
  * AUD-5801 — _apply_fused_chain propagates the upstream revision
  * AUD-5802 — _apply_merge_blend unbinds texture unit 0 after the draw
  * AUD-5803 — merge auto color-space conversion textures are returned to
              the pool after the draw
  * AUD-5804 — _upload_ai_result_texture propagates the reference revision
  * AUD-5805 — _readback_texture_fullres keeps HDR values >1.0; the AI
              model request clamps its own copy before submitting
  * AUD-5806 — node-eval failure rolls back pool textures acquired during
              the failed evaluation and logs at exception level
  * AUD-5903 — nine color effect methods release acquired pool textures
              when they fail mid-evaluation
  * AUD-5910 — blur separable / single-pass / defocus / sharpen release
              acquired pool textures when they fail mid-evaluation
  * AUD-5920 — disk-cache hit restores depth/AOV/render_diagnostics via
              the same auxiliary propagation a fresh evaluation runs
  * AUD-5930 — four channel effect methods release acquired pool textures
              when they fail mid-evaluation
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import numpy as np
import pytest

from src.features.composite.edit_service import (
    CompositeEditError,
    CompositeEditPlan,
    CompositeEditService,
)
from src.features.composite.evaluators.base_evaluator import (
    BaseEvaluatorMixin,
)
from src.features.composite.evaluators.blur_evaluator import (
    BlurEvaluatorMixin,
)
from src.features.composite.evaluators.cache_evaluator import (
    CacheEvaluatorMixin,
)
from src.features.composite.evaluators.channel_evaluator import (
    ChannelEvaluatorMixin,
)
from src.features.composite.evaluators.color_evaluator import (
    ColorEvaluatorMixin,
)
from src.features.composite.core.format import BBox, MergeGeometry
from src.features.composite.nodes.base_node import TextureResult


# ── edit_service helpers ─────────────────────────────────────────────────

def _graph():
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.transform_node import TransformNode

    graph = NodeGraph()
    graph.add_node(TransformNode(node_id="xf"))
    return graph


def _apply(graph, operations, plan_id="p"):
    return CompositeEditService(graph).apply(
        {"plan_id": plan_id, "operations": operations})


def _first_error(result) -> str:
    assert not result.applied
    errors = result.report.errors
    assert errors, "expected a named validation diagnostic"
    return errors[0].message


# ── AUD-5603: use-after-remove inside one plan ──────────────────────────

class TestAud5603UseAfterRemove:
    def test_keyframe_op_after_remove_is_rejected(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "remove_node", "node_id": "xf"},
            {"op": "add_keyframe", "node_id": "xf",
             "param_name": "scale_x", "frame": 0, "value": 1.0},
        ])
        assert "already removed" in _first_error(result)
        # The plan is rejected as a whole — the node survives.
        assert graph.get_node("xf") is not None

    def test_set_parameter_after_remove_is_rejected(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "remove_node", "node_id": "xf"},
            {"op": "set_parameter", "node_id": "xf",
             "param_name": "scale_x", "value": 2.0},
        ])
        assert "already removed" in _first_error(result)

    def test_set_expression_after_remove_is_rejected(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "remove_node", "node_id": "xf"},
            {"op": "set_expression", "node_id": "xf",
             "param_name": "scale_x", "expression": "1.0 + 2.0"},
        ])
        assert "already removed" in _first_error(result)

    def test_set_output_target_after_remove_is_rejected(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "remove_node", "node_id": "xf"},
            {"op": "set_output_target", "node_id": "xf", "name": "display"},
        ])
        # Previously validate passed and apply left
        # _viewer_state.output_target_node_id dangling at the removed node.
        assert "already removed" in _first_error(result)
        assert graph._viewer_state.get("output_target_node_id") != "xf"

    def test_group_referencing_removed_node_is_rejected(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "remove_node", "node_id": "xf"},
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]},
        ])
        # Previously validate passed and create_group stored the dead id.
        assert "already removed" in _first_error(result)
        assert "g1" not in graph._node_groups

    def test_duplicate_after_remove_is_rejected(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "remove_node", "node_id": "xf"},
            {"op": "duplicate_node", "node_id": "xf", "new_node_id": "copy"},
        ])
        assert "already removed" in _first_error(result)

    def test_reference_to_plan_added_node_still_works(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "add_node", "node_type": "blur", "node_id": "bl"},
            {"op": "set_parameter", "node_id": "bl",
             "param_name": "radius", "value": 4.0},
        ])
        assert result.applied, result.error


# ── AUD-5604: registry None placeholders ─────────────────────────────────

class TestAud5604DeferredPlaceholder:
    def _placeholder_type(self, monkeypatch) -> str:
        from src.features.composite.core.node_engine import NODE_REGISTRY

        for node_type, cls in NODE_REGISTRY.items():
            if cls is None:
                return node_type
        # Registry fully populated (optional module enabled) — inject one.
        monkeypatch.setitem(NODE_REGISTRY, "deferred_test_node", None)
        return "deferred_test_node"

    def test_add_deferred_type_without_params_is_named_error(self, monkeypatch):
        graph = _graph()
        node_type = self._placeholder_type(monkeypatch)
        result = _apply(graph, [
            {"op": "add_node", "node_type": node_type, "node_id": "n1"}])
        # Previously: validate() only catches CompositeEditError — the
        # NoneType-not-callable TypeError escaped straight to the host.
        message = _first_error(result)
        assert "not available" in message or "unknown node_type" in message

    def test_add_deferred_type_with_params_is_named_error(self, monkeypatch):
        graph = _graph()
        node_type = self._placeholder_type(monkeypatch)
        result = _apply(graph, [{
            "op": "add_node", "node_type": node_type, "node_id": "n1",
            "parameters": {"anything": 1.0}}])
        # This is the exact path that raised TypeError before the fix
        # (None(node_id="_probe") in the parameters probe).
        message = _first_error(result)
        assert "not available" in message or "unknown node_type" in message
        assert graph.get_node("n1") is None

    def test_validate_never_leaks_raw_typeerror(self, monkeypatch):
        graph = _graph()
        node_type = self._placeholder_type(monkeypatch)
        service = CompositeEditService(graph)
        # validate() only catches CompositeEditError — before the fix the
        # None(node_id="_probe") TypeError escaped straight to the host
        # instead of becoming a diagnostic.
        report = service.validate({"operations": [
            {"op": "add_node", "node_type": node_type,
             "parameters": {"anything": 1.0}}]})
        assert not report.ok
        assert "not available" in report.errors[0].message

    def test_unknown_type_still_lists_available(self):
        graph = _graph()
        result = _apply(graph, [{
            "op": "add_node", "node_type": "definitely_not_a_node"}])
        assert "unknown node_type" in _first_error(result)


# ── AUD-5605: value-key + string-family type validation ─────────────────

class TestAud5605ParamValueType:
    def _node_with_str_param(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.group_node import GroupNode

        graph = NodeGraph()
        graph.add_node(GroupNode(node_id="grp"))
        return graph

    def _node_with_file_param(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.model3d_node import Model3DNode

        graph = NodeGraph()
        graph.add_node(Model3DNode(node_id="m3d"))
        return graph

    def test_set_parameter_missing_value_is_rejected(self):
        graph = _graph()
        result = _apply(graph, [{
            "op": "set_parameter", "node_id": "xf",
            "param_name": "scale_x"}])
        # Previously .get("value") returned None, passed every check, and
        # apply raised KeyError('value') → anonymous whole-plan rollback.
        assert "required" in _first_error(result)

    def test_set_param_missing_value_is_rejected(self):
        graph = _graph()
        result = _apply(graph, [{
            "op": "set_param", "node_id": "xf",
            "param_name": "scale_x"}])
        assert "required" in _first_error(result)

    def test_scalar_cannot_be_written_into_str_param(self):
        graph = self._node_with_str_param()
        result = _apply(graph, [{
            "op": "set_parameter", "node_id": "grp",
            "param_name": "group_name", "value": 42}])
        assert "must be a string" in _first_error(result)

    def test_bool_cannot_be_written_into_file_param(self):
        graph = self._node_with_file_param()
        result = _apply(graph, [{
            "op": "set_param", "node_id": "m3d",
            "param_name": "model_path", "value": True}])
        assert "must be a string" in _first_error(result)

    def test_add_node_parameters_string_family_checked(self):
        graph = self._node_with_str_param()
        result = _apply(graph, [{
            "op": "add_node", "node_type": "group", "node_id": "g2",
            "parameters": {"group_name": 3.14}}])
        assert "must be a string" in _first_error(result)

    def test_valid_string_value_still_applies(self):
        graph = self._node_with_str_param()
        result = _apply(graph, [{
            "op": "set_parameter", "node_id": "grp",
            "param_name": "group_name", "value": "My Group"}])
        assert result.applied, result.error
        assert graph.get_node("grp").parameters["group_name"] == "My Group"


# ── AUD-5606: add_node.position validation ──────────────────────────────

class TestAud5606PositionValidation:
    def _add(self, graph, position):
        return _apply(graph, [{
            "op": "add_node", "node_type": "blur", "node_id": "bl",
            "position": position}])

    def test_non_numeric_position_rejected_at_validate(self):
        graph = _graph()
        result = self._add(graph, ["a", "b"])
        # Previously validate passed and apply crashed on float("a").
        assert "position" in _first_error(result)
        assert graph.get_node("bl") is None

    def test_wrong_length_position_rejected(self):
        graph = _graph()
        assert "position" in _first_error(self._add(graph, [1, 2, 3]))

    def test_infinite_position_rejected(self):
        graph = _graph()
        # Previously inf passed isinstance checks and Node.to_dict wrote
        # non-standard JSON "Infinity" into saved projects.
        assert "position" in _first_error(
            self._add(graph, [float("inf"), 0.0]))
        assert "position" in _first_error(self._add(graph, [0.0, float("nan")]))

    def test_scalar_position_rejected(self):
        graph = _graph()
        assert "position" in _first_error(self._add(graph, 5))

    def test_valid_position_applies(self):
        graph = _graph()
        result = self._add(graph, [12.0, 34])
        assert result.applied, result.error
        assert graph.get_node("bl").position == [12.0, 34.0]

    def test_absent_position_still_applies(self):
        graph = _graph()
        result = _apply(graph, [{
            "op": "add_node", "node_type": "blur", "node_id": "bl"}])
        assert result.applied, result.error


# ── AUD-5607: group/ungroup existence validation ────────────────────────

class TestAud5607GroupExistence:
    def test_duplicate_group_rejected_at_validate(self):
        graph = _graph()
        assert _apply(graph, [
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]}]).applied
        # Second plan re-creating the same group: previously validate was
        # green and apply failed with a whole-plan rollback.
        result = _apply(graph, [
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]}], "p2")
        assert "group already exists" in _first_error(result)

    def test_ungroup_missing_group_rejected_at_validate(self):
        graph = _graph()
        result = _apply(graph, [{"op": "ungroup", "group_id": "nope"}])
        assert "group not found" in _first_error(result)

    def test_duplicate_group_within_one_plan_rejected(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]},
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]},
        ])
        assert "group already exists" in _first_error(result)

    def test_group_then_ungroup_in_one_plan_is_valid(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]},
            {"op": "ungroup", "group_id": "g1"},
        ])
        # Plan-internal group tracking must keep this sequence legal
        # (apply would have executed both ops successfully before).
        assert result.applied, result.error
        assert "g1" not in graph._node_groups

    def test_ungroup_then_group_same_id_in_one_plan_is_valid(self):
        graph = _graph()
        assert _apply(graph, [
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]}]).applied
        result = _apply(graph, [
            {"op": "ungroup", "group_id": "g1"},
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]},
        ], "p2")
        assert result.applied, result.error
        assert "g1" in graph._node_groups

    def test_group_then_ungroup_then_ungroup_rejected(self):
        graph = _graph()
        result = _apply(graph, [
            {"op": "group", "group_id": "g1", "node_ids": ["xf"]},
            {"op": "ungroup", "group_id": "g1"},
            {"op": "ungroup", "group_id": "g1"},
        ])
        assert "group not found" in _first_error(result)


# ── AUD-5608: from_dict domain boundary ─────────────────────────────────

class TestAud5608FromDictBoundary:
    def test_non_mapping_operation_raises_domain_error(self):
        with pytest.raises(CompositeEditError) as excinfo:
            CompositeEditPlan.from_dict({"operations": ["not-a-dict"]})
        assert "operation #0" in str(excinfo.value)

    def test_int_operation_raises_domain_error(self):
        with pytest.raises(CompositeEditError):
            # dict(5) raised TypeError before the fix.
            CompositeEditPlan.from_dict({"operations": [5]})

    def test_string_operation_raises_domain_error(self):
        with pytest.raises(CompositeEditError):
            # dict("ab") raised ValueError before the fix.
            CompositeEditPlan.from_dict({"operations": ["ab"]})

    def test_bad_base_generation_raises_domain_error(self):
        with pytest.raises(CompositeEditError) as excinfo:
            CompositeEditPlan.from_dict({
                "operations": [], "base_generation": "abc"})
        assert "base_generation" in str(excinfo.value)

    def test_none_base_generation_is_allowed(self):
        plan = CompositeEditPlan.from_dict({"operations": []})
        assert plan.base_generation is None

    def test_valid_plan_round_trips(self):
        plan = CompositeEditPlan.from_dict({
            "plan_id": "p", "base_generation": 3,
            "operations": [{"op": "set_parameter", "node_id": "n",
                            "param_name": "x", "value": 1.0}]})
        assert plan.base_generation == 3
        assert plan.operations[0]["value"] == 1.0

    def test_apply_path_raises_domain_error_not_typeerror(self):
        # _coerce_plan → from_dict runs before any validation; before the
        # fix a raw TypeError escaped the service boundary instead of the
        # displayable domain error.
        service = CompositeEditService(_graph())
        with pytest.raises(CompositeEditError) as excinfo:
            service.apply({"operations": [None]})
        assert "operation #0" in str(excinfo.value)


# ── headless evaluator harness ───────────────────────────────────────────

class _FakeTexture:
    def __init__(self, glo: int, w: int, h: int, pixels=None, fail_write=False):
        self.glo = glo
        self.width = w
        self.height = h
        self.pixels = pixels  # (h, w, 4) float32 for readback fakes
        self.fail_write = fail_write
        self.written: list[bytes] = []

    def use(self, unit: int = 0) -> None:
        pass

    def write(self, data) -> None:
        if self.fail_write:
            raise RuntimeError("GL write failed")
        self.written.append(bytes(data))


class _FakeUniform:
    def __init__(self, program: "_FakeProgram", name: str):
        self._program = program
        self._name = name

    @property
    def value(self):
        return self._program.uniform_values.get(self._name)

    @value.setter
    def value(self, v) -> None:
        if self._name in self._program.error_on_set:
            raise self._program.error_cls(
                f"uniform upload failed: {self._name}")
        self._program.uniform_values[self._name] = v


class _FakeProgram:
    """moderngl.Program stand-in: dict-style uniform access.

    ``missing`` names raise KeyError on __getitem__ (uniform optimized out
    of the shader); ``error_on_set`` names raise on assignment.
    """

    def __init__(self, missing=(), error_on_set=(), error_cls=KeyError):
        self.missing = set(missing)
        self.error_on_set = set(error_on_set)
        self.error_cls = error_cls
        self.uniform_values: dict[str, object] = {}

    def __getitem__(self, name: str) -> _FakeUniform:
        if name in self.missing:
            raise KeyError(name)
        return _FakeUniform(self, name)

    def __contains__(self, name: str) -> bool:
        return name not in self.missing


class _FakeFbo:
    def __init__(self, tex: _FakeTexture):
        self._tex = tex

    def use(self) -> None:
        pass

    def read(self, components: int = 4, dtype: str = "f4") -> bytes:
        assert components == 4 and dtype == "f4"
        return np.ascontiguousarray(
            self._tex.pixels, dtype=np.float32).tobytes()


class _FakeFboManager:
    def __init__(self, pool: "_FakePool"):
        self._pool = pool
        self._active_textures: list[_FakeTexture] = []

    def get_fbo(self, tex) -> _FakeFbo:
        if tex not in self._active_textures:
            self._active_textures.append(tex)
        return _FakeFbo(tex)


class _FakePool:
    """GPUResourcePool stand-in tracking active textures by glo."""

    def __init__(self):
        self._active_textures: dict[int, _FakeTexture] = {}
        self._next_glo = 100
        self.released: list[int] = []
        self._pooled_textures: dict[tuple, list[_FakeTexture]] = {}

    def acquire_texture(self, w: int, h: int, components: int = 4,
                        dtype: str = 'f4') -> _FakeTexture:
        # R-01：统一上传 helper（upload_pooled_texture）按生产池签名
        # 传递 components/dtype——f2 默认池正是审计 R-01 的缺陷根源，
        # 桩必须显式接受 dtype 才能继续代表生产合同。
        self._next_glo += 1
        tex = _FakeTexture(self._next_glo, w, h)
        self._active_textures[tex.glo] = tex
        return tex

    def release_texture(self, tex, force_evict: bool = False) -> None:
        glo = getattr(tex, "glo", -1)
        self._active_textures.pop(glo, None)
        self.released.append(glo)

    def release_texture_by_id(self, tex_id: int,
                              force_evict: bool = False) -> None:
        tex = self._active_textures.get(tex_id)
        if tex is None:
            return
        del self._active_textures[tex_id]
        self.released.append(tex_id)

    def active_count(self) -> int:
        return len(self._active_textures)


class _FakeCtx:
    def __init__(self):
        self.viewport = (0, 0, 0, 0)
        self.cleared: list[tuple] = []

    def clear(self, *args, **kwargs) -> None:
        self.cleared.append(args)

    def texture(self, size, components, data=None, dtype="f1"):
        return _FakeTexture(1, size[0], size[1])


class _EffectHarness(
    BaseEvaluatorMixin,
    BlurEvaluatorMixin,
    ColorEvaluatorMixin,
    ChannelEvaluatorMixin,
    CacheEvaluatorMixin,
):
    """Composes the evaluator mixins with fakes — no GL context required.

    ``_render_quad_script`` is a list consumed one entry per draw; each
    entry is either None (draw succeeds) or an exception instance to raise.
    """

    def __init__(self, shaders=None):
        self.ctx = _FakeCtx()
        self._resource_pool = _FakePool()
        self.fb_manager = _FakeFboManager(self._resource_pool)
        self.utility_shaders: dict[str, _FakeProgram] = dict(shaders or {})
        self._render_nodes: dict[str, object] = {}
        self._snapshot = None
        self.bound: list[tuple[int, int]] = []
        self.quad_calls: list[_FakeProgram] = []
        self._render_quad_script: list = []

    # ── GL-touching overrides ──
    def _render_quad(self, program) -> None:
        self.quad_calls.append(program)
        if self._render_quad_script:
            error = self._render_quad_script.pop(0)
            if error is not None:
                raise error

    def _bind_texture_unit(self, texture_id: int, unit: int) -> None:
        self.bound.append((int(texture_id), int(unit)))

    def _unbind_texture_unit(self, unit: int) -> None:
        self._bind_texture_unit(0, unit)

    def _get_texture_by_id(self, texture_id: int):
        return self._resource_pool._active_textures.get(texture_id)

    def _compute_merge_geometry(self, input_a, input_b, input_mask,
                                format_policy, bbox_policy) -> MergeGeometry:
        w = max(input_a.width, input_b.width)
        h = max(input_a.height, input_b.height)
        bbox = BBox(0, 0, w, h)
        return MergeGeometry(
            output_width=w, output_height=h, output_bbox=bbox,
            bbox_a=bbox, bbox_b=bbox, bbox_mask=None,
            resolution_a=(input_a.width, input_a.height),
            resolution_b=(input_b.width, input_b.height),
            resolution_mask=None, output_format=None)


def _tex(texture_id: int = 1, w: int = 4, h: int = 4, **kwargs) -> TextureResult:
    return TextureResult(texture_id=texture_id, width=w, height=h, **kwargs)


@pytest.fixture(autouse=True)
def _no_raw_gl_binding(monkeypatch):
    """Never dispatch raw PyOpenGL binds from these headless tests.

    ``gl_bind.bind_texture_unit`` no-ops without a current context, but a
    stale/dead context pointer left in the host process makes the dispatch
    segfault the interpreter. Several evaluator paths import it at call
    time (``from ..core.gl_bind import bind_texture_unit``), so patching
    the module attribute covers them all; the harness records the
    ``self._bind_texture_unit`` calls it needs to assert on.
    """
    monkeypatch.setattr(
        "src.features.composite.core.gl_bind.bind_texture_unit",
        lambda texture_id, unit: None)


# ── AUD-5801: fused chain revision propagation ──────────────────────────

class TestAud5801FusedChainRevision:
    def test_output_carries_input_revision(self):
        harness = _EffectHarness({"fused": _FakeProgram()})
        chain = SimpleNamespace(node_ids=["n1"], node_types=["grade"])
        input_tex = _tex(texture_id=11, revision=7)
        result = harness._apply_fused_chain(
            chain, input_tex, [{"lift_r": 0.1, "lift_g": 0.0, "lift_b": 0.0}],
            w=4, h=4)
        # revision defaulted to 0 before the fix — a recycled pool glo then
        # aliased downstream cache keys across eviction cycles.
        assert result["result"].revision == 7

    def test_zero_revision_propagates_faithfully(self):
        harness = _EffectHarness({"fused": _FakeProgram()})
        chain = SimpleNamespace(node_ids=["n1"], node_types=[])
        result = harness._apply_fused_chain(chain, _tex(), [], w=4, h=4)
        assert result["result"].revision == 0


# ── AUD-5802: merge blend texture-unit-0 unbind ─────────────────────────

class TestAud5802MergeBlendUnbind:
    def test_unit0_unbound_after_render(self):
        harness = _EffectHarness({"blend": _FakeProgram()})
        result = harness._apply_merge_blend(
            _tex(texture_id=21), _tex(texture_id=22), None,
            "over", 1.0, "from_b", "union")
        assert result["result"].is_valid
        unit0_binds = [b for b in harness.bound if b[1] == 0]
        # input_a bound to unit 0 during the draw, then explicitly unbound
        # (P1-6) — the last unit-0 record must be the (0, 0) unbind.
        assert unit0_binds[0] == (21, 0)
        assert unit0_binds[-1] == (0, 0)


# ── AUD-5803: merge color-space conversion texture ownership ────────────

class TestAud5803ConversionTextureOwnership:
    def test_converted_input_released_after_merge(self):
        harness = _EffectHarness({
            "blend": _FakeProgram(),
            "cs_transform": _FakeProgram(),
        })
        original_a = _tex(texture_id=31, color_space="srgb")
        input_b = _tex(texture_id=32, color_space="linear")
        active_before = set(harness._resource_pool._active_textures)

        result = harness._apply_merge_blend(
            original_a, input_b, None, "over", 1.0, "from_b", "union")

        assert result["result"].is_valid
        released = harness._resource_pool.released
        # The conversion allocated exactly one new texture (sRGB A→linear);
        # together with the merge output that is exactly two new glos.
        new_glos = ((set(harness._resource_pool._active_textures)
                     | set(released)) - active_before)
        assert len(new_glos) == 2  # conversion scratch + merge output
        # …while the original upstream textures are untouched.
        assert 31 not in released and 32 not in released
        # The merge result texture stays active (owned by the caller).
        assert result["result"].texture_id \
            in harness._resource_pool._active_textures
        # The conversion texture was released, not left active forever.
        assert set(released) & new_glos, (
            "conversion texture must be returned to the pool after the draw")

    def test_no_conversion_no_release(self):
        harness = _EffectHarness({"blend": _FakeProgram()})
        result = harness._apply_merge_blend(
            _tex(texture_id=41, color_space="linear"),
            _tex(texture_id=42, color_space="linear"),
            None, "over", 1.0, "from_b", "union")
        assert result["result"].is_valid
        assert harness._resource_pool.released == []


# ── AUD-5804: AI result texture revision ────────────────────────────────

class TestAud5804AiResultRevision:
    def test_revision_follows_reference(self):
        harness = _EffectHarness()
        result_bgr = np.zeros((4, 6, 3), dtype=np.float32)
        reference = _tex(texture_id=51, w=6, h=4, revision=9)
        out = harness._upload_ai_result_texture(result_bgr, reference)
        # Previously hardcoded revision=1 — a recycled pool glo then hit
        # stale downstream cache entries keyed (glo, 1).
        assert out is not None
        assert out.revision == 9
        assert (out.width, out.height) == (6, 4)

    def test_zero_reference_revision_propagates(self):
        harness = _EffectHarness()
        out = harness._upload_ai_result_texture(
            np.zeros((2, 2, 3), dtype=np.float32), _tex(w=2, h=2, revision=0))
        assert out.revision == 0


# ── AUD-5805: HDR-preserving readback, model-domain clamp ───────────────

def _harness_with_readable_texture(value: float = 2.5):
    harness = _EffectHarness()
    pixels = np.full((4, 4, 4), value, dtype=np.float32)
    pixels[:, :, 3] = 1.0
    tex = _FakeTexture(60, 4, 4, pixels=pixels)
    harness._resource_pool._active_textures[60] = tex
    return harness, tex


class TestAud5805HdrReadback:
    def test_readback_preserves_values_above_one(self):
        harness, _ = _harness_with_readable_texture(value=2.5)
        arr = harness._readback_texture_fullres(_tex(texture_id=60, w=4, h=4))
        # Previously the final np.clip(0, 1) flattened every HDR highlight
        # on the composite write-back path.
        assert arr is not None
        assert arr.max() == pytest.approx(2.5)
        assert arr.shape == (4, 4, 3)

    def test_ai_model_request_clamps_its_own_copy(self):
        harness, tex = _harness_with_readable_texture(value=2.5)
        requests: list[dict] = []

        class _FakeAIManager:
            def is_processing(self, key):
                return False

            def request_compute(self, key, node_type, inputs, params,
                                request_id=0, node_id=""):
                requests.append(inputs)

        class _FakeAICache:
            def compute_key(self, *args, **kwargs):
                return "k1"

            def has_cache(self, key):
                return False

            def get_cache_path(self, key):
                return None

        harness._ai_compute_manager = _FakeAIManager()
        harness._ai_cache_manager = _FakeAICache()
        ns = SimpleNamespace(
            node_id="n1", node_type="ai_remove",
            output_ports=[("image",)], parameters={})
        outputs = harness._evaluate_ai_node(ns, {
            "image": _tex(texture_id=60, w=4, h=4)})
        # Model input domain is [0, 1] — only the submitted copy is clamped.
        assert requests, "compute request must have been issued"
        assert requests[0]["image"].max() <= 1.0
        # …and the node still passes the (HDR) input through meanwhile.
        assert outputs["image"].texture_id == 60

    def test_masked_composite_preserves_hdr_outside_mask(self, monkeypatch):
        import cv2
        harness, _ = _harness_with_readable_texture(value=2.5)
        img_np = np.full((2, 2, 3), 4.0, dtype=np.float32)
        mask_np = np.zeros((2, 2), dtype=np.float32)
        cached = np.full((2, 2, 4), 0.5, dtype=np.float32)
        monkeypatch.setattr(cv2, "imread",
                            lambda path, flags: cached.copy(), raising=False)

        class _Cache:
            @staticmethod
            def get_cache_path(key):
                return "/fake/cache.exr"

        result = BaseEvaluatorMixin._read_ai_cache_result(
            _Cache(), "k", img_np, mask_np)
        # Outside the mask (m=0) the un-clamped linear input wins: 4.0
        # survives instead of being flattened to 1.0.
        assert result.max() == pytest.approx(4.0)


# ── AUD-5806: node-eval failure texture rollback ────────────────────────

class TestAud5806FailedEvalRollback:
    def test_snapshot_and_rollback_release_only_delta(self):
        harness = _EffectHarness()
        pool = harness._resource_pool
        keep = pool.acquire_texture(4, 4)      # pre-existing active texture
        baseline = harness._snapshot_active_texture_ids()
        assert baseline == {keep.glo}

        leaked_1 = pool.acquire_texture(4, 4)  # "acquired by the node"
        leaked_2 = pool.acquire_texture(4, 4)
        assert harness._snapshot_active_texture_ids() == {
            keep.glo, leaked_1.glo, leaked_2.glo}

        harness._release_failed_node_textures(baseline)

        assert keep.glo in pool._active_textures
        assert leaked_1.glo not in pool._active_textures
        assert leaked_2.glo not in pool._active_textures
        assert set(pool.released) == {leaked_1.glo, leaked_2.glo}

    def test_source_node_textures_are_not_rolled_back(self):
        harness = _EffectHarness()

        class _SourceNode:
            @staticmethod
            def get_texture_id() -> int:
                return 70

        harness._render_nodes["src"] = _SourceNode()
        pool = harness._resource_pool
        pool._active_textures[70] = _FakeTexture(70, 4, 4)
        baseline = harness._snapshot_active_texture_ids() - {70}

        harness._release_failed_node_textures(baseline)
        # Source nodes manage their own texture lifetimes.
        assert 70 not in pool.released

    def test_evaluate_snapshot_wires_rollback_and_stack_trace(self):
        source = inspect.getsource(BaseEvaluatorMixin._evaluate_snapshot)
        # The exception path must log at exception level (with stack) and
        # roll back the textures acquired by the failed node.
        assert "logger.exception" in source
        assert "_release_failed_node_textures(active_textures_before)" in source
        assert "active_textures_before = self._snapshot_active_texture_ids()" \
            in source

    def test_pool_introspection_failure_is_conservative(self):
        harness = _EffectHarness()
        harness._resource_pool = None
        # Pool unavailable → snapshot empty and rollback a no-op; the
        # error path must never raise a secondary error of its own.
        assert harness._snapshot_active_texture_ids() == set()
        harness._release_failed_node_textures(set())


# ── AUD-5910: blur evaluator exception-path texture release ─────────────

class TestAud5910BlurTextureRelease:
    def test_separable_h_pass_failure_releases_temp(self):
        harness = _EffectHarness({
            "blur_separable_h": _FakeProgram(),
            "blur_separable_v": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("H-pass boom")]
        with pytest.raises(RuntimeError, match="H-pass boom"):
            harness._blur_separable(_tex(), {"radius": 5.0}, 4, 4, "box")
        # temp texture acquired before the failure must be returned.
        assert harness._resource_pool.released, "temp texture leaked"
        assert harness._resource_pool.active_count() == 0

    def test_separable_v_pass_failure_releases_temp_and_out(self):
        harness = _EffectHarness({
            "blur_separable_h": _FakeProgram(),
            "blur_separable_v": _FakeProgram()})
        harness._render_quad_script = [None, RuntimeError("V-pass boom")]
        with pytest.raises(RuntimeError, match="V-pass boom"):
            harness._blur_separable(_tex(), {"radius": 5.0}, 4, 4, "box")
        assert harness._resource_pool.active_count() == 0
        assert len(harness._resource_pool.released) == 2  # temp + out

    def test_single_pass_failure_releases_out(self):
        harness = _EffectHarness({"blur_bilateral": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            harness._blur_single_pass(
                _tex(), {"radius": 5.0}, 4, 4, "blur_bilateral")
        assert harness._resource_pool.active_count() == 0
        assert len(harness._resource_pool.released) == 1

    def test_defocus_missing_uniform_failure_releases_out(self):
        harness = _EffectHarness({
            "defocus": _FakeProgram(missing={"u_mode"})})
        with pytest.raises(KeyError):
            harness._apply_defocus(_tex(), {"max_radius": 5.0}, 4, 4)
        assert harness._resource_pool.active_count() == 0
        assert len(harness._resource_pool.released) == 1

    def test_sharpen_failure_releases_out(self):
        harness = _EffectHarness({"sharpen": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            harness._apply_sharpen(
                _tex(), {"amount": 1.0, "radius": 1.0}, 4, 4)
        assert harness._resource_pool.active_count() == 0
        assert len(harness._resource_pool.released) == 1

    def test_separable_success_path_owns_output(self):
        harness = _EffectHarness({
            "blur_separable_h": _FakeProgram(),
            "blur_separable_v": _FakeProgram()})
        result = harness._blur_separable(_tex(), {"radius": 5.0}, 4, 4, "box")
        out_id = result["result"].texture_id
        # Success semantics unchanged: exactly the H-pass temp texture was
        # returned to the pool; the output stays active and owned by the
        # returned TextureResult.
        assert len(harness._resource_pool.released) == 1
        assert harness._resource_pool.released[0] != out_id
        assert out_id in harness._resource_pool._active_textures
        assert harness._resource_pool.active_count() == 1


# ── AUD-5903: color evaluator exception-path texture release ────────────

class TestAud5903ColorTextureRelease:
    def test_grade_scalar_lift_failure_releases_out(self):
        harness = _EffectHarness({"grade": _FakeProgram()})
        with pytest.raises(TypeError):
            # tuple(0.5) — the scalar-parameter failure mode from the audit.
            harness._apply_grade(_tex(), {"lift": 0.5}, 4, 4)
        assert harness._resource_pool.active_count() == 0
        assert len(harness._resource_pool.released) == 1

    def test_color_correct_scalar_lifting_failure_releases_out(self):
        harness = _EffectHarness({"color_correct": _FakeProgram()})
        with pytest.raises(TypeError):
            harness._apply_color_correct(
                _tex(), {"shadows_lifting": 0.5}, 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_huecorrect_render_failure_releases_out(self):
        harness = _EffectHarness({"huecorrect": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            harness._apply_huecorrect(_tex(), {}, 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_normalize_scalar_mask_failure_releases_out(self):
        harness = _EffectHarness({"normalize": _FakeProgram()})
        with pytest.raises(TypeError):
            harness._apply_normalize(_tex(), {"channel_mask": 0.5}, 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_invert_scalar_channels_failure_releases_out(self):
        harness = _EffectHarness({"invert": _FakeProgram()})
        with pytest.raises(TypeError):
            harness._apply_invert(_tex(), {"channels": 0.5}, 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_premult_render_failure_releases_out(self):
        harness = _EffectHarness({"premult": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            harness._apply_premult(_tex(), 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_unpremult_render_failure_releases_out(self):
        harness = _EffectHarness({"unpremult": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            harness._apply_unpremult(_tex(), 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_clamp_non_string_channels_failure_releases_out(self):
        harness = _EffectHarness({"clamp": _FakeProgram()})
        with pytest.raises(TypeError):
            harness._apply_clamp(_tex(), {"channels": 5}, 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_topos_color_render_failure_releases_out(self, monkeypatch):
        monkeypatch.setattr(
            "src.features.color.core.color_stack_render_service"
            ".ColorStackRenderService",
            type("_FakeStackService", (), {
                "resolve_grading_params": lambda self, **kw: {}}))
        harness = _EffectHarness({"topos_color": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            harness._apply_topos_color(_tex(), {}, 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_premult_success_path_owns_output(self):
        harness = _EffectHarness({"premult": _FakeProgram()})
        result = harness._apply_premult(_tex(), 4, 4)
        assert result["result"].texture_id \
            in harness._resource_pool._active_textures
        assert harness._resource_pool.released == []
        assert result["result"].alpha_premultiplied is True


# ── AUD-5930: channel evaluator exception-path texture release ──────────

class TestAud5930ChannelTextureRelease:
    def test_shuffle_missing_uniform_failure_releases_out(self):
        harness = _EffectHarness({
            "shuffle": _FakeProgram(missing={"u_g_from"})})
        with pytest.raises(KeyError):
            harness._apply_shuffle(_tex(), {}, 4, 4)
        assert harness._resource_pool.active_count() == 0
        assert len(harness._resource_pool.released) == 1

    def test_copy_channel_render_failure_releases_out(self):
        harness = _EffectHarness({"copy_channel": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            harness._apply_copy_channel(
                _tex(texture_id=81), {"input_b": _tex(texture_id=82)}, 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_remove_channels_render_failure_releases_out(self):
        harness = _EffectHarness({"remove_channels": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        with pytest.raises(RuntimeError):
            harness._apply_remove_channels(_tex(), {}, 4, 4)
        assert harness._resource_pool.active_count() == 0

    def test_channel_merge_render_failure_releases_out(self):
        harness = _EffectHarness({"channel_merge": _FakeProgram()})
        harness._render_quad_script = [RuntimeError("boom")]
        params = {"input_r": _tex(texture_id=91),
                  "input_g": _tex(texture_id=92)}
        with pytest.raises(RuntimeError):
            harness._apply_channel_merge(params, 4, 4)
        assert harness._resource_pool.active_count() == 0
        # Error path also unbinds the four texture units it used.
        tail = harness.bound[-4:]
        assert tail == [(0, 0), (0, 1), (0, 2), (0, 3)]

    def test_shuffle_success_path_owns_output(self):
        harness = _EffectHarness({"shuffle": _FakeProgram()})
        result = harness._apply_shuffle(_tex(), {}, 4, 4)
        assert result["image"].texture_id \
            in harness._resource_pool._active_textures
        assert harness._resource_pool.released == []


# ── AUD-5920: disk-cache hit restores auxiliary metadata ────────────────

class TestAud5920DiskCacheAuxMetadata:
    def _harness_and_inputs(self):
        harness = _EffectHarness()
        working_inputs = {
            "image": _tex(
                texture_id=7, w=4, h=4,
                depth_texture_id=99, has_deep_data=True,
                aov_texture_ids={"diffuse": 77},
                aov_dimensions={"diffuse": (4, 4)},
                render_diagnostics={"active_camera": "cam1"}),
        }
        return harness, working_inputs

    def test_hit_restores_depth_aov_and_diagnostics(self, monkeypatch):
        rgba = np.zeros((4, 4, 4), dtype=np.float32)
        monkeypatch.setattr(
            "src.features.composite.render.disk_cache_store"
            ".get_disk_cache_store",
            lambda: type("_Store", (), {"load": staticmethod(
                lambda key: rgba.copy())})())
        harness, working_inputs = self._harness_and_inputs()
        ns = SimpleNamespace(
            node_id="n", node_type="depth_matte",
            output_ports=[("result",)], parameters={"cache_to_disk": True})

        outputs = harness._load_disk_cache_outputs(ns, "k", working_inputs)

        # Fresh evaluations run _propagate_auxiliary_metadata; the disk
        # branch used to skip it, so depth/AOV/render_diagnostics silently
        # vanished on hit frames.
        assert outputs is not None
        restored = outputs["result"]
        assert restored.has_depth()
        assert restored.depth_texture_id == 99
        assert restored.aov_texture_ids == {"diffuse": 77}
        assert restored.render_diagnostics == {"active_camera": "cam1"}
        # The restored color texture is a fresh pool upload.
        assert restored.texture_id != 7

    def test_miss_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "src.features.composite.render.disk_cache_store"
            ".get_disk_cache_store",
            lambda: type("_Store", (), {"load": staticmethod(
                lambda key: None)})())
        harness, working_inputs = self._harness_and_inputs()
        ns = SimpleNamespace(node_id="n", node_type="depth_matte",
                             output_ports=[("result",)], parameters={})
        assert harness._load_disk_cache_outputs(
            ns, "k", working_inputs) is None

    def test_upload_write_failure_releases_acquired_texture(self):
        harness = _EffectHarness()
        pool = harness._resource_pool
        original_acquire = pool.acquire_texture

        def acquire_then_fail(w, h):
            tex = original_acquire(w, h)
            tex.fail_write = True
            return tex

        pool.acquire_texture = acquire_then_fail
        rgba = np.zeros((2, 2, 4), dtype=np.float32)
        # The AUD-5930-pattern leak flagged alongside this batch: acquire
        # succeeded, tex.write raised, the texture stayed active forever.
        assert harness._upload_rgba_array_result(rgba, None) is None
        assert pool.active_count() == 0
        assert pool.released
