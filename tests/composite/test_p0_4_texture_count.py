"""P0-4：texture_binds/uploads 计数完整回填测试。

覆盖审计发现的两个缺口：
1. _bind_descriptor 里 was_last_cached 在 get_or_upload 之前读（延迟一帧 bug）
2. _bind_null_material_samplers / _unbind_material_textures 的 glBindTexture(0)
   没计入 texture_binds

注：用源码级断言，与 P0-P8 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect


class TestTextureUploadCountFix:
    """was_last_cached 读取顺序修复（P0-4 核心 bug）。"""

    def test_was_cached_read_after_get_or_upload(self):
        """_bind_descriptor 在 get_or_upload 之后读 was_last_cached（之前在前面）。

        之前：_was_cached = loader.was_last_cached（读到上一次状态）
              tex = loader.get_or_upload(...)
        现在：tex = loader.get_or_upload(...)
              _was_cached = loader.was_last_cached（读到本次状态）
        """
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._bind_material_textures)
        # 找 _bind_descriptor 函数体
        assert "def _bind_descriptor" in src
        # get_or_upload 调用应在 was_last_cached 读取之前
        idx_upload = src.find("get_or_upload")
        idx_cached = src.find("_was_cached = self._material_tex_loader.was_last_cached")
        assert idx_upload > 0
        assert idx_cached > 0
        assert idx_upload < idx_cached, "was_last_cached 应在 get_or_upload 之后读"

    def test_bind_descriptor_counts_uploads(self):
        """_bind_descriptor 在 cache miss 时累加 texture_uploads。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._bind_material_textures)
        assert "self._perf_stats.texture_uploads += 1" in src
        assert "if not _was_cached:" in src

    def test_bind_descriptor_counts_binds(self):
        """_bind_descriptor 在 glBindTexture 后累加 texture_binds。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._bind_material_textures)
        assert "self._perf_stats.texture_binds += 1" in src


class TestNullBindCounting:
    """_bind_null_material_samplers / _unbind_material_textures 的 bind 计数。"""

    def test_unbind_material_textures_counts_binds(self):
        """_unbind_material_textures 每次 glBindTexture(0) 计 texture_binds。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._unbind_material_textures)
        assert "self._perf_stats.texture_binds += 1" in src

    def test_bind_null_material_samplers_counts_binds(self):
        """_bind_null_material_samplers 每次 glBindTexture(0) 计 texture_binds。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._bind_null_material_samplers)
        assert "self._perf_stats.texture_binds += 1" in src


class TestLoaderCacheFlag:
    """MaterialTextureLoader.was_last_cached 在 get_or_upload 后正确设置。"""

    def test_get_or_upload_sets_cached_flag(self):
        """get_or_upload 命中时 was_last_cached=True，未命中 False。"""
        from src.features.composite.render3d.material_texture_loader import (
            MaterialTextureLoader)
        src = inspect.getsource(MaterialTextureLoader.get_or_upload)
        assert "self.was_last_cached = True" in src   # cache hit
        assert "self.was_last_cached = False" in src  # cache miss
