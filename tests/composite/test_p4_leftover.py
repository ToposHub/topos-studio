"""P4 遗留子任务测试（Milestone E 补完）。

覆盖：
- P4-3：.fbm 贴图 manifest（ImportCacheEntry.textures_extracted 字段 + put/get）。
- P4-6UI：cache status 在 viewer perf overlay 第 3 行显示（model cache + material 命中率）。

注：P4-4（SceneGraph npz 缓存）推迟——P4-2 FBX→GLB 缓存已覆盖主要重解析成本，
SceneGraph npz 序列化复杂度高且版本风险大，留待后续。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import tempfile

import pytest

from src.features.composite.render3d.model_import_cache import (
    ModelImportCache, ImportCacheEntry, build_cache_key,
)


# ──────────────────────────────────────────────────────────────────────────
# P4-3：.fbm 贴图 manifest（textures_extracted）
# ──────────────────────────────────────────────────────────────────────────

class TestTexturesExtractedManifest:
    """ImportCacheEntry.textures_extracted 字段 + put/get 读写。"""

    def test_entry_has_field(self):
        entry = ImportCacheEntry(digest="abc", source_path="", source_mtime_ns=0,
                                 source_size=0, created_at=0.0, cache_version=1)
        assert hasattr(entry, "textures_extracted")
        assert entry.textures_extracted is False  # 默认 False

    def test_put_with_textures_extracted(self, tmp_path):
        src = tmp_path / "m.fbx"
        src.write_bytes(b"data")
        cache = ModelImportCache(cache_dir=str(tmp_path / "c"), enabled=True)
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        cache.put(key, textures_extracted=True, texture_count=4)
        entry = cache.get(key)
        assert entry is not None
        assert entry.textures_extracted is True
        assert entry.texture_count == 4

    def test_put_default_textures_not_extracted(self, tmp_path):
        src = tmp_path / "m.fbx"
        src.write_bytes(b"data")
        cache = ModelImportCache(cache_dir=str(tmp_path / "c"), enabled=True)
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        cache.put(key)
        entry = cache.get(key)
        assert entry.textures_extracted is False

    def test_textures_extracted_survives_reload(self, tmp_path):
        """跨"进程"（重新创建 cache 实例）textures_extracted 仍可读。"""
        src = tmp_path / "m.fbx"
        src.write_bytes(b"data")
        cache_dir = str(tmp_path / "c")
        key = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        cache1 = ModelImportCache(cache_dir=cache_dir, enabled=True)
        cache1.put(key, textures_extracted=True)
        # 新实例（模拟重启）
        cache2 = ModelImportCache(cache_dir=cache_dir, enabled=True)
        entry = cache2.get(key)
        assert entry.textures_extracted is True

    def test_textures_extracted_invalidates_on_source_change(self, tmp_path):
        """源文件变化 → 新 key（textures_extracted 不复用，需重新提取）。"""
        import time
        src = tmp_path / "m.fbx"
        src.write_bytes(b"v1")
        cache = ModelImportCache(cache_dir=str(tmp_path / "c"), enabled=True)
        key1 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        cache.put(key1, textures_extracted=True)
        # 源变化
        time.sleep(0.01)
        src.write_bytes(b"v2 different")
        os.utime(str(src))
        key2 = build_cache_key(str(src), normalize=False, flip_uv_v=False)
        entry = cache.get(key2)
        assert entry is None  # 新 key miss，需重新提取


# ──────────────────────────────────────────────────────────────────────────
# P4-6UI：cache status 在 viewer perf overlay 显示
# ──────────────────────────────────────────────────────────────────────────

class TestCacheStatusOverlay:
    """CompositeViewer perf overlay 第 3 行显示 cache 状态。"""

    def test_draw_perf_overlay_includes_cache_status(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_perf_overlay)
        assert "model_cache_status" in src
        assert "material_cache_hit_rate" in src

    def test_draw_perf_overlay_third_line(self):
        """overlay 第 3 行 cache 信息（model:xxx | mat:nn%）。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer
        src = inspect.getsource(CompositeViewer._draw_perf_overlay)
        assert "line3" in src
        assert "cache_parts" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
