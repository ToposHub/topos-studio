"""wd6-g38 批 D 修复回归测试（composite render 基础设施域, 16 条 Major）。

覆盖 REMEDIATION_PLAN 批 D 条目：
  AUD-81003 / AUD-81004 / AUD-81010 / AUD-8201 / AUD-8202 / AUD-8211 /
  AUD-8212 / AUD-8300 / AUD-8306 / AUD-8307 / AUD-8308 / AUD-8309 /
  AUD-8310 / AUD-8311 / AUD-8312 / AUD-8400

全部 headless：无 GL 上下文依赖（fake fb_manager / fake pool / mock ctx）。
"""

import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import numpy as np
import pytest


# ═══════════════════════════════════════════════════════════════════════
# AUD-8400 — pbo_readback duplicate method definitions
# ═══════════════════════════════════════════════════════════════════════

def test_aud_8400_poll_ready_defined_once():
    from src.features.composite.render.pbo_readback import AsyncPBOReadback
    source = inspect.getsource(AsyncPBOReadback)
    assert source.count("def poll_ready(") == 1
    assert source.count("def get_latest(") == 1


# ═══════════════════════════════════════════════════════════════════════
# AUD-8300 — disk_cache_store getsize race
# ═══════════════════════════════════════════════════════════════════════

def test_aud_8300_getsize_race_returns_false_and_cleans(tmp_path, monkeypatch):
    from src.features.composite.render.disk_cache_store import DiskCacheStore

    store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1)
    rgba = np.zeros((4, 4, 4), np.float32)

    def _racy_getsize(path):
        # Simulate a concurrent clear() deleting the payload between the
        # os.replace and the size probe.
        store.clear()
        raise FileNotFoundError(path)

    monkeypatch.setattr("os.path.getsize", _racy_getsize)
    assert store.store("key1", rgba) is False
    assert store.has("key1") is False


def test_aud_8300_store_still_succeeds_without_race(tmp_path):
    from src.features.composite.render.disk_cache_store import DiskCacheStore

    store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1)
    rgba = np.ones((4, 4, 4), np.float32)
    assert store.store("key2", rgba) is True
    assert store.has("key2") is True


# ═══════════════════════════════════════════════════════════════════════
# AUD-8306 / 8307 / 8308 / 8309 — gl_utils
# ═══════════════════════════════════════════════════════════════════════

def test_aud_8306_upload_uses_real_pool_api():
    from src.features.composite.render import gl_utils

    acquired = []
    written = []
    tex = SimpleNamespace(glo=77, write=lambda b: written.append(b))
    pool = SimpleNamespace(
        acquire_texture=lambda w, h, c, dtype:
            (acquired.append((w, h, c, dtype)), tex)[1])

    ctx = SimpleNamespace(version="4.1 core")  # not a mock context
    img = np.ones((4, 6, 3), np.float32)

    texture_id = gl_utils.upload_numpy_texture(
        ctx, img, fb_manager=None, resource_pool=pool)

    assert texture_id == 77
    # Pool path must actually hit, with the pool's real signature.
    assert acquired == [(6, 4, 3, "f4")]
    assert written and len(written[0]) == 6 * 4 * 3 * 4


def test_aud_8306_pool_failure_raises_instead_of_returning_minus_one():
    from src.features.composite.render import gl_utils

    def _boom(*_a, **_k):
        raise RuntimeError("VRAM exhausted")

    pool = SimpleNamespace(acquire_texture=_boom)
    with pytest.raises(RuntimeError):
        gl_utils.upload_numpy_texture(
            SimpleNamespace(version="4.1"),
            np.zeros((2, 2, 3), np.float32), None, pool)


def test_aud_8307_download_restores_caller_fbo_binding(monkeypatch):
    from src.features.composite.render import gl_utils

    restored = []
    monkeypatch.setattr(gl_utils, "_get_framebuffer_binding", lambda: 42)
    monkeypatch.setattr(gl_utils, "_restore_framebuffer_binding",
                        lambda fbo: restored.append(fbo))

    screen_use = Mock()
    ctx = SimpleNamespace(
        version="4.1",
        viewport=(0, 0, 8, 8),
        screen=SimpleNamespace(use=screen_use),
        texture=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        gl_utils.download_texture(1, 4, 4, ctx=ctx)

    assert restored == [42]              # caller's binding restored
    screen_use.assert_not_called()       # no unconditional ctx.screen rebind


def test_aud_8308_contract_declares_rgb_not_bgr():
    from src.features.composite.render import gl_utils

    down_src = inspect.getsource(gl_utils.download_texture)
    up_src = inspect.getsource(gl_utils.upload_numpy_texture)
    assert "BGR float32" not in down_src
    assert "BGR/BGRA" not in up_src
    assert "RGB float32" in down_src
    assert "RGB/RGBA float32" in up_src


def test_aud_8309_mock_path_still_returns_zero_array():
    from src.features.composite.render import gl_utils

    arr = gl_utils.download_texture(1, 4, 4, ctx=None)
    assert arr.shape == (4, 4, 3)
    assert not arr.any()


# ═══════════════════════════════════════════════════════════════════════
# Fake GL plumbing for gpu_resource_pool tests
# ═══════════════════════════════════════════════════════════════════════

class _FakeTex:
    _counter = 0

    def __init__(self):
        _FakeTex._counter += 1
        self.glo = _FakeTex._counter
        self.released = False

    def release(self):
        self.released = True


class _FakeFBManager:
    def __init__(self):
        self._fbo_cache = {}
        self._active_textures = set()

    def get_temp_texture(self, w, h, c, dtype):
        tex = _FakeTex()
        self._active_textures.add(tex)
        return tex

    def release_temp_texture(self, tex):
        self._active_textures.discard(tex)


def _make_pool(enable_video_slots: bool = False):
    from src.features.composite.render.gpu_resource_pool import GPUResourcePool
    return GPUResourcePool(
        _FakeFBManager(), max_memory_mb=256,
        enable_vram_monitoring=False,
        enable_video_slots=enable_video_slots)


# ═══════════════════════════════════════════════════════════════════════
# AUD-8310 — small-pool LRU eviction must restore pinned entries
# ═══════════════════════════════════════════════════════════════════════

def test_aud_8310_pinned_entry_restored_to_lru():
    pool = _make_pool()
    tex_a = pool.acquire_temp_texture(4, 4)
    tex_b = pool.acquire_temp_texture(4, 4)
    pool.release_texture(tex_a)          # oldest pooled entry
    pool.release_texture(tex_b)
    pool.pin_texture(tex_a.glo)          # pin the OLDEST entry
    assert len(pool._pool_lru) == 2

    # Ask for more bytes than the whole small pool holds.
    pooled_bytes = sum(info.bytes for info in pool._pool_lru.values())
    pool._evict_to_free(pooled_bytes)

    # B (unpinned) got evicted for real.
    assert tex_b.glo not in pool._pool_lru
    assert tex_b.released is True
    # A was refused but RESTORED — still visible to eviction + accounting
    # instead of lingering as an undeletable orphan in _pooled_textures.
    assert tex_a.glo in pool._pool_lru
    assert any(tex_a in lst for lst in pool._pooled_textures.values())
    assert tex_a.released is False


# ═══════════════════════════════════════════════════════════════════════
# AUD-8311 — dtype-aware video slot release/clear
# ═══════════════════════════════════════════════════════════════════════

def test_aud_8311_export_slot_released_through_legacy_f2_call():
    from src.features.composite.render.gpu_resource_pool import RenderMode

    pool = _make_pool(enable_video_slots=True)
    pool.acquire_video_slot(64, 32, 4, slot_id=0, render_mode=RenderMode.EXPORT)
    # EXPORT derives dtype 'f4' — the f4-keyed slot exists.
    assert (64, 32, 4, "f4", 0) in pool._video_slot_pool._slots

    # release/clear with the legacy default-'f2' key construction must
    # still resolve and clear the f4 slot (previously leaked forever).
    pool.release_video_slot(64, 32, 4, slot_id=0)
    pool.clear_video_slot(64, 32, 4, slot_id=0)
    assert pool._video_slot_pool.get_slot_count() == 0


def test_aud_8311_preview_slot_release_unchanged():
    pool = _make_pool(enable_video_slots=True)
    pool.acquire_video_slot(64, 32, 4, slot_id=1)  # preview -> 'f2'
    assert (64, 32, 4, "f2", 1) in pool._video_slot_pool._slots
    pool.clear_video_slot(64, 32, 4, slot_id=1)
    assert pool._video_slot_pool.get_slot_count() == 0


# ═══════════════════════════════════════════════════════════════════════
# AUD-8312 — VideoSlotPool reclamation + cap
# ═══════════════════════════════════════════════════════════════════════

def test_aud_8312_idle_slots_reclaimed_after_threshold():
    from src.features.composite.render.gpu_resource_pool import (
        VIDEO_SLOT_IDLE_FRAMES, VideoSlotPool)

    released = []

    class _RP:
        def acquire_temp_texture(self, w, h, c, dtype):
            return _FakeTex()

        def release_texture(self, tex):
            released.append(tex)

    vsp = VideoSlotPool(_FakeFBManager(), _RP())
    vsp.begin_frame()
    vsp.acquire_slot(64, 64)
    vsp.end_frame()
    for _ in range(VIDEO_SLOT_IDLE_FRAMES - 1):
        vsp.begin_frame()
        vsp.end_frame()
    # Below the threshold the slot stays for fast reuse.
    assert vsp.get_slot_count() == 1
    vsp.begin_frame()
    vsp.end_frame()
    assert vsp.get_slot_count() == 0
    assert len(released) == 1


def test_aud_8312_slot_cap_evicts_idle_slots(monkeypatch):
    import src.features.composite.render.gpu_resource_pool as grp
    from src.features.composite.render.gpu_resource_pool import VideoSlotPool

    monkeypatch.setattr(grp, "MAX_VIDEO_SLOTS", 2)
    released = []

    class _RP:
        def acquire_temp_texture(self, w, h, c, dtype):
            return _FakeTex()

        def release_texture(self, tex):
            released.append(tex)

    vsp = VideoSlotPool(_FakeFBManager(), _RP())
    vsp.begin_frame()
    vsp.acquire_slot(64, 64, slot_id=0)
    vsp.acquire_slot(64, 64, slot_id=1)
    vsp.end_frame()

    vsp.begin_frame()
    vsp.acquire_slot(32, 32, slot_id=0)  # new geometry forces a new slot
    vsp.end_frame()

    assert vsp.get_slot_count() <= 2
    assert len(released) == 1  # one idle 64x64 slot was reclaimed


# ═══════════════════════════════════════════════════════════════════════
# AUD-8211 / AUD-8212 — compute_shader_manager
# ═══════════════════════════════════════════════════════════════════════

class _FakeUniform:
    def __init__(self):
        self.value = None


class _FakeComputeProg:
    def __init__(self):
        self._uniforms = {}
        self.runs = []

    def __getitem__(self, name):
        return self._uniforms.setdefault(name, _FakeUniform())

    def run(self, gx, gy, gz):
        self.runs.append((gx, gy, gz))


def _bare_manager(ctx):
    from src.features.composite.render.compute_shader_manager import (
        ComputeShaderManager)
    mgr = object.__new__(ComputeShaderManager)
    mgr._ctx = ctx
    mgr._metal_bridge = None
    return mgr


def test_aud_8211_memory_barrier_issued_after_blur_dispatch():
    import moderngl
    from src.features.composite.render.compute_shader_manager import (
        ComputeShaderManager)

    barriers = []
    mgr = _bare_manager(
        SimpleNamespace(memory_barrier=lambda bits: barriers.append(bits)))
    prog = _FakeComputeProg()
    input_tex = SimpleNamespace(use=lambda unit: None)
    output_tex = SimpleNamespace(bind_to_image=lambda *a, **k: None)

    ComputeShaderManager._run_blur_pass(
        mgr, prog, input_tex, output_tex,
        radius=20.0, w=64, h=64, edge_policy=0)

    assert prog.runs == [(4, 4, 1)]
    assert barriers == [moderngl.TEXTURE_FETCH_BARRIER_BIT
                        | moderngl.SHADER_IMAGE_ACCESS_BARRIER_BIT]


def test_aud_8211_memory_barrier_missing_api_is_tolerated():
    from src.features.composite.render.compute_shader_manager import (
        ComputeShaderManager)

    mgr = _bare_manager(SimpleNamespace())  # no memory_barrier attr
    prog = _FakeComputeProg()
    ComputeShaderManager._run_blur_pass(
        mgr, prog, SimpleNamespace(use=lambda u: None),
        SimpleNamespace(bind_to_image=lambda *a, **k: None),
        radius=20.0, w=64, h=64, edge_policy=0)
    assert prog.runs  # dispatch still happened


def test_aud_8212_mirror_policy_declines_compute_path():
    from src.features.composite.render.compute_shader_manager import (
        ComputeShaderManager)

    mgr = _bare_manager(SimpleNamespace())
    mgr._available = True
    result = ComputeShaderManager.apply_blur_compute(
        mgr, SimpleNamespace(), radius=50.0, w=64, h=64,
        resource_pool=None, edge_policy=2)
    assert result is None  # fragment fallback implements mirror


def test_aud_8212_clamp_and_wrap_still_eligible():
    from src.features.composite.render.compute_shader_manager import (
        ComputeShaderManager)

    mgr = _bare_manager(SimpleNamespace())
    mgr._available = False
    mgr._metal_bridge = None
    # Non-mirror policies must not be gated by the mirror guard: with no
    # backend available they return None via the availability check, not
    # via the mirror warning — distinguish by resetting the once-warned set.
    from src.features.composite.render import compute_shader_manager as csm
    csm._UNAVAILABLE_WARNING_KEYS.discard("mirror-edge-compute-blur")
    ComputeShaderManager.apply_blur_compute(
        mgr, SimpleNamespace(), radius=50.0, w=64, h=64,
        resource_pool=None, edge_policy=0)
    assert "mirror-edge-compute-blur" not in csm._UNAVAILABLE_WARNING_KEYS


# ═══════════════════════════════════════════════════════════════════════
# AUD-81010 — exporter one-shot guard
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def qcore_app():
    from PyQt6.QtCore import QCoreApplication
    return QCoreApplication.instance() or QCoreApplication([])


def test_aud_81010_export_after_termination_refused(qcore_app, monkeypatch):
    import src.features.composite.render.composite_render_worker as crw

    exporter = crw.CompositeRenderExporter()
    try:
        invoked = []
        monkeypatch.setattr(
            crw, "QMetaObject",
            SimpleNamespace(invokeMethod=lambda *a, **k: invoked.append(a)))
        errors = []

        exporter._terminated = True
        exporter.export(settings={}, context={}, on_error=errors.append)

        assert invoked == []  # no run posted onto a quit/deleteLater'd worker
        assert len(errors) == 1 and "terminated" in errors[0]
    finally:
        exporter.cleanup(timeout_ms=2000)


def test_aud_81010_terminal_slots_flip_terminated(qcore_app):
    import src.features.composite.render.composite_render_worker as crw

    exporter = crw.CompositeRenderExporter()
    try:
        assert exporter.is_terminated is False
        exporter._on_worker_finished("/tmp/out.mp4")
        assert exporter.is_terminated is True
        assert exporter.is_exporting is False
    finally:
        exporter.cleanup(timeout_ms=2000)


# ═══════════════════════════════════════════════════════════════════════
# AUD-8201 — transient texture release on AI/3D fast paths
# ═══════════════════════════════════════════════════════════════════════

def _ai_fake_self(valid: bool):
    emitted = []
    rendered = []
    released = []
    fake = SimpleNamespace(
        _initialized=True,
        _snapshot=SimpleNamespace(),
        _clear_eval_cache=lambda: None,
        _evaluate_snapshot=lambda **k: SimpleNamespace(is_valid=valid),
        _render_to_output=lambda *a, **k: rendered.append(k.get("request_id")),
        frame_ready=SimpleNamespace(emit=lambda *a: emitted.append(a)),
        # AUD-48006 帧推进改造后走公开访问器 latest_request_id
        _scheduler=SimpleNamespace(latest_request_id=9),
        _release_transient_textures=lambda: released.append(1),
    )
    return fake, emitted, rendered, released


def test_aud_8201_ai_result_ready_invalid_path_releases_transient():
    from src.features.composite.render.composite_renderer import CompositeRenderer

    fake, emitted, _rendered, released = _ai_fake_self(valid=False)
    CompositeRenderer._handle_ai_result_ready(fake, {})
    assert emitted == [(9, 0, 0, 0)]
    assert released == [1]


def test_aud_8201_ai_result_ready_valid_path_releases_transient():
    from src.features.composite.render.composite_renderer import CompositeRenderer

    fake, _emitted, rendered, released = _ai_fake_self(valid=True)
    CompositeRenderer._handle_ai_result_ready(fake, {})
    assert rendered == [9]
    assert released == [1]


def test_aud_8201_render_3d_fast_path_releases_transient():
    from src.features.composite.render.composite_renderer import CompositeRenderer

    source = inspect.getsource(CompositeRenderer._handle_render_3d_view)
    assert "_release_transient_textures" in source
    assert "finally:" in source


# ═══════════════════════════════════════════════════════════════════════
# AUD-8202 — PREFETCH_EVALUATE real implementation
# ═══════════════════════════════════════════════════════════════════════

class _RecordingCacheManager:
    _SOURCE_NODE_TYPES = frozenset({"image_input"})

    def __init__(self):
        self.puts = []

    def put(self, key, entry, *args, **kwargs):
        self.puts.append((key, kwargs))


def _prefetch_fixture():
    from src.features.composite.core.graph_snapshot import (
        GraphSnapshot, NodeSnapshot)
    from src.features.composite.render.render_scheduler import PrefetchRequest

    node = NodeSnapshot(
        node_id="blur1", node_type="blur", parameters={"radius": 5.0},
        disabled=False, input_ports=[], output_ports=[])
    snapshot = GraphSnapshot(generation=1, nodes={"blur1": node})
    request = PrefetchRequest(
        snapshot=snapshot, request_id=11, node_id="blur1",
        param_name="radius", predicted_value=7.0, confidence=0.9)
    return snapshot, request


def test_aud_8202_prefetch_evaluates_predicted_snapshot():
    from src.features.composite.render.cache_manager import CacheCategory
    from src.features.composite.render.composite_renderer import CompositeRenderer

    snapshot, request = _prefetch_fixture()
    cache_mgr = _RecordingCacheManager()
    seen = {}

    def _evaluate_snapshot(**kwargs):
        seen["snapshot"] = fake._snapshot
        seen["request_id"] = kwargs.get("request_id")
        cache_mgr.put("blur:predicted-key", SimpleNamespace())
        return None

    fake = SimpleNamespace(
        _initialized=True,
        _snapshot=snapshot,
        _cache_manager=cache_mgr,
        _evaluate_snapshot=_evaluate_snapshot,
        _release_transient_textures=Mock(),
    )

    CompositeRenderer._handle_prefetch_evaluate(
        fake, {"prefetch_request": request})

    # The evaluation ran against the PREDICTED parameter value...
    assert seen["request_id"] == 11
    assert seen["snapshot"].nodes["blur1"].parameters["radius"] == 7.0
    # ...without mutating the original snapshot...
    assert snapshot.nodes["blur1"].parameters["radius"] == 5.0
    # ...and the renderer state was restored.
    assert fake._snapshot is snapshot
    assert fake._prefetch_mode is False
    fake._release_transient_textures.assert_called_once()

    # Cache writes made during the window are tagged PREFETCH.
    assert cache_mgr.puts[0][0] == "blur:predicted-key"
    assert cache_mgr.puts[0][1]["category"] is CacheCategory.PREFETCH
    # SOURCE-node writes keep their auto category (no demotion).
    cache_mgr.put("image_input:src", SimpleNamespace())
    assert "category" not in cache_mgr.puts[-1][1]


def test_aud_8202_prefetch_unknown_param_is_a_noop():
    from src.features.composite.render.composite_renderer import CompositeRenderer
    from src.features.composite.render.render_scheduler import PrefetchRequest
    from src.features.composite.core.graph_snapshot import (
        GraphSnapshot, NodeSnapshot)

    node = NodeSnapshot(
        node_id="n1", node_type="grade", parameters={"gain": 1.0},
        disabled=False, input_ports=[], output_ports=[])
    snapshot = GraphSnapshot(generation=2, nodes={"n1": node})
    request = PrefetchRequest(
        snapshot=snapshot, request_id=12, node_id="n1",
        param_name="does_not_exist", predicted_value=2.0, confidence=0.9)

    evaluate = Mock()
    fake = SimpleNamespace(
        _initialized=True, _snapshot=snapshot,
        _cache_manager=_RecordingCacheManager(),
        _evaluate_snapshot=evaluate,
        _release_transient_textures=Mock())

    CompositeRenderer._handle_prefetch_evaluate(
        fake, {"prefetch_request": request})
    evaluate.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# AUD-81003 — thread-affine parallel renderer safety net
# ═══════════════════════════════════════════════════════════════════════

def test_aud_81003_safety_net_never_cleans_cross_thread():
    from src.features.composite.render.composite_render_output import (
        CompositeRenderOutput)

    out = CompositeRenderOutput()
    cleaned = []
    renderer = SimpleNamespace(cleanup=lambda: cleaned.append("r1"))
    out._parallel_renderers[(1, 0)] = renderer
    out._parallel_in_flight[(1, 0)] = 1

    out._cleanup_parallel_renderers()

    assert cleaned == []  # no GL cleanup on the caller thread
    assert out._parallel_renderers == {}
    assert (1, 0) in out._abandoned_parallel_renderers

    # The worker's own (late) teardown still releases it on its thread.
    out._teardown_parallel_renderer(1, 0)
    assert cleaned == ["r1"]
    assert (1, 0) not in out._abandoned_parallel_renderers


def test_aud_81003_teardown_is_generation_scoped():
    from src.features.composite.render.composite_render_output import (
        CompositeRenderOutput)

    out = CompositeRenderOutput()
    cleaned = []
    out._parallel_renderers[(1, 0)] = SimpleNamespace(
        cleanup=lambda: cleaned.append("stage1"))
    out._parallel_renderers[(2, 0)] = SimpleNamespace(
        cleanup=lambda: cleaned.append("stage2"))

    # A stuck stage-1 worker tearing down after stage 2 registered its own
    # renderer must only destroy its own.
    out._teardown_parallel_renderer(1, 0)
    assert cleaned == ["stage1"]
    assert (2, 0) in out._parallel_renderers


# ═══════════════════════════════════════════════════════════════════════
# AUD-81004 — None frames no longer compress the video timeline
# ═══════════════════════════════════════════════════════════════════════

def _install_serial_video_env(monkeypatch, tmp_path, frame_results):
    import src.shared.export.video_encoder as ve
    import src.shared.color.color_transform_provider as ctp
    from src.features.composite.render.composite_render_output import (
        CompositeRenderOutput)

    encoded = []

    class _FakeEncoder:
        def __init__(self, path, cfg):
            pass

        def open(self):
            pass

        def encode_frame(self, decoded):
            encoded.append(decoded)

        def close(self):
            pass

    monkeypatch.setattr(ve, "FFmpegVideoEncoder", _FakeEncoder)
    monkeypatch.setattr(
        ctp, "transform_color",
        lambda arr, src, dst, config_manager=None: arr)
    monkeypatch.setattr(CompositeRenderOutput, "_resolve_parallel_frames",
                        lambda self, n: 1)

    def _render_one_frame(self, graph_data, timeline_frame, **kwargs):
        return frame_results[timeline_frame]

    monkeypatch.setattr(CompositeRenderOutput, "_render_one_frame",
                        _render_one_frame)
    out = CompositeRenderOutput()
    return out, encoded


def test_aud_81004_serial_midstream_none_duplicated_not_skipped(
        tmp_path, monkeypatch):
    out, encoded = _install_serial_video_env(
        monkeypatch, tmp_path,
        {0: np.full((4, 4, 3), 0.25, np.float32),
         1: None,
         2: np.full((4, 4, 3), 0.75, np.float32)})

    result = out.render_video(
        graph_data={}, output_path=str(tmp_path / "v.mp4"),
        width=4, height=4, fps=24.0, start_frame=0, end_frame=2)

    assert result.success is True
    assert result.frames_written == 3          # duration preserved
    assert result.frames_dropped == 1
    assert [d.pts for d in encoded] == [0, 1, 2]  # timeline not compressed
    assert np.array_equal(encoded[1].data, encoded[0].data)  # duplicated


def test_aud_81004_serial_first_frame_none_gets_black_placeholder(
        tmp_path, monkeypatch):
    out, encoded = _install_serial_video_env(
        monkeypatch, tmp_path,
        {0: None,
         1: np.full((4, 4, 3), 0.5, np.float32)})

    result = out.render_video(
        graph_data={}, output_path=str(tmp_path / "v.mp4"),
        width=4, height=4, fps=24.0, start_frame=0, end_frame=1)

    assert result.success is True
    assert result.frames_written == 2
    assert result.frames_dropped == 1
    assert encoded[0].pixel_format == "bgr24"
    assert not encoded[0].data.any()  # black placeholder keeps the slot


def test_aud_81004_result_dataclass_has_frames_dropped_default():
    from src.features.composite.render.composite_render_output import (
        RenderOutputResult)
    result = RenderOutputResult(success=True)
    assert result.frames_dropped == 0
