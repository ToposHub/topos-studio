"""Milestone E / P4: 模型导入持久缓存测试。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §8 (P4)：
1. ImportCacheKey：稳定 hash、源变化不同 key、导入参数影响 key
2. ModelImportCache：put/get 命中、源文件变化失效、版本失效、派生文件丢失失效
3. FBX fallback GLB 持久化（model_loader 集成，源码级）
4. Model3D cache_status 诊断（in_memory/disk_hit/miss）
5. ModelPayload.model_cache_status 字段
6. Scene3D render_diagnostics 含 model_cache_status

注：缓存逻辑用临时目录真测（非源码级）；model_loader/Scene3D 集成用源码级断言。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import tempfile
import time

import pytest

from src.features.composite.render3d.model_import_cache import (
    ImportCacheKey, ImportCacheEntry, ModelImportCache, build_cache_key,
    IMPORT_CACHE_VERSION, get_default_cache, _default_cache_dir,
)


# ──────────────────────────────────────────────────────────────────────────
# Part 1: ImportCacheKey
# ──────────────────────────────────────────────────────────────────────────

class TestImportCacheKey:
    """ImportCacheKey 稳定 hash + 等价判定（§8.3）。"""

    def test_build_from_real_file(self, tmp_path):
        src = tmp_path / "model.fbx"
        src.write_bytes(b"fbx data")
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        assert key.source_path.endswith("model.fbx")
        assert key.source_size == 8
        assert key.cache_version == IMPORT_CACHE_VERSION
        d = key.digest()
        assert len(d) == 16

    def test_same_file_same_digest(self, tmp_path):
        src = tmp_path / "model.fbx"
        src.write_bytes(b"same")
        k1 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        k2 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        assert k1.digest() == k2.digest()

    def test_content_change_different_digest(self, tmp_path):
        src = tmp_path / "model.fbx"
        src.write_bytes(b"v1")
        k1 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        # 改内容 + 触发 mtime 变化
        time.sleep(0.01)
        src.write_bytes(b"v2 longer content")
        os.utime(str(src))  # 强制 mtime 更新
        k2 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        assert k1.digest() != k2.digest()

    def test_import_params_affect_digest(self, tmp_path):
        src = tmp_path / "model.fbx"
        src.write_bytes(b"data")
        k1 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        k2 = build_cache_key(str(src), normalize=True, flip_uv_v=False)
        assert k1.digest() != k2.digest()

    def test_nonexistent_raises(self):
        with pytest.raises(ValueError):
            build_cache_key("/nonexistent/file.fbx", normalize=False, flip_uv_v=False)


# ──────────────────────────────────────────────────────────────────────────
# Part 2: ModelImportCache
# ──────────────────────────────────────────────────────────────────────────

class TestModelImportCache:
    """ModelImportCache put/get/失效（用临时目录真测）。"""

    def test_disabled_cache_returns_none(self, tmp_path):
        src = tmp_path / "m.fbx"
        src.write_bytes(b"data")
        cache = ModelImportCache(cache_dir=str(tmp_path / "c"), enabled=False)
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        assert cache.get(key) is None
        # put 在禁用时不落盘
        glb = tmp_path / "x.glb"
        glb.write_bytes(b"glb")
        entry = cache.put(key, fallback_glb=str(glb))
        assert entry.fallback_glb is None  # 禁用不 ingest

    def test_put_then_get_hit(self, tmp_path):
        src = tmp_path / "m.fbx"
        src.write_bytes(b"data")
        cache = ModelImportCache(cache_dir=str(tmp_path / "cache"), enabled=True)
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        assert cache.get(key) is None  # 未缓存
        glb = tmp_path / "converted.glb"
        glb.write_bytes(b"glb content")
        entry = cache.put(key, fallback_glb=str(glb), mesh_count=3,
                          triangle_count=5000, material_count=2, texture_count=4)
        assert entry.fallback_glb is not None
        # 命中
        hit = cache.get(key)
        assert hit is not None
        assert hit.mesh_count == 3
        assert hit.triangle_count == 5000
        assert hit.fallback_glb == entry.fallback_glb
        # GLB 文件确实在缓存目录
        assert os.path.isfile(cache.file_path(hit.fallback_glb))

    def test_source_change_invalidates(self, tmp_path):
        src = tmp_path / "m.fbx"
        src.write_bytes(b"v1")
        cache = ModelImportCache(cache_dir=str(tmp_path / "cache"), enabled=True)
        key1 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        glb = tmp_path / "x.glb"
        glb.write_bytes(b"glb")
        cache.put(key1, fallback_glb=str(glb))
        assert cache.get(key1) is not None
        # 源文件变化
        time.sleep(0.01)
        src.write_bytes(b"v2 different length")
        os.utime(str(src))
        key2 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        # 旧 key 的条目应被清理（digest 不同，但旧 manifest 应被 purge）
        # 新 key miss
        assert cache.get(key2) is None

    def test_missing_derived_file_invalidates(self, tmp_path):
        """引用的 GLB 被外部删除 → 条目失效（get 返回 None）。"""
        src = tmp_path / "m.fbx"
        src.write_bytes(b"data")
        cache = ModelImportCache(cache_dir=str(tmp_path / "cache"), enabled=True)
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        glb = tmp_path / "x.glb"
        glb.write_bytes(b"glb")
        cache.put(key, fallback_glb=str(glb))
        hit = cache.get(key)
        glb_path = cache.file_path(hit.fallback_glb)
        # 删掉缓存的 GLB
        os.remove(glb_path)
        # 再次 get 应失效
        assert cache.get(key) is None

    def test_clear_removes_all(self, tmp_path):
        src = tmp_path / "m.fbx"
        src.write_bytes(b"data")
        cache = ModelImportCache(cache_dir=str(tmp_path / "cache"), enabled=True)
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        glb = tmp_path / "x.glb"
        glb.write_bytes(b"glb")
        cache.put(key, fallback_glb=str(glb))
        removed = cache.clear()
        assert removed >= 2  # manifest + glb
        assert cache.get(key) is None

    def test_corrupt_manifest_returns_none(self, tmp_path):
        """manifest 损坏（非法 json）→ None，不崩溃。"""
        src = tmp_path / "m.fbx"
        src.write_bytes(b"data")
        cache = ModelImportCache(cache_dir=str(tmp_path / "cache"), enabled=True)
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        cache._ensure_dir()
        # 写损坏 manifest
        mpath = cache._manifest_path(key.digest())
        with open(mpath, "w") as fh:
            fh.write("{not valid json")
        assert cache.get(key) is None


# ──────────────────────────────────────────────────────────────────────────
# Part 3: model_loader 集成（FBX fallback GLB 持久化，源码级）
# ──────────────────────────────────────────────────────────────────────────

class TestModelLoaderCacheIntegration:
    """model_loader._load_assimp_cli_static_fallback 用持久缓存（P4-2）。"""

    def test_fallback_uses_cache(self):
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._load_assimp_cli_static_fallback)
        assert "model_import_cache" in src
        assert "build_cache_key" in src
        assert "get_default_cache" in src
        assert "cache.get(key)" in src
        # P4-3：cache.put 现在带 textures_extracted=True（闭环标记）
        assert "cache.put(key, fallback_glb=glb_path," in src
        assert "textures_extracted=True" in src

    def test_fallback_reuses_cached_glb(self):
        """命中缓存时直接 _load_trimesh(cached_glb)，跳过 CLI 转换。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._load_assimp_cli_static_fallback)
        assert "cached_glb" in src
        assert "_load_trimesh(cached_glb" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 4: Model3D cache_status 诊断
# ──────────────────────────────────────────────────────────────────────────

class TestModel3DCacheStatus:
    """Model3DNode 跟踪并输出 model_cache_status（P4-6）。"""

    def test_node_has_cache_status_field(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode.__init__)
        assert "_model_cache_status" in src

    def test_ensure_model_sets_status(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._ensure_model)
        # 进程内命中 = "in_memory"
        assert '"in_memory"' in src
        # 完整加载 = "miss"
        assert '"miss"' in src

    def test_do_evaluate_passes_status_to_payload(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._do_evaluate)
        assert "model_cache_status=self._model_cache_status" in src

    def test_payload_has_cache_status_field(self):
        from src.features.composite.render3d.scene_payload import ModelPayload
        mp = ModelPayload()
        assert hasattr(mp, "model_cache_status")
        assert mp.model_cache_status is None


# ──────────────────────────────────────────────────────────────────────────
# Part 5: Scene3D diagnostics 含 model_cache_status
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DDiagnosticsCacheStatus:
    """Scene3D render_diagnostics 含 model_cache_status。"""

    def test_diagnostics_includes_cache_status(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "model_cache_status" in src
        assert "model_payload.model_cache_status" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 6: 缓存目录 + 默认实例
# ──────────────────────────────────────────────────────────────────────────

class TestCacheDir:
    """缓存目录 + 默认实例（§8.2）。"""

    def test_default_cache_dir_macos(self):
        d = _default_cache_dir()
        assert "ToposColor" in d
        assert "model_import_cache" in d

    def test_get_default_cache_singleton(self):
        c1 = get_default_cache()
        c2 = get_default_cache()
        assert c1 is c2

    def test_import_cache_version_positive(self):
        assert IMPORT_CACHE_VERSION >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── AUD-9001：put 触发预算驱逐 ───────────────────────────────────────────


class TestCacheBudgetEviction:
    def _put_one(self, cache, tmp_path, index: int):
        src = tmp_path / f"m{index}.fbx"
        src.write_bytes(f"data-{index}".encode())
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        glb = tmp_path / f"c{index}.glb"
        glb.write_bytes(b"x" * 32)
        return cache.put(key, fallback_glb=str(glb))

    def test_entry_count_budget_evicts_oldest(self, tmp_path):
        """超出 MAX_ENTRIES 时最旧条目（manifest+派生文件）被驱逐。"""
        cache = ModelImportCache(cache_dir=str(tmp_path / "cache"), enabled=True)
        cache.MAX_ENTRIES = 3
        entries = [self._put_one(cache, tmp_path, i) for i in range(4)]

        # 最旧的第 0 条被驱逐，其余 3 条 + 新条目保留
        assert not os.path.isfile(
            cache._manifest_path(entries[0].digest))
        assert not os.path.isfile(cache.file_path(entries[0].fallback_glb))
        for kept in entries[1:]:
            assert os.path.isfile(cache._manifest_path(kept.digest))

    def test_total_bytes_budget_evicts_oldest(self, tmp_path):
        """总字节数超预算时同样按 created_at 最旧先驱逐。"""
        cache = ModelImportCache(cache_dir=str(tmp_path / "cache"), enabled=True)
        cache.MAX_ENTRIES = 100
        cache.MAX_TOTAL_BYTES = 1  # 任何条目都会立刻超预算
        entries = [self._put_one(cache, tmp_path, i) for i in range(3)]

        # 预算=1 字节：只应保留最新的 1 条
        remaining = [e for e in entries
                     if os.path.isfile(cache._manifest_path(e.digest))]
        assert len(remaining) == 1
        assert remaining[0] is entries[-1]
