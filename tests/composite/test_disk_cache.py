"""A6 DiskCacheStore tests — content addressing, atomicity, LRU eviction,
corruption recovery, and the evaluator mixin round-trip.
"""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.render import disk_cache_store as dcs
from src.features.composite.render.disk_cache_store import (
    DiskCacheStore,
    reset_disk_cache_store,
)

pytestmark = pytest.mark.composite_core


@pytest.fixture()
def store(tmp_path):
    return DiskCacheStore(root_dir=str(tmp_path), capacity_gb=20.0)


def _rgba(h=16, w=16, seed=0):
    rng = np.random.default_rng(seed)
    arr = rng.random((h, w, 4)).astype(np.float32)
    arr[..., 3] = 1.0
    return arr


class TestStoreRoundtrip:
    def test_store_load_pixel_identical(self, store):
        arr = _rgba(seed=1)
        assert store.store("k1", arr, {"node_id": "n1"}) is True
        assert store.has("k1") is True
        loaded = store.load("k1")
        assert loaded is not None
        # EXR half float: 3 decimal digits of mantissa — allow small noise.
        np.testing.assert_allclose(loaded, arr, atol=5e-3)

    def test_metadata_manifest(self, store):
        arr = _rgba(seed=2)
        store.store("k2", arr, {"node_id": "n2", "node_type": "blur",
                                "frame": 7})
        meta = store.load_metadata("k2")
        assert meta["node_id"] == "n2"
        assert meta["node_type"] == "blur"
        assert meta["frame"] == 7
        # 格式随 native codec 可用性而定：auto 策略 TOPOS 优先、缺失时
        # npz 回退（T-P1-71 修复 topos 载荷后本机走 topos 路径）。
        from src.features.composite.render.topos_cache_codec import (
            FORMAT_ID, probe_topos_payload_codec,
        )
        expected = (FORMAT_ID if probe_topos_payload_codec().available
                    else "npz_f16")
        assert meta["format"] == expected
        assert meta["width"] == 16 and meta["height"] == 16

    def test_missing_key_is_miss(self, store):
        assert store.has("nope") is False
        assert store.load("nope") is None

    def test_delete_and_clear(self, store):
        store.store("k3", _rgba(seed=3))
        store.delete("k3")
        assert store.has("k3") is False
        store.store("k4", _rgba(seed=4))
        assert store.clear() == 1
        assert store.has("k4") is False

    def test_touch_updates_last_used(self, store):
        store.store("k5", _rgba(seed=5))
        before = store.load_metadata("k5")["last_used"]
        store.load("k5")
        after = store.load_metadata("k5")["last_used"]
        assert after >= before


class TestAtomicityAndCorruption:
    def test_payload_without_manifest_is_invisible(self, store, tmp_path):
        arr = _rgba(seed=6)
        store.store("k6", arr)
        # Simulate a crash between payload and manifest writes.
        os.unlink(store._meta_path("k6"))
        assert store.has("k6") is False
        assert store.load("k6") is None

    def test_manifest_without_payload_is_invisible(self, store):
        store.store("k7", _rgba(seed=7))
        # payload 可能是 .npz（回退）或 .toos（native 可用）——按实际
        # 存在的载荷文件删除，语义不变：manifest 没有载荷 = 不可见。
        for payload in (store._payload_path("k7"),
                        store._topos_payload_path("k7")):
            if os.path.exists(payload):
                os.unlink(payload)
        assert store.has("k7") is False

    def test_no_tmp_files_recognised(self, store):
        # A leftover .writing file from a crashed write must never be read.
        with open(os.path.join(store._root, "k8.npz.writing"), "wb") as f:
            f.write(b"junk")
        assert store.has("k8") is False

    def test_corrupt_payload_is_dropped(self, store):
        store.store("k9", _rgba(seed=9))
        # 腐蚀实际存在的载荷文件（.npz 或 .toos），load 必须判失败并清理。
        target = next(
            (p for p in (store._payload_path("k9"),
                         store._topos_payload_path("k9"))
             if os.path.exists(p)), None)
        assert target is not None
        with open(target, "wb") as f:
            f.write(b"garbage-not-a-payload")
        assert store.load("k9") is None
        # The broken pair was cleaned up so later loads stay misses.
        assert store.has("k9") is False

    def test_none_and_bad_shapes_rejected(self, store):
        assert store.store("kA", None) is False
        assert store.store("kB", _rgba()[:, :, :3]) is False


class TestEviction:
    def test_lru_eviction_respects_capacity(self, tmp_path):
        store = DiskCacheStore(root_dir=str(tmp_path / "c1"),
                               capacity_gb=20.0)
        # 按实际载荷尺寸设定预算（npz 与 topos 载荷大小不同，不能假设
        # 固定字节数）：先存一条量出大小，预算 = 2.5 × 单条。
        store.store("probe", _rgba(seed=99))
        per_entry = max(1, store.total_bytes())
        store.delete("probe")
        store._capacity_bytes = int(per_entry * 2.5)
        keys = []
        for i in range(6):
            key = f"ev{i}"
            store.store(key, _rgba(seed=10 + i))
            keys.append(key)
        remaining = [k for k in keys if store.has(k)]
        assert store.total_bytes() <= store._capacity_bytes
        # Oldest inserts are gone; the newest survive.
        assert remaining and remaining[-1] == keys[-1]

    def test_pinned_entries_survive(self, tmp_path):
        store = DiskCacheStore(root_dir=str(tmp_path / "c2"),
                               capacity_gb=20.0)
        store._capacity_bytes = 1024
        store.store("pinned", _rgba(seed=20), pinned=True)
        store.evict_if_needed(capacity_bytes=0)
        assert store.has("pinned") is True

    def test_stats(self, store):
        store.store("s1", _rgba(seed=30))
        store.load("s1")
        store.load("missing")
        stats = store.stats()
        assert stats["entries"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] >= 1
        assert stats["writes"] == 1
        assert stats["bytes"] > 0


# ── Evaluator mixin wiring (real GL) ───────────────────────────────────────

class _Ns:
    """Minimal NodeSnapshot stand-in for the mixin helpers."""

    def __init__(self):
        self.node_id = "n1"
        self.node_type = "vignette"
        self.output_ports = [("result", "output")]
        self.parameters = {"cache_to_disk": True}


class TestEvaluatorMixinWiring:
    @pytest.fixture()
    def harness(self, tmp_path, monkeypatch):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.cache_evaluator import (
            CacheEvaluatorMixin,
        )
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )

        test_store = DiskCacheStore(root_dir=str(tmp_path / "eval"),
                                    capacity_gb=1.0)
        monkeypatch.setattr(dcs, "_STORE_INSTANCE", test_store)

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f2"):
                # Mirrors GPUResourcePool.acquire_texture: half-float
                # textures are the pipeline default, so the disk-cache
                # upload path must narrow its payload to f16 bytes.
                tex = self.ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

        class _H(BaseEvaluatorMixin, CacheEvaluatorMixin,
                 ColorEvaluatorMixin, FilterEvaluatorMixin):
            def __init__(self, ctx):
                self.ctx = ctx
                self.utility_shaders = {}
                self._resource_pool = _Pool(ctx)

                from src.shared.gpu.gl_backend import FramebufferManager
                self.fb_manager = FramebufferManager(ctx)

        yield _H(ctx)
        ctx.release()
        reset_disk_cache_store()

    def test_upload_readback_roundtrip(self, harness, qapp):
        arr = _rgba(h=12, w=20, seed=40)
        tex = harness._upload_rgba_array_result(arr, None)
        assert tex is not None and tex.is_valid
        back = harness._readback_result_array(tex)
        # Pool textures are half-float: the roundtrip quantises to f16.
        np.testing.assert_allclose(back, arr, atol=5e-3)

    def test_store_then_load_outputs_roundtrip(self, harness, qapp):
        import time as _time

        from PyQt6.QtTest import QTest

        arr = _rgba(h=12, w=20, seed=41)
        tex = harness._upload_rgba_array_result(arr, None)
        outputs = {"result": tex}
        ns = _Ns()
        harness._store_disk_cache_outputs(ns, "keyA", outputs)

        from src.features.composite.render.disk_cache_store import \
            get_disk_cache_store
        store = get_disk_cache_store()
        # The mixin namespaces keys: "keyA" lands under "nkeyA".
        assert store.has("keyA") is False
        # AUD-5923: the payload is handed to the background writer thread —
        # wait (bounded) for the write to land instead of assuming sync IO.
        deadline = _time.monotonic() + 5.0
        while not store.has("nkeyA") and _time.monotonic() < deadline:
            QTest.qWait(20)
        assert store.has("nkeyA") is True

        restored = harness._load_disk_cache_outputs(ns, "keyA", {})
        assert restored is not None
        assert "result" in restored
        assert restored["result"].is_valid
        back = harness._readback_result_array(restored["result"])
        np.testing.assert_allclose(back, arr, atol=5e-3)

    def test_load_miss_returns_none(self, harness, qapp):
        assert harness._load_disk_cache_outputs(_Ns(), "nokey", {}) is None
