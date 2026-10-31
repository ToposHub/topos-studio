"""wd7-g37 remediation batch tests.

Covers the Major entries fixed in this batch:
  AUD-7605  video_frame_node: dead GC finalizer removed
  AUD-7700  volume3d_node: self-managed GL texture (no per-eval leak)
  AUD-7710  warp_node: grid_size change resamples the lattice by (row,col)
  AUD-7900  ai_cache_manager: no EXR/manifest/evict I/O under the lock
  AUD-7901  ai_cache_manager: cross-instance manifest read-modify-write
  AUD-7908  ai_compute_manager: custom-model failure emits a terminal error
  AUD-7909  ai_compute_manager: concurrency-cap eviction emits compute_stale
  AUD-7910  ai_compute_manager: cancellation no longer reported as failure
  AUD-7911  ai_compute_manager: _on_worker_stale deleteLater
  AUD-7912  ai_compute_manager: cancel* wait outside the manager lock
  AUD-7913  ai_compute_manager: bounded LRU ONNX session cache
  AUD-7914  ai_compute_manager: tile/overlap + expand_* input sanitization
  AUD-80002  composite_export_renderer: render_gpu_handle one-shot cleanup
  AUD-80003  composite_export_renderer: None-pass AOV reads stay linear
  AUD-81001  composite_render_output: unified mid-cancel semantics
  AUD-81002  composite_render_output: sequence exception-path cleanup
"""

import os
import sys
import threading
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

from src.features.composite.render import ai_compute_manager as acm
from src.features.composite.render import ai_cache_manager as acm_cache
from src.features.composite.render.composite_export_renderer import (
    CompositeExportRenderer,
)
from src.features.composite.render.composite_render_output import (
    CompositeRenderOutput,
)
from src.features.composite.nodes.warp_node import WarpNode

pytestmark = pytest.mark.composite_core


# ═══════════════════════════════════════════════════════════════════════
# AUD-7605 — GC finalizer dead code removed
# ═══════════════════════════════════════════════════════════════════════

class TestAud7605GcFinalizer:
    def test_no_dead_finalizer_registered(self):
        """The permanently-empty _gc_resources finalizer is gone."""
        from src.features.composite.nodes.video_frame_node import VideoFrameNode
        node = VideoFrameNode(node_id="gc1")
        assert not hasattr(node, "_finalizer")
        assert not hasattr(node, "_gc_resources")

    def test_cleanup_on_gc_helper_retained_and_functional(self):
        """Locked contract (test_resource_cleanup_on_exception.py): the
        helper exists on the class and releases what it is given."""
        from src.features.composite.nodes.video_frame_node import VideoFrameNode
        assert hasattr(VideoFrameNode, "_cleanup_on_gc")

        released = []

        class _Res:
            def release(self):
                released.append(self)

        class _Closable:
            def close(self):
                released.append(self)

        VideoFrameNode._cleanup_on_gc([_Res(), _Closable(), None])
        assert len(released) == 2


# ═══════════════════════════════════════════════════════════════════════
# AUD-7700 — volume3d self-managed texture
# ═══════════════════════════════════════════════════════════════════════

class _FakeGLTexture:
    _next_glo = 1000

    def __init__(self):
        self.glo = _FakeGLTexture._next_glo
        _FakeGLTexture._next_glo += 1
        self.released = False

    def release(self):
        self.released = True


class _FakeGL:
    def __init__(self):
        self.created = []

    def texture(self, size, components, data=None, dtype=None):
        tex = _FakeGLTexture()
        self.created.append(tex)
        return tex


class _FakeCtx:
    def __init__(self, gl):
        self.gl = gl


def _make_volume_node(monkeypatch, density=0.5, grid_shape=(4, 8, 8)):
    from src.features.composite.nodes.volume3d_node import Volume3DNode
    from src.features.composite.render3d import volume_grid

    grid = volume_grid.VolumeGrid(
        density=np.full(grid_shape, density, dtype=np.float32),
        spacing=1.0)
    monkeypatch.setattr(volume_grid, "load_volume", lambda path: grid)
    node = Volume3DNode(node_id="vol1")
    node.parameters["file_path"] = "/fake/vol.npz"
    return node


class TestAud7700VolumeTextureOwnership:
    def test_texture_held_and_reused_between_evaluations(self, monkeypatch):
        node = _make_volume_node(monkeypatch)
        gl = _FakeGL()
        out1 = node._do_evaluate({}, _FakeCtx(gl), None)
        assert out1["result"].texture_id == gl.created[0].glo
        assert node._gpu_texture is gl.created[0]
        assert len(gl.created) == 1

        # Same content → the held texture is reused, nothing new created.
        out2 = node._do_evaluate({}, _FakeCtx(gl), None)
        assert out2["result"].texture_id == out1["result"].texture_id
        assert len(gl.created) == 1
        assert not gl.created[0].released

    def test_content_change_releases_old_and_flags_invalidation(
            self, monkeypatch):
        node = _make_volume_node(monkeypatch)
        gl = _FakeGL()
        node._do_evaluate({}, _FakeCtx(gl), None)
        old = gl.created[0]

        node.set_parameter("density_multiplier", 5.0)
        out = node._do_evaluate({}, _FakeCtx(gl), None)
        assert len(gl.created) == 2
        assert old.released
        assert out["result"].texture_id == gl.created[1].glo
        # constant_node pattern: stale cache entries hold the released glo.
        assert node._cache_invalidated_needed is True

    def test_cleanup_releases_held_texture(self, monkeypatch):
        node = _make_volume_node(monkeypatch)
        gl = _FakeGL()
        node._do_evaluate({}, _FakeCtx(gl), None)
        tex = node._gpu_texture
        node.cleanup()
        assert tex.released
        assert node._gpu_texture is None


# ═══════════════════════════════════════════════════════════════════════
# AUD-7710 — warp grid_size resampling
# ═══════════════════════════════════════════════════════════════════════

class TestAud7710WarpGridResample:
    def test_upsample_keeps_normalized_position(self, qapp):
        """Old point (row 1, col 2) on a 4-grid keeps its frame position on
        the 8-grid at (row 2, col 4) — NOT flat index 7 → (0, 7)."""
        node = WarpNode(node_id="w")
        node.set_parameter("grid_size", 4)
        offsets = [[0.0, 0.0]] * 25
        offsets[7] = [0.3, 0.0]          # (row 1, col 2)
        node.set_offsets(offsets)

        node.set_parameter("grid_size", 8)
        off8 = node.get_offsets()
        assert off8[2 * 9 + 4][0] == pytest.approx(0.3, abs=1e-6)
        assert off8[2 * 9 + 4][1] == pytest.approx(0.0, abs=1e-6)
        # The old flat-index location (0, 7) is no longer the carrier.
        assert off8[7] == [0.0, 0.0]
        # Neighbouring new points get the bilinear spread, not a jump.
        assert off8[2 * 9 + 3][0] == pytest.approx(0.15, abs=1e-6)

    def test_downsample_keeps_corners_and_position(self, qapp):
        node = WarpNode(node_id="w")
        node.set_parameter("grid_size", 8)
        offsets = [[0.0, 0.0]] * 81
        offsets[4 * 9 + 4] = [0.2, 0.1]  # centre (row 4, col 4)
        offsets[0] = [0.05, -0.05]       # corner (0, 0)
        node.set_offsets(offsets)

        node.set_parameter("grid_size", 4)
        off4 = node.get_offsets()
        assert off4[2 * 5 + 2] == pytest.approx([0.2, 0.1], abs=1e-6)
        assert off4[0] == pytest.approx([0.05, -0.05], abs=1e-6)

    def test_channels_resynced_after_grid_change(self, qapp):
        node = WarpNode(node_id="w")
        node.set_parameter("grid_size", 4)
        offsets = [[0.0, 0.0]] * 25
        offsets[7] = [0.3, 0.0]
        node.set_offsets(offsets)
        node.set_parameter("grid_size", 8)
        assert node.parameters["warp_point_22_dx"] == pytest.approx(0.3)

    def test_same_grid_write_is_plain_normalization(self, qapp):
        node = WarpNode(node_id="w")
        node.set_parameter("grid_size", 4)
        offsets = [[0.0, 0.0]] * 25
        offsets[12] = [0.1, -0.1]
        node.set_offsets(offsets)
        node.set_parameter("grid_size", 4)   # clamped, no change
        assert node.get_offsets()[12] == pytest.approx([0.1, -0.1])


# ═══════════════════════════════════════════════════════════════════════
# AUD-7900 / AUD-7901 — ai_cache_manager
# ═══════════════════════════════════════════════════════════════════════

_IMG = np.linspace(0.0, 1.0, 16 * 16 * 3,
                   dtype=np.float32).reshape(16, 16, 3)


class TestAud7900PutLockDiscipline:
    def test_has_cache_not_blocked_by_exr_write(self, tmp_path, monkeypatch):
        mgr = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        entered = threading.Event()
        release = threading.Event()
        real_imwrite = acm_cache.cv2.imwrite

        def slow_imwrite(path, img, params=None):
            entered.set()
            release.wait(5.0)
            return real_imwrite(path, img, params) if params else \
                real_imwrite(path, img)

        monkeypatch.setattr(acm_cache.cv2, "imwrite", slow_imwrite)
        t = threading.Thread(target=lambda: mgr.put("k", _IMG))
        t.start()
        try:
            assert entered.wait(2.0), "EXR write never started"
            # While the EXR write is blocked, a cache query from another
            # thread must complete (previously it waited on the lock).
            probe = threading.Thread(target=lambda: mgr.has_cache("other"))
            probe.start()
            probe.join(timeout=1.0)
            assert not probe.is_alive(), (
                "has_cache blocked behind put()'s in-lock EXR write")
        finally:
            release.set()
            t.join(5.0)
        assert mgr.has_cache("k")

    def test_eviction_delete_files_outside_lock(self, tmp_path, monkeypatch):
        """The eviction plan is computed under the lock; file deletion
        happens after — a slow os.remove must not block has_cache."""
        mgr = acm_cache.AICacheManager(cache_dir=str(tmp_path), max_size_gb=1)
        mgr.put("k1", _IMG)
        mgr.put("k2", _IMG)
        # Force eviction of both entries with a tiny budget instance.
        small = acm_cache.AICacheManager(cache_dir=str(tmp_path),
                                         max_size_gb=0.000001)
        entered = threading.Event()
        release = threading.Event()
        real_remove = acm_cache.os.remove

        def slow_remove(path):
            entered.set()
            release.wait(5.0)
            return real_remove(path)

        monkeypatch.setattr(acm_cache.os, "remove", slow_remove)
        t = threading.Thread(target=lambda: small.put("k3", _IMG))
        t.start()
        try:
            assert entered.wait(2.0), "eviction never started"
            probe = threading.Thread(target=lambda: mgr.has_cache("k1"))
            probe.start()
            probe.join(timeout=1.0)
            assert not probe.is_alive(), (
                "has_cache blocked behind eviction file deletion")
        finally:
            release.set()
            t.join(5.0)


class TestAud7901CrossInstanceManifest:
    def test_interleaved_instances_do_not_lose_entries(self, tmp_path):
        """module.py builds a throwaway AICacheManager per save callback —
        a blind whole-file overwrite dropped the other instance's entry."""
        a = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        b = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        pa = a.put("from_a", _IMG)
        pb = b.put("from_b", _IMG)       # b's in-memory manifest lacks from_a

        fresh = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        assert fresh.has_cache("from_a")
        assert fresh.has_cache("from_b")
        assert fresh.get_cache_path("from_a") == pa
        assert fresh.get_cache_path("from_b") == pb

    def test_invalidate_visible_across_instances(self, tmp_path):
        a = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        b = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        pb = b.put("gone", _IMG)
        a.invalidate("gone")
        fresh = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        assert not fresh.has_cache("gone")
        assert not os.path.exists(pb)

    def test_clear_all_removes_sibling_entries(self, tmp_path):
        a = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        b = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        pa = a.put("x", _IMG)
        pb = b.put("y", _IMG)
        a.clear_all()
        fresh = acm_cache.AICacheManager(cache_dir=str(tmp_path))
        assert not fresh.has_cache("x")
        assert not fresh.has_cache("y")
        assert not os.path.exists(pa)
        assert not os.path.exists(pb)


# ═══════════════════════════════════════════════════════════════════════
# AUD-7908 / AUD-7910 / AUD-7914 — worker run() semantics
# ═══════════════════════════════════════════════════════════════════════

class _FakeCacheManager:
    def __init__(self):
        self.saved = {}

    def put(self, cache_key, image):
        self.saved[cache_key] = image
        return f"/fake/{cache_key}.exr"


class _FakePort:
    name = "input"


class _EchoSession:
    """ONNX-shaped echo: returns the fed tensor's first 3 channels (a
    LaMa-like model that outputs an RGB image)."""

    def get_inputs(self):
        return [_FakePort()]

    def get_outputs(self):
        return [_FakePort()]

    def run(self, names, feeds):
        return [np.ascontiguousarray(feeds["input"][:, :3])]


def _make_worker(node_type="ai_inpaint", params=None, cache_key="k"):
    return acm.AIComputeWorker(
        cache_key, node_type,
        {"image": np.linspace(0.0, 0.75, 32 * 32 * 3,
                              dtype=np.float32).reshape(32, 32, 3)},
        params or {}, cache_manager=_FakeCacheManager(), request_id=1)


class TestAud7908CustomModelFailure:
    def test_none_result_emits_terminal_error(self, qapp, monkeypatch,
                                              tmp_path):
        model = tmp_path / "custom.onnx"
        model.write_bytes(b"\x00")
        worker = _make_worker("ai_generate", {
            "mode": "txt2img", "prompt": "a cube",
            "model_path": str(model), "width": 8, "height": 8})
        monkeypatch.setattr(worker, "_run_custom_generate_model",
                            lambda *a, **k: None)
        errors = []
        finished = []
        worker.error.connect(lambda k, m, r: errors.append(m))
        worker.finished.connect(lambda k, p, r: finished.append(p))
        worker.run()
        assert len(errors) == 1
        assert "Custom model inference failed" in errors[0]
        assert not finished

    def test_cancelled_custom_model_stays_silent(self, qapp, monkeypatch,
                                                 tmp_path):
        model = tmp_path / "custom.onnx"
        model.write_bytes(b"\x00")
        worker = _make_worker("ai_generate", {
            "mode": "txt2img", "prompt": "a cube",
            "model_path": str(model), "width": 8, "height": 8})
        monkeypatch.setattr(worker, "_run_custom_generate_model",
                            lambda *a, **k: None)
        worker.cancel()
        errors = []
        worker.error.connect(lambda k, m, r: errors.append(m))
        worker.run()
        assert errors == []


class TestAud7910CancelNotFailure:
    def test_cancel_exception_reports_nothing(self, qapp, monkeypatch):
        worker = _make_worker()
        worker._cancelled = True
        monkeypatch.setattr(
            worker, "_get_model_path", lambda: "/fake/model.onnx")

        def raise_cancel(model_path):
            raise acm._ComputeCancelled()

        monkeypatch.setattr(worker, "_load_onnx_session", raise_cancel)
        errors = []
        worker.error.connect(lambda k, m, r: errors.append(m))
        worker.run()
        assert errors == []

    def test_real_failure_still_reported(self, qapp, monkeypatch):
        worker = _make_worker()
        monkeypatch.setattr(
            worker, "_get_model_path", lambda: "/fake/model.onnx")

        def raise_boom(model_path):
            raise ValueError("boom")

        monkeypatch.setattr(worker, "_load_onnx_session", raise_boom)
        monkeypatch.setattr(acm.traceback, "print_exc", lambda *a, **k: None)
        errors = []
        worker.error.connect(lambda k, m, r: errors.append(m))
        worker.run()
        assert len(errors) == 1
        assert "boom" in errors[0]


class TestAud7914TilingSanitization:
    def test_sanitize_clamps(self):
        s = acm.AIComputeWorker._sanitize_tiling_params
        assert s(512, 512) == (512, 256)     # overlap >= tile → ValueError
        assert s(512, 1) == (512, 0)         # NaN Hanning weights
        assert s(512, -3) == (512, 0)
        assert s(0, 64) == (512, 64)
        assert s("x", None) == (512, 64)
        assert s(64, 32) == (64, 32)         # valid values untouched
        # The sanitized pair always yields a positive stride.
        for ts, ov in (s(7, 7), s(16, 15), s(3, 2), s(512, 511)):
            assert ts - ov >= 1

    def test_expand_negative_clamped(self, qapp):
        worker = _make_worker("ai_expand")
        image = np.zeros((16, 16, 3), dtype=np.float32)
        canvas, _mask, expand = worker._prepare_outpaint_input(
            image, {"expand_left": -8, "expand_right": 4,
                    "expand_top": -2, "expand_bottom": 2,
                    "blend_radius": 2})
        assert canvas.shape == (16 + 2, 16 + 4, 3)
        assert expand["expand_left"] == 0
        assert expand["expand_top"] == 0

    def _run_tiled(self, qapp, monkeypatch, tile_size, overlap):
        worker = _make_worker(params={
            "tile_size": tile_size, "overlap": overlap, "use_tiling": True,
            "enable_preview": False})
        monkeypatch.setattr(
            worker, "_get_model_path", lambda: "/fake/model.onnx")
        monkeypatch.setattr(worker, "_load_onnx_session",
                            lambda path: _EchoSession())
        errors, finished = [], []
        worker.error.connect(lambda k, m, r: errors.append(m))
        worker.finished.connect(lambda k, p, r: finished.append(p))
        worker.run()
        return worker, errors, finished

    def test_overlap_equal_tile_renders_instead_of_valueerror(
            self, qapp, monkeypatch):
        # Pre-fix: stride 0 → "range() arg 3 must not be zero" error signal.
        worker, errors, finished = self._run_tiled(
            qapp, monkeypatch, tile_size=16, overlap=16)
        assert errors == []
        assert finished == ["/fake/k.exr"]
        assert worker.cache_manager.saved["k"] is not None

    def test_overlap_one_produces_finite_output(self, qapp, monkeypatch):
        # Pre-fix: 0/(1-1) → NaN weights → nan_to_num zeroed the result.
        worker, errors, finished = self._run_tiled(
            qapp, monkeypatch, tile_size=16, overlap=1)
        assert errors == []
        assert finished == ["/fake/k.exr"]
        result = worker.cache_manager.saved["k"]
        assert np.isfinite(result).all()
        # overlap sanitized to 0 → non-overlapping tiles echo the input.
        expected = np.clip(worker.input_images["image"], 0, 1)
        assert np.allclose(result, expected, atol=1e-5)


# ═══════════════════════════════════════════════════════════════════════
# AUD-7909 / AUD-7911 / AUD-7912 / AUD-7913 — manager bookkeeping
# ═══════════════════════════════════════════════════════════════════════

class _FakeWorker:
    instances = []

    def __init__(self, cache_key, node_type, input_images, params,
                 cache_manager, request_id=0):
        self.cache_key = cache_key
        self.request_id = request_id
        self.cancelled = False
        self.deleted = False
        self.started = False
        signal = types.SimpleNamespace(connect=lambda *a, **k: None)
        self.finished = signal
        self.preview = signal
        self.progress = signal
        self.error = signal
        self.stale_detected = signal
        _FakeWorker.instances.append(self)

    def set_stale_callback(self, cb):
        pass

    def cancel(self):
        self.cancelled = True

    def start(self):
        self.started = True

    def deleteLater(self):
        self.deleted = True

    def wait(self, ms=None):
        pass


def _make_manager(monkeypatch):
    _FakeWorker.instances = []
    monkeypatch.setattr(acm, "AIComputeWorker", _FakeWorker)
    return acm.AIComputeManager(cache_manager=object())


class TestAud7909EvictionSignals:
    def test_evicted_worker_cancelled_and_stale_emitted(self, qapp,
                                                        monkeypatch):
        mgr = _make_manager(monkeypatch)
        stale = []
        mgr.compute_stale.connect(stale.append)
        assert mgr.request_compute("a", "ai_inpaint", {}, {})
        assert mgr.request_compute("b", "ai_inpaint", {}, {})
        assert mgr.request_compute("c", "ai_inpaint", {}, {})  # evicts a

        assert stale == ["a"]
        evicted = _FakeWorker.instances[0]
        assert evicted.cancelled is True
        assert evicted.started is True      # it did run before eviction
        assert not mgr.is_processing("a")
        assert mgr.is_processing("b") and mgr.is_processing("c")


class TestAud7911StaleHandlerDeleteLater:
    def test_on_worker_stale_schedules_delete_and_emits(self, qapp,
                                                        monkeypatch):
        mgr = _make_manager(monkeypatch)
        worker = _FakeWorker("k", "ai_inpaint", {}, {}, None)
        mgr._workers["k"] = worker
        stale = []
        mgr.compute_stale.connect(stale.append)
        mgr._on_worker_stale("k", 7)
        assert worker.deleted is True
        assert "k" not in mgr._workers
        assert stale == ["k"]


class TestAud7912CancelOutsideLock:
    def _blocking_worker(self):
        w = _FakeWorker("k", "ai_inpaint", {}, {}, None)
        w.entered_wait = threading.Event()
        w.release = threading.Event()

        def wait(ms=None):
            w.entered_wait.set()
            w.release.wait(5.0)

        w.wait = wait
        return w

    def test_cancel_waits_without_holding_lock(self, qapp, monkeypatch):
        mgr = _make_manager(monkeypatch)
        worker = self._blocking_worker()
        mgr._workers["k"] = worker
        t = threading.Thread(target=lambda: mgr.cancel("k"))
        t.start()
        try:
            assert worker.entered_wait.wait(2.0)
            # The manager lock must be free while cancel() blocks in wait().
            acquired = mgr._lock.acquire(timeout=0.5)
            assert acquired, "cancel() held the manager lock during wait()"
            mgr._lock.release()
        finally:
            worker.release.set()
            t.join(5.0)
        assert "k" not in mgr._workers
        assert worker.cancelled is True

    def test_cancel_all_waits_without_holding_lock(self, qapp, monkeypatch):
        mgr = _make_manager(monkeypatch)
        workers = [self._blocking_worker() for _ in range(3)]
        for i, w in enumerate(workers):
            mgr._workers[f"k{i}"] = w
        t = threading.Thread(target=mgr.cancel_all)
        t.start()
        try:
            # cancel_all waits per worker sequentially — the first one
            # entering wait() proves we are inside the (lock-free) waits.
            assert workers[0].entered_wait.wait(2.0)
            assert all(w.cancelled for w in workers)
            acquired = mgr._lock.acquire(timeout=0.5)
            assert acquired, "cancel_all() held the lock during wait()"
            mgr._lock.release()
        finally:
            for w in workers:
                w.release.set()
            t.join(5.0)
        assert mgr._workers == {}


@pytest.fixture
def session_cache_state():
    saved_cache = dict(acm.AIComputeWorker._session_cache)
    saved_lru = list(acm.AIComputeWorker._session_lru)
    yield
    acm.AIComputeWorker._session_cache.clear()
    acm.AIComputeWorker._session_cache.update(saved_cache)
    acm.AIComputeWorker._session_lru.clear()
    acm.AIComputeWorker._session_lru.extend(saved_lru)


class TestAud7913SessionCacheBound:
    def test_cache_never_exceeds_cap(self, session_cache_state):
        cap = acm.AIComputeWorker.MAX_SESSION_CACHE
        for i in range(cap + 3):
            acm.AIComputeWorker._store_session(f"/m{i}.onnx", object())
        assert len(acm.AIComputeWorker._session_cache) <= cap
        assert len(acm.AIComputeWorker._session_lru) <= cap

    def test_lru_refresh_protects_recently_used(self, session_cache_state):
        cap = acm.AIComputeWorker.MAX_SESSION_CACHE
        for i in range(cap):
            acm.AIComputeWorker._store_session(f"/m{i}.onnx", f"s{i}")
        # Touch the oldest — it must survive the next insertion.
        assert acm.AIComputeWorker._cached_session("/m0.onnx") == "s0"
        acm.AIComputeWorker._store_session("/new.onnx", "new")
        assert "/m0.onnx" in acm.AIComputeWorker._session_cache
        assert "/m1.onnx" not in acm.AIComputeWorker._session_cache
        assert acm.AIComputeWorker._cached_session("/missing.onnx") is None


# ═══════════════════════════════════════════════════════════════════════
# AUD-80002 / AUD-80003 — export renderer entry points
# ═══════════════════════════════════════════════════════════════════════

def _stub_renderer(monkeypatch):
    r = CompositeExportRenderer()
    monkeypatch.setattr(r, "_ensure_gpu_resources", lambda: None)
    monkeypatch.setattr(r, "_sync_render_nodes", lambda snap: None)
    return r


class TestAud80002GpuHandleCleanup:
    def test_one_shot_handle_cleans_up(self, monkeypatch):
        r = _stub_renderer(monkeypatch)
        monkeypatch.setattr(r, "_evaluate_snapshot", lambda **kw: None)
        cleanups = []
        monkeypatch.setattr(r, "cleanup", lambda: cleanups.append(1))
        handle = r.render_gpu_handle(types.SimpleNamespace())
        assert handle.available is False
        assert cleanups == [1]

    def test_persistent_handle_keeps_resources(self, monkeypatch):
        r = _stub_renderer(monkeypatch)
        r.set_persistent_mode(True)
        monkeypatch.setattr(r, "_evaluate_snapshot", lambda **kw: None)
        cleanups = []
        monkeypatch.setattr(r, "cleanup", lambda: cleanups.append(1))
        r.render_gpu_handle(types.SimpleNamespace())
        assert cleanups == []


class _FakeEvalResult:
    is_valid = True
    texture_id = 3
    width = 8
    height = 8

    def __init__(self, aovs):
        self.aov_texture_ids = aovs


class _FakeSelected:
    is_valid = True
    texture_id = 3
    width = 8
    height = 8
    color_space = "linear"
    alpha_premultiplied = False


class TestAud80003PassesSrgbGuard:
    def _run_passes(self, monkeypatch, render_passes, aovs):
        r = _stub_renderer(monkeypatch)
        monkeypatch.setattr(r, "_evaluate_snapshot",
                            lambda **kw: _FakeEvalResult(aovs))
        monkeypatch.setattr(r, "_select_render_pass",
                            lambda result, name: _FakeSelected())
        formats = []

        def readback(selected):
            formats.append(r._readback_output)
            return np.zeros((8, 8, 3), dtype=np.uint8)

        monkeypatch.setattr(r, "_readback", readback)
        result = r.render_and_readback_passes(
            types.SimpleNamespace(), render_passes=render_passes,
            readback_output="srgb_u8")
        assert not result.error, result.error
        return formats, result

    def test_none_with_aovs_forces_linear(self, monkeypatch):
        formats, result = self._run_passes(
            monkeypatch, None, {"depth": 7, "object_id": 9})
        # beauty AND both AOVs were read through the linear path.
        assert formats == ["linear_f32"] * 3
        assert set(result.passes) == {"beauty", "depth", "object_id"}

    def test_none_without_aovs_keeps_srgb(self, monkeypatch):
        formats, _ = self._run_passes(monkeypatch, None, {})
        assert formats == ["srgb_u8"]

    def test_explicit_mixed_passes_forced_linear(self, monkeypatch):
        formats, _ = self._run_passes(
            monkeypatch, ("beauty", "depth"), {"depth": 7})
        assert formats == ["linear_f32"] * 2


# ═══════════════════════════════════════════════════════════════════════
# AUD-81001 / AUD-81002 — render output cancel/crash contracts
# ═══════════════════════════════════════════════════════════════════════

class _FakeEncoder:
    def __init__(self, *args, **kwargs):
        self.encoded = 0
        self.closed = False

    def open(self):
        pass

    def encode_frame(self, decoded):
        self.encoded += 1

    def close(self):
        self.closed = True


def _patch_parallel_render(monkeypatch, behavior):
    """Fake render_frames_parallel; behavior(frame_no) → packed or None,
    raises, or returns after setting the cancel flag. Worker exceptions are
    reported through on_worker_error and abort with ok=False, matching the
    real implementation."""
    import src.features.composite.render.parallel_frame_render as pfr

    def fake(frames, factory, sink, worker_count, is_cancelled=None,
             progress_cb=None, on_worker_error=None, factory_fallback=None,
             teardown_fn=None):
        for frame_no in frames:
            try:
                packed = behavior(frame_no)
            except Exception as exc:
                if on_worker_error is not None:
                    on_worker_error(str(exc))
                return False
            if packed is _CANCEL:
                return False
            sink(frame_no, packed)
        return True

    monkeypatch.setattr(pfr, "render_frames_parallel", fake)


_CANCEL = object()


class TestAud81001ParallelVideoCancelParity:
    def _stage(self, monkeypatch, cancel_after, total=6):
        out = CompositeRenderOutput()
        encoder = _FakeEncoder()
        monkeypatch.setattr(out, "_cleanup_parallel_renderers", lambda: None)
        flag = {"cancelled": False}

        def behavior(frame_no):
            if frame_no >= cancel_after:
                flag["cancelled"] = True
                return _CANCEL
            return (np.zeros((4, 4, 3), np.uint8), "bgr24")

        _patch_parallel_render(monkeypatch, behavior)
        result = out._run_parallel_video_stage(
            graph_data={}, encoder=encoder, frames=list(range(total)),
            start_frame=0, fps=24.0, is_hdr=False, output_descriptor=None,
            edit_project=None, timeline_sequences=None, comp_clip=None,
            render_pass="beauty", worker_count=2,
            is_cancelled=lambda: flag["cancelled"], progress_cb=None,
            frame_width=4, frame_height=4, output_path="/tmp/_gone.mp4")
        return result, encoder

    def test_midstream_cancel_keeps_artifact_as_partial(
            self, monkeypatch, tmp_path):
        # cancel_after=3 → 3 frames encoded, then cancel.
        # R-08 契约更新：已写部分帧的取消保留成果（AUD-81001 的保留
        # 语义不变）但报告 partial、不报 success——"已写一帧就报告完成"
        # 是 2026-10-14 审计 R-08 缺陷。
        result, encoder = self._stage(monkeypatch, cancel_after=3)
        assert result.success is False
        assert result.status == "partial"
        assert result.frames_written == 3
        assert result.output_path == "/tmp/_gone.mp4"
        assert result.error == "EXPORT_CANCELLED"
        assert encoder.closed is True

    def test_zero_frame_cancel_reports_cancelled(self, monkeypatch):
        result, encoder = self._stage(monkeypatch, cancel_after=0)
        assert result.success is False
        assert result.error == "EXPORT_CANCELLED"
        assert encoder.closed is True

    def test_worker_error_reports_failure(self, monkeypatch, tmp_path):
        out = CompositeRenderOutput()
        encoder = _FakeEncoder()
        removed = []
        monkeypatch.setattr(out, "_cleanup_parallel_renderers", lambda: None)
        monkeypatch.setattr(out, "_safe_remove",
                            staticmethod(lambda p: removed.append(p)))

        def behavior(frame_no):
            if frame_no == 1:
                raise RuntimeError("GL explosion")
            return (np.zeros((4, 4, 3), np.uint8), "bgr24")

        _patch_parallel_render(monkeypatch, behavior)
        result = out._run_parallel_video_stage(
            graph_data={}, encoder=encoder, frames=list(range(4)),
            start_frame=0, fps=24.0, is_hdr=False, output_descriptor=None,
            edit_project=None, timeline_sequences=None, comp_clip=None,
            render_pass="beauty", worker_count=2,
            is_cancelled=lambda: False, progress_cb=None,
            frame_width=4, frame_height=4, output_path="/tmp/_gone.mp4")
        assert result.success is False
        assert "GL explosion" in result.error
        # R-08：有成功帧的 worker 错误是 partial（成果保留供诊断/续传），
        # 不再删除文件。
        assert result.status == "partial"
        assert removed == []

    def test_serial_video_midstream_cancel_is_partial(self, monkeypatch,
                                                      tmp_path):
        """Lock the serial contract the parallel stage was aligned to."""
        import src.shared.export.video_encoder as ve
        import src.shared.color.color_transform_provider as ctp

        monkeypatch.setattr(ve, "FFmpegVideoEncoder", _FakeEncoder)
        monkeypatch.setattr(
            ctp, "transform_color",
            lambda arr, src, dst, config_manager=None: arr)
        out = CompositeRenderOutput()
        monkeypatch.setattr(out, "_resolve_parallel_frames", lambda n: 1)
        flag = {"v": False}

        def render_one(graph_data, timeline_frame, **kw):
            if timeline_frame >= 1:
                flag["v"] = True
                return None  # cancel lands mid-stream; frame 1 renders None
            return np.full((4, 4, 3), 0.5, np.float32)

        monkeypatch.setattr(out, "_render_one_frame", render_one)
        result = out.render_video(
            graph_data={}, output_path=str(tmp_path / "v.mp4"),
            width=4, height=4, fps=24.0, start_frame=0, end_frame=3,
            is_cancelled=lambda: flag["v"])
        # R-08 契约更新：串行中途取消与并行同一合同——partial（保留
        # 成果、不报成功）。(Frame 1 is duplicated per AUD-81004's
        # timeline preservation; the cancel then breaks the loop at frame 2.)
        assert result.success is False
        assert result.status == "partial"
        assert result.frames_written == 2
        assert result.frames_dropped == 1
        assert result.error == "EXPORT_CANCELLED"


class TestAud81001ParallelSequenceCancelParity:
    def _stage(self, monkeypatch, tmp_path, cancel_after):
        from src.shared.io.file_io import FileIO

        out = CompositeRenderOutput()
        written = []  # the stage's _sink appends every saved frame here

        def fake_save(path, img, options=None):
            with open(path, "wb") as f:
                f.write(b"x")
            return True

        monkeypatch.setattr(FileIO, "save_image",
                            staticmethod(fake_save))
        flag = {"cancelled": False}

        def behavior(frame_no):
            if frame_no >= cancel_after:
                flag["cancelled"] = True
                return _CANCEL
            return np.zeros((4, 4, 3), np.uint8)

        _patch_parallel_render(monkeypatch, behavior)
        result = out._run_parallel_sequence_stage(
            graph_data={}, output_dir=str(tmp_path), base_name="f",
            ext="png", start_frame=0, end_frame=5, pad_width=4,
            image_options=None, output_descriptor=None, edit_project=None,
            timeline_sequences=None, comp_clip=None, render_pass="beauty",
            worker_count=2, is_cancelled=lambda: flag["cancelled"],
            progress_cb=None, written_ref=written)
        return result, written

    def test_midstream_cancel_keeps_frames(self, monkeypatch, tmp_path):
        # R-08 契约更新：partial 保留已写帧、不报成功。
        result, written = self._stage(monkeypatch, tmp_path, cancel_after=3)
        assert result.success is False
        assert result.status == "partial"
        assert result.frames_written == 3
        assert result.error == ""
        assert len(written) == 3
        assert all(os.path.exists(p) for p in written)

    def test_zero_frame_cancel_removes_nothing_and_reports(self, monkeypatch,
                                                           tmp_path):
        result, written = self._stage(monkeypatch, tmp_path, cancel_after=0)
        assert result.success is False
        assert result.error == "EXPORT_CANCELLED"
        assert written == []


class TestAud81002SequenceExceptionCleanup:
    def test_serial_exception_removes_written_frames(self, monkeypatch,
                                                     tmp_path):
        from src.shared.io.file_io import FileIO
        out = CompositeRenderOutput()
        monkeypatch.setattr(out, "_resolve_parallel_frames", lambda n: 1)

        def fake_save(path, img, options=None):
            with open(path, "wb") as f:
                f.write(b"x")
            return True

        monkeypatch.setattr(FileIO, "save_image",
                            staticmethod(fake_save))

        def render_one(graph_data, timeline_frame, **kw):
            if timeline_frame == 0:
                return np.zeros((4, 4, 3), np.float32)
            raise RuntimeError("GL explosion mid-sequence")

        monkeypatch.setattr(out, "_render_one_frame", render_one)
        out_dir = tmp_path / "seq"
        result = out.render_image_sequence(
            graph_data={}, output_dir=str(out_dir), base_name="f",
            ext="png", start_frame=0, end_frame=3)
        assert result.success is False
        assert "GL explosion mid-sequence" in result.error
        # Frame 0 was on disk when the crash hit — it must be gone now,
        # and the emptied directory is removed too.
        assert not out_dir.exists()

    def test_parallel_stage_exception_removes_written_frames(
            self, monkeypatch, tmp_path):
        from src.shared.io.file_io import FileIO
        out = CompositeRenderOutput()
        written = []  # the stage's _sink appends every saved frame here

        def fake_save(path, img, options=None):
            with open(path, "wb") as f:
                f.write(b"x")
            return True

        monkeypatch.setattr(FileIO, "save_image",
                            staticmethod(fake_save))
        import src.features.composite.render.parallel_frame_render as pfr

        def fake_parallel(frames, factory, sink, worker_count,
                          is_cancelled=None, progress_cb=None,
                          on_worker_error=None, factory_fallback=None,
                          teardown_fn=None):
            sink(frames[0], np.zeros((4, 4, 3), np.uint8))
            raise RuntimeError("encoder died")

        monkeypatch.setattr(pfr, "render_frames_parallel", fake_parallel)
        out_dir = tmp_path / "seqp"
        out_dir.mkdir()
        result = out._run_parallel_sequence_stage(
            graph_data={}, output_dir=str(out_dir), base_name="f",
            ext="png", start_frame=0, end_frame=3, pad_width=4,
            image_options=None, output_descriptor=None, edit_project=None,
            timeline_sequences=None, comp_clip=None, render_pass="beauty",
            worker_count=2, is_cancelled=lambda: False, progress_cb=None,
            written_ref=written)
        assert result.success is False
        assert "encoder died" in result.error
        # The frame the sink wrote before the crash is gone, and the
        # emptied directory is removed too.
        assert not out_dir.exists()
