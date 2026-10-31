"""Regression tests for remediation batch wd5-g33 (plan batch D, Major):

- AUD-6002: 3D LUT texture cache is unbounded / never released.
- AUD-6009: histogram cache key trusts bare glo with revision 0; empty
  results get pinned into the cache.
- AUD-6100: keyer module has no ``logger`` — unknown matte cleanup op
  raised NameError inside the fallback branch itself.
- AUD-6101: keylight fallback dropped the user's screen colour.
- AUD-6106: deep_merge shader-missing fallback declared input_b's canvas
  over input_a's texture.
- AUD-6207: ml_roto apply_keyframes animated only pos channels, freezing
  the bezier handles at the last keyframe.
- AUD-6212: AI cache callback built a fresh AICacheManager per write.
- AUD-6213: _save_composite_handler was a fake-save data-loss trap.
- AUD-6306: provider output replaced the frame outside the mask.
- AUD-6307/6308: generation + cost confirm ran synchronously on the
  render thread with no timeout.
- AUD-6309: every evaluation paid a full-frame readback + pixel SHA-256,
  cache hits included.
- AUD-6310: output texture was created outside the resource pool.
- AUD-64001: _init_port_revisions ran before subclasses assigned outputs.
- AUD-64002: from_dict loaded parameters without any type validation.

Headless: no GL context is created. Evaluator collaborators are faked;
GL-dependent paths are monkeypatched at their seam.
"""

import json
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core import shaders as shader_sources
from src.features.composite.evaluators.filter_evaluator import (
    FilterEvaluatorMixin,
    _LUT_CACHE_MAX_ENTRIES,
)
from src.features.composite.evaluators.histogram_manager import (
    HistogramManager,
    HistogramResult,
)
from src.features.composite.evaluators.keyer_evaluator import KeyerEvaluatorMixin
from src.features.composite.evaluators.merge_evaluator import MergeEvaluatorMixin
from src.features.composite.ml_roto import apply_keyframes, mask_to_keyframe
from src.features.composite.nodes.ai_gen_expand_node import (
    AIGenExpandNode,
    reset_generation_transport,
    set_generation_transport,
)
from src.features.composite.nodes.base_node import (
    Node,
    ParamDef,
    Port,
    TextureResult,
)
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.chroma_keyer_node import ChromaKeyerNode
from src.features.composite.nodes.roto_shape_node import RotoShapeNode
from src.features.composite.nodes.shuffle_node import ShuffleNode
from src.features.composite.render.disk_cache_store import (
    DiskCacheStore,
    reset_disk_cache_store,
)
from src.features.composite.core.format import BBox

pytestmark = pytest.mark.composite_core

_W, _H = 32, 24


# ──────────────────────────── shared fakes ────────────────────────────


class _Uniform:
    def __init__(self, name: str) -> None:
        self.name = name
        self.value = None


class _FakeProgram:
    """Program accepting exactly the uniforms a GLSL source declares."""

    def __init__(self, fragment_source: str) -> None:
        import re
        self.uniforms = {}
        for match in re.finditer(
                r"uniform\s+\w+\s+(\w+)\s*(?:\[[^\]]*\])?\s*;",
                fragment_source):
            self.uniforms[match.group(1)] = _Uniform(match.group(1))

    def __getitem__(self, name: str) -> _Uniform:
        uniform = self.uniforms.get(name)
        if uniform is None:
            raise KeyError(name)
        return uniform


class _FakeTexture:
    _next_glo = 100

    def __init__(self) -> None:
        _FakeTexture._next_glo += 1
        self.glo = _FakeTexture._next_glo
        self.written: list = []
        self.write_kwargs: dict = {}

    def write(self, data) -> None:
        self.written.append(data)


class _FakeFBO:
    def use(self) -> None:
        pass


class _FakeFramebufferManager:
    def get_fbo(self, texture) -> _FakeFBO:
        return _FakeFBO()


class _FakeResourcePool:
    def __init__(self) -> None:
        self.acquired: list = []

    def acquire_texture(self, w: int, h: int, **kwargs):
        tex = _FakeTexture()
        tex.write_kwargs = kwargs
        self.acquired.append(tex)
        return tex


class _FakeContext:
    def __init__(self) -> None:
        self.viewport = (0, 0, 0, 0)

    def clear(self, *args, **kwargs) -> None:
        pass


class _EvaluatorHarness(KeyerEvaluatorMixin, MergeEvaluatorMixin,
                        FilterEvaluatorMixin):
    """Evaluator stand-in with stubbed BaseEvaluatorMixin collaborators."""

    def __init__(self, programs: dict) -> None:
        self.utility_shaders = programs
        self._resource_pool = _FakeResourcePool()
        self.fb_manager = _FakeFramebufferManager()
        self.ctx = _FakeContext()
        self._white_mask = _FakeTexture()

    def _render_quad(self, prog) -> None:
        pass

    def _bind_external_texture(self, texture_id: int) -> None:
        pass

    def _bind_mask_texture(self, texture_id: int) -> None:
        pass

    def _get_white_mask_texture(self):
        return self._white_mask

    def _check_color_space_mismatch(self, a, b, tag: str) -> None:
        pass

    def _resolve_merge_dims(self, a, b, policy: str) -> tuple:
        return (max(a.width or 1, b.width or 1), max(a.height or 1, b.height or 1))

    def _resolve_merge_format(self, a, b, policy: str):
        return a.format

    def _resolve_merge_bbox(self, a, b, policy: str):
        return a.bbox


def _make_input(texture_id: int = 7, **kwargs) -> TextureResult:
    defaults = dict(width=4, height=4, alpha_premultiplied=True,
                    color_space="linear")
    defaults.update(kwargs)
    return TextureResult(texture_id=texture_id, **defaults)


def _rectangle_mask(h=120, w=160, x0=30, x1=110, y0=25, y1=90):
    mask = np.zeros((h, w), dtype=np.float32)
    mask[y0:y1, x0:x1] = 1.0
    return mask


# ──────────────────────────── AUD-6100 ────────────────────────────


class TestMatteCleanupUnknownOperation:
    """The unknown-operation fallback must not NameError (AUD-6100)."""

    def test_unknown_operation_passes_through_without_raising(self):
        harness = _EvaluatorHarness({})
        input_tex = _make_input()
        out = harness._apply_matte_cleanup(
            input_tex, {"operation": "nuclear_blast"}, 4, 4)
        assert out == {"image": input_tex}

    def test_known_operation_still_dispatches(self):
        harness = _EvaluatorHarness({})
        out = harness._apply_matte_cleanup(
            _make_input(), {"operation": "erode", "size": 3}, 4, 4)
        assert "image" in out


# ──────────────────────────── AUD-6101 ────────────────────────────


class TestKeylightFallbackScreenColor:
    """The degraded keylight path must keep the user's screen colour."""

    def _run_fallback(self, screen_color):
        program = _FakeProgram(shader_sources._CHROMA_KEYER_FRAGMENT)
        harness = _EvaluatorHarness({"chroma_keyer": program})
        harness._apply_keylight(_make_input(), {
            "screen_color": screen_color,
            "clip_black": 0.2,
            "softness": 0.1,
            "despill_strength": 0.5,
            "output_mode": "result",
        }, 4, 4)
        return program

    def test_user_screen_color_reaches_the_chroma_shader(self):
        program = self._run_fallback((0.1, 0.2, 0.3))
        assert program.uniforms["u_screen_color"].value == (0.1, 0.2, 0.3)

    def test_malformed_screen_color_falls_back_to_default_green(self):
        program = self._run_fallback("green")
        assert program.uniforms["u_screen_color"].value == (0.0, 1.0, 0.0)

    def test_default_screen_color_is_green(self):
        program = self._run_fallback(None)
        # params.get("screen_color", default) — None is a present key, so
        # None[0] raises and the guard falls back to default green.
        assert program.uniforms["u_screen_color"].value == (0.0, 1.0, 0.0)


# ──────────────────────────── AUD-6106 ────────────────────────────


class TestDeepMergeMissingShaderFallback:
    """Shader-missing passthrough must carry input_a's own metadata."""

    def test_fallback_returns_input_a_verbatim(self):
        harness = _EvaluatorHarness({})  # no 'deep_merge' program
        a = _make_input(7, width=4, height=4)
        b = _make_input(8, width=16, height=16)
        out = harness._apply_deep_merge({
            "_effect_input_a": a,
            "_effect_input_b": b,
            "_effect_params": {"format_policy": "from_b"},
        })
        # The declared data window must match the GL texture returned.
        assert out["result"] is a
        assert out["result"].width == 4
        assert out["result"].height == 4


# ──────────────────────────── AUD-6002 ────────────────────────────


class _FakeLutTexture:
    def __init__(self) -> None:
        self.glo = id(self)
        self.size = (2, 2, 2)
        self.components = 3
        self.released = False

    def release(self) -> None:
        self.released = True


class TestLutCacheBounded:
    """The 3D LUT cache must evict (releasing GL objects) and be
    fully releasable (AUD-6002)."""

    def _harness(self, monkeypatch):
        from src.shared.color import lut_loader
        monkeypatch.setattr(
            lut_loader, "load_lut",
            lambda path: SimpleNamespace(
                table=np.zeros((2, 2, 2, 3), dtype="f4"), size=2))
        harness = _EvaluatorHarness({})
        harness.ctx = SimpleNamespace(
            texture3d=lambda size, components, data=None, dtype=None:
                _FakeLutTexture())
        return harness

    def test_cache_is_bounded_and_evictions_release(self, monkeypatch):
        harness = self._harness(monkeypatch)
        textures = [harness._get_or_upload_lut(f"lut{i}.cube")
                    for i in range(_LUT_CACHE_MAX_ENTRIES + 2)]
        assert len(harness._lut_cache) == _LUT_CACHE_MAX_ENTRIES
        # The two least-recently-used LUTs were evicted and released.
        assert textures[0].released is True
        assert textures[1].released is True
        # The most recent entries are still alive.
        assert textures[-1].released is False

    def test_hit_refreshes_lru_order(self, monkeypatch):
        harness = self._harness(monkeypatch)
        first = harness._get_or_upload_lut("first.cube")
        second = harness._get_or_upload_lut("second.cube")
        for i in range(_LUT_CACHE_MAX_ENTRIES - 2):
            harness._get_or_upload_lut(f"filler{i}.cube")
        assert len(harness._lut_cache) == _LUT_CACHE_MAX_ENTRIES
        # Touch "first" so "second" becomes the least recently used.
        assert harness._get_or_upload_lut("first.cube") is first
        harness._get_or_upload_lut("one-more.cube")
        assert second.released is True
        assert first.released is False

    def test_release_lut_cache_releases_everything(self, monkeypatch):
        harness = self._harness(monkeypatch)
        textures = [harness._get_or_upload_lut(f"lut{i}.cube")
                    for i in range(3)]
        harness.release_lut_cache()
        assert all(tex.released for tex in textures)
        assert len(harness._lut_cache) == 0


# ──────────────────────────── AUD-6009 ────────────────────────────


class _FakeGLTex:
    def __init__(self, glo: int) -> None:
        self.glo = glo  # no ``revision`` attribute, like a raw GL texture


def _histogram_manager() -> HistogramManager:
    ctx = MagicMock()
    ctx.info = {"GL_VERSION": "4.3.0 Test"}
    return HistogramManager(ctx)


def _hist_result(total_pixels: int = 10) -> HistogramResult:
    hist = np.zeros(1024, dtype=np.uint32)
    hist[:total_pixels] = 1
    cdf = np.zeros((4, 256), dtype=np.float32)
    cdf[0] = np.linspace(0.0, 1.0, 256)
    return HistogramResult(histogram=hist, cdf=cdf,
                           total_pixels=total_pixels, channel_mask=1,
                           texture_revision=0)


class TestHistogramCacheKey:
    """The cache key must track content, not the reused GL object."""

    def _spy_fingerprint(self, mgr, sequence=None):
        """Patch the fingerprint probe; ``sequence`` pops per call, then
        repeats its last element (default constant "fp-a")."""
        calls = []
        queue = list(sequence) if sequence else []

        def _fingerprint(tex, w, h, fb, pool):
            calls.append(tex.glo)
            if queue:
                value = queue.pop(0)
                if not queue:
                    queue.append(value)  # repeat-last semantics
                return value
            return "fp-a"

        mgr._texture_content_fingerprint = _fingerprint
        return calls

    def test_same_glo_different_content_misses(self, monkeypatch):
        mgr = _histogram_manager()
        self._spy_fingerprint(mgr, sequence=["fp-a", "fp-b"])
        results = []
        monkeypatch.setattr(
            mgr, "compute_histogram",
            lambda *a, **k: results.append(1) or _hist_result())
        tex = _FakeGLTex(5)
        mgr.compute_percentiles(tex, 64, 48, 1.0, 99.0, use_cache=True)
        mgr.compute_percentiles(tex, 64, 48, 1.0, 99.0, use_cache=True)
        # Two different contents behind the same glo must recompute —
        # the old (glo, 0) key would have served the first image's
        # percentiles to the second.
        assert len(results) == 2

    def test_same_content_hits_cache(self, monkeypatch):
        mgr = _histogram_manager()
        self._spy_fingerprint(mgr, ["fp-a"])
        results = []
        monkeypatch.setattr(
            mgr, "compute_histogram",
            lambda *a, **k: results.append(1) or _hist_result())
        tex = _FakeGLTex(5)
        mgr.compute_percentiles(tex, 64, 48, 1.0, 99.0, use_cache=True)
        out = mgr.compute_percentiles(tex, 64, 48, 1.0, 99.0, use_cache=True)
        assert len(results) == 1  # second call was a cache hit
        assert 0.0 < out["low"] < out["high"] < 1.0

    def test_authoritative_revision_skips_fingerprint(self, monkeypatch):
        mgr = _histogram_manager()
        calls = self._spy_fingerprint(mgr)
        monkeypatch.setattr(mgr, "compute_histogram",
                            lambda *a, **k: _hist_result())
        tex = _FakeGLTex(5)
        mgr.compute_percentiles(tex, 64, 48, 1.0, 99.0, use_cache=True,
                                texture_revision=7)
        assert calls == []  # revision is authoritative, no probe needed

    def test_empty_result_is_not_pinned_into_cache(self, monkeypatch):
        mgr = _histogram_manager()
        self._spy_fingerprint(mgr)
        results = []
        monkeypatch.setattr(
            mgr, "compute_histogram",
            lambda *a, **k: results.append(1) or _hist_result(total_pixels=0))
        tex = _FakeGLTex(5)
        mgr.compute_percentiles(tex, 64, 48, 1.0, 99.0, use_cache=True)
        mgr.compute_percentiles(tex, 64, 48, 1.0, 99.0, use_cache=True)
        assert len(results) == 2  # degenerate result recomputed, not cached

    def test_cache_key_dims_participate(self):
        mgr = _histogram_manager()
        key_a = mgr._cache.compute_key(5, "rev1", 1, "1.00:99.00:64x48")
        key_b = mgr._cache.compute_key(5, "rev1", 1, "1.00:99.00:128x96")
        assert key_a != key_b


# ──────────────────────────── AUD-6207 ────────────────────────────


class TestMlRotoHandleTracks:
    """apply_keyframes must animate the c1/c2 bezier handles too."""

    def test_handle_channels_get_tracks(self, qapp):
        node = RotoShapeNode(node_id="r")
        keyframe = mask_to_keyframe(_rectangle_mask(), frame=5)
        assert keyframe is not None
        apply_keyframes(node, [keyframe])
        for channel in ("c1_x", "c1_y", "c2_x", "c2_y"):
            track = node.keyframe_tracks.get(f"roto_point_0_{channel}")
            assert track is not None, channel
            assert any(int(kf["frame"]) == 5
                       for kf in track["keyframes"])

    def test_handles_interpolate_between_keyframes(self, qapp):
        node = RotoShapeNode(node_id="r")
        k0 = mask_to_keyframe(_rectangle_mask(x0=30), frame=0)
        k10 = mask_to_keyframe(_rectangle_mask(x0=60), frame=10)
        assert k0 is not None and k10 is not None
        apply_keyframes(node, [k0, k10])
        track = node.keyframe_tracks["roto_point_0_c1_x"]
        values = {int(kf["frame"]): float(kf["value"])
                  for kf in track["keyframes"]}
        # Handles must move with the shape instead of freezing at the
        # last keyframe's position.
        assert values[0] != values[10]

    def test_pos_tracks_still_written(self, qapp):
        node = RotoShapeNode(node_id="r")
        keyframe = mask_to_keyframe(_rectangle_mask(), frame=5)
        apply_keyframes(node, [keyframe])
        assert node.keyframe_tracks.get("roto_point_0_x") is not None
        assert node.keyframe_tracks.get("roto_point_0_y") is not None


# ──────────────────────────── AUD-6212 / AUD-6213 ────────────────────────────


class TestCompositeModule:
    def test_ai_cache_callback_reuses_one_manager(self, monkeypatch):
        from src.features.composite import module as composite_module
        import src.features.composite.render.ai_cache_manager as acm_mod
        import src.shared.ai_models.inference_engine as ie_mod

        instances = []

        class _FakeManager:
            def __init__(self) -> None:
                instances.append(self)
                self.puts: list = []

            def put(self, cache_key, result) -> None:
                self.puts.append((cache_key, result))

        class _FakeEngine:
            def __init__(self) -> None:
                self.fn = None

            def register_cache_fn(self, fn) -> None:
                self.fn = fn

        engine = _FakeEngine()
        monkeypatch.setattr(acm_mod, "AICacheManager", _FakeManager)
        monkeypatch.setattr(ie_mod, "get_inference_engine",
                            lambda: engine)

        composite_module.CompositeModule._register_ai_cache_callback()

        assert engine.fn is not None
        engine.fn("k1", object())
        engine.fn("k2", object())
        # One long-lived manager (AUD-6212), not one per write.
        assert len(instances) == 1
        assert len(instances[0].puts) == 2

    def test_fake_save_handler_removed(self):
        from src.features.composite.module import CompositeModule
        # AUD-6213: the handler computed graph_data, threw it away and
        # reported success — a data-loss trap. It must not exist.
        assert not hasattr(CompositeModule, "_save_composite_handler")


# ──────────────────────────── AUD-6306..6310 ────────────────────────────


class _Ctx:
    frame_number = 0
    gl = None


def _tex(tex_id=5, rev=1):
    return TextureResult(texture_id=tex_id, width=_W, height=_H,
                         revision=rev, bbox=BBox.full_frame(_W, _H),
                         alpha_premultiplied=False, color_space="linear")


def _gen_node(**overrides):
    node = AIGenExpandNode(node_id="g1")
    node.set_parameter("prompt", "a serene lake")
    for key, value in overrides.items():
        node.set_parameter(key, value)
    return node


@pytest.fixture()
def _store(tmp_path, monkeypatch):
    store = DiskCacheStore(root_dir=str(tmp_path / "cache"), capacity_gb=1.0)
    monkeypatch.setattr(
        "src.features.composite.render.disk_cache_store._STORE_INSTANCE",
        store)
    yield
    reset_generation_transport()
    reset_disk_cache_store()


def _input_pixels(value: float = 0.1) -> np.ndarray:
    return np.full((_H, _W, 4), value, dtype=np.float32)


def _mask_pixels() -> np.ndarray:
    mask = np.zeros((_H, _W, 4), dtype=np.float32)
    mask[:_H // 2, :, 0] = 1.0  # top half = generate
    return mask


def _rgba(value: float = 0.9) -> np.ndarray:
    return np.full((_H, _W, 4), value, dtype=np.float32)


class TestMaskedGenerationComposite:
    """AUD-6306: outside the mask the input pixels must win."""

    def test_composite_masked_blends_by_mask(self):
        inside = _rgba(0.9)
        result = AIGenExpandNode._composite_masked(
            _input_pixels(0.1), inside, _mask_pixels()[..., 0])
        assert result is not None
        assert np.allclose(result[:_H // 2], 0.9)   # inside mask: generated
        assert np.allclose(result[_H // 2:], 0.1)   # outside: input wins

    def test_composite_masked_none_without_input(self):
        assert AIGenExpandNode._composite_masked(
            None, _rgba(), _mask_pixels()[..., 0]) is None

    def test_composite_masked_passthrough_without_mask(self):
        rgba = _rgba()
        assert AIGenExpandNode._composite_masked(
            _input_pixels(), rgba, None) is rgba

    def test_masked_evaluation_preserves_outside_mask(
            self, monkeypatch, _store):
        requests = []

        def transport(request, key, size):
            requests.append(request)
            return _rgba(0.9)

        set_generation_transport(transport, lambda: "sk-test")

        read_calls = []

        def fake_read_rgba(tex_result, ctx, fb_manager, resource_pool=None):
            read_calls.append(tex_result.texture_id)
            if tex_result.texture_id == 1:
                return _input_pixels(0.1)
            return _mask_pixels()

        import src.features.composite.nodes._gpu_readback as gpu_readback
        monkeypatch.setattr(gpu_readback, "read_rgba", fake_read_rgba)

        node = _gen_node()
        pool = _FakeResourcePool()
        out = node._do_evaluate(
            {"image": _tex(1), "mask": _tex(2)}, _Ctx(), None, pool)

        assert node.last_state == "DONE"
        assert len(pool.acquired) == 1  # AUD-6310: pooled upload
        assert pool.acquired[0].write_kwargs == \
            {"components": 4, "dtype": "f4"}
        written = np.frombuffer(
            b"".join(pool.acquired[0].written), dtype=np.float32
        ).reshape(_H, _W, 4)
        # GL rows are bottom-up (upload flips); bottom half in GL space is
        # the top half in image space = generated region.
        assert np.allclose(written[:_H // 2], 0.1)  # outside mask: input
        assert np.allclose(written[_H // 2:], 0.9)  # inside mask: generated
        # The request carried the real mask (red channel of the readback).
        assert requests and np.allclose(requests[0]["mask"],
                                        _mask_pixels()[..., 0])

    def test_cache_hit_does_no_readback(self, monkeypatch, _store):
        """AUD-6309: repeat evaluations must not pay the readback again."""

        def transport(request, key, size):  # first call only
            return _rgba(0.9)

        set_generation_transport(transport, lambda: "sk-test")

        def fake_read_rgba(tex_result, ctx, fb_manager, resource_pool=None):
            if tex_result.texture_id == 1:
                return _input_pixels(0.1)
            return _mask_pixels()

        import src.features.composite.nodes._gpu_readback as gpu_readback
        monkeypatch.setattr(gpu_readback, "read_rgba", fake_read_rgba)

        node = _gen_node()
        pool = _FakeResourcePool()
        node._do_evaluate({"image": _tex(1), "mask": _tex(2)},
                          _Ctx(), None, pool)

        def forbidden_read(*args, **kwargs):
            raise AssertionError("readback on cache-hit path (AUD-6309)")

        monkeypatch.setattr(gpu_readback, "read_rgba", forbidden_read)
        pool2 = _FakeResourcePool()
        out = node._do_evaluate({"image": _tex(1), "mask": _tex(2)},
                                _Ctx(), None, pool2)
        assert node.last_state == "DONE"
        assert node.state_detail == "composited cache hit"
        assert len(pool2.acquired) == 1


class TestBoundedOffThreadGeneration:
    """AUD-6307/6308: transport + cost confirm run off the render thread,
    and a hung transport times out visibly."""

    def test_transport_runs_off_render_thread(self, _store):
        seen_threads = []

        def transport(request, key, size):
            seen_threads.append(threading.current_thread())
            return _rgba()

        set_generation_transport(transport, lambda: "sk-test")
        node = _gen_node()
        node._do_evaluate({"image": _tex(1)}, _Ctx(), None, _FakeResourcePool())
        assert seen_threads
        assert all(t is not threading.main_thread() for t in seen_threads)

    def test_cost_confirm_runs_off_render_thread(self, _store):
        seen_threads = []

        def cost_confirm(count, prompt):
            seen_threads.append(threading.current_thread())
            return True

        set_generation_transport(
            lambda request, key, size: _rgba(), lambda: "sk-test",
            cost_confirm=cost_confirm)
        node = _gen_node()
        node._do_evaluate({"image": _tex(1)}, _Ctx(), None, _FakeResourcePool())
        assert seen_threads
        assert all(t is not threading.main_thread() for t in seen_threads)

    def test_hung_transport_times_out_visibly(self, monkeypatch, _store):
        import src.features.composite.nodes.ai_gen_expand_node as node_mod
        monkeypatch.setattr(node_mod, "_GENERATION_TIMEOUT_S", 0.05)

        release = threading.Event()

        def transport(request, key, size):
            release.wait(timeout=5)
            return _rgba()

        try:
            set_generation_transport(transport, lambda: "sk-test")
            node = _gen_node()
            out = node._do_evaluate({"image": _tex(1)}, _Ctx(), None,
                                    _FakeResourcePool())
            # Bounded stall + visible degradation + input keeps flowing.
            assert node.last_state == "FAILED"
            assert "timed out" in node.state_detail
            assert out["image"].texture_id == 1
        finally:
            release.set()

    def test_transport_exception_surfaces_as_failed(self, _store):
        def transport(request, key, size):
            raise RuntimeError("boom")

        set_generation_transport(transport, lambda: "sk-test")
        node = _gen_node()
        out = node._do_evaluate({"image": _tex(1)}, _Ctx(), None,
                                _FakeResourcePool())
        assert node.last_state == "FAILED"
        assert out["image"].texture_id == 1


class TestFullFrameGenerationKeying:
    """AUD-6309: the cache key must not hash full pixel content."""

    def test_full_frame_hit_skips_network_and_readback(self, monkeypatch,
                                                       _store):
        calls = []

        def transport(request, key, size):
            calls.append(request)
            return _rgba(0.5)

        set_generation_transport(transport, lambda: "sk-test")
        node = _gen_node()
        node._do_evaluate({"image": _tex(1)}, _Ctx(), None,
                          _FakeResourcePool())
        assert len(calls) == 1

        # Second evaluation of the same upstream: identity-keyed cache
        # hit — no network call, no readback (GL path would readback here
        # under the old scheme; without GL the identity key is what keeps
        # the entry valid).
        node2_out = node._do_evaluate({"image": _tex(1)}, _Ctx(), None,
                                      _FakeResourcePool())
        assert len(calls) == 1
        assert node.last_state == "DONE"

    def test_key_changes_with_upstream_identity(self, _store):
        node = _gen_node()
        job_a = node._build_job(_tex(1), None, None, "r0")
        job_b = node._build_job(_tex(2), None, None, "r0")
        assert job_a.key != job_b.key

    def test_mask_identity_participates_in_key(self, _store):
        node = _gen_node()
        job_a = node._build_job(_tex(1), None, None, "r0",
                                mask_key_material=node._mask_key_material(
                                    _tex(9)))
        job_b = node._build_job(_tex(1), None, None, "r0",
                                mask_key_material=node._mask_key_material(
                                    _tex(10)))
        assert job_a.key != job_b.key


# ──────────────────────────── AUD-64001 ────────────────────────────


class _LateOutputsNode(Node):
    """Assigns outputs after super().__init__() — the AUD-64001 pattern."""

    node_type = "late_outputs"

    def __init__(self) -> None:
        super().__init__("late-1")
        self.outputs = [Port("result", "output", "image")]

    def _do_evaluate(self, inputs, ctx, fb_manager, resource_pool=None,
                     render_mode="preview"):
        return {}


class TestPortRevisionRegistration:
    """Output ports must be revision-tracked regardless of the order in
    which subclasses declare them (AUD-64001).

    Ports are declared at revision 0 ("no content change signalled yet")
    so the scheduler's port-revision layer stays inert for unevaluated
    graphs — the first bump produces the 0→1 content-change transition.
    """

    def test_subclass_assignment_registers_revisions(self):
        node = _LateOutputsNode()
        assert node._port_revisions == {"result": 0}

    def test_real_node_without_manual_init_registers(self):
        # BlurNode assigns outputs after super() without a manual
        # _init_port_revisions call — previously tracked nothing.
        node = BlurNode(node_id="b1")
        assert "result" in node._port_revisions

    def test_bump_output_revision_bumps_registered_port(self):
        node = _LateOutputsNode()
        node.bump_output_revision()
        assert node._port_revisions["result"] == 1

    def test_bump_auto_registers_unknown_port(self):
        node = _LateOutputsNode()
        node.bump_output_revision("extra")
        assert node._port_revisions["extra"] == 1

    def test_reassignment_keeps_live_counters(self):
        node = _LateOutputsNode()
        node.bump_output_revision()
        node.outputs = [Port("result", "output", "image")]
        assert node._port_revisions["result"] == 1  # not reset to 0

    def test_manual_reinit_still_supported(self):
        node = _LateOutputsNode()
        node.bump_output_revision()
        node._init_port_revisions()
        assert node._port_revisions["result"] == 1


# ──────────────────────────── AUD-64002 ────────────────────────────


class TestFromDictParameterSanitization:
    """Persisted parameters are external data and must be type-checked at
    load time (AUD-64002)."""

    def test_float_from_numeric_string_is_coerced(self):
        data = BlurNode(node_id="b").to_dict()
        data["parameters"]["radius"] = "12.5"
        clone = BlurNode.from_dict(data)
        assert clone.parameters["radius"] == 12.5

    def test_float_from_garbage_falls_back_to_default(self):
        data = BlurNode(node_id="b").to_dict()
        data["parameters"]["radius"] = {"evil": True}
        clone = BlurNode.from_dict(data)
        assert clone.parameters["radius"] == 8.0  # ParamDef default

    def test_bool_from_string_is_coerced(self):
        data = ChromaKeyerNode(node_id="c").to_dict()
        data["parameters"]["despill"] = "false"
        clone = ChromaKeyerNode.from_dict(data)
        assert clone.parameters["despill"] is False

    def test_option_non_string_falls_back(self):
        data = ShuffleNode(node_id="s").to_dict()
        data["parameters"]["a_from"] = 5
        clone = ShuffleNode.from_dict(data)
        assert clone.parameters["a_from"] == "a"  # ParamDef default

    def test_option_legacy_string_is_kept(self):
        """Read-time compat mappings own legacy labels (blur maps
        "directional" → "motion"); load-time must not destroy them."""
        data = BlurNode(node_id="b").to_dict()
        data["parameters"]["blur_type"] = "directional"
        clone = BlurNode.from_dict(data)
        assert clone.parameters["blur_type"] == "directional"

    def test_valid_values_pass_through_untouched(self):
        original = BlurNode(node_id="b")
        original.set_parameter("radius", 42.0)
        clone = BlurNode.from_dict(original.to_dict())
        assert clone.parameters == original.parameters

    def test_non_dict_parameters_payload_is_tolerated(self):
        data = BlurNode(node_id="b").to_dict()
        data["parameters"] = ["not", "a", "dict"]
        clone = BlurNode.from_dict(data)
        # Defaults survive; the load does not crash.
        assert clone.parameters["radius"] == 8.0

    def test_dynamic_roto_channels_are_sanitized(self):
        node = RotoShapeNode(node_id="r")
        data = node.to_dict()
        data["parameters"]["roto_point_0_x"] = "oops"
        clone = RotoShapeNode.from_dict(data)
        value = clone.parameters["roto_point_0_x"]
        assert isinstance(value, float)
