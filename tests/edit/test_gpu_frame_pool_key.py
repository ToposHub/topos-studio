"""
Batch 4 tests: GpuFramePool cache key 强化。
对应缺陷计划 RP-P0-06 / P1-2。

核心验收：同 clip/frame 在不同 source（proxy/master/cache）、不同 pixel_format、
不同 size 时不复用 GPU texture，避免显示错帧。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    GpuFrameHandle,
    GpuFramePool,
    FrameHandle,
    FrameIdentity,
    SourceKind,
)


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


def _make_frame(
    frame_index: int = 0,
    clip_id: str = "c1",
    source_kind: SourceKind = SourceKind.DISK_PROXY,
    proxy_key: str | None = "proxy-v1",
    render_cache_key: str | None = None,
    pixel_format: str = "yuv420p",
    bit_depth: int = 8,
    width: int = 1920,
    height: int = 1080,
) -> FrameHandle:
    return FrameHandle(
        identity=FrameIdentity(
            timeline_id="t1", clip_id=clip_id, asset_id="a",
            source_path="/x.mov", source_frame=frame_index,
            source_kind=source_kind,
            proxy_key=proxy_key, render_cache_key=render_cache_key,
        ),
        frame_index=frame_index,
        width=width, height=height,
        pixel_format=pixel_format, bit_depth=bit_depth,
        color_space=None, pts_seconds=0.0,
        source_kind=source_kind,
        payload=None, payload_type="cpu_ndarray",
    )


def _make_gpu_handle(frame: FrameHandle, tex_id: int = 42) -> GpuFrameHandle:
    return GpuFrameHandle(
        identity=frame.identity,
        texture_ids=(tex_id,),
        texture_type="rgb",
        width=frame.width, height=frame.height,
        bit_depth=frame.bit_depth,
        color_space=None,
        generation_id=0,
        release=None,
    )


# =============================================================================
# Cache key 强度
# =============================================================================


class TestCacheKeyStrength:
    """不同 source/format 的同 clip/frame 不复用 texture。"""

    def test_same_frame_same_source_cached(self, qapp) -> None:
        """相同参数的帧第二次查询命中 cache。"""
        pool = GpuFramePool()
        upload_count = [0]
        def upload(f):
            upload_count[0] += 1
            return _make_gpu_handle(f)
        pool.set_upload_function(upload)

        frame = _make_frame(10)
        gh1 = pool.ensure_gpu_frame(frame, generation_id=1)
        gh2 = pool.ensure_gpu_frame(frame, generation_id=1)
        assert gh1 is gh2
        assert upload_count[0] == 1  # 只上传一次

    def test_different_source_kind_not_shared(self, qapp) -> None:
        """DISK_PROXY 和 MASTER 的同帧不复用（防错帧）。"""
        pool = GpuFramePool()
        uploads = []
        def upload(f):
            gh = _make_gpu_handle(f, tex_id=len(uploads) + 100)
            uploads.append(gh)
            return gh
        pool.set_upload_function(upload)

        proxy_frame = _make_frame(10, source_kind=SourceKind.DISK_PROXY)
        master_frame = _make_frame(10, source_kind=SourceKind.MASTER)
        gh1 = pool.ensure_gpu_frame(proxy_frame, generation_id=1)
        gh2 = pool.ensure_gpu_frame(master_frame, generation_id=1)
        assert gh1 is not gh2
        assert len(uploads) == 2

    def test_different_proxy_key_not_shared(self, qapp) -> None:
        """proxy 版本变化（key 变）不复用旧 proxy texture。"""
        pool = GpuFramePool()
        uploads = []
        def upload(f):
            gh = _make_gpu_handle(f, tex_id=len(uploads) + 100)
            uploads.append(gh)
            return gh
        pool.set_upload_function(upload)

        f1 = _make_frame(10, proxy_key="proxy-v1")
        f2 = _make_frame(10, proxy_key="proxy-v2")  # proxy 重新生成
        gh1 = pool.ensure_gpu_frame(f1, generation_id=1)
        gh2 = pool.ensure_gpu_frame(f2, generation_id=1)
        assert gh1 is not gh2
        assert len(uploads) == 2

    def test_different_pixel_format_not_shared(self, qapp) -> None:
        """yuv420p 和 yuv422p10le 不复用（不同纹理格式）。"""
        pool = GpuFramePool()
        uploads = []
        def upload(f):
            gh = _make_gpu_handle(f, tex_id=len(uploads) + 100)
            uploads.append(gh)
            return gh
        pool.set_upload_function(upload)

        f1 = _make_frame(10, pixel_format="yuv420p", bit_depth=8)
        f2 = _make_frame(10, pixel_format="yuv422p10le", bit_depth=10)
        gh1 = pool.ensure_gpu_frame(f1, generation_id=1)
        gh2 = pool.ensure_gpu_frame(f2, generation_id=1)
        assert gh1 is not gh2

    def test_different_size_not_shared(self, qapp) -> None:
        """1080p 和 4K 不复用。"""
        pool = GpuFramePool()
        uploads = []
        def upload(f):
            gh = _make_gpu_handle(f, tex_id=len(uploads) + 100)
            uploads.append(gh)
            return gh
        pool.set_upload_function(upload)

        f1 = _make_frame(10, width=1920, height=1080)
        f2 = _make_frame(10, width=3840, height=2160)
        gh1 = pool.ensure_gpu_frame(f1, generation_id=1)
        gh2 = pool.ensure_gpu_frame(f2, generation_id=1)
        assert gh1 is not gh2

    def test_different_render_cache_key_not_shared(self, qapp) -> None:
        """render cache 版本变化不复用。"""
        pool = GpuFramePool()
        uploads = []
        def upload(f):
            gh = _make_gpu_handle(f, tex_id=len(uploads) + 100)
            uploads.append(gh)
            return gh
        pool.set_upload_function(upload)

        f1 = _make_frame(10, source_kind=SourceKind.RENDER_CACHE,
                         proxy_key=None, render_cache_key="rc-v1")
        f2 = _make_frame(10, source_kind=SourceKind.RENDER_CACHE,
                         proxy_key=None, render_cache_key="rc-v2")
        gh1 = pool.ensure_gpu_frame(f1, generation_id=1)
        gh2 = pool.ensure_gpu_frame(f2, generation_id=1)
        assert gh1 is not gh2

    def test_different_clip_not_shared(self, qapp) -> None:
        """同素材不同 clip 不复用（cursor 隔离）。"""
        pool = GpuFramePool()
        uploads = []
        def upload(f):
            gh = _make_gpu_handle(f, tex_id=len(uploads) + 100)
            uploads.append(gh)
            return gh
        pool.set_upload_function(upload)

        f1 = _make_frame(10, clip_id="clip-A")
        f2 = _make_frame(10, clip_id="clip-B")
        gh1 = pool.ensure_gpu_frame(f1, generation_id=1)
        gh2 = pool.ensure_gpu_frame(f2, generation_id=1)
        assert gh1 is not gh2


# =============================================================================
# Cache key 单元测试
# =============================================================================


class TestCacheKeyUnit:
    def test_key_includes_all_fields(self) -> None:
        """_build_cache_key 应纳入所有影响 texture 内容的字段。"""
        frame = _make_frame(10)
        key = GpuFramePool._build_cache_key(frame, generation_id=1)
        # 13 个字段
        assert len(key) == 13
        assert key[1] == "c1"  # clip_id
        assert key[5] is SourceKind.DISK_PROXY  # source_kind
        assert key[6] == "proxy-v1"  # proxy_key
        assert key[8] == "yuv420p"  # pixel_format
        assert key[9] == 8  # bit_depth

    def test_key_changes_with_source_kind(self) -> None:
        f1 = _make_frame(10, source_kind=SourceKind.DISK_PROXY)
        f2 = _make_frame(10, source_kind=SourceKind.MASTER)
        k1 = GpuFramePool._build_cache_key(f1, 1)
        k2 = GpuFramePool._build_cache_key(f2, 1)
        assert k1 != k2

    def test_key_changes_with_generation(self) -> None:
        frame = _make_frame(10)
        k1 = GpuFramePool._build_cache_key(frame, 1)
        k2 = GpuFramePool._build_cache_key(frame, 2)
        assert k1 != k2
