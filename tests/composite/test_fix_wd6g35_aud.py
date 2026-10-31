"""wd6-g35 regression tests — batch D (composite nodes), 15 fixed entries.

Covers the remediation entries fixed in wave d6/g35:
AUD-6901/6902 (image_input), AUD-6913 (light3d), AUD-7004 (mask_merge),
AUD-7006 (matte_cleanup), AUD-7008/7009/7010 (model3d), AUD-7101 (noise),
AUD-7107 (particle), AUD-7202/7203/7204 (reformat), AUD-7208 (relight),
AUD-7210 (roto_shape).  AUD-7300 (scene3d ortho depth renderbuffer leak) is
BLOCKED for this batch — its target file is not in the whitelist and is
under concurrent modification by a sibling batch; verification evidence is
in the fix report instead.
"""

import json
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.core.format import BBox, ImageFormat
from src.features.composite.core.image_loader import (DecodedImage,
                                                      ImageLoadState)
from src.features.composite.nodes import image_input_node as iiw
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.light3d_node import Light3DNode
from src.features.composite.nodes.mask_merge_node import MaskMergeNode
from src.features.composite.nodes.matte_cleanup_node import MatteCleanupNode
from src.features.composite.nodes.model3d_node import Model3DNode
from src.features.composite.nodes.noise_node import NoiseNode
from src.features.composite.nodes.particle_node import (ParticleNode,
                                                        _noise01,
                                                        _noise01_batch)
from src.features.composite.nodes.reformat_node import ReformatNode
from src.features.composite.nodes.relight_node import RelightNode
from src.features.composite.nodes.roto_shape_node import RotoShapeNode
from src.features.composite.nodes.base_node import TextureResult

pytestmark = pytest.mark.composite_core


# ── shared fakes ────────────────────────────────────────────────────────────

class _FakeTexture:
    def __init__(self, glo: int) -> None:
        self.glo = glo
        self.filter = None
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeGL:
    """Minimal moderngl.Context stand-in for texture upload paths."""

    def __init__(self) -> None:
        self._next_glo = 100
        self.created: list = []
        self.create_threads: list = []

    def texture(self, size, components, data=None, dtype='f4'):
        self._next_glo += 1
        tex = _FakeTexture(self._next_glo)
        self.created.append(tex)
        self.create_threads.append(threading.get_ident())
        return tex


def _decoded(path: str, w: int = 8, h: int = 6,
             comps: int = 3, mtime: float = 1.0) -> DecodedImage:
    return DecodedImage(
        file_path=path,
        data=np.zeros((h, w, comps), dtype=np.float32),
        width=w, height=h, components=comps,
        mtime=mtime, color_space_hint="linear",
        bit_depth="float32", has_alpha=comps == 4)


# ── AUD-6901: async sequence decode ─────────────────────────────────────────

class TestAud6901AsyncSequence:

    @pytest.fixture()
    def seq_node(self, tmp_path, monkeypatch):
        for frame in (1, 2, 3, 5):
            (tmp_path / f"plate.{frame:04d}.exr").write_bytes(b"")
        calls: dict = {"count": 0, "paths": []}
        # Per-file decode gates: a gated file's decode blocks until the
        # test releases it, making in-flight/ready states deterministic.
        gates: dict = {}
        lock = threading.Lock()

        def fake_decode(path, exr_part="", extra_channels=None):
            name = os.path.basename(str(path))
            gate = gates.get(name)
            if gate is not None:
                gate.wait(timeout=10.0)
            with lock:
                calls["count"] += 1
                calls["paths"].append(str(path))
            if name == "plate.0009.exr":
                return None  # corrupt frame
            return _decoded(str(path))

        monkeypatch.setattr(iiw, "decode_image_file", fake_decode)

        node = ImageInputNode(node_id="seq-a6901")
        node.set_parameter("file_path", str(tmp_path / "plate.####.exr"))
        node.set_parameter("frame_padding", 4)
        node._test_calls = calls
        node._test_lock = lock
        node._test_gates = gates
        yield node
        # Release any gate so a blocked worker cannot delay teardown.
        for gate in gates.values():
            gate.set()
        node.cleanup()

    def _evaluate(self, node, gl, frame, render_mode="preview"):
        eval_ctx = CompositeEvalContext(gl=gl)
        eval_ctx.frame_number = frame
        return node._do_evaluate({}, eval_ctx, None,
                                 render_mode=render_mode)

    def test_first_paint_is_synchronous(self, seq_node):
        gl = _FakeGL()
        out = self._evaluate(seq_node, gl, 1)
        assert out and out["image"].is_valid
        assert seq_node._test_calls["count"] == 1
        assert "plate.0001.exr" in "".join(seq_node._test_calls["paths"])

    def test_playback_decodes_off_thread_and_keeps_previous_frame(
            self, seq_node):
        gl = _FakeGL()
        # Gate frame 2's decode so it is guaranteed in-flight while the
        # evaluation runs.
        seq_node._test_gates["plate.0002.exr"] = threading.Event()
        out1 = self._evaluate(seq_node, gl, 1)
        glo1 = out1["image"].texture_id

        # Frame change: the new frame is decoded off-thread only.  Proof:
        # the evaluation still returned the PREVIOUS frame's texture and
        # left the node in UPLOADED_GPU (a synchronous decode would have
        # replaced the texture within this same evaluation).
        out2 = self._evaluate(seq_node, gl, 2)
        assert out2["image"].texture_id == glo1
        assert seq_node._load_state == ImageLoadState.UPLOADED_GPU

        # Release the decode and wait for frame 2 (and its frame-3
        # prefetch) so the assertions below are race-free.
        seq_node._test_gates["plate.0002.exr"].set()
        key2 = seq_node._sequence_request_key(2)
        key3 = seq_node._sequence_request_key(3)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with seq_node._seq_lock:
                if key2 in seq_node._seq_results and \
                        key3 in seq_node._seq_results:
                    break
            time.sleep(0.02)
        with seq_node._seq_lock:
            assert key2 in seq_node._seq_results
            assert key3 in seq_node._seq_results

        # Next evaluation picks the result up and uploads it.
        count_pickup = seq_node._test_calls["count"]
        out3 = self._evaluate(seq_node, gl, 2)
        assert out3["image"].texture_id != glo1
        assert seq_node._test_calls["count"] == count_pickup  # cache hit

        # Scrub between cached frames: no new decode either way.
        count_back = seq_node._test_calls["count"]
        out4 = self._evaluate(seq_node, gl, 3)  # prefetched frame
        out5 = self._evaluate(seq_node, gl, 2)  # cached result re-applied
        assert seq_node._test_calls["count"] == count_back
        assert out4["image"].is_valid
        assert out5["image"].is_valid

    def test_export_mode_uses_synchronous_exact_frame_decode(
            self, seq_node):
        gl = _FakeGL()
        self._evaluate(seq_node, gl, 1)
        count_before = seq_node._test_calls["count"]
        out = self._evaluate(seq_node, gl, 5, render_mode="export")
        # The export path decoded frame 5 inline (same evaluation).
        assert seq_node._test_calls["count"] == count_before + 1
        assert "plate.0005.exr" in "".join(seq_node._test_calls["paths"])
        assert out and out["image"].is_valid

    def test_corrupt_frame_is_sticky_across_scrubs(self, seq_node, tmp_path):
        # Frame 9 exists on disk but decode fails (corrupt content).
        (tmp_path / "plate.0009.exr").write_bytes(b"")
        gl = _FakeGL()
        self._evaluate(seq_node, gl, 1)
        # Warm the async cache with frame 2 (applied), then hit frame 9.
        self._evaluate(seq_node, gl, 2)
        key2 = seq_node._sequence_request_key(2)
        key3 = seq_node._sequence_request_key(3)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with seq_node._seq_lock:
                if key2 in seq_node._seq_results and \
                        key3 in seq_node._seq_results:
                    break
            time.sleep(0.02)
        self._evaluate(seq_node, gl, 2)

        # Frame 9: background worker attempts the decode once and records
        # the sticky failure.
        self._evaluate(seq_node, gl, 9)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with seq_node._seq_lock:
                if any("plate.0009.exr" in p for p in seq_node._seq_failures):
                    break
            time.sleep(0.02)
        with seq_node._seq_lock:
            assert any("plate.0009.exr" in p for p in seq_node._seq_failures)
        attempts = [p for p in seq_node._test_calls["paths"]
                    if "plate.0009.exr" in p]
        assert len(attempts) == 1

        # Scrub to a cached frame and back to the corrupt one: the sticky
        # failure prevents resubmitting a full decode.
        count_sticky = seq_node._test_calls["count"]
        self._evaluate(seq_node, gl, 2)
        self._evaluate(seq_node, gl, 9)
        time.sleep(0.2)
        assert seq_node._test_calls["count"] == count_sticky

    def test_worker_never_touches_gl_objects(self, seq_node):
        # The worker only produces DecodedImage entries; GL textures are
        # created exclusively on the render (calling) thread.  A fast
        # worker may deliver its result within the same evaluation — the
        # upload then still happens on the evaluating thread.
        gl = _FakeGL()
        self._evaluate(seq_node, gl, 1)   # first paint: 1 texture
        self._evaluate(seq_node, gl, 2)   # enqueue frame 2 off-thread
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with seq_node._seq_lock:
                if seq_node._seq_results:
                    break
            time.sleep(0.02)
        assert len(gl.created) >= 1
        main_thread = threading.get_ident()
        assert all(tid == main_thread for tid in gl.create_threads)


# ── AUD-6902: stale-glo cache invalidation flag ─────────────────────────────

class TestAud6902CacheInvalidationFlag:

    def test_upload_from_cpu_flags_release_of_previous_texture(self):
        node = ImageInputNode(node_id="img-a6902")
        gl = _FakeGL()
        node.set_decoded_image(_decoded("a.exr", mtime=1.0))
        assert node.upload_from_cpu(gl) is True
        # First upload creates a texture (nothing released) — no flag.
        assert node._cache_invalidated_needed is False

        node.set_decoded_image(_decoded("b.exr", mtime=2.0))
        assert node.upload_from_cpu(gl) is True
        # The previous texture was released and replaced — flag set so the
        # evaluator invalidates stale cache entries holding the old glo.
        assert node._cache_invalidated_needed is True
        assert node._revision == 2

    def test_upload_fallback_flags_release_too(self):
        node = ImageInputNode(node_id="img-a6902b")
        gl = _FakeGL()
        node.set_decoded_image(_decoded("a.exr", mtime=1.0))
        node.upload_from_cpu(gl)
        node._cache_invalidated_needed = False

        node.set_parameter("missing_policy", "black")
        assert node._upload_fallback(gl) is True
        assert node._cache_invalidated_needed is True

    def test_first_fallback_does_not_flag(self):
        node = ImageInputNode(node_id="img-a6902c")
        node.set_parameter("missing_policy", "checker")
        assert node._upload_fallback(_FakeGL()) is True
        assert node._cache_invalidated_needed is False


# ── AUD-6913: light3d legacy migration reachability ─────────────────────────

class TestAud6913Light3dMigration:

    def test_v6_slot0_light_is_migrated(self):
        data = {
            "id": "l-v6",
            "type": "light3d",
            "parameters": {
                "light0_dir_x": 0.1, "light0_dir_y": 0.2, "light0_dir_z": 0.9,
                "light0_color_r": 0.5, "light0_color_g": 0.6,
                "light0_color_b": 0.7,
                "light0_intensity": 5.0,
            },
        }
        node = Light3DNode.from_dict(data)
        assert node.parameters["light_type"] == "directional"
        assert node.parameters["light_intensity"] == 5.0
        assert node.parameters["light_dir_x"] == 0.1
        assert node.parameters["light_dir_y"] == 0.2
        assert node.parameters["light_dir_z"] == 0.9
        assert node.parameters["light_color_r"] == 0.5
        assert node.parameters["light_color_g"] == 0.6
        assert node.parameters["light_color_b"] == 0.7
        # Legacy slot keys are cleaned out of the live parameters.
        assert "light0_intensity" not in node.parameters
        assert not any(k.startswith("light0_") for k in node.parameters)

    def test_v6_legacy_main_light_is_migrated(self):
        data = {
            "id": "l-v6b",
            "type": "light3d",
            "parameters": {
                "light_intensity": 3.5,
                "light_dir_x": -0.1, "light_dir_y": 0.9, "light_dir_z": 0.2,
            },
        }
        node = Light3DNode.from_dict(data)
        assert node.parameters["light_type"] == "directional"
        assert node.parameters["light_intensity"] == 3.5
        assert node.parameters["light_dir_y"] == 0.9

    def test_v6_slot0_zero_intensity_keeps_default(self):
        data = {
            "id": "l-v6c",
            "type": "light3d",
            "parameters": {"light0_intensity": 0.0,
                           "light0_dir_x": 1.0},
        }
        node = Light3DNode.from_dict(data)
        # intensity stays at the default 2.0 (slot0 was dark)
        assert node.parameters["light_intensity"] == 2.0

    def test_v7_light_is_not_touched(self):
        data = {
            "id": "l-v7",
            "type": "light3d",
            "parameters": {
                "light_type": "point",
                "light_pos_x": 1.0, "light_pos_y": 2.0, "light_pos_z": 3.0,
                "light_intensity": 3.0,
            },
        }
        node = Light3DNode.from_dict(data)
        assert node.parameters["light_type"] == "point"
        assert node.parameters["light_intensity"] == 3.0
        assert node.parameters["light_pos_x"] == 1.0

    def test_garbage_legacy_values_do_not_crash_load(self):
        data = {
            "id": "l-bad",
            "type": "light3d",
            "parameters": {
                "light_intensity": "not-a-number",
                "light_dir_x": None,
            },
        }
        node = Light3DNode.from_dict(data)  # must not raise
        assert node.parameters["light_intensity"] == 2.0
        assert node.parameters["light_dir_x"] == -0.6


# ── AUD-7004: mask_merge output contract matches dispatcher ─────────────────

class TestAud7004MaskMergeContract:

    def test_output_bbox_is_a_bbox_not_union(self):
        node = MaskMergeNode()
        bbox_a = BBox(x=10, y=20, width=100, height=50)
        bbox_b = BBox(x=0, y=0, width=1920, height=1080)
        out = node.compute_output_bbox({"A": bbox_a, "B": bbox_b}, {})
        # Dispatcher declares input_a.bbox or input_b.bbox.
        assert (out.x, out.y, out.width, out.height) == (10, 20, 100, 50)

    def test_output_bbox_single_input_falls_back(self):
        node = MaskMergeNode()
        bbox_b = BBox(x=5, y=6, width=700, height=400)
        out = node.compute_output_bbox({"B": bbox_b}, {})
        assert (out.x, out.y) == (5, 6)

    def test_marker_dimensions_are_max_of_inputs(self):
        node = MaskMergeNode()
        a = TextureResult(texture_id=11, width=100, height=50,
                          bbox=BBox(0, 0, 100, 50))
        b = TextureResult(texture_id=12, width=1920, height=1080,
                          bbox=BBox(0, 0, 1920, 1080))
        out = node._do_evaluate({"A": a, "B": b}, None, None)
        assert out["result"].width == 1920
        assert out["result"].height == 1080

    def test_marker_dimensions_max_per_axis(self):
        node = MaskMergeNode()
        a = TextureResult(texture_id=11, width=2000, height=500,
                          bbox=BBox(0, 0, 2000, 500))
        b = TextureResult(texture_id=12, width=100, height=600,
                          bbox=BBox(0, 0, 100, 600))
        out = node._do_evaluate({"A": a, "B": b}, None, None)
        assert out["result"].width == 2000
        assert out["result"].height == 600


# ── AUD-7006: matte_cleanup blur ROI padding = full radius ──────────────────

class TestAud7006MatteCleanupPadding:

    def test_blur_padding_is_full_radius(self):
        node = MatteCleanupNode()
        assert node.requires_roi_expansion(
            {"operation": "blur", "blur_amount": 50.0}) == 50
        assert node.requires_roi_expansion(
            {"operation": "blur", "blur_amount": 2.0}) == 2

    def test_erode_dilate_padding_unchanged(self):
        node = MatteCleanupNode()
        assert node.requires_roi_expansion(
            {"operation": "erode", "size": 5}) == 3
        assert node.requires_roi_expansion(
            {"operation": "dilate", "size": 7}) == 4

    def test_garbage_amount_falls_back_without_raising(self):
        node = MatteCleanupNode()
        assert node.requires_roi_expansion(
            {"operation": "blur", "blur_amount": "garbage"}) == 2


# ── AUD-7008: model3d sticky import failure ─────────────────────────────────

class _FakeImportResult:
    def __init__(self) -> None:
        self.succeeded = False
        self.status = "failed"
        self.error = "corrupt asset"
        self.progress = 1.0
        self.phase = "failed"
        self.resource_estimate = None
        self.probe = None
        self.root = None


class _FakeImportHandle:
    def __init__(self, result: _FakeImportResult) -> None:
        self._result = result
        self.resource_estimate = SimpleNamespace(to_dict=lambda: {})

    def progress_snapshot(self):
        return {"phase": "failed", "progress": 1.0}

    def poll(self):
        return self._result

    def add_done_callback(self, cb):
        pass

    def cancel(self):
        pass


class TestAud7008ModelImportStickyFailure:

    def test_failed_import_is_not_resubmitted_until_source_changes(
            self, tmp_path, monkeypatch):
        import src.features.composite.render3d.model_import_service as svc
        model_path = tmp_path / "broken.glb"
        model_path.write_bytes(b"not a glb")

        submits = {"count": 0}

        def fake_submit(request):
            submits["count"] += 1
            return _FakeImportHandle(_FakeImportResult())

        monkeypatch.setattr(svc, "get_shared_model_import_service",
                            lambda: SimpleNamespace(submit=fake_submit))

        node = Model3DNode(node_id="m-a7008")
        assert node._ensure_model(str(model_path)) is None
        assert submits["count"] == 1  # first attempt submits

        # Poll the finished (failed) handle on the next evaluation.
        assert node._ensure_model(str(model_path)) is None
        assert node._model_error == "corrupt asset"
        assert node._model_cache_status == "error"
        assert node._model_import_failed_key is not None

        # Subsequent evaluations: sticky — no resubmission, error kept.
        for _ in range(3):
            assert node._ensure_model(str(model_path)) is None
        assert submits["count"] == 1
        assert node._model_error == "corrupt asset"

        # A source change (mtime) releases the sticky failure.
        later = time.time() + 10
        os.utime(model_path, (later, later))
        assert node._ensure_model(str(model_path)) is None
        assert submits["count"] == 2


# ── AUD-7009: model3d LOD variant construction ──────────────────────────────

class TestAud7009LodVariants:

    def test_only_reachable_lod_levels_are_built(self, monkeypatch):
        import src.features.composite.render3d.lod_builder as lod_mod
        built = {"ratios": []}

        def fake_build_lod(mesh, target_ratio=0.5):
            built["ratios"].append(target_ratio)
            return SimpleNamespace(indices=np.arange(12, dtype=np.uint32))

        monkeypatch.setattr(lod_mod, "build_lod", fake_build_lod)

        node = Model3DNode(node_id="m-a7009")
        flat = [{
            "mesh": SimpleNamespace(
                indices=np.arange(30, dtype=np.uint32),
                is_skinned=False),
            "is_skinned": False,
            "material": None,
        }]
        out = node._apply_lod_to_flat_meshes(flat, 2)

        # Exactly the two reachable levels (1 and 2); LOD3 is unreachable
        # (_resolve_lod_level only returns 0/1/2) and must not be built.
        assert len(built["ratios"]) == 2
        variants = node._mesh_lod_variants[0]
        assert set(variants.keys()) == {1, 2}
        assert out[0]["lod_mesh"] is variants[2]
        assert out[0]["lod_index_count"] == 12

        # Cached: a second navigation frame does not rebuild.
        node._apply_lod_to_flat_meshes(flat, 1)
        assert len(built["ratios"]) == 2
        assert node._mesh_lod_variants[0][1] is not None


# ── AUD-7010: model3d tolerant parameter reads ──────────────────────────────

class TestAud7010Model3dSafeParams:

    def test_safe_float_falls_back_and_warns_once(self, caplog):
        node = Model3DNode(node_id="m-a7010")
        with caplog.at_level(logging.WARNING):
            assert node._safe_param_float("unit_scale", 1.0) == 1.0
            node.set_parameter("unit_scale", "garbage")
            assert node._safe_param_float("unit_scale", 1.0) == 1.0
            # Same garbage value warns only once.
            node._safe_param_float("unit_scale", 1.0)
        warnings = [r for r in caplog.records
                    if "unit_scale" in r.getMessage()]
        assert len(warnings) == 1

    def test_safe_int_and_bounds(self):
        node = Model3DNode(node_id="m-a7010b")
        node.set_parameter("matte_id", "42")
        assert node._safe_param_int("matte_id", 0) == 42
        node.set_parameter("anim_clip", None)
        assert node._safe_param_int("anim_clip", 0, min_val=0) == 0
        assert node._safe_param_float("x", 1.0, min_val=0.0,
                                      max_val=1.0) == 1.0

    def test_do_evaluate_survives_garbage_numeric_params(self):
        node = Model3DNode(node_id="m-a7010c")
        # Corrupted/legacy project payload: numeric params as strings/None.
        for key, value in [
            ("unit_scale", "bad"), ("anim_speed", None),
            ("anim_clip", "x"), ("matte_id", []),
            ("shadow_intensity", "high"), ("object_pos_x", None),
            ("object_rot_z", "rot"), ("mesh_scale", ""),
            ("emissive_intensity", "e"),
        ]:
            node.parameters[key] = value
        # Must not raise; the no-model path emits an empty payload.
        out = node._do_evaluate({}, SimpleNamespace(), None)
        assert out == {}


# ── AUD-7101: noise animate/time wiring (node side) ─────────────────────────

class TestAud7101NoiseTime:

    def test_marker_carries_time_from_frame_context(self):
        node = NoiseNode(node_id="n-a7101")
        ctx = SimpleNamespace(frame_number=7)
        out = node._do_evaluate({}, ctx, None)
        assert out["_effect_params"]["time"] == 7.0

    def test_marker_time_defaults_to_zero_without_frame(self):
        node = NoiseNode(node_id="n-a7101b")
        out = node._do_evaluate({}, SimpleNamespace(), None)
        assert out["_effect_params"]["time"] == 0.0

    def test_frame_cache_required_only_when_animate(self):
        assert NoiseNode.params_require_frame_cache({"animate": True})
        assert not NoiseNode.params_require_frame_cache({"animate": False})
        assert not NoiseNode.params_require_frame_cache({})

    def test_garbage_animate_flag_does_not_raise(self):
        # Truthy garbage is treated as animated (safe direction: extra
        # cache invalidation instead of stale-frame reuse); falsy is not.
        assert NoiseNode.params_require_frame_cache({"animate": "x"})
        assert not NoiseNode.params_require_frame_cache({"animate": 0})


# ── AUD-7107: vectorized turbulence field ───────────────────────────────────

class TestAud7107VectorizedTurbulence:

    def test_scalar_and_batch_noise_agree(self):
        rng = np.random.default_rng(3)
        xs = rng.uniform(-50.0, 50.0, 64)
        ys = rng.uniform(-50.0, 50.0, 64)
        for seed in (0, 7919, 123456):
            batch = _noise01_batch(seed, xs, ys)
            for i in range(0, 64, 7):
                assert _noise01(seed, float(xs[i]), float(ys[i])) == \
                    pytest.approx(float(batch[i]), abs=1e-15)

    def test_simulation_is_deterministic(self):
        def make():
            node = ParticleNode(node_id="p-a7107")
            for k, v in dict(rate=6.0, lifetime=25.0, seed=11,
                             turb_amount=3.0, turb_scale=0.02,
                             output_width=160,
                             output_height=120).items():
                node.set_parameter(k, v)
            return node

        a = make()._simulate(40, 160, 120)
        b = make()._simulate(40, 160, 120)
        assert a.shape == b.shape
        np.testing.assert_array_equal(a, b)

    def test_turbulence_changes_trajectories(self):
        def make(turb):
            node = ParticleNode(node_id="p-a7107b")
            for k, v in dict(rate=6.0, lifetime=25.0, seed=11,
                             turb_amount=turb, turb_scale=0.02,
                             output_width=160,
                             output_height=120).items():
                node.set_parameter(k, v)
            return node

        calm = make(0.0)._simulate(30, 160, 120)
        wild = make(3.0)._simulate(30, 160, 120)
        assert calm.shape == wild.shape
        assert calm.shape[0] > 0
        assert not np.allclose(calm[:, :2], wild[:, :2])

    def test_heavy_turbulence_replay_is_fast(self):
        """AUD-7107 perf smoke: the old per-particle loop needed tens of
        seconds for comparable parameters; the vectorized field must stay
        well under the interactive budget."""
        node = ParticleNode(node_id="p-a7107c")
        for k, v in dict(rate=60.0, lifetime=120.0, lifetime_variance=0.5,
                         seed=3, turb_amount=5.0, turb_scale=0.01,
                         particle_cap=2000, output_width=320,
                         output_height=240).items():
            node.set_parameter(k, v)
        t0 = time.perf_counter()
        particles = node._simulate(150, 320, 240)
        elapsed = time.perf_counter() - t0
        assert particles.shape[0] > 0
        assert elapsed < 5.0, f"turbulence replay too slow: {elapsed:.2f}s"


# ── AUD-7202: reformat passthrough metadata ─────────────────────────────────

class TestAud7202ReformatPassthroughMetadata:

    @staticmethod
    def _rich_input() -> TextureResult:
        return TextureResult(
            texture_id=42, width=1920, height=1080, revision=7,
            format=ImageFormat(width=1920, height=1080, name="HD"),
            bbox=BBox(0, 0, 1920, 1080),
            alpha_premultiplied=False, color_space="linear",
            bit_depth=16, frame_metadata={"codec": "prores"},
            depth_texture_id=99, has_deep_data=True,
            aov_texture_ids={"normal": 55},
            aov_dimensions={"normal": (1920, 1080)},
            render_diagnostics={"scene": "ok"})

    def _node(self, resize_mode: str) -> ReformatNode:
        node = ReformatNode(node_id="r-a7202")
        node.set_parameter("format_mode", "custom")
        node.set_parameter("width", 1920)
        node.set_parameter("height", 1080)
        node.set_parameter("resize_mode", resize_mode)
        return node

    def test_no_resize_same_size_preserves_metadata(self):
        node = self._node("no_resize")
        out = node._do_evaluate({"image": self._rich_input()}, None, None)
        result = out["result"]
        assert result.texture_id == 42
        assert result.revision == 7
        assert result.bit_depth == 16
        assert result.frame_metadata == {"codec": "prores"}
        assert result.depth_texture_id == 99
        assert result.has_deep_data is True
        assert result.aov_texture_ids == {"normal": 55}
        assert result.aov_dimensions == {"normal": (1920, 1080)}
        assert result.render_diagnostics == {"scene": "ok"}
        assert result.format.name == "Reformatted"

    def test_fit_same_size_preserves_metadata(self):
        node = self._node("fit")
        out = node._do_evaluate({"image": self._rich_input()}, None, None)
        result = out["result"]
        assert result.revision == 7
        assert result.bit_depth == 16
        assert result.depth_texture_id == 99
        assert result.aov_texture_ids == {"normal": 55}


# ── AUD-7203: reformat forward bbox mapping matches the renderer ────────────

class TestAud7203ReformatOutputBbox:

    @staticmethod
    def _params(target_w: int, target_h: int, mode: str) -> dict:
        return {"format_mode": "custom", "width": target_w,
                "height": target_h, "resize_mode": mode, "filter": "linear"}

    def test_full_frame_fit_matches_renderer_formula(self):
        node = ReformatNode()
        bbox = BBox(0, 0, 3840, 2160)
        out = node.compute_output_bbox(
            {"image": bbox}, self._params(1920, 1080, "fit"))
        assert (out.x, out.y, out.width, out.height) == (0, 0, 1920, 1080)

    def test_subwindow_bbox_position_is_scaled_and_centered(self):
        node = ReformatNode()
        # scale=2.0, offsets 0 → unclamped (400, 200, 1000, 1000); the
        # canvas clamp then trims the overhang (old code dropped the bbox
        # origin entirely and returned (0, 0, ...)).
        bbox = BBox(200, 100, 500, 500)
        out = node.compute_output_bbox(
            {"image": bbox}, self._params(1000, 1000, "fit"))
        assert (out.x, out.y, out.width, out.height) == (400, 200, 600, 800)

    def test_letterbox_offset_uses_half_pixel_not_floor_div(self):
        node = ReformatNode()
        # scale = min(1000/250, 1000/100) = 4.0 → scaled (1000, 400);
        # offset_y = (1000-400)*0.5 = 300 → y=300 (matches renderer).
        bbox = BBox(0, 0, 250, 100)
        out = node.compute_output_bbox(
            {"image": bbox}, self._params(1000, 1000, "fit"))
        assert (out.x, out.y, out.width, out.height) == (0, 300, 1000, 400)

    def test_distort_scales_per_axis_and_keeps_origin(self):
        node = ReformatNode()
        bbox = BBox(100, 50, 500, 250)
        out = node.compute_output_bbox(
            {"image": bbox}, self._params(1000, 500, "distort"))
        # scale_x=2, scale_y=2 → unclamped (200, 100, 1000, 500); the
        # canvas clamp trims the overhang.
        assert (out.x, out.y, out.width, out.height) == (200, 100, 800, 400)

    def test_preserve_bbox_passthrough(self):
        node = ReformatNode()
        bbox = BBox(7, 8, 90, 70)
        out = node.compute_output_bbox(
            {"image": bbox}, self._params(1000, 1000, "fit"))
        params = self._params(1000, 1000, "fit")
        params["preserve_bbox"] = True
        out2 = node.compute_output_bbox({"image": bbox}, params)
        assert (out2.x, out2.y, out2.width, out2.height) == (7, 8, 90, 70)


# ── AUD-7204: reformat inverse ROI mapping ──────────────────────────────────

class TestAud7204ReformatInputRoi:

    @staticmethod
    def _params(target_w: int, target_h: int, mode: str,
                filt: str = "linear") -> dict:
        return {"format_mode": "custom", "width": target_w,
                "height": target_h, "resize_mode": mode, "filter": filt}

    def test_no_resize_applies_filter_padding(self):
        node = ReformatNode()
        roi = BBox(10, 10, 100, 100)
        out = node.compute_input_roi(
            roi, self._params(1000, 1000, "no_resize", filt="lanczos"))
        expected = roi.expanded(2)
        assert out["image"].x == expected.x
        assert out["image"].width == expected.width

    def test_fit_inverse_subtracts_centring_offset(self):
        node = ReformatNode()
        # Forward: scale=1.0, offset_x=(2000-1000)*0.5=500, offset_y=0.
        # The output ROI (500, 0, 1000, 1000) maps back to exactly the
        # full input frame (+1px linear-filter padding).
        out = node.compute_input_roi(
            BBox(500, 0, 1000, 1000),
            self._params(2000, 1000, "fit"),
            input_bboxes={"image": BBox(0, 0, 1000, 1000)})
        roi = out["image"]
        assert roi.x == -1 and roi.y == -1
        assert roi.width == 1002 and roi.height == 1002

    def test_distort_inverse_uses_per_axis_scales(self):
        node = ReformatNode()
        # scale_x=0.5, scale_y=1.0 → inverse (x2, y1): the 1000x1000 output
        # ROI needs the full 2000x1000 input (average scale would request a
        # wrong 1333x1333 window).
        out = node.compute_input_roi(
            BBox(0, 0, 1000, 1000),
            self._params(1000, 1000, "distort"),
            input_bboxes={"image": BBox(0, 0, 2000, 1000)})
        roi = out["image"]
        assert roi.x == -1 and roi.y == -1
        assert roi.width == 2002 and roi.height == 1002

    def test_fit_inverse_outside_letterbox_area(self):
        node = ReformatNode()
        # Output region entirely in the letterbox bar (offset area): the
        # inverse maps to a negative/oversized input window rather than a
        # wrong positive offset region.
        out = node.compute_input_roi(
            BBox(0, 800, 1000, 200),
            self._params(2000, 1000, "fit"),
            input_bboxes={"image": BBox(0, 0, 1000, 1000)})
        roi = out["image"]
        assert roi.y + roi.height >= 1000  # reaches into the input frame


# ── AUD-7208: relight color validation ──────────────────────────────────────

class TestAud7208RelightColor:

    @staticmethod
    def _inputs() -> dict:
        image = TextureResult(texture_id=21, width=64, height=48)
        normal = TextureResult(texture_id=22, width=64, height=48)
        return {"image": image, "normal": normal}

    def test_valid_color_passes_through(self):
        node = RelightNode(node_id="rl-a7208")
        node.set_parameter("color", (0.2, 0.3, 0.4, 1.0))
        out = node._do_evaluate(self._inputs(), None, None)
        assert out["_effect_params"]["color"] == (0.2, 0.3, 0.4)

    def test_none_color_falls_back_to_white(self, caplog):
        node = RelightNode(node_id="rl-a7208b")
        node.parameters["color"] = None
        with caplog.at_level(logging.WARNING):
            out = node._do_evaluate(self._inputs(), None, None)
        assert out["_effect_params"]["color"] == (1.0, 1.0, 1.0)
        assert any("color" in r.getMessage() for r in caplog.records)

    def test_short_or_nonnumeric_color_falls_back_per_channel(self, caplog):
        node = RelightNode(node_id="rl-a7208c")
        node.parameters["color"] = (1.0, 2.0)  # too short
        with caplog.at_level(logging.WARNING):
            out = node._do_evaluate(self._inputs(), None, None)
        assert out["_effect_params"]["color"] == (1.0, 1.0, 1.0)

        node2 = RelightNode(node_id="rl-a7208d")
        node2.parameters["color"] = (0.2, "bad", 0.4, 1.0)
        with caplog.at_level(logging.WARNING):
            out2 = node2._do_evaluate(self._inputs(), None, None)
        assert out2["_effect_params"]["color"] == (0.2, 1.0, 0.4)


# ── AUD-7210: roto point parameter validation ───────────────────────────────

class TestAud7210RotoPointParameters:

    def test_valid_write_updates_both_channels(self):
        node = RotoShapeNode(node_id="ro-a7210")
        node.set_parameter("roto_point_0_x", 0.123)
        assert node.parameters["roto_point_0_x"] == 0.123
        points = node.parse_points(node.parameters["points"])
        assert points[0]["pos"][0] == 0.123

        node.set_parameter("roto_point_1_c1_y", 0.7)
        points = node.parse_points(node.parameters["points"])
        assert points[1]["c1"][1] == 0.7

    def test_invalid_value_is_rejected_before_commit(self, caplog):
        node = RotoShapeNode(node_id="ro-a7210b")
        node.set_parameter("roto_point_0_x", 0.5)
        json_before = node.parameters["points"]
        with caplog.at_level(logging.WARNING):
            node.set_parameter("roto_point_0_x", None)
            node.set_parameter("roto_point_0_y", "not-a-number")
        # Neither the numeric channel nor the JSON diverged.
        assert node.parameters["roto_point_0_x"] == 0.5
        assert node.parameters["points"] == json_before
        assert len([r for r in caplog.records
                    if "rejected" in r.getMessage()]) == 2

    def test_out_of_range_index_and_channel_rejected(self, caplog):
        node = RotoShapeNode(node_id="ro-a7210c")
        with caplog.at_level(logging.WARNING):
            node.set_parameter("roto_point_9_x", 0.1)   # only 4 points
            node.set_parameter("roto_point_0_bogus", 0.1)
            node.set_parameter("roto_point_x", 0.1)
        assert "roto_point_9_x" not in node.parameters
        assert "roto_point_0_bogus" not in node.parameters

    def test_feather_write_is_clamped(self):
        node = RotoShapeNode(node_id="ro-a7210d")
        node.set_parameter("roto_point_0_feather", 2.0)
        assert node.parameters["roto_point_0_feather"] == 0.5
        points = node.parse_points(node.parameters["points"])
        assert points[0]["feather"] == 0.5

    def test_overrides_tolerate_garbage_channel_values(self):
        node = RotoShapeNode(node_id="ro-a7210e")
        points = node.parse_points(node.parameters["points"])
        out = RotoShapeNode.apply_point_parameter_overrides(
            points, {"roto_point_0_x": "bad", "roto_point_1_y": None,
                     "roto_point_2_feather": "x"})
        assert out[0]["pos"][0] == points[0]["pos"][0]
        assert out[1]["pos"][1] == points[1]["pos"][1]
        assert "feather" not in out[2]
        # Valid overrides still apply.
        out2 = RotoShapeNode.apply_point_parameter_overrides(
            points, {"roto_point_0_x": 0.9})
        assert out2[0]["pos"][0] == 0.9
