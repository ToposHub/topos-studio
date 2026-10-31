"""P4-3：textures_extracted 标记读取侧闭环测试。

覆盖审计发现：textures_extracted 标记在 cache entry 里存了，但 model_loader
读取侧没闭环（不读 entry.textures_extracted，cache.put 也不传该标记）。

P4-3 闭环：
- cache.get 命中 + textures_extracted=True → _set_skip_embedded_persist(True)
- _persist_embedded_texture 检查 _should_skip_embedded_persist + 文件存在 → 跳过 PNG 编码
- cache.put 在 _load_trimesh（贴图提取）后调用，传 textures_extracted=True
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect


class TestTexturesExtractedReadSide:
    """model_loader 读取 textures_extracted 标记（P4-3 核心）。"""

    def test_fallback_reads_textures_extracted(self):
        """_load_assimp_cli_static_fallback 读 entry.textures_extracted。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._load_assimp_cli_static_fallback)
        assert "_cached_textures_extracted" in src
        assert "entry.textures_extracted" in src

    def test_fallback_sets_skip_flag_on_cache_hit(self):
        """cache 命中 + textures_extracted → _set_skip_embedded_persist(True)。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._load_assimp_cli_static_fallback)
        assert "_set_skip_embedded_persist(True)" in src
        # finally 恢复 False（避免泄漏到后续加载）
        assert "_set_skip_embedded_persist(False)" in src

    def test_fallback_puts_textures_extracted_true(self):
        """cache.put 在 _load_trimesh 后调用，传 textures_extracted=True。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._load_assimp_cli_static_fallback)
        assert "textures_extracted=True" in src


class TestSkipEmbeddedPersistMechanism:
    """_persist_embedded_texture 的跳过机制（P4-3）。"""

    def test_has_skip_helpers(self):
        """model_loader 有 _set_skip_embedded_persist / _should_skip_embedded_persist。"""
        from src.features.composite.render3d import model_loader
        assert hasattr(model_loader, "_set_skip_embedded_persist")
        assert hasattr(model_loader, "_should_skip_embedded_persist")

    def test_skip_helpers_thread_local(self):
        """跳过标志是线程局部的（默认 False）。"""
        from src.features.composite.render3d import model_loader
        # 默认 False
        model_loader._set_skip_embedded_persist(False)
        assert model_loader._should_skip_embedded_persist() is False
        # 设置 True
        model_loader._set_skip_embedded_persist(True)
        assert model_loader._should_skip_embedded_persist() is True
        # 恢复
        model_loader._set_skip_embedded_persist(False)

    def test_persist_checks_skip_flag(self):
        """_persist_embedded_texture 检查 _should_skip_embedded_persist。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._persist_embedded_texture)
        assert "_should_skip_embedded_persist()" in src

    def test_persist_uses_texture_digest_for_filename(self):
        """_persist_embedded_texture 用 _texture_digest 命名（支持预计算命中）。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._persist_embedded_texture)
        assert "_texture_digest(" in src

    def test_texture_digest_uses_image_bytes(self):
        """_texture_digest 用图像原始字节（tobytes），非 PNG 编码。"""
        from src.features.composite.render3d import model_loader
        src = inspect.getsource(model_loader._texture_digest)
        assert "tobytes" in src


class TestCacheEntryTexturesExtractedField:
    """ImportCacheEntry 的 textures_extracted 字段 + manifest 序列化。"""

    def test_entry_has_textures_extracted_field(self):
        from src.features.composite.render3d.model_import_cache import ImportCacheEntry
        entry = ImportCacheEntry(digest="abc", source_path="/x.fbx",
                                 source_mtime_ns=0, source_size=0,
                                 created_at=0.0, cache_version=1)
        assert hasattr(entry, "textures_extracted")
        assert entry.textures_extracted is False  # 默认

    def test_put_accepts_textures_extracted(self):
        """cache.put 支持 textures_extracted 参数。"""
        from src.features.composite.render3d.model_import_cache import (
            ModelImportCache, ImportCacheKey)
        import inspect as _inspect
        sig = _inspect.signature(ModelImportCache.put)
        assert "textures_extracted" in sig.parameters
