"""P2-03/P2-04: opaque plane resource contract + backend-owned release.

Verifies plan §3.3 rules: bare texture_id is OpenGL-only; non-GL planes
must carry a GpuPlaneResource; release is exactly-once across drop/seek/
cancel/close paths; release_when_fenced never frees an unfenced resource.
"""
from __future__ import annotations

import pytest

from src.shared.gpu.present_frame import SlotState, TextureLease
from src.shared.media.gpu_frame import (
    GpuDecodedFrame,
    GpuFramePlane,
    GpuFrameRelease,
    GpuPlaneResource,
)


# ---------------------------------------------------------------------------
# P2-03: GpuPlaneResource / GpuFramePlane
# ---------------------------------------------------------------------------

def test_opengl_plane_without_resource_stays_compatible() -> None:
    plane = GpuFramePlane("Y", "opengl", 1, "2d", 3840, 2160)
    assert plane.resource is None
    assert plane.resolved_handle() == 1


def test_non_opengl_plane_requires_resource() -> None:
    with pytest.raises(ValueError, match="requires a GpuPlaneResource"):
        GpuFramePlane("Y", "metal", 123, "2d", 1920, 1080)


def test_metal_plane_carries_opaque_handle() -> None:
    class _FakeMTLTexture:  # 上层不得解释，只需可持有
        pass

    tex = _FakeMTLTexture()
    resource = GpuPlaneResource(
        api="metal", native_handle=tex, resource_format="r16uint",
        width=1920, height=1080, ownership="borrowed",
    )
    plane = GpuFramePlane(
        "Y", "metal", 0, "2d", 1920, 1080, resource=resource)
    assert plane.resolved_handle() is tex
    desc = resource.as_descriptor()
    assert desc["api"] == "metal"
    assert desc["resource_format"] == "r16uint"
    assert desc["ownership"] == "borrowed"
    assert "native_handle" not in desc  # 诊断描述不泄漏裸 handle


def test_resource_format_vocabulary_is_backend_neutral() -> None:
    resource = GpuPlaneResource("opengl", 7, "rg8unorm", 960, 540)
    assert "GL_" not in resource.resource_format


# ---------------------------------------------------------------------------
# P2-03: TextureLease reuse (same state machine, no second lease protocol)
# ---------------------------------------------------------------------------

def test_texture_lease_from_resource_shares_state_machine() -> None:
    resource = GpuPlaneResource(
        "metal", object(), "bgra8unorm", 1920, 1080)
    lease = TextureLease.from_resource(3, resource, 1920, 1080)
    assert lease.state is SlotState.FREE
    assert lease.is_opengl_path() is False

    lease.begin_writing(generation_id=1, frame_index=0)
    lease.mark_ready(fence_ns=0)
    lease.begin_viewer_use()
    lease.ack()
    lease.release()
    assert lease.state is SlotState.FREE
    assert lease.resolved_handle() is resource.native_handle


def test_gl_lease_fast_path_unchanged() -> None:
    lease = TextureLease(slot_id=0, texture_glo=42, width=64, height=64)
    assert lease.is_opengl_path() is True
    assert lease.resolved_handle() == 42


# ---------------------------------------------------------------------------
# P2-04: exactly-once release across drop / seek / cancel / close
# ---------------------------------------------------------------------------

def _frame_with_callback(track: list[str], tag: str) -> GpuDecodedFrame:
    return GpuDecodedFrame(
        frame_index=0, pts=None, time_seconds=0.0,
        width=1920, height=1080, visible_rect=None,
        pixel_format="nv12", bit_depth=8,
        planes=[GpuFramePlane("Y", "opengl", 1, "2d", 1920, 1080)],
        release=GpuFrameRelease(
            release_callback=lambda: track.append(tag),
            destructor_policy="thread_safe",
        ),
    )


def test_drop_and_cancel_are_exactly_once() -> None:
    released: list[str] = []
    frame = _frame_with_callback(released, "drop")
    frame.release.release()
    frame.release.release()  # 重复调用（再次丢弃/取消）无害
    assert released == ["drop"]


def test_seek_generation_release_is_exactly_once() -> None:
    released: list[str] = []
    old_frame = _frame_with_callback(released, "seek_old")
    new_frame = _frame_with_callback(released, "seek_new")
    old_frame.release.release()  # seek 后旧 generation 回收
    new_frame.release.release()
    old_frame.release.release()
    assert released == ["seek_old", "seek_new"]


def test_release_when_fenced_blocks_until_signaled() -> None:
    released: list[str] = []
    frame = _frame_with_callback(released, "fenced")
    frame.release.fence = object()

    assert frame.release.release_when_fenced(lambda _f: False) is False
    assert released == []  # fence 未确认绝不释放

    assert frame.release.release_when_fenced(lambda _f: True) is True
    assert released == ["fenced"]

    # 之后任何路径的 release 都是 no-op（窗口关闭兜底）
    frame.release.release()
    assert released == ["fenced"]


def test_release_when_fenced_without_fence_releases_immediately() -> None:
    released: list[str] = []
    frame = _frame_with_callback(released, "nofence")
    assert frame.release.release_when_fenced(lambda _f: False) is True
    assert released == ["nofence"]


def test_owner_backend_recorded_for_dispatch() -> None:
    frame = _frame_with_callback([], "x")
    assert frame.release.owner_backend == "opengl"
    frame.release.owner_backend = "metal"  # backend-owned dispatch
    assert frame.release.owner_backend == "metal"
