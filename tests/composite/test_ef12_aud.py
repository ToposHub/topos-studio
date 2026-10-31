"""EF-12 batch regression tests — Suggestion-level audit fixes in
src/features/composite/nodes.

Covers (one test group per AUD id):
  AUD-6312  ai_gen_expand transport stack replaced as an atomic group
            (lock + _generation_stack snapshot reads)
  AUD-6706  face_track _DETECTOR snapshot accessor + init-time contract
  AUD-6802  frame_interpolate RIFE path cached identity-keyed readbacks
  AUD-6909  invert HDR semantics documented (pivot 1.0, negative outputs)
  AUD-7000  luma_keyer HELP_TEXT matches PARAM_DEFS
  AUD-7200  ramp ramp_type/extend_mode whitelist validation + docstring
  AUD-7209  retime docstring no longer promises optical-flow interpolation
  AUD-7309  scene3d vbo_cache_hit False branch + model_cache_hit from status
  AUD-7310  scene3d shadow-catcher blend_func restored + except narrowed
  AUD-7312  scene3d redraw_with_view writes parameters only for the virtual
            viewer scene
  AUD-7401  set_alpha HELP_TEXT parameter name is ``value``
  AUD-7413  text caption override via _text_override, no parameters mutation
  AUD-7509  unknown_placeholder actually logs the promised warning
  AUD-7606  video_frame vt gate counters under _cache_lock
  AUD-7704  volume3d CPU gather memory budget (downsample, no MemoryError)

Every test is a pure-data / mock path: no GL context, no modal dialogs,
no real decode threads.
"""

import inspect
import logging
import os
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.nodes.ai_gen_expand_node import (
    AIGenExpandNode,
    _generation_stack,
    reset_generation_transport,
    set_generation_transport,
    _TRANSPORT_LOCK,
)
import src.features.composite.nodes.ai_gen_expand_node as ai_gen_mod
from src.features.composite.nodes.face_track_node import (
    FaceTrackNode,
    current_face_detector,
    set_face_detector,
)
import src.features.composite.nodes.face_track_node as face_mod
from src.features.composite.nodes.frame_interpolate_node import (
    FrameInterpolateNode,
)
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.invert_node import InvertNode
from src.features.composite.nodes.luma_keyer_node import LumaKeyerNode
from src.features.composite.nodes.ramp_node import RampNode, RAMP_TYPES, EXTEND_MODES
from src.features.composite.nodes.retime_node import RetimeNode
from src.features.composite.nodes.scene3d_node import (
    Scene3DNode,
    merge_vbo_cache_hit,
    model_cache_hit_from_status,
)
import src.features.composite.nodes.scene3d_node as scene3d_mod
from src.features.composite.nodes.set_alpha_node import SetAlphaNode
from src.features.composite.nodes.text_node import TextNode
from src.features.composite.nodes.unknown_placeholder_node import (
    UnknownPlaceholderNode,
)
from src.features.composite.nodes.video_frame_node import VideoFrameNode
import src.features.composite.nodes.video_frame_node as video_mod
from src.features.composite.nodes.volume3d_node import (
    Volume3DNode,
    budget_output_size,
    _MAX_SAMPLE_GATHER_BYTES,
)
import src.features.composite.nodes.volume3d_node as volume_mod
import src.features.composite.render3d.volume_grid as volume_grid_mod
from src.features.composite.render3d.volume_grid import VolumeGrid

pytestmark = pytest.mark.composite_core


# ──────────────────────────────────────────────────────────────────────────
# AUD-6312 — transport stack replaced as one atomic group
# ──────────────────────────────────────────────────────────────────────────

class _GateLock:
    """Lock whose __enter__ signals `entered` then blocks on a gate."""

    def __init__(self) -> None:
        self.inner = threading.Lock()
        self.entered = threading.Event()
        self.release_gate = threading.Event()

    def __enter__(self) -> "_GateLock":
        self.inner.acquire()
        self.entered.set()
        self.release_gate.wait(timeout=5.0)
        return self

    def __exit__(self, *exc: object) -> bool:
        self.inner.release()
        return False


class TestAud6312TransportAtomic:
    def test_snapshot_roundtrip(self):
        reset_generation_transport()
        try:
            assert _generation_stack() == (None, None, None)
            t = lambda req, key, size: None  # noqa: E731
            kp = lambda: "k"  # noqa: E731
            cc = lambda cost, prompt: True  # noqa: E731
            set_generation_transport(t, kp, cc)
            got = _generation_stack()
            assert got == (t, kp, cc)
        finally:
            reset_generation_transport()
        assert _generation_stack() == (None, None, None)

    def test_node_reads_via_snapshot(self):
        # _do_evaluate and _build_job must go through the snapshot helper
        # (AUD-6312), not the bare module globals.
        for func in (AIGenExpandNode._do_evaluate, AIGenExpandNode._build_job):
            assert "_generation_stack" in inspect.getsource(func), (
                f"{func.__name__} must read the stack via _generation_stack")
        # And the helper itself must hold the lock.
        assert "_TRANSPORT_LOCK" in inspect.getsource(_generation_stack)

    def test_replacement_is_atomic_under_lock(self, monkeypatch):
        reset_generation_transport()
        gate = _GateLock()
        monkeypatch.setattr(ai_gen_mod, "_TRANSPORT_LOCK", gate)

        new_trio = ("T", "K", "C")
        done = threading.Event()

        def writer() -> None:
            set_generation_transport(*new_trio)
            done.set()

        writer_thread = threading.Thread(target=writer, daemon=True)
        writer_thread.start()
        # The writer must be parked inside the lock before any assignment.
        assert gate.entered.wait(timeout=2.0), (
            "set_generation_transport must take _TRANSPORT_LOCK (AUD-6312)")

        reader_done = threading.Event()
        reader_out: dict = {}

        def reader() -> None:
            reader_out["stack"] = _generation_stack()
            reader_done.set()

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        # While the writer holds the lock, the snapshot read blocks —
        # i.e. a reader can never observe the trio mid-replacement.
        assert not reader_done.wait(timeout=0.3), (
            "_generation_stack observed state while writer was mid-replacement")
        gate.release_gate.set()
        assert reader_done.wait(timeout=2.0)
        # After the writer finished, the reader sees the complete new trio.
        assert reader_out["stack"] == new_trio
        assert done.wait(timeout=2.0)
        monkeypatch.undo()
        reset_generation_transport()

    def test_node_helpers_exist_for_churn(self):
        # Sanity: the lock is a real lock (churn relies on it).
        assert hasattr(_TRANSPORT_LOCK, "acquire")
        assert hasattr(_TRANSPORT_LOCK, "release")


# ──────────────────────────────────────────────────────────────────────────
# AUD-6706 — face detector snapshot accessor
# ──────────────────────────────────────────────────────────────────────────

class TestAud6706FaceDetectorSnapshot:
    @pytest.fixture(autouse=True)
    def _restore_detector(self):
        old = face_mod._DETECTOR
        yield
        face_mod._DETECTOR = old

    def test_accessor_returns_installed_detector(self):
        set_face_detector(None)
        assert current_face_detector() is None
        sentinel = object()
        set_face_detector(sentinel)
        assert current_face_detector() is sentinel

    def test_detect_at_reads_via_accessor(self):
        # The installed detector is a decoy that yields NO results; the
        # accessor is monkeypatched to return a working detector.  If the
        # read path regresses to the bare global, detection reports
        # NO_FACE instead of OK and this test fails.
        set_face_detector(lambda frame, image: ([], 0.0))
        sentinel_detector = lambda frame, image: ([(0.5, 0.5)], 0.9)  # noqa: E731
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(face_mod, "current_face_detector",
                           lambda: sentinel_detector)
            node = FaceTrackNode("face1")
            assert node.detect_at(7) is True
            assert node.detection_state == "OK"
            tracks = node.get_landmarks()
            assert 7 in tracks
        finally:
            monkey.undo()

    def test_documented_threading_contract(self):
        doc = (face_mod.__doc__ or "") + \
            (inspect.getdoc(current_face_detector) or "") + \
            (inspect.getdoc(set_face_detector) or "")
        assert "AUD-6706" in doc


# ──────────────────────────────────────────────────────────────────────────
# AUD-6802 — RIFE readback cache
# ──────────────────────────────────────────────────────────────────────────

class _FakePoolTex:
    def __init__(self, w: int, h: int) -> None:
        self.glo = 9001
        self.width = w
        self.height = h
        self.written: list = []

    def write(self, data: bytes) -> None:
        self.written.append(data)


class _FakeInterpolator:
    used_fallback = False

    def has_model(self) -> bool:
        return True

    def interpolate(self, a, b, t, hd_mode=False):
        return (a * (1.0 - t) + b * t).astype(np.float32)


class TestAud6802ReadbackCache:
    def _make_node(self):
        node = FrameInterpolateNode("interp1")
        node.parameters["t"] = 0.25
        node._interpolator = _FakeInterpolator()
        pool = SimpleNamespace(
            acquire_texture=lambda w, h, components=4, dtype="f4":
                _FakePoolTex(w, h))
        ctx = SimpleNamespace(gl=SimpleNamespace())
        return node, ctx, pool

    def test_repeated_evaluations_read_each_frame_once(self, monkeypatch):
        node, ctx, pool = self._make_node()
        calls = {"n": 0}

        def fake_read(tex_result, c, fb):
            calls["n"] += 1
            return np.full((4, 4, 4), float(tex_result.texture_id),
                           dtype=np.float32)

        monkeypatch.setattr(node, "_read_input", fake_read)
        frame_a = TextureResult(texture_id=11, width=4, height=4, revision=1)
        frame_b = TextureResult(texture_id=22, width=4, height=4, revision=1)

        node.parameters["t"] = 0.25
        node._do_evaluate({"frame_a": frame_a, "frame_b": frame_b},
                          ctx, None, resource_pool=pool)
        assert calls["n"] == 2
        # Same neighbour pair, different t → cached readbacks, no re-read.
        node.parameters["t"] = 0.75
        node._do_evaluate({"frame_a": frame_a, "frame_b": frame_b},
                          ctx, None, resource_pool=pool)
        assert calls["n"] == 2

    def test_revision_change_invalidates(self, monkeypatch):
        node, ctx, pool = self._make_node()
        calls = {"n": 0}

        def fake_read(tex_result, c, fb):
            calls["n"] += 1
            return np.zeros((4, 4, 4), dtype=np.float32)

        monkeypatch.setattr(node, "_read_input", fake_read)
        frame_a = TextureResult(texture_id=11, width=4, height=4, revision=1)
        frame_b = TextureResult(texture_id=22, width=4, height=4, revision=1)
        node._do_evaluate({"frame_a": frame_a, "frame_b": frame_b},
                          ctx, None, resource_pool=pool)
        frame_a_new = TextureResult(texture_id=11, width=4, height=4,
                                    revision=2)
        node._do_evaluate({"frame_a": frame_a_new, "frame_b": frame_b},
                          ctx, None, resource_pool=pool)
        assert calls["n"] == 3  # only the changed frame re-read

    def test_cache_bounded_and_cleared_by_cleanup(self, monkeypatch):
        node, ctx, pool = self._make_node()
        monkeypatch.setattr(
            node, "_read_input",
            lambda tr, c, fb: np.zeros((4, 4, 4), dtype=np.float32))
        for rev in (1, 2, 3):
            node._do_evaluate(
                {"frame_a": TextureResult(texture_id=11, width=4, height=4,
                                          revision=rev),
                 "frame_b": TextureResult(texture_id=22, width=4, height=4,
                                          revision=rev)},
                ctx, None, resource_pool=pool)
        assert len(node._readback_cache) <= 2
        node.cleanup()
        assert node._readback_cache == {}


# ──────────────────────────────────────────────────────────────────────────
# AUD-6909 — invert HDR semantics documented
# ──────────────────────────────────────────────────────────────────────────

class TestAud6909InvertHdrDoc:
    def test_hdr_semantics_documented(self):
        doc = (InvertNode.__doc__ or "")
        module_doc = (inspect.getmodule(InvertNode).__doc__ or "")
        help_text = InvertNode.HELP_TEXT
        assert "HDR" in module_doc and "HDR" in help_text
        assert "AUD-6909" in module_doc and "AUD-6909" in help_text
        combined = module_doc + help_text
        # The negative-output consequence must be stated explicitly.
        assert ("负值" in combined) or ("negative" in combined.lower())
        # ...and the fixed pivot.
        assert "1.0" in combined

    def test_invert_marker_unchanged(self):
        node = InvertNode("inv1")
        image = TextureResult(texture_id=5, width=8, height=8)
        out = node._do_evaluate({"image": image}, None, None)
        assert out["_effect_params"]["channels"] == [1.0, 1.0, 1.0, 0.0]


# ──────────────────────────────────────────────────────────────────────────
# AUD-7000 — luma_keyer HELP_TEXT matches PARAM_DEFS
# ──────────────────────────────────────────────────────────────────────────

class TestAud7000LumaKeyerHelpText:
    def test_help_text_covers_actual_params(self):
        help_text = LumaKeyerNode.HELP_TEXT
        for param in LumaKeyerNode.PARAM_DEFS:
            assert param.name in help_text, (
                f"HELP_TEXT missing actual parameter {param.name!r}")
        assert "low_threshold" not in help_text
        assert "high_threshold" not in help_text


# ──────────────────────────────────────────────────────────────────────────
# AUD-7200 — ramp whitelist validation + docstring
# ──────────────────────────────────────────────────────────────────────────

class TestAud7200RampWhitelist:
    def test_invalid_ramp_type_falls_back(self, caplog):
        node = RampNode("ramp1")
        node.parameters["ramp_type"] = "Bogus"
        node.parameters["extend_mode"] = "clamp"
        with caplog.at_level(
                logging.WARNING,
                logger="src.features.composite.nodes.ramp_node"):
            out = node._do_evaluate({}, None, None)
        assert out["_effect_params"]["ramp_type"] == "linear"
        assert any("ramp_type" in rec.message for rec in caplog.records)

    def test_invalid_extend_mode_falls_back(self, caplog):
        node = RampNode("ramp2")
        node.parameters["ramp_type"] = "diamond"
        node.parameters["extend_mode"] = "spiral"
        with caplog.at_level(
                logging.WARNING,
                logger="src.features.composite.nodes.ramp_node"):
            out = node._do_evaluate({}, None, None)
        assert out["_effect_params"]["extend_mode"] == "clamp"
        assert out["_effect_params"]["ramp_type"] == "diamond"
        assert any("extend_mode" in rec.message for rec in caplog.records)

    def test_valid_values_pass_through(self):
        node = RampNode("ramp3")
        for ramp_type in RAMP_TYPES:
            node.parameters["ramp_type"] = ramp_type
            out = node._do_evaluate({}, None, None)
            assert out["_effect_params"]["ramp_type"] == ramp_type
        for extend_mode in EXTEND_MODES:
            node.parameters["extend_mode"] = extend_mode
            out = node._do_evaluate({}, None, None)
            assert out["_effect_params"]["extend_mode"] == extend_mode

    def test_docstring_lists_diamond(self):
        module_doc = inspect.getmodule(RampNode).__doc__ or ""
        assert "diamond" in module_doc


# ──────────────────────────────────────────────────────────────────────────
# AUD-7209 — retime docstring honesty
# ──────────────────────────────────────────────────────────────────────────

class TestAud7209RetimeDoc:
    def test_module_doc_no_longer_promises_optical_flow(self):
        module_doc = inspect.getmodule(RetimeNode).__doc__ or ""
        class_doc = RetimeNode.__doc__ or ""
        eval_doc = RetimeNode._do_evaluate.__doc__ or ""
        combined = module_doc + class_doc + eval_doc
        # The over-promising sentence is gone.
        assert "when available" not in module_doc
        assert "frame blending for simple transitions" not in module_doc
        # The reserved status is stated.
        assert "RESERVED" in combined or "预留" in combined
        assert "AUD-7209" in combined

    def test_evaluation_still_passthrough(self):
        node = RetimeNode("retime1")
        image = TextureResult(texture_id=9, width=4, height=4)
        out = node._do_evaluate({"input": image}, None, None)
        assert out["_effect_input"] is image


# ──────────────────────────────────────────────────────────────────────────
# AUD-7309 — scene3d perf stats truthfulness
# ──────────────────────────────────────────────────────────────────────────

class TestAud7309PerfStats:
    def test_merge_vbo_cache_hit_truth_table(self):
        assert merge_vbo_cache_hit(None, True) is True
        assert merge_vbo_cache_hit(None, False) is False
        assert merge_vbo_cache_hit(True, False) is True
        assert merge_vbo_cache_hit(False, True) is True
        assert merge_vbo_cache_hit(True, True) is True
        assert merge_vbo_cache_hit(False, False) is False

    def test_model_cache_hit_from_status(self):
        assert model_cache_hit_from_status("in_memory") is True
        assert model_cache_hit_from_status("disk_hit") is True
        assert model_cache_hit_from_status("miss") is False
        assert model_cache_hit_from_status(None) is None
        assert model_cache_hit_from_status("") is None

    def test_recording_sites_use_helpers(self):
        src_ensure = inspect.getsource(Scene3DNode._ensure_flat_meshes_gl)
        assert "merge_vbo_cache_hit" in src_ensure
        # The old only-True assignment is gone.
        assert "if _pool.last_was_cached:\n                    self._last_vbo_cache_hit = True" \
            not in src_ensure
        src_eval = inspect.getsource(Scene3DNode._do_evaluate)
        assert "model_cache_hit_from_status" in src_eval
        # The dead read is gone (mentions inside comments don't count).
        assert ('model_cache_hit = getattr(self, "_perf_model_cache_hit"'
                not in src_eval)


# ──────────────────────────────────────────────────────────────────────────
# AUD-7310 — GL state restore + narrowed except
# ──────────────────────────────────────────────────────────────────────────

class TestAud7310GlState:
    def test_shadow_catcher_restores_blend_func(self):
        src = inspect.getsource(Scene3DNode._render_shadow_catchers)
        assert "prev_blend_func" in src
        # The finally block restores what was captured.
        assert "ctx.blend_func = prev_blend_func" in src
        assert src.rindex("ctx.blend_func = prev_blend_func") > \
            src.rindex("finally:")

    def test_sampler_except_narrowed(self):
        module_src = inspect.getsource(scene3d_mod)
        assert "except (KeyError, Exception)" not in module_src
        src = inspect.getsource(Scene3DNode._bind_null_material_samplers)
        assert "except KeyError" in src


# ──────────────────────────────────────────────────────────────────────────
# AUD-7312 — redraw_with_view parameter writes gated to virtual viewer
# ──────────────────────────────────────────────────────────────────────────

def _redraw_ready_node(node_id):
    node = Scene3DNode(node_id)
    node._last_scene_content_sig = "sig"
    node._gpu_texture = SimpleNamespace(glo=1)
    eval_ctx = SimpleNamespace(scene_connections={})
    captured: dict = {}

    def fake_do_evaluate(inputs, ctx, fb, **kw):
        captured["params"] = dict(node.parameters)
        return {}

    node._do_evaluate = fake_do_evaluate  # type: ignore[method-assign]
    return node, eval_ctx, captured


class TestAud7312RedrawWithView:
    CAM = {"cam_dist": 5.0, "cam_orbit_yaw": 12.0}

    def test_real_node_parameters_not_written(self):
        node, eval_ctx, _ = _redraw_ready_node("scene3d_real")
        before = dict(node.parameters)
        node.redraw_with_view(self.CAM, eval_ctx, None)
        assert node.parameters == before, (
            "redraw_with_view must not write nav values into a real "
            "Scene3D node's parameters (AUD-7312)")

    def test_virtual_viewer_still_receives_nav_values(self):
        node, eval_ctx, _ = _redraw_ready_node("__viewer3d_scene__")
        node.redraw_with_view(self.CAM, eval_ctx, None)
        assert node.parameters["cam_dist"] == 5.0
        assert node.parameters["cam_orbit_yaw"] == 12.0

    def test_invalid_float_returns_none_and_writes_nothing(self):
        node, eval_ctx, _ = _redraw_ready_node("__viewer3d_scene__")
        before = dict(node.parameters)
        assert node.redraw_with_view({"cam_dist": "abc"}, eval_ctx,
                                     None) is None
        assert node.parameters == before


# ──────────────────────────────────────────────────────────────────────────
# AUD-7401 — set_alpha HELP_TEXT parameter name
# ──────────────────────────────────────────────────────────────────────────

class TestAud7401SetAlphaHelpText:
    def test_help_text_uses_value(self):
        help_text = SetAlphaNode.HELP_TEXT
        assert "constant_value" not in help_text
        for param in SetAlphaNode.PARAM_DEFS:
            assert param.name in help_text


# ──────────────────────────────────────────────────────────────────────────
# AUD-7413 — caption override without parameters mutation
# ──────────────────────────────────────────────────────────────────────────

class TestAud7413CaptionOverride:
    def _caption_node(self):
        node = TextNode("text1")
        node.parameters["caption_input_enabled"] = True
        return node

    def _eval_ctx(self):
        return SimpleNamespace(caption_input={"text": "CAPTION"},
                               runtime_context=None, frame_number=0)

    def test_caption_seen_but_parameters_untouched(self):
        node = self._caption_node()
        seen: dict = {}

        def recorder(inputs, ctx, fb, resource_pool=None, render_mode="p"):
            seen["effective"] = node._effective_text()
            seen["param_text"] = node.parameters.get("text")
            return {}

        node._do_evaluate_with_caption = recorder  # type: ignore[method-assign]
        params_before = dict(node.parameters)
        node._do_evaluate({}, self._eval_ctx(), None)
        assert seen["effective"] == "CAPTION"
        assert seen["param_text"] == params_before.get("text")
        assert node.parameters == params_before
        assert node._text_override is None

    def test_missing_text_param_stays_absent(self):
        node = self._caption_node()
        del node.parameters["text"]
        seen: dict = {}

        def recorder(inputs, ctx, fb, resource_pool=None, render_mode="p"):
            seen["param_has_text"] = "text" in node.parameters
            seen["effective"] = node._effective_text()
            return {}

        node._do_evaluate_with_caption = recorder  # type: ignore[method-assign]
        node._do_evaluate({}, self._eval_ctx(), None)
        assert seen["effective"] == "CAPTION"
        assert seen["param_has_text"] is False
        assert "text" not in node.parameters

    def test_no_caption_means_no_override(self):
        node = self._caption_node()
        node.parameters["caption_input_enabled"] = False
        seen: dict = {}

        def recorder(inputs, ctx, fb, resource_pool=None, render_mode="p"):
            seen["effective"] = node._effective_text()
            return {}

        node._do_evaluate_with_caption = recorder  # type: ignore[method-assign]
        node._do_evaluate({}, self._eval_ctx(), None)
        assert seen["effective"] == "Text"

    def test_content_hash_reflects_override(self):
        node = self._caption_node()
        base = node._content_hash()
        try:
            node._text_override = "DIFFERENT"
            overridden = node._content_hash()
        finally:
            node._text_override = None
        assert overridden != base


# ──────────────────────────────────────────────────────────────────────────
# AUD-7509 — unknown placeholder logs the promised warning
# ──────────────────────────────────────────────────────────────────────────

class TestAud7509UnknownPlaceholderLog:
    def test_warning_logged_once_per_instance(self, caplog):
        node = UnknownPlaceholderNode("ph1", original_type="my_custom_v99")
        with caplog.at_level(
                logging.WARNING,
                logger="src.features.composite.nodes."
                       "unknown_placeholder_node"):
            out = node._do_evaluate({}, None, None)
        assert out == {}
        warnings = [r for r in caplog.records
                    if "my_custom_v99" in r.getMessage()]
        assert warnings, "the docstring-promised warning was not logged"
        # Second evaluation: no duplicate warning (once per instance).
        node._do_evaluate({}, None, None)
        warnings_after = [r for r in caplog.records
                          if "my_custom_v99" in r.getMessage()]
        assert len(warnings_after) == len(warnings)


# ──────────────────────────────────────────────────────────────────────────
# AUD-7606 — vt gate counters under _cache_lock
# ──────────────────────────────────────────────────────────────────────────

class TestAud7606VtGateLock:
    def _fresh_node(self):
        return VideoFrameNode()

    def test_gate_marks_ok_after_two_misses(self):
        node = self._fresh_node()
        ok = node._record_vt_cpu_gate(10, attached=True)
        assert ok is False
        assert 10 not in node._vt_cpu_ok
        ok = node._record_vt_cpu_gate(10, attached=True)
        assert ok is True
        assert 10 in node._vt_cpu_ok

    def test_gate_releases_unattached_immediately(self):
        node = self._fresh_node()
        assert node._record_vt_cpu_gate(11, attached=False) is True
        assert 11 in node._vt_cpu_ok

    def test_bookkeeping_runs_under_cache_lock(self, monkeypatch):
        node = self._fresh_node()
        gate = _GateLock()
        monkeypatch.setattr(node, "_cache_lock", gate)

        first_done = threading.Event()

        def worker1() -> None:
            node._record_vt_cpu_gate(1, attached=False)
            first_done.set()

        t1 = threading.Thread(target=worker1, daemon=True)
        t1.start()
        assert gate.entered.wait(timeout=2.0), (
            "_record_vt_cpu_gate must acquire _cache_lock (AUD-7606)")

        second_done = threading.Event()

        def worker2() -> None:
            node._record_vt_cpu_gate(2, attached=False)
            second_done.set()

        t2 = threading.Thread(target=worker2, daemon=True)
        t2.start()
        # Second writer must block on the held lock (serialized access).
        assert not second_done.wait(timeout=0.3), (
            "vt gate bookkeeping is not serialized by _cache_lock")
        gate.release_gate.set()
        assert second_done.wait(timeout=2.0)
        assert first_done.wait(timeout=2.0)
        assert 1 in node._vt_cpu_ok and 2 in node._vt_cpu_ok
        monkeypatch.undo()

    def test_read_sites_snapshot_under_lock(self):
        # upload_frame / _decode_and_store take the gate snapshot inside
        # the locked block instead of an unlocked follow-up read.
        src_upload = inspect.getsource(VideoFrameNode.upload_frame)
        assert "vt_cpu_ok = frame_number in self._vt_cpu_ok" in src_upload
        assert ("vt_gate and frame_number not in self._vt_cpu_ok"
                not in src_upload)
        src_bg = inspect.getsource(VideoFrameNode._decode_and_store)
        assert "vt_cpu_ok = frame_num in self._vt_cpu_ok" in src_bg


# ──────────────────────────────────────────────────────────────────────────
# AUD-7704 — volume gather memory budget
# ──────────────────────────────────────────────────────────────────────────

class _FakeGL:
    def __init__(self) -> None:
        self.created: list = []

    def texture(self, size, components, data=None, dtype=None):
        tex = _FakePoolTex(size[0], size[1])
        self.created.append((size, components, dtype))
        return tex


class TestAud7704GatherBudget:
    def test_small_params_unchanged(self):
        assert budget_output_size(32, 360, 480) == (360, 480)

    def test_extreme_params_downsampled_within_budget(self):
        h, w = budget_output_size(128, 8192, 8192)
        assert (h, w) != (8192, 8192)
        assert h >= 1 and w >= 1
        used = 128 * h * w * 4
        assert used <= _MAX_SAMPLE_GATHER_BYTES

    def test_tiny_budget_clamps_to_minimum(self):
        h, w = budget_output_size(8, 100, 100, max_bytes=1000)
        assert h >= 1 and w >= 1
        assert 8 * h * w * 4 <= 1000
        assert (h, w) != (100, 100)

    def test_budget_respects_custom_limit(self):
        h, w = budget_output_size(64, 512, 512, max_bytes=131072)
        assert 64 * h * w * 4 <= 131072

    def test_evaluate_downsamples_instead_of_memory_error(
            self, monkeypatch, caplog):
        grid = VolumeGrid(
            np.random.default_rng(0).random((8, 16, 12)).astype(np.float32)
            * 0.1)
        monkeypatch.setattr(volume_grid_mod, "load_volume",
                            lambda path: grid)
        tiny_budget = 200_000
        monkeypatch.setattr(volume_mod, "_MAX_SAMPLE_GATHER_BYTES",
                            tiny_budget)

        node = Volume3DNode("vol1")
        node.parameters["file_path"] = "/tmp/fake.npz"
        node.parameters["output_width"] = 512
        node.parameters["output_height"] = 512
        node.parameters["step_count"] = 128
        fake_gl = _FakeGL()
        ctx = SimpleNamespace(frame_number=0, gl=fake_gl)

        with caplog.at_level(logging.WARNING,
                             logger="src.features.composite.nodes.volume3d_node"):
            out = node._do_evaluate({"result": None}, ctx, None,
                                    render_mode="full")
        assert out and out["result"].is_valid
        r = out["result"]
        # Effective gather stays within the (patched) budget.
        assert 128 * r.width * r.height * 4 <= tiny_budget
        assert r.width < 512 and r.height < 512
        assert any("budget" in rec.getMessage() for rec in caplog.records)
        assert "downsampled" in (node.error_message or "")

    def test_evaluate_untouched_when_within_budget(
            self, monkeypatch):
        grid = VolumeGrid(
            np.random.default_rng(0).random((8, 16, 12)).astype(np.float32)
            * 0.1)
        monkeypatch.setattr(volume_grid_mod, "load_volume",
                            lambda path: grid)
        node = Volume3DNode("vol2")
        node.parameters["file_path"] = "/tmp/fake.npz"
        node.parameters["output_width"] = 12
        node.parameters["output_height"] = 16
        node.parameters["step_count"] = 8
        ctx = SimpleNamespace(frame_number=0, gl=_FakeGL())
        out = node._do_evaluate({"result": None}, ctx, None,
                                render_mode="full")
        assert out and out["result"].width == 12
        assert out["result"].height == 16
        assert "downsampled" not in (node.error_message or "")
