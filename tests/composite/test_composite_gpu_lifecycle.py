"""P0-08 lifecycle contracts for the composite GPU renderer and pool."""

from unittest.mock import Mock, patch

import pytest

from src.features.composite.render.composite_renderer import CompositeRenderer
from src.features.composite.render.gpu_resource_pool import (
    GPUResourcePool,
    TextureInfo,
    TextureKey,
    TextureUsage,
)


pytestmark = pytest.mark.composite_core


class _FakeGLObject:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    def release(self) -> None:
        self._log.append(self.name)


def test_output_ring_defers_release_until_fence_signals() -> None:
    log: list[str] = []
    renderer = CompositeRenderer.__new__(CompositeRenderer)
    renderer.ctx = Mock()
    renderer.fbo_ring = [_FakeGLObject("fbo", log) for _ in range(3)]
    renderer.tex_ring = [_FakeGLObject("tex", log) for _ in range(3)]
    renderer.write_idx = 1
    renderer._deferred_output_releases = []
    renderer._create_output_release_fence = Mock(return_value="fence")

    renderer._release_output_buffers()

    assert log == []
    assert len(renderer._deferred_output_releases) == 1
    assert renderer.fbo_ring == [None, None, None]
    assert renderer.tex_ring == [None, None, None]

    with patch("OpenGL.GL.glClientWaitSync", return_value=0x911B):
        assert renderer._drain_deferred_output_releases() == 0
    assert log == []

    with patch("OpenGL.GL.glClientWaitSync", return_value=0x911A), \
            patch("OpenGL.GL.glDeleteSync"):
        assert renderer._drain_deferred_output_releases() == 3
    assert log == ["fbo", "tex"] * 3
    assert renderer._deferred_output_releases == []


def test_output_ring_forced_drain_releases_after_finish() -> None:
    log: list[str] = []
    renderer = CompositeRenderer.__new__(CompositeRenderer)
    renderer.ctx = Mock()
    renderer.fbo_ring = [_FakeGLObject("fbo", log), None, None]
    renderer.tex_ring = [_FakeGLObject("tex", log), None, None]
    renderer.write_idx = 0
    renderer._deferred_output_releases = []
    renderer._create_output_release_fence = Mock(return_value="fence")

    renderer._release_output_buffers()
    with patch("OpenGL.GL.glClientWaitSync", return_value=0x911B), \
            patch("OpenGL.GL.glDeleteSync"):
        assert renderer._drain_deferred_output_releases(block=True) == 1

    renderer.ctx.finish.assert_called_once_with()
    assert log == ["fbo", "tex"]


def test_pool_eviction_defers_unsignaled_texture_release() -> None:
    fb = Mock()
    tex = Mock(glo=7)
    fb._active_textures = {tex}
    pool = GPUResourcePool(fb, enable_vram_monitoring=False)
    key = TextureKey(32, 32, usage=TextureUsage.CACHE)
    info = TextureInfo(glo=7, key=key, fence="fence")
    pool._pool_lru[7] = info
    pool._pooled_textures[key] = [tex]
    pool._is_fence_signaled = Mock(return_value=False)
    pool._delete_fence = Mock()
    pool._destroy_texture = Mock()

    assert pool._try_evict_texture(7, info) is True
    assert pool._destroy_texture.call_count == 0
    assert len(pool._deferred_releases) == 1
    assert pool.pooled_count() == 0

    pool._is_fence_signaled.return_value = True
    pool.drain_deferred_releases()
    pool._destroy_texture.assert_called_once_with(tex)
    pool._delete_fence.assert_called_once_with("fence")


def test_pool_force_cleanup_finishes_before_destruction() -> None:
    fb = Mock()
    fb._active_textures = set()
    fb.ctx = Mock()
    fb.cleanup = Mock()
    pool = GPUResourcePool(fb, enable_vram_monitoring=False)
    pool.force_cleanup("test")
    fb.ctx.finish.assert_called_once_with()
