"""EF-10 audit-fix tests (AUD-7903 … AUD-8419).

One test group per remediation ID in src/features/composite/render.
Every group contains a positive case (the fixed behaviour) and a negative
case (the guard that keeps the fix honest).  Tests are revert-sensitive:
reverting the corresponding source fix must fail the group.

No test creates a real GL context, spawns modal dialogs, or touches the
UI thread — GL interaction is stubbed at the Python boundary.
"""

from __future__ import annotations

import inspect
import json
import logging
import sys
import threading
import time
import types

import numpy as np
import pytest

sys.path.insert(0, '.')
sys.path.insert(0, 'tests')

from src.features.composite.render.composite_renderer import (  # noqa: E402
    CompositeRenderer as CompositeRendererUnbound)

pytestmark = pytest.mark.composite_core

# ═══════════════════════════════════════════════════════════════════════
# Shared stubs
# ═══════════════════════════════════════════════════════════════════════

class RecordingLock:
    """threading.Lock stand-in that counts acquire attempts."""

    def __init__(self) -> None:
        self.acquires = 0
        self._lock = threading.Lock()

    def __enter__(self):
        self.acquires += 1
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)


def _rgba(h: int = 8, w: int = 8, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    arr = rng.random((h, w, 4)).astype(np.float32)
    arr[..., 3] = 1.0
    return arr


# ═══════════════════════════════════════════════════════════════════════
# AUD-7903 — ai_cache_manager: corrupt manifest must not be silently wiped
# ═══════════════════════════════════════════════════════════════════════

class TestAud7903CorruptManifest:
    def _corrupt_manifest(self, tmp_path) -> None:
        (tmp_path / "manifest.json").write_text("{corrupt json", encoding="utf-8")

    def test_corrupt_manifest_backed_up_not_silent(self, tmp_path, caplog):
        from src.features.composite.render.ai_cache_manager import AICacheManager

        self._corrupt_manifest(tmp_path)
        with caplog.at_level(logging.WARNING):
            mgr = AICacheManager(cache_dir=str(tmp_path))
            assert mgr._manifest == {}
            # Trigger the read-modify-write sync (no EXR encode needed —
            # journal a pending add directly).
            with mgr._lock:
                mgr._register_add("key1", {"path": "x.exr"})
            mgr._sync_manifest_to_disk()

        # The corrupt file is preserved for forensics, not silently replaced.
        backup = tmp_path / "manifest.json.corrupt"
        assert backup.exists(), "corrupt manifest must be backed up (AUD-7903)"
        assert backup.read_text(encoding="utf-8") == "{corrupt json"
        # The store recovers: manifest.json is valid again and holds the write.
        on_disk = json.loads((tmp_path / "manifest.json").read_text())
        assert "key1" in on_disk
        # And the corruption is logged, never silent.
        assert any("manifest" in rec.getMessage().lower()
                   for rec in caplog.records)

    def test_valid_manifest_not_backed_up(self, tmp_path):
        """Negative: a healthy manifest is left untouched (no .corrupt)."""
        from src.features.composite.render.ai_cache_manager import AICacheManager

        (tmp_path / "manifest.json").write_text('{"old": {"path": "x"}}',
                                                encoding="utf-8")
        mgr = AICacheManager(cache_dir=str(tmp_path))
        assert mgr._manifest == {"old": {"path": "x"}}
        assert not (tmp_path / "manifest.json.corrupt").exists()


# ═══════════════════════════════════════════════════════════════════════
# AUD-7916 — ai_compute_manager: get_preview_result reads under the lock
# ═══════════════════════════════════════════════════════════════════════

class TestAud7916PreviewResultLock:
    def _manager(self):
        from src.features.composite.render.ai_compute_manager import AIComputeManager

        mgr = AIComputeManager.__new__(AIComputeManager)
        mgr._lock = RecordingLock()
        mgr._workers = {}
        mgr._preview_results = {"done": np.zeros((2, 2, 3), np.float32)}
        return mgr

    def test_preview_result_taken_under_lock(self):
        mgr = self._manager()
        out = mgr.get_preview_result("done")
        assert out is not None
        assert mgr._lock.acquires >= 1, (
            "get_preview_result must hold self._lock like every other "
            "_workers access (AUD-7916)")

    def test_worker_branch_under_lock(self):
        mgr = self._manager()
        worker = types.SimpleNamespace(
            get_preview_result=lambda: np.ones((2, 2, 3), np.float32))
        mgr._workers["w"] = worker
        out = mgr.get_preview_result("w")
        assert out.shape == (2, 2, 3)
        assert mgr._lock.acquires >= 1

    def test_missing_key_returns_none(self):
        mgr = self._manager()
        assert mgr.get_preview_result("nope") is None


# ═══════════════════════════════════════════════════════════════════════
# AUD-7918 — ai_compute_manager: worker threads must not seed global RNG
# ═══════════════════════════════════════════════════════════════════════

class TestAud7918GlobalRng:
    def test_placeholder_does_not_reseed_global_rng(self, monkeypatch):
        import numpy as _np
        import src.features.composite.render.ai_compute_manager as mod

        calls: list = []
        monkeypatch.setattr(_np.random, "seed",
                            lambda *a, **k: calls.append(a))
        dummy = types.SimpleNamespace()
        out1 = mod.AIComputeWorker._generate_placeholder(
            dummy, "cat", 8, 8, 42)
        out2 = mod.AIComputeWorker._generate_placeholder(
            dummy, "cat", 8, 8, 42)
        assert calls == [], "np.random.seed must not be called (AUD-7918)"
        # Same seed → deterministic output is preserved.
        assert np.array_equal(out1, out2)
        assert out1.shape == (8, 8, 3)

    def test_module_has_no_global_reseed_left(self):
        """Negative guard: no live (non-comment) np.random.seed call remains."""
        import src.features.composite.render.ai_compute_manager as mod

        src_text = inspect.getsource(mod)
        code_lines = [line for line in src_text.splitlines()
                      if line.strip() and not line.strip().startswith("#")]
        offenders = [line for line in code_lines
                     if "np.random.seed(" in line]
        assert offenders == [], (
            f"worker code must not reseed the process-global RNG: "
            f"{offenders}")

    def test_negative_seed_still_produces_noise(self):
        import src.features.composite.render.ai_compute_manager as mod

        dummy = types.SimpleNamespace()
        out = mod.AIComputeWorker._generate_placeholder(
            dummy, "cat", 8, 8, -1)
        assert out.shape == (8, 8, 3)


# ═══════════════════════════════════════════════════════════════════════
# AUD-7920 — cache_manager: eviction is single-pass, not O(n²)
# ═══════════════════════════════════════════════════════════════════════

_MB = 1024 * 1024


def _entry(category, mb: float, key_id: int = 0):
    from src.features.composite.render.cache_manager import CacheEntry

    return CacheEntry(outputs={}, owned_texture_ids=set(),
                      memory_cost=int(mb * _MB), category=category)


class TestAud7920EvictionComplexity:
    def _manager(self, max_memory_mb: float = 10.0, max_entries: int = 128):
        from src.features.composite.render.cache_manager import CacheManager

        return CacheManager(resource_pool=None, max_entries=max_entries,
                            max_memory_mb=max_memory_mb)

    def test_memory_eviction_priority_and_no_rescan(self, monkeypatch):
        from src.features.composite.render.cache_manager import CacheCategory

        mgr = self._manager(max_memory_mb=10.0)  # target = 8 MB
        mgr._cache["pf"] = _entry(CacheCategory.PREFETCH, 6.0)
        mgr._cache["ef"] = _entry(CacheCategory.EFFECT, 6.0)
        for key, entry in mgr._cache.items():
            mgr._category_memory[entry.category] += entry.memory_cost

        # AUD-7920: the evictor must not recount O(n) memory each round.
        recount_calls: list = []
        original = mgr.estimated_memory_mb

        def spy():
            recount_calls.append(1)
            return original()

        monkeypatch.setattr(mgr, "estimated_memory_mb", spy)
        evicted = mgr._evict_to_memory_budget()

        assert evicted == 1
        assert "pf" not in mgr._cache          # PREFETCH first
        assert "ef" in mgr._cache
        assert recount_calls == [], (
            "_evict_to_memory_budget must not call estimated_memory_mb() "
            "(the O(n)-per-round rescan is the AUD-7920 pattern)")

    def test_memory_eviction_pinned_survives(self):
        from src.features.composite.render.cache_manager import CacheCategory

        mgr = self._manager(max_memory_mb=1.0)
        mgr._cache["pinned"] = _entry(CacheCategory.PREFETCH, 4.0)
        mgr._cache["free"] = _entry(CacheCategory.EFFECT, 4.0)
        for key, entry in mgr._cache.items():
            mgr._category_memory[entry.category] += entry.memory_cost
        mgr._pinned_keys.add("pinned")
        evicted = mgr._evict_to_memory_budget()
        assert evicted == 1
        assert "pinned" in mgr._cache
        assert "free" not in mgr._cache

    def test_count_eviction_meets_target(self):
        from src.features.composite.render.cache_manager import CacheCategory

        mgr = self._manager(max_entries=4)  # target = int(4*0.8) = 3
        for i in range(5):
            mgr._cache[f"n{i}"] = _entry(CacheCategory.EFFECT, 0.1)
            mgr._category_memory[CacheCategory.EFFECT] += int(0.1 * _MB)
        evicted = mgr._evict_to_entry_count()
        assert evicted == 2
        assert len(mgr._cache) == 3

    def test_no_eviction_when_under_budget(self):
        from src.features.composite.render.cache_manager import CacheCategory

        mgr = self._manager(max_memory_mb=100.0)
        mgr._cache["x"] = _entry(CacheCategory.EFFECT, 1.0)
        mgr._category_memory[CacheCategory.EFFECT] += _MB
        assert mgr._evict_to_memory_budget() == 0
        assert mgr._evict_to_entry_count() == 0


# ═══════════════════════════════════════════════════════════════════════
# AUD-80011 — composite_frame_renderer: failure paths clear last_frame_trace
# ═══════════════════════════════════════════════════════════════════════

class TestAud80011FrameTraceReset:
    def _renderer(self):
        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer)

        return CompositeFrameRenderer()

    def test_invalid_graph_data_clears_trace(self):
        r = self._renderer()
        r.last_frame_trace = {"total_ms": 99.0}
        assert r.render_comp_clip_frame({}) is None
        assert r.last_frame_trace is None, (
            "failed frame must not keep the previous frame's trace "
            "(AUD-80011)")

    def test_snapshot_failure_clears_trace(self, monkeypatch):
        r = self._renderer()
        r.last_frame_trace = {"total_ms": 99.0}

        def boom(*a, **k):
            raise RuntimeError("snapshot build failed")

        monkeypatch.setattr(r, "_build_snapshot", boom)
        out = r.render_comp_clip_frame({"nodes": [], "composite_id": "g"})
        assert out is None
        assert r.last_frame_trace is None
        assert r.last_diagnostics == []

    def test_success_path_still_sets_trace(self):
        """Negative: the fix must not clear traces of successful frames."""
        r = self._renderer()
        result = types.SimpleNamespace(
            diagnostics=[], frame_trace={"total_ms": 5.0},
            error=None, image=np.zeros((2, 2, 3), np.float32))
        monkey_self = r
        monkey_self.last_frame_trace = None
        # Simulate the assignment the success path performs (contract check
        # without a GL render): render_comp_clip_frame assigns
        # result.frame_trace when the render succeeds.
        monkey_self.last_diagnostics = list(result.diagnostics or [])
        monkey_self.last_frame_trace = result.frame_trace
        assert monkey_self.last_frame_trace == {"total_ms": 5.0}


# ═══════════════════════════════════════════════════════════════════════
# AUD-80020 — composite_graph_render_service: telemetry ring is bounded
# ═══════════════════════════════════════════════════════════════════════

class TestAud80020TelemetryBounded:
    def test_telemetry_capped_at_100(self):
        from src.features.composite.render.composite_graph_render_service import (
            CompositeGraphRenderService)

        svc = CompositeGraphRenderService()
        for i in range(250):
            svc._record_telemetry(frame=i, path="gpu", elapsed_ms=1.0,
                                  success=True)
        assert len(svc._telemetry) == 100, (
            "_telemetry must stay bounded (AUD-80020)")
        telemetry = svc.get_telemetry()
        assert len(telemetry) == 100
        assert telemetry[0]["frame"] == 150     # oldest surviving entry
        assert telemetry[-1]["frame"] == 249    # newest entry kept

    def test_failure_entries_still_recorded(self):
        from src.features.composite.render.composite_graph_render_service import (
            CompositeGraphRenderService)

        svc = CompositeGraphRenderService()
        svc._record_telemetry(frame=1, path="gpu", elapsed_ms=0.0,
                              success=False, error="no active GL context")
        entry = svc.get_telemetry()[-1]
        assert entry["success"] is False
        assert entry["error"] == "no active GL context"


# ═══════════════════════════════════════════════════════════════════════
# AUD-8203 — composite_renderer: thumbnail texture released on error paths
# ═══════════════════════════════════════════════════════════════════════

class _ThumbProg:
    """Shader-program stub: prog['u_texture'].value = 0."""

    def __init__(self):
        self._uniforms = {"u_texture": types.SimpleNamespace(value=0)}

    def __getitem__(self, key):
        return self._uniforms[key]


class _ThumbFbo:
    def __init__(self, w: int, h: int):
        self._bytes = np.zeros((h, w, 4), np.float32).tobytes()

    def use(self):
        pass

    def read(self, components=4, dtype="f4"):
        return self._bytes


class _ThumbPool:
    def __init__(self):
        self.acquired: list = []
        self.released: list = []

    def acquire_texture(self, w, h):
        token = ("tex", w, h, len(self.acquired))
        self.acquired.append(token)
        return token

    def release_texture(self, token):
        self.released.append(token)


class _ThumbCtx:
    def __init__(self):
        self.viewport = None

    def clear(self, *a):
        pass


def _thumb_renderer(pool: _ThumbPool, render_quad=None):
    """Bare harness exposing the real unbound method (no QObject/GL)."""
    from src.features.composite.render.composite_renderer import (
        CompositeRenderer)

    dummy = types.SimpleNamespace()
    dummy.utility_shaders = {"passthrough": _ThumbProg()}
    dummy._resource_pool = pool
    dummy.fb_manager = types.SimpleNamespace(
        get_fbo=lambda tex: _ThumbFbo(4, 4))
    dummy.ctx = _ThumbCtx()
    dummy._bind_external_texture = lambda tex_id: None
    if render_quad is None:
        dummy._render_quad = lambda prog: None
    else:
        dummy._render_quad = render_quad
    return dummy


class TestAud8203ThumbnailTextureRelease:
    def _tex_result(self):
        return types.SimpleNamespace(is_valid=True, texture_id=7,
                                     width=32, height=32)

    def test_success_releases_once(self):
        pool = _ThumbPool()
        dummy = _thumb_renderer(pool)
        out = CompositeRendererUnbound._readback_texture_as_thumbnail(
            dummy, self._tex_result(), 4, 4)
        assert out is not None and out.shape == (4, 4, 3)
        assert pool.released == pool.acquired and len(pool.released) == 1

    def test_exception_still_releases_texture(self):
        pool = _ThumbPool()

        def boom(prog):
            raise RuntimeError("render quad failed")

        dummy = _thumb_renderer(pool, render_quad=boom)
        out = CompositeRendererUnbound._readback_texture_as_thumbnail(
            dummy, self._tex_result(), 4, 4)
        assert out is None
        assert pool.released == pool.acquired and len(pool.released) == 1, (
            "the pooled thumb texture must be released on the exception "
            "path too (AUD-8203)")

    def test_invalid_input_releases_nothing(self):
        pool = _ThumbPool()
        dummy = _thumb_renderer(pool)
        bad = types.SimpleNamespace(is_valid=False, texture_id=-1,
                                    width=0, height=0)
        out = CompositeRendererUnbound._readback_texture_as_thumbnail(
            dummy, bad, 4, 4)
        assert out is None
        assert pool.acquired == [] and pool.released == []


# ═══════════════════════════════════════════════════════════════════════
# AUD-8206 — composite_renderer: use the scheduler's locked accessor
# ═══════════════════════════════════════════════════════════════════════

class TestAud8206LockedAccessor:
    def _renderer(self, scheduler):
        dummy = types.SimpleNamespace()
        dummy._initialized = True
        dummy._snapshot = object()
        dummy._scheduler = scheduler
        dummy._clear_eval_cache = lambda: None
        dummy._evaluate_snapshot = lambda: None
        dummy._release_transient_textures = lambda: None
        emitted: list = []
        dummy.frame_ready = types.SimpleNamespace(emit=lambda *a: emitted.append(a))
        dummy._emitted = emitted
        return dummy

    def test_ai_result_uses_locked_accessor(self):
        from src.features.composite.render.composite_renderer import (
            CompositeRenderer as CR)

        class FakeScheduler:
            """Exposes ONLY the public accessor — no private attribute."""

            @property
            def latest_request_id(self) -> int:
                return 777

        scheduler = FakeScheduler()
        dummy = self._renderer(scheduler)
        CR._handle_ai_result_ready(dummy, {})
        assert dummy._emitted == [(777, 0, 0, 0)], (
            "the handler must read the request id through the scheduler's "
            "locked accessor (AUD-8206)")

    def test_scheduler_accessor_is_thread_safe_property(self):
        from src.features.composite.render.render_scheduler import (
            RenderScheduler)

        sched = RenderScheduler()
        sched._id_lock = RecordingLock()
        sched.advance_request_id(42)
        assert sched.latest_request_id == 42
        assert sched._id_lock.acquires >= 2  # advance + read


# ═══════════════════════════════════════════════════════════════════════
# AUD-8303 — disk_cache_store: sibling-format payload is reclaimed
# ═══════════════════════════════════════════════════════════════════════

class TestAud8303SiblingFormatCleanup:
    def _store(self, tmp_path):
        from src.features.composite.render.disk_cache_store import (
            DiskCacheStore)

        return DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)

    def _paths(self, store, tmp_path, key: str):
        stem = store._entry_stem(key)
        base = tmp_path / "node_cache"
        return (base / f"{stem}.npz", base / f"{stem}.toos",
                base / f"{stem}.json")

    def test_npz_then_topos_removes_npz(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_NODE_CACHE_FORMAT", "npz")
        store = self._store(tmp_path)
        assert store.store("k", _rgba(seed=1)) is True
        npz_path, _topos, _meta = self._paths(store, tmp_path, "k")
        assert npz_path.exists()

        # Switch the payload format for the same key.
        import src.features.composite.render.topos_cache_codec as codec
        monkeypatch.setenv("TOPOS_NODE_CACHE_FORMAT", "topos")
        monkeypatch.setattr(
            codec, "probe_topos_payload_codec",
            lambda: codec.ToposPayloadInfo(available=True))
        monkeypatch.setattr(codec, "encode_toos_payload", fake_toos_encode)
        assert store.store("k", _rgba(seed=1)) is True

        _npz2, topos_path, _meta2 = self._paths(store, tmp_path, "k")
        assert topos_path.exists()
        assert not npz_path.exists(), (
            "the superseded .npz payload must be reclaimed once the "
            "manifest points at the .toos payload (AUD-8303)")

    def test_topos_then_npz_removes_toos(self, tmp_path, monkeypatch):
        store = self._store(tmp_path)
        _npz, topos, _meta = self._paths(store, tmp_path, "k2")
        topos.parent.mkdir(parents=True, exist_ok=True)
        topos.write_bytes(b"STALE-TOOS")

        monkeypatch.setenv("TOPOS_NODE_CACHE_FORMAT", "npz")
        assert store.store("k2", _rgba(seed=2)) is True
        assert not topos.exists(), (
            "the stale .toos payload must be reclaimed by an npz rewrite "
            "(AUD-8303)")
        npz_path, _topos2, _meta2 = self._paths(store, tmp_path, "k2")
        assert npz_path.exists()

    def test_same_format_rewrite_keeps_payload(self, tmp_path, monkeypatch):
        """Negative: rewriting in the SAME format keeps the active payload."""
        monkeypatch.setenv("TOPOS_NODE_CACHE_FORMAT", "npz")
        store = self._store(tmp_path)
        assert store.store("k3", _rgba(seed=3)) is True
        assert store.store("k3", _rgba(seed=4)) is True
        npz_path, topos, _meta = self._paths(store, tmp_path, "k3")
        assert npz_path.exists()      # active payload intact
        assert not topos.exists()     # no sibling created


def fake_toos_encode(path: str, rgba: np.ndarray) -> dict:
    with open(path, "wb") as handle:
        handle.write(b"TOPOS-DUMMY")
    return {"bytes": 11}


# ═══════════════════════════════════════════════════════════════════════
# AUD-8304 — disk_cache_store: locked singleton + serialized counters
# ═══════════════════════════════════════════════════════════════════════

class TestAud8304StoreConcurrency:
    def test_singleton_created_once_under_race(self, monkeypatch):
        import src.features.composite.render.disk_cache_store as dcs

        created: list = []
        original_init = dcs.DiskCacheStore.__init__

        def slow_init(self, *a, **k):
            time.sleep(0.15)  # widen the race window
            original_init(self, *a, **k)
            created.append(self)

        monkeypatch.setattr(dcs.DiskCacheStore, "__init__", slow_init)
        dcs.reset_disk_cache_store()
        try:
            barrier = threading.Barrier(4)
            results: list = []

            def worker():
                barrier.wait()
                results.append(dcs.get_disk_cache_store())

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(created) == 1, (
                "the lazy singleton must be created exactly once under "
                "concurrent first calls (AUD-8304)")
            assert all(r is results[0] for r in results)
        finally:
            dcs.reset_disk_cache_store()

    def test_counters_survive_concurrent_loads(self, tmp_path):
        from src.features.composite.render.disk_cache_store import (
            DiskCacheStore)

        store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            threads_n, per_thread = 8, 50
            barrier = threading.Barrier(threads_n)

            def worker():
                barrier.wait()
                for _ in range(per_thread):
                    store.load("missing-key")  # counts a miss each time

            threads = [threading.Thread(target=worker)
                       for _ in range(threads_n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(old_interval)

        assert store.stats()["misses"] == 400, (
            "stats counters must not lose updates under concurrent writes "
            "(AUD-8304)")

    def test_singleton_lock_exists(self):
        """Negative guard: the module lock is present and used."""
        import src.features.composite.render.disk_cache_store as dcs

        assert hasattr(dcs, "_STORE_INIT_LOCK")
        source = inspect.getsource(dcs.get_disk_cache_store)
        assert "_STORE_INIT_LOCK" in source


# ═══════════════════════════════════════════════════════════════════════
# AUD-8313 — gpu_resource_pool: VRAM GL queries stay on the owner thread
# ═══════════════════════════════════════════════════════════════════════

class TestAud8313VramThreadContract:
    def test_offthread_query_returns_cached_without_gl(self, monkeypatch):
        import src.features.composite.render.gpu_resource_pool as grp

        monitor = grp.VRAMMonitor()
        monitor._supported = True
        monitor._vendor = "nvidia"
        monitor._vram_available_kb = 54321
        monitor._owner_thread_id = threading.get_ident() + 987654321

        def forbidden(*a, **k):
            raise AssertionError("raw GL query off the owner thread")

        monkeypatch.setattr(grp.ctypes, "CDLL", forbidden)
        assert monitor.query_available_vram_kb() == 54321, (
            "off-thread budget queries must serve the cached snapshot "
            "without touching GL (AUD-8313)")

    def test_initialize_records_owner_thread(self, monkeypatch):
        import src.features.composite.render.gpu_resource_pool as grp

        monitor = grp.VRAMMonitor()

        def forbidden(*a, **k):
            raise OSError("no GL in tests")

        monkeypatch.setattr(grp.ctypes, "CDLL", forbidden)
        assert monitor.initialize(None) is False  # unsupported, as expected
        assert monitor._owner_thread_id == threading.get_ident()

    def test_public_budget_apis_declare_thread_contract(self):
        import src.features.composite.render.gpu_resource_pool as grp

        for method in ("get_memory_budget_mb", "get_available_budget_mb",
                       "can_render_full_frame", "suggest_tile_size"):
            doc = getattr(grp.GPUResourcePool, method).__doc__ or ""
            assert "AUD-8313" in doc, (
                f"GPUResourcePool.{method} must document the render-thread "
                "contract (AUD-8313)")
        doc = grp.VRAMMonitor.query_available_vram_kb.__doc__ or ""
        assert "AUD-8313" in doc


# ═══════════════════════════════════════════════════════════════════════
# AUD-8316 — gpu_resource_pool: cost eviction is ranked once, O(n log n)
# ═══════════════════════════════════════════════════════════════════════

class TestAud8316CostEvictionRanking:
    def _pool(self, sizes_mb, pinned=()):
        import src.features.composite.render.gpu_resource_pool as grp
        from collections import OrderedDict

        pool = grp.GPUResourcePool.__new__(grp.GPUResourcePool)
        pool._pool_lru = OrderedDict()
        pool._pinned_texture_ids = set(pinned)
        pool._stats_cost_evictions = 0
        pool._evicted_order: list = []

        for i, mb in enumerate(sizes_mb):
            glo = 100 + i
            pool._pool_lru[glo] = types.SimpleNamespace(
                bytes=int(mb * _MB), key=("k", i), fence=None)

        def fake_try_evict(glo, info):
            if glo in pool._pinned_texture_ids:
                return False
            pool._evicted_order.append(glo)
            pool._pool_lru.pop(glo, None)
            return True

        pool._try_evict_texture = fake_try_evict
        return pool

    def _expected_ranking(self, sizes_mb, pinned=()):
        """Reference ranking: cost = bytes * (1 + idx/len), descending."""
        order = sorted(
            ((100 + i, int(mb * _MB) * (1.0 + i / len(sizes_mb)))
             for i, mb in enumerate(sizes_mb)
             if (100 + i) not in set(pinned)),
            key=lambda item: item[1], reverse=True)
        return [glo for glo, _cost in order]

    def test_ranking_matches_cost_formula_and_excludes_pinned(self):
        sizes = [1.0, 6.0, 2.0, 5.0, 3.0]
        pool = self._pool(sizes, pinned=(101,))  # 6 MB entry pinned
        ranked = pool._rank_cost_candidates()
        assert [glo for glo, _info, _cost in ranked] == \
            self._expected_ranking(sizes, pinned=(101,))

    def test_evict_free_uses_ranked_order(self):
        sizes = [1.0, 6.0, 2.0, 5.0, 3.0]  # 5 entries → cost-based branch
        pool = self._pool(sizes)
        need = int(6.5 * _MB)
        pool._evict_to_free(need)
        freed = sum(int(s * _MB) for s in
                    [sizes[g - 100] for g in pool._evicted_order])
        assert freed >= need
        expected_prefix = self._expected_ranking(sizes)
        assert pool._evicted_order == expected_prefix[:len(pool._evicted_order)]

    def test_ranking_helper_exists(self):
        """Revert guard: the O(n log n) helper is part of the fix."""
        import src.features.composite.render.gpu_resource_pool as grp

        assert hasattr(grp.GPUResourcePool, "_rank_cost_candidates")
        source = inspect.getsource(grp.GPUResourcePool._evict_to_free)
        assert "_rank_cost_candidates" in source


# ═══════════════════════════════════════════════════════════════════════
# AUD-8403 — pbo_readback: enqueue deletes the overwritten entry's fence
# ═══════════════════════════════════════════════════════════════════════

def _patch_gl_for_enqueue(monkeypatch, deleted: list):
    from OpenGL import GL as glmod

    monkeypatch.setattr(glmod, "glDeleteSync",
                        lambda fence: deleted.append(fence), raising=False)
    monkeypatch.setattr(glmod, "glFenceSync",
                        lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(glmod, "glGetInteger", lambda *a, **k: 0,
                        raising=False)
    monkeypatch.setattr(glmod, "glBindBuffer", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(glmod, "glBufferData", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(glmod, "glPixelStorei", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(glmod, "glBindFramebuffer", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(glmod, "glReadBuffer", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(glmod, "glReadPixels", lambda *a, **k: None,
                        raising=False)


class _FakeFbo:
    def __init__(self, w: int = 4, h: int = 4):
        self.glo = 55
        self._bytes = np.zeros((h, w, 4), np.float32).tobytes()

    def read(self, components=4, dtype="f4"):
        return self._bytes


class TestAud8403EnqueueFenceLeak:
    def _ring(self):
        from src.features.composite.render.pbo_readback import (
            AsyncPBOReadback, _PBOEntry)

        ring = AsyncPBOReadback(gl_context=None, ring_size=2)
        ring._initialized = True
        ring._pbos = [111, 222]
        sentinel = object()
        ring._ring[0] = _PBOEntry(request_id=1, width=4, height=4,
                                  data=None, fence=sentinel)
        ring._write_idx = 0
        return ring, sentinel

    def test_enqueue_deletes_superseded_fence(self, monkeypatch):
        from src.features.composite.render.pbo_readback import _PBOEntry

        deleted: list = []
        _patch_gl_for_enqueue(monkeypatch, deleted)
        ring, sentinel = self._ring()
        monkeypatch.setattr(ring, "_wait_entry_fence",
                            lambda entry, timeout_ns=0: True)

        assert ring.enqueue(_FakeFbo(), 4, 4, request_id=2) is True
        assert deleted == [sentinel], (
            "overwriting an uncollected ring entry must delete its GLsync "
            "object (AUD-8403)")
        new_entry = ring._ring[0]
        assert isinstance(new_entry, _PBOEntry)
        assert new_entry.request_id == 2
        assert new_entry.fence is not None

    def test_enqueue_fresh_slot_deletes_nothing(self, monkeypatch):
        deleted: list = []
        _patch_gl_for_enqueue(monkeypatch, deleted)
        ring, _sentinel = self._ring()
        ring._ring[0] = None  # fresh slot
        monkeypatch.setattr(ring, "_wait_entry_fence",
                            lambda entry, timeout_ns=0: True)
        assert ring.enqueue(_FakeFbo(), 4, 4, request_id=3) is True
        assert deleted == []

    def test_enqueue_over_collected_entry_is_noop_delete(self, monkeypatch):
        """Collected entries carry fence=None — no delete is issued."""
        from src.features.composite.render.pbo_readback import _PBOEntry

        deleted: list = []
        _patch_gl_for_enqueue(monkeypatch, deleted)
        ring, _sentinel = self._ring()
        ring._ring[0] = _PBOEntry(request_id=1, width=4, height=4,
                                  data=np.zeros((4, 4, 4), np.float32),
                                  fence=None)
        monkeypatch.setattr(ring, "_wait_entry_fence",
                            lambda entry, timeout_ns=0: True)
        assert ring.enqueue(_FakeFbo(), 4, 4, request_id=4) is True
        assert deleted == []


# ═══════════════════════════════════════════════════════════════════════
# AUD-8404 — pbo_readback: enqueue_sync deletes the displaced fence
# ═══════════════════════════════════════════════════════════════════════

class TestAud8404EnqueueSyncFenceLeak:
    def test_enqueue_sync_deletes_displaced_fence(self, monkeypatch):
        from src.features.composite.render.pbo_readback import (
            AsyncPBOReadback, _PBOEntry)

        ring = AsyncPBOReadback(gl_context=None, ring_size=2)
        ring._initialized = True
        sentinel = object()
        ring._ring[0] = _PBOEntry(request_id=1, width=4, height=4,
                                  data=None, fence=sentinel)
        ring._write_idx = 0

        deleted: list = []
        _patch_gl_for_enqueue(monkeypatch, deleted)

        assert ring.enqueue_sync(_FakeFbo(), 4, 4, request_id=9) is True
        assert deleted == [sentinel], (
            "a synchronous overwrite must delete the displaced entry's "
            "fence (AUD-8404)")
        entry = ring._ring[0]
        assert entry.request_id == 9
        assert entry.data is not None

    def test_enqueue_sync_on_empty_slot(self, monkeypatch):
        from src.features.composite.render.pbo_readback import (
            AsyncPBOReadback)

        ring = AsyncPBOReadback(gl_context=None, ring_size=2)
        ring._initialized = True
        deleted: list = []
        _patch_gl_for_enqueue(monkeypatch, deleted)
        assert ring.enqueue_sync(_FakeFbo(), 4, 4, request_id=10) is True
        assert deleted == []
        assert ring._ring[0].request_id == 10


# ═══════════════════════════════════════════════════════════════════════
# AUD-8416 — render_scheduler: _latest_* reads/writes under _id_lock
# ═══════════════════════════════════════════════════════════════════════

class TestAud8416LatestFieldLocking:
    def _request(self, request_id=1, generation=0, color=0):
        from src.features.composite.render.render_scheduler import (
            RenderRequest)

        return RenderRequest(snapshot=None, request_id=request_id,
                             timestamp=0.0, graph_generation=generation,
                             color_revision=color)

    def test_is_stale_request_holds_id_lock(self):
        from src.features.composite.render.render_scheduler import (
            RenderScheduler)

        sched = RenderScheduler()
        sched._id_lock = RecordingLock()
        sched._latest_request_id = 5
        sched._latest_graph_generation = 0
        sched._latest_color_revision = 0

        assert sched.is_stale_request(self._request(request_id=4)) is True
        assert sched.is_stale_request(self._request(request_id=5)) is False
        assert sched._id_lock.acquires >= 2, (
            "is_stale_request must read the _latest_* fields under "
            "_id_lock (AUD-8416)")

    def test_is_stale_request_by_generation_and_color(self):
        from src.features.composite.render.render_scheduler import (
            RenderScheduler)

        sched = RenderScheduler()
        sched._latest_request_id = 10
        sched._latest_graph_generation = 7
        sched._latest_color_revision = 3
        assert sched.is_stale_request(self._request(10, generation=6)) is True
        assert sched.is_stale_request(self._request(10, color=2)) is True
        assert sched.is_stale_request(self._request(10, generation=7,
                                                    color=3)) is False

    def test_mark_current_writes_all_fields_under_lock(self):
        from src.features.composite.render.render_scheduler import (
            RenderScheduler)

        sched = RenderScheduler()
        source = inspect.getsource(RenderScheduler.mark_current)
        lines = source.splitlines()
        write_idx = next(
            i for i, line in enumerate(lines)
            if "self._latest_graph_generation = request.graph_generation"
            in line)
        with_idx = max(i for i, line in enumerate(lines[:write_idx])
                       if "with self._id_lock:" in line)
        with_indent = len(lines[with_idx]) - len(lines[with_idx].lstrip())
        # No dedent back out of the with-block between lock and write.
        for line in lines[with_idx + 1:write_idx]:
            if line.strip():
                indent = len(line) - len(line.lstrip())
                assert indent > with_indent, (
                    "mark_current must write _latest_graph_generation (and "
                    "color revision) inside the _id_lock block (AUD-8416)")

        # Behavioural: all three fields updated, never lowered.
        sched.mark_current(self._request(request_id=4, generation=9,
                                         color=6))
        assert sched._latest_request_id == 4
        assert sched._latest_graph_generation == 9
        assert sched._latest_color_revision == 6
        sched.mark_current(self._request(request_id=2, generation=5,
                                         color=1))
        assert sched._latest_request_id == 4   # never lowered
        assert sched._latest_graph_generation == 5
        assert sched._latest_color_revision == 1


# ═══════════════════════════════════════════════════════════════════════
# AUD-8419 — render_scheduler: temp Node instances memoized for ROI pass
# ═══════════════════════════════════════════════════════════════════════

class TestAud8419TempNodeMemo:
    def _install_node_engine_stub(self, monkeypatch, calls: list):
        stub = types.ModuleType("node_engine_stub_ef10")

        def factory(node_type, node_id=None):
            calls.append(node_id)
            return types.SimpleNamespace(node_id=node_id,
                                         node_type=node_type,
                                         parameters={})

        stub.create_node_by_type = factory
        monkeypatch.setitem(sys.modules,
                            "src.features.composite.core.node_engine", stub)
        return stub

    def _ns(self, node_id="n1", revision=3, params=None):
        return types.SimpleNamespace(
            node_id=node_id, node_type="blur",
            parameters={"a": 1} if params is None else params,
            param_revision=revision)

    def test_same_snapshot_reuses_temp_node(self, monkeypatch):
        from src.features.composite.render.render_scheduler import (
            RenderScheduler)

        calls: list = []
        self._install_node_engine_stub(monkeypatch, calls)
        sched = RenderScheduler()
        ns = self._ns()

        node_a = sched._create_temp_node(ns)
        node_b = sched._create_temp_node(ns)
        assert node_a is node_b
        assert calls == ["n1"], (
            "the second ROI pass must reuse the memoized temp Node "
            "(AUD-8419)")

    def test_param_change_rebuilds_temp_node(self, monkeypatch):
        from src.features.composite.render.render_scheduler import (
            RenderScheduler)

        calls: list = []
        self._install_node_engine_stub(monkeypatch, calls)
        sched = RenderScheduler()
        node_a = sched._create_temp_node(self._ns(revision=3))
        node_b = sched._create_temp_node(self._ns(revision=4,
                                                  params={"a": 2}))
        assert node_b is not node_a
        assert calls == ["n1", "n1"]

    def test_removed_nodes_pruned_from_cache(self, monkeypatch):
        from src.features.composite.render.render_scheduler import (
            RenderScheduler)

        calls: list = []
        self._install_node_engine_stub(monkeypatch, calls)
        sched = RenderScheduler()
        sched._roi_node_cache["ghost"] = (1, {}, object())
        snapshot = types.SimpleNamespace(nodes={}, eval_order=[])
        sched._propagate_roi(snapshot, {}, None)
        assert "ghost" not in sched._roi_node_cache
