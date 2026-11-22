"""Native publish branch and lease keys in the present protocol (plan task 1.1).

A non-GL slot publishes through ``TextureLease.make_present_frame(native=...)``.
The descriptor carries the backend, device identity, pixel format, visible
rect, orientation, colour quad and frame number; every descriptor carries a
lease key (pool generation, slot, submission) so an acknowledgement can only
land on the submission it was issued for.  GL descriptors keep their exact
previous shape.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.gpu.present_frame import (
    NATIVE_ORIGIN_TOP_LEFT,
    GpuPresentFrame,
    NativePresentDescriptor,
    OutputSlotPool,
    PresentColorInfo,
    PresentLeaseKey,
    SlotState,
    TextureLease,
)


class _MetalTexture:
    """Stand-in for an MTLTexture proxy: identity, no useful equality."""


class _MetalResource:
    """Duck-typed ``gpu_frame.GpuPlaneResource`` for a BGRA Metal texture."""

    api = "metal"
    device_identity = "metal:GPU-A:1"
    resource_format = "bgra8unorm"
    width = 1920
    height = 1080
    native_handle = _MetalTexture()


def _pool(size: int = 2, generation: int = 3) -> OutputSlotPool:
    pool = OutputSlotPool(size=size, pool_generation=generation)
    pool.register_slot(0, texture_glo=0, width=1920, height=1080, resource=_MetalResource())
    pool.register_slot(1, texture_glo=555, width=1920, height=1080)
    return pool


def _published_native(pool: OutputSlotPool, frame: int = 10) -> GpuPresentFrame:
    lease = pool.acquire_for_writing(generation_id=1, frame_index=frame)
    assert lease is not None
    lease.mark_ready(0)  # native frames carry no GL fence
    return lease.make_present_frame(
        frame=frame, generation_id=1,
        native=NativePresentDescriptor.from_resource(
            _MetalResource(), origin=NATIVE_ORIGIN_TOP_LEFT,
            color=PresentColorInfo(color_range="limited", color_matrix="bt709",
                                   color_primaries="bt709", color_transfer="bt709"),
        ),
    )


class TestNativePublishBranch:
    def test_descriptor_carries_backend_format_rect_orientation_colour(self) -> None:
        present = _published_native(_pool())
        assert present.is_native is True
        assert present.api == "metal"
        # No GL name and no GL fence on a native frame.
        assert present.texture_id == 0
        assert present.fence_ns == 0
        assert present.has_fence is False
        native = present.native
        assert native.api == "metal"
        assert native.device_identity == "metal:GPU-A:1"
        assert native.resource is _MetalResource.native_handle
        assert native.resource_format == "bgra8unorm"
        assert (native.width, native.height) == (1920, 1080)
        assert native.visible_rect == (0, 0, 1920, 1080)
        assert native.origin == NATIVE_ORIGIN_TOP_LEFT
        assert native.color == PresentColorInfo("limited", "bt709", "bt709", "bt709")
        assert present.frame == 10

    def test_visible_rect_defaults_to_slot_size_and_can_be_overridden(self) -> None:
        pool = _pool()
        lease = pool.acquire_for_writing(generation_id=1, frame_index=11)
        assert lease is not None
        lease.mark_ready(0)
        present = lease.make_present_frame(
            frame=11, generation_id=1,
            native=NativePresentDescriptor(
                api="metal", device_identity="metal:GPU-A:1",
                resource=_MetalTexture(), resource_format="rgba16float",
                visible_rect=(8, 8, 1912, 1072),
            ),
        )
        assert present.native is not None
        assert present.native.visible_rect == (8, 8, 1912, 1072)
        # Dimensions the caller left at 0 fall back to the slot's.
        assert (present.native.width, present.native.height) == (1920, 1080)

    def test_extra_copy_is_not_shared_with_caller(self) -> None:
        pool = _pool()
        lease = pool.acquire_for_writing(generation_id=1, frame_index=12)
        assert lease is not None
        lease.mark_ready(0)
        extra = {"gpu_submit_ms": 0.4}
        present = lease.make_present_frame(
            frame=12, generation_id=1, extra=extra,
            native=NativePresentDescriptor.from_resource(
                _MetalResource(), origin=NATIVE_ORIGIN_TOP_LEFT),
        )
        extra["gpu_submit_ms"] = 99.0
        assert present.extra["gpu_submit_ms"] == 0.4
        # The backend is declared for trace consumers.
        assert present.extra["render_backend"] == "metal"

    def test_resource_handle_never_decides_equality_or_repr(self) -> None:
        class _OtherHandle(_MetalResource):
            native_handle = _MetalTexture()

        first = NativePresentDescriptor.from_resource(
            _MetalResource(), origin=NATIVE_ORIGIN_TOP_LEFT)
        second = NativePresentDescriptor.from_resource(
            _OtherHandle(), origin=NATIVE_ORIGIN_TOP_LEFT)
        assert first.resource is not second.resource
        assert first == second
        assert "MTLTexture" not in repr(first)


def _gl_pool() -> OutputSlotPool:
    """A pool with a single GL slot, so acquire can only return that lease."""
    pool = OutputSlotPool(size=1, pool_generation=3)
    pool.register_slot(1, texture_glo=555, width=1920, height=1080)
    return pool


class TestNativePublishGuards:
    def test_gl_slot_cannot_publish_natively(self) -> None:
        pool = _gl_pool()
        gl_lease = pool.acquire_for_writing(generation_id=1, frame_index=2)
        assert gl_lease is not None and gl_lease.is_opengl_path()
        gl_lease.mark_ready(1)
        with pytest.raises(RuntimeError, match="non-GL resource lease"):
            gl_lease.make_present_frame(
                frame=2, generation_id=1,
                native=NativePresentDescriptor.from_resource(
                    _MetalResource(), origin=NATIVE_ORIGIN_TOP_LEFT))

    def test_api_mismatch_between_descriptor_and_slot_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.shared.gpu.present_frame as present_frame

        # Admit a second native API so the descriptor itself is constructible.
        monkeypatch.setattr(present_frame, "NATIVE_PRESENT_APIS", frozenset({"metal", "d3d12"}))
        pool = _pool()
        lease = pool.acquire_for_writing(generation_id=1, frame_index=3)
        assert lease is not None
        lease.mark_ready(0)
        with pytest.raises(RuntimeError, match="does not match slot resource api"):
            lease.make_present_frame(
                frame=3, generation_id=1,
                native=NativePresentDescriptor(
                    api="d3d12", device_identity="", resource=_MetalTexture()))

    def test_opengl_is_not_a_native_publish_api(self) -> None:
        # GL slots publish through the GL branch; there is no "native GL".
        with pytest.raises(ValueError, match="not a native present API"):
            NativePresentDescriptor(
                api="opengl", device_identity="", resource=_MetalTexture())

    def test_publish_requires_ready(self) -> None:
        pool = _pool()
        lease = pool.acquire_for_writing(generation_id=1, frame_index=4)
        assert lease is not None
        with pytest.raises(RuntimeError, match="requires READY"):
            lease.make_present_frame(
                frame=4, generation_id=1,
                native=NativePresentDescriptor.from_resource(
                    _MetalResource(), origin=NATIVE_ORIGIN_TOP_LEFT))

    @pytest.mark.parametrize("origin", ["upside_down", "", "top"])
    def test_unknown_orientation_is_rejected(self, origin: str) -> None:
        with pytest.raises(ValueError, match="origin"):
            NativePresentDescriptor(
                api="metal", device_identity="", resource=_MetalTexture(), origin=origin)


class TestGlBranchUnchanged:
    def test_gl_descriptor_keeps_its_previous_shape(self) -> None:
        pool = _gl_pool()
        gl_lease = pool.acquire_for_writing(generation_id=1, frame_index=11)
        assert gl_lease is not None and gl_lease.texture_glo == 555
        gl_lease.mark_ready(999)
        present = gl_lease.make_present_frame(
            frame=11, generation_id=1, color_space="bt709", is_gl_oriented=True,
            source_kinds=("macos_vt_zero_copy",), extra={"render_backend": "opengl"},
        )
        assert present.is_native is False
        assert present.native is None
        assert present.api == "opengl"
        assert (present.texture_id, present.width, present.height) == (555, 1920, 1080)
        assert present.fence_ns == 999 and present.has_fence is True
        assert present.color_space == "bt709"
        assert present.is_gl_oriented is True
        assert present.source_kinds == ("macos_vt_zero_copy",)
        assert present.extra == {"render_backend": "opengl"}
        assert present.as_legacy_tuple() == (555, 1920, 1080)

    def test_old_consumers_still_recognise_a_gl_descriptor(self) -> None:
        # ProgramViewerWidget.set_frame duck-types on these two attributes.
        present = GpuPresentFrame(texture_id=555, width=1920, height=1080,
                                  frame=1, generation_id=1, slot_id=1)
        assert hasattr(present, "texture_id") and hasattr(present, "slot_id")
        assert present.is_native is False
        assert present.lease_key is None

    def test_directly_built_descriptor_has_no_lease_key(self) -> None:
        present = GpuPresentFrame(texture_id=1, width=2, height=2, frame=0,
                                  generation_id=0, slot_id=0)
        assert present.lease_key is None


class TestLeaseKey:
    def test_key_identifies_pool_slot_and_submission(self) -> None:
        pool = _pool(generation=7)
        first = _published_native(pool, frame=1)
        assert first.lease_key == PresentLeaseKey(pool_generation=7, slot_id=0, submission_id=1)
        pool.ack(0, first.lease_key)
        second = _published_native(pool, frame=2)
        assert second.lease_key == PresentLeaseKey(pool_generation=7, slot_id=0, submission_id=2)

    def test_gl_descriptors_carry_the_key_too(self) -> None:
        pool = _pool(generation=9)
        lease = pool.acquire_for_writing(generation_id=1, frame_index=5)
        assert lease is not None
        lease.mark_ready(1)
        present = lease.make_present_frame(frame=5, generation_id=1)
        assert present.lease_key == PresentLeaseKey(9, lease.slot_id, 1)

    def test_pools_get_distinct_generations_by_default(self) -> None:
        a, b = OutputSlotPool(size=1), OutputSlotPool(size=1)
        assert a.generation != b.generation
        assert a.stats()["pool_generation"] == a.generation


class TestAckWithLeaseKey:
    def test_duplicate_ack_is_idempotent(self) -> None:
        pool = _pool()
        present = _published_native(pool)
        assert pool.ack(present.slot_id, present.lease_key) is True
        assert pool.get_slot(0).state is SlotState.RELEASABLE
        assert pool.ack(present.slot_id, present.lease_key) is False
        assert pool.get_slot(0).state is SlotState.RELEASABLE
        assert pool.stats()["stale_acks"] == 0

    def test_ack_for_an_older_submission_does_not_release_the_current_frame(
        self,
    ) -> None:
        pool = _pool()
        first = _published_native(pool, frame=1)
        # The viewer confirms frame 1 …
        assert pool.ack(0, first.lease_key) is True
        # … the slot is reused for frame 2 …
        second = _published_native(pool, frame=2)
        assert second.lease_key.submission_id == 2
        # … and a duplicate of frame 1's confirmation arrives late.
        assert pool.ack(0, first.lease_key) is False
        slot = pool.get_slot(0)
        assert slot.state is SlotState.READY
        assert slot.frame_index == 2
        assert pool.stats()["stale_acks"] == 1
        # Slot-id-only acknowledgement (legacy callers) still targets frame 2.
        assert pool.ack(0) is True

    def test_late_ack_from_a_retired_pool_spares_the_new_pool(self) -> None:
        retired = _pool(generation=1)
        old_frame = _published_native(retired, frame=100)
        # Resize: a new pool with the same slot ids takes over.
        current = _pool(generation=2)
        new_frame = _published_native(current, frame=101)
        assert new_frame.lease_key.pool_generation == 2

        assert current.ack(0, old_frame.lease_key) is False
        assert current.get_slot(0).state is SlotState.READY
        assert current.get_slot(0).frame_index == 101
        # The acknowledgement belongs to the retired pool and lands there.
        assert retired.ack(0, old_frame.lease_key) is True

    def test_discard_with_stale_key_leaves_the_slot_alone(self) -> None:
        pool = _pool()
        present = _published_native(pool)
        stale = replace(present.lease_key, submission_id=present.lease_key.submission_id - 1)
        assert pool.discard(0, stale) is False
        assert pool.get_slot(0).state is SlotState.READY
        assert pool.discard(0, present.lease_key) is True

    def test_mark_in_viewer_only_for_the_matching_submission(self) -> None:
        pool = _pool()
        present = _published_native(pool)
        stale = replace(present.lease_key, submission_id=99)
        assert pool.mark_in_viewer(0, stale) is False
        assert pool.get_slot(0).state is SlotState.READY
        assert pool.mark_in_viewer(0, present.lease_key) is True
        assert pool.get_slot(0).state is SlotState.IN_VIEWER

    def test_ack_without_key_keeps_legacy_semantics(self) -> None:
        pool = _pool()
        present = _published_native(pool)
        assert pool.ack(0) is True
        assert pool.stats()["stale_acks"] == 0


class TestConsumerValidation:
    def test_matching_consumer_accepts(self) -> None:
        present = _published_native(_pool())
        assert present.validate_for_consumer(
            api="metal", device_identity="metal:GPU-A:1",
            resource_formats=("bgra8unorm", "rgba16float")) == ""

    def test_device_change_is_a_machine_readable_reason(self) -> None:
        present = _published_native(_pool())
        assert present.validate_for_consumer(
            api="metal", device_identity="metal:GPU-B:2") == (
            "device_mismatch:frame=metal:GPU-A:1|consumer=metal:GPU-B:2")

    def test_api_and_format_mismatch(self) -> None:
        present = _published_native(_pool())
        assert "graphics_api_mismatch:metal" in present.validate_for_consumer(api="opengl")
        assert present.validate_for_consumer(
            api="metal", device_identity="metal:GPU-A:1",
            resource_formats=("rgba16float",)) == (
            "resource_format_unsupported:bgra8unorm|consumer=rgba16float")

    def test_unknown_device_on_either_side_skips_the_device_check(self) -> None:
        present = _published_native(_pool())
        # The consumer did not state a device: no comparison is possible.
        assert present.validate_for_consumer(api="metal", device_identity="") == ""
        anonymous = NativePresentDescriptor.from_resource(
            type("R", (), {"api": "metal", "device_identity": "", "resource_format": "bgra8unorm",
                           "width": 4, "height": 4, "native_handle": None})(),
            origin=NATIVE_ORIGIN_TOP_LEFT)
        assert anonymous.validate_for_consumer(api="metal", device_identity="metal:GPU-A:1") == ""

    def test_gl_descriptor_cannot_feed_a_native_consumer(self) -> None:
        present = GpuPresentFrame(texture_id=5, width=2, height=2, frame=0,
                                  generation_id=0, slot_id=0)
        assert present.validate_for_consumer(api="opengl") == ""
        assert present.validate_for_consumer(api="metal") == (
            "graphics_api_mismatch:opengl|consumer=metal")


class TestResourceSlotRegistration:
    def test_resource_slot_registers_without_a_gl_name(self) -> None:
        pool = OutputSlotPool(size=1)
        lease = pool.register_slot(0, texture_glo=0, width=4, height=4, resource=_MetalResource())
        assert lease.is_opengl_path() is False
        assert lease.resolved_handle() is _MetalResource.native_handle
        assert lease.texture_glo == 0

    def test_gl_slot_still_requires_a_positive_name(self) -> None:
        pool = OutputSlotPool(size=1)
        with pytest.raises(ValueError, match="texture_glo must be > 0"):
            pool.register_slot(0, texture_glo=0, width=4, height=4)
        with pytest.raises(ValueError, match="width/height must be > 0"):
            pool.register_slot(0, texture_glo=1, width=0, height=4)

    def test_slot_key_is_stable_while_writing(self) -> None:
        pool = _pool()
        lease = pool.acquire_for_writing(generation_id=1, frame_index=1)
        assert isinstance(lease, TextureLease)
        key = lease.lease_key
        lease.mark_ready(0)
        lease.begin_viewer_use()
        assert lease.lease_key == key
