"""RD5-02: macOS IOSurface mapping has no NumPy staging copy."""
from __future__ import annotations

import ctypes
import platform

import pytest

from src.shared.media.macos_vt.topos_surface import (
    MACOS_TOPOS_SURFACE_AVAILABLE,
    ToposIOSurfacePool,
    ToposSurfaceUnavailable,
)
from src.shared.media.frame_path import DecodeSurfaceState


@pytest.mark.skipif(not MACOS_TOPOS_SURFACE_AVAILABLE, reason="macOS IOSurface unavailable")
def test_iosurface_pool_maps_topos_planes_and_reuses_slot() -> None:
    try:
        pool = ToposIOSurfacePool(
            17, 9, pixel_format=0, plane_count=3, ring_size=1,
            fence_callback=lambda _surface: None,
        )
    except (ToposSurfaceUnavailable, OSError) as exc:
        pytest.skip(str(exc))
    try:
        lease = pool.acquire()
        assert lease is not None
        lease.acquire()
        views = lease.map()
        assert lease.state is DecodeSurfaceState.MAPPED
        assert views[0].stride >= 17
        assert views[1].stride >= 9
        # Write through the mapped C pointer.  This is the same address the
        # native codec receives; no NumPy array or staging copy is involved.
        first = ctypes.cast(views[0].pixels, ctypes.POINTER(ctypes.c_uint16))
        first[0] = 777
        lease.unmap()
        lease.fence()
        lease.release()
        assert lease.state is DecodeSurfaceState.RELEASED
        again = pool.acquire()
        assert again is not None
        again.acquire()
        again.map()
        again.unmap()
        again.fence()
        again.release()
    finally:
        pool.close()


@pytest.mark.skipif(platform.system() == "Darwin", reason="non-macOS fallback test")
def test_iosurface_pool_fails_closed_off_macos() -> None:
    with pytest.raises(ToposSurfaceUnavailable):
        ToposIOSurfacePool(16, 8)


# ---------------- C18（2026-09-27）：lease 异常清理不泄漏槽位 ----------------

@pytest.mark.skipif(not MACOS_TOPOS_SURFACE_AVAILABLE, reason="macOS IOSurface unavailable")
def test_lease_released_when_frame_construction_fails(monkeypatch) -> None:
    """C18 反例：post-fence 段（GpuDecodedFrame 构造）抛错 → lease 必须被
    释放——历史直接返回 None，槽位随失败累积泄漏，8 槽耗尽后永久回落 CPU。"""
    import src.shared.media.gpu_frame as gpu_frame_mod
    from src.shared.media.topos_source import ToposMediaSource

    src = ToposMediaSource("tests/test_videos/2K_topos_422_Proxy.mov")
    src.open()
    try:
        def boom(*_a, **_kw):
            raise RuntimeError("injected frame construction failure")

        monkeypatch.setattr(gpu_frame_mod, "GpuDecodedFrame", boom)
        assert src.read_frame_metal(0) is None  # 首次调用同时惰性建池
        monkeypatch.undo()
        pool = src._metal_surface_pool
        assert pool is not None
        total = len(pool._slots)

        for attempt in range(1, total + 3):  # 超过槽位数：无泄漏才不饿死
            monkeypatch.setattr(gpu_frame_mod, "GpuDecodedFrame", boom)
            try:
                assert src.read_frame_metal(attempt) is None
            finally:
                monkeypatch.undo()
            lease = pool.acquire()
            assert lease is not None, f"attempt {attempt}: slot leaked"
            lease.acquire()
            lease.abort()  # 恢复探针：acquired 未映射态的规范归还路径
    finally:
        src.close()
