"""
Batch 1 unit tests: GpuPresentFrame + TextureLease + OutputSlotPool.
对应缺陷计划 RP-P0-01/02/12 / §3.1 验收测试。

核心验收：
- slot 状态机 FREE->WRITING->READY->IN_VIEWER->RELEASABLE->FREE。
- 未 ack 的 slot 永不复用（slot reuse stress）。
- pool 满时 acquire 返回 None（back-pressure，不阻塞）。
- fence cleanup 在 ack 时被调用。
- GpuPresentFrame 携带完整 descriptor（texture/fence/slot/generation）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    OutputSlotPool,
    SlotState,
    TextureLease,
)


# =============================================================================
# TextureLease 状态机
# =============================================================================


class TestTextureLeaseStateMachine:
    def test_initial_state_is_free(self) -> None:
        lease = TextureLease(slot_id=0, texture_glo=101, width=1920, height=1080)
        assert lease.state is SlotState.FREE
        assert lease.is_writable

    def test_full_lifecycle(self) -> None:
        lease = TextureLease(slot_id=0, texture_glo=101, width=1920, height=1080)
        # FREE -> WRITING
        lease.begin_writing(generation_id=1, frame_index=10)
        assert lease.state is SlotState.WRITING
        assert not lease.is_writable
        # WRITING -> READY
        lease.mark_ready(fence_ns=999)
        assert lease.state is SlotState.READY
        assert lease.is_ready_for_viewer
        assert lease.fence_ns == 999
        # READY -> IN_VIEWER
        lease.begin_viewer_use()
        assert lease.state is SlotState.IN_VIEWER
        # IN_VIEWER -> RELEASABLE
        lease.ack()
        assert lease.state is SlotState.RELEASABLE
        assert lease.acked
        # RELEASABLE -> FREE
        lease.release()
        assert lease.state is SlotState.FREE

    def test_begin_writing_from_releasable_allowed(self) -> None:
        """RELEASABLE 状态可直接 begin_writing（热路径优化）。"""
        lease = TextureLease(slot_id=0, texture_glo=101, width=1920, height=1080)
        lease.begin_writing(generation_id=1, frame_index=10)
        lease.mark_ready(fence_ns=1)
        lease.ack()
        assert lease.state is SlotState.RELEASABLE
        lease.begin_writing(generation_id=2, frame_index=11)
        assert lease.state is SlotState.WRITING
        assert lease.generation_id == 2
        assert lease.frame_index == 11

    def test_begin_writing_from_writing_raises(self) -> None:
        lease = TextureLease(slot_id=0, texture_glo=101, width=1920, height=1080)
        lease.begin_writing(generation_id=1, frame_index=10)
        with pytest.raises(RuntimeError):
            lease.begin_writing(generation_id=1, frame_index=11)

    def test_begin_writing_from_ready_raises(self) -> None:
        """未 ack 的 READY slot 不能重新写入（防混帧核心）。"""
        lease = TextureLease(slot_id=0, texture_glo=101, width=1920, height=1080)
        lease.begin_writing(generation_id=1, frame_index=10)
        lease.mark_ready(fence_ns=1)
        with pytest.raises(RuntimeError):
            lease.begin_writing(generation_id=1, frame_index=11)

    def test_mark_ready_from_wrong_state_raises(self) -> None:
        lease = TextureLease(slot_id=0, texture_glo=101, width=1920, height=1080)
        with pytest.raises(RuntimeError):
            lease.mark_ready(fence_ns=1)

    def test_ack_clears_fence(self) -> None:
        """ack 后 fence 应被清理（Viewer 已等待完成）。"""
        cleaned = []
        lease = TextureLease(slot_id=0, texture_glo=101, width=1920, height=1080)
        lease.begin_writing(generation_id=1, frame_index=10)
        lease.mark_ready(
            fence_ns=999,
            fence_cleanup=lambda f: cleaned.append(f),
        )
        lease.begin_viewer_use()
        lease.ack()
        assert cleaned == [999]
        assert lease.fence_ns == 0

    def test_ack_from_ready_directly_allowed(self) -> None:
        """同步 blit 完成时可跳过 IN_VIEWER 直接 ack。"""
        lease = TextureLease(slot_id=0, texture_glo=101, width=1920, height=1080)
        lease.begin_writing(generation_id=1, frame_index=10)
        lease.mark_ready(fence_ns=1)
        lease.ack()  # 直接从 READY ack
        assert lease.state is SlotState.RELEASABLE


# =============================================================================
# GpuPresentFrame descriptor
# =============================================================================


class TestGpuPresentFrame:
    def test_basic_fields(self) -> None:
        f = GpuPresentFrame(
            texture_id=101, width=1920, height=1080,
            frame=10, generation_id=1, slot_id=0,
            fence_ns=999,
        )
        assert f.texture_id == 101
        assert f.has_fence
        assert f.slot_id == 0

    def test_frozen(self) -> None:
        f = GpuPresentFrame(
            texture_id=101, width=1920, height=1080,
            frame=10, generation_id=1, slot_id=0,
        )
        with pytest.raises(Exception):
            f.texture_id = 999  # type: ignore[misc]

    def test_as_legacy_tuple_compat(self) -> None:
        """过渡期兼容旧 (tex_id, w, h) tuple。"""
        f = GpuPresentFrame(
            texture_id=101, width=1920, height=1080,
            frame=10, generation_id=1, slot_id=0,
        )
        assert f.as_legacy_tuple() == (101, 1920, 1080)

    def test_carries_metadata(self) -> None:
        f = GpuPresentFrame(
            texture_id=101, width=1920, height=1080,
            frame=10, generation_id=1, slot_id=0,
            color_space="bt709",
            is_gl_oriented=True,
            source_kinds=("disk_proxy",),
        )
        assert f.color_space == "bt709"
        assert f.is_gl_oriented is True
        assert f.source_kinds == ("disk_proxy",)


# =============================================================================
# OutputSlotPool
# =============================================================================


class TestOutputSlotPool:
    def test_acquire_and_release(self) -> None:
        pool = OutputSlotPool(size=3)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)
        pool.register_slot(2, texture_glo=103, width=1920, height=1080)

        lease = pool.acquire_for_writing(generation_id=1, frame_index=10)
        assert lease is not None
        assert lease.state is SlotState.WRITING
        assert lease.texture_glo in (101, 102, 103)

    def test_full_lifecycle_through_pool(self) -> None:
        pool = OutputSlotPool(size=2)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)

        # renderer 写入
        lease = pool.acquire_for_writing(generation_id=1, frame_index=10)
        assert lease is not None
        lease.mark_ready(fence_ns=999)
        present = lease.make_present_frame(frame=10, generation_id=1)
        # viewer 取走
        pool.mark_in_viewer(present.slot_id)
        # viewer 完成
        pool.ack(present.slot_id)

        slot = pool.get_slot(present.slot_id)
        assert slot is not None
        assert slot.state is SlotState.RELEASABLE

    def test_reuse_after_ack(self) -> None:
        """ack 后 slot 可重新 acquire。"""
        pool = OutputSlotPool(size=2)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)

        lease = pool.acquire_for_writing(generation_id=1, frame_index=10)
        assert lease is not None
        lease.mark_ready(fence_ns=1)
        pool.ack(lease.slot_id)

        # 重新 acquire 应该能拿到（RELEASABLE 优先）
        lease2 = pool.acquire_for_writing(generation_id=2, frame_index=11)
        assert lease2 is not None

    def test_no_reuse_without_ack(self) -> None:
        """核心不变量：未 ack 的 slot 永不复用。"""
        pool = OutputSlotPool(size=2)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)

        # 占用两个 slot 都不 ack
        l1 = pool.acquire_for_writing(generation_id=1, frame_index=10)
        l2 = pool.acquire_for_writing(generation_id=1, frame_index=11)
        assert l1 is not None
        assert l2 is not None
        l1.mark_ready(fence_ns=1)
        l2.mark_ready(fence_ns=2)

        # 第 3 次 acquire 应返回 None（两个 slot 都未 ack）
        l3 = pool.acquire_for_writing(generation_id=1, frame_index=12)
        assert l3 is None
        # acquire_failures 统计
        assert pool.stats()["acquire_failures"] >= 1

    def test_pool_full_returns_none(self) -> None:
        """pool 满时返回 None（back-pressure，调用方应 hold/drop）。"""
        pool = OutputSlotPool(size=1)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)

        l1 = pool.acquire_for_writing(generation_id=1, frame_index=10)
        assert l1 is not None
        # 第二次 acquire 应 None
        l2 = pool.acquire_for_writing(generation_id=1, frame_index=11)
        assert l2 is None

    def test_ack_unknown_slot_ignored(self) -> None:
        """ack 不存在的 slot 不应崩溃。"""
        pool = OutputSlotPool(size=1)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.ack(999)  # 不应抛异常

    def test_double_ack_safe(self) -> None:
        """重复 ack 不应崩溃。"""
        pool = OutputSlotPool(size=1)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        lease = pool.acquire_for_writing(generation_id=1, frame_index=10)
        assert lease is not None
        lease.mark_ready(fence_ns=1)
        pool.ack(0)
        pool.ack(0)  # 第二次 ack，应被 debug 日志吞掉

    def test_release_all(self) -> None:
        pool = OutputSlotPool(size=2)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)
        l1 = pool.acquire_for_writing(generation_id=1, frame_index=10)
        l2 = pool.acquire_for_writing(generation_id=1, frame_index=11)
        assert l1 and l2
        l1.mark_ready(fence_ns=1)
        l2.mark_ready(fence_ns=2)
        pool.release_all()
        assert pool.stats()["states"].get("FREE", 0) == 2

    def test_assert_no_reuse_violations(self) -> None:
        """正常使用下 reuse_violations 应为 0。"""
        pool = OutputSlotPool(size=2)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)
        # 模拟正常播放循环
        for i in range(20):
            lease = pool.acquire_for_writing(generation_id=1, frame_index=i)
            if lease is None:
                continue  # back-pressure
            lease.mark_ready(fence_ns=1)
            pool.ack(lease.slot_id)
        pool.assert_no_reuse_violations()


# =============================================================================
# Slot reuse stress（缺陷计划 §3.1 验收）
# =============================================================================


class TestSlotReuseStress:
    """模拟高频渲染，验证未 ack slot 永不复用。"""

    def test_stress_no_reuse_without_ack(self) -> None:
        pool = OutputSlotPool(size=3)
        for sid in range(3):
            pool.register_slot(sid, texture_glo=100 + sid, width=1920, height=1080)

        # 模拟 renderer 高速写入，Viewer 偶尔 ack
        acquires = 0
        acks = 0
        in_flight: list[int] = []
        for i in range(100):
            lease = pool.acquire_for_writing(generation_id=1, frame_index=i)
            if lease is None:
                # 所有 slot 都未 ack，正确 back-pressure
                # 强制 ack 一个最旧的来推进
                if in_flight:
                    pool.ack(in_flight.pop(0))
                    acks += 1
                continue
            lease.mark_ready(fence_ns=1)
            in_flight.append(lease.slot_id)
            acquires += 1
            # 每 5 帧 ack 一次（模拟 Viewer 慢）
            if len(in_flight) >= 2:
                pool.ack(in_flight.pop(0))
                acks += 1

        pool.assert_no_reuse_violations()
        assert acquires > 0
        assert acks > 0

    def test_stats_track_acquisition(self) -> None:
        pool = OutputSlotPool(size=2)
        pool.register_slot(0, texture_glo=101, width=1920, height=1080)
        pool.register_slot(1, texture_glo=102, width=1920, height=1080)
        l = pool.acquire_for_writing(generation_id=1, frame_index=0)
        assert l is not None
        assert l.acquired_count == 1
        l.mark_ready(fence_ns=1)
        pool.ack(0)
        l2 = pool.acquire_for_writing(generation_id=1, frame_index=1)
        # 可能是同一 slot
        if l2 is not None and l2.slot_id == 0:
            assert l2.acquired_count == 2
