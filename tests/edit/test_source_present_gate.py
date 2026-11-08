"""VP-B02：Source present descriptor 配对与回收（单槽 latest-wins）。

缺口（计划 VP-B02 第 3 条）：file_controller 的
``_on_source_present_frame_ready``/``_on_source_gpu_frame_ready`` 里，
被顶掉的 pending、frame+generation 不匹配、widget 已关闭、迟到回调
这些路径**直接丢弃 descriptor 而不 ack**——OutputSlotPool 的不变量是
"未 ack 的 slot 永不复用"，漏 ack 的 slot 停在 READY/IN_VIEWER，池被
渐进耗尽后 ``present_slot_unavailable``、Source 画面冻结。

契约（抽成纯领域件 ``SourcePresentGate``，file_controller 只做接线）：

- stash 被顶掉的旧 pending **即刻** ack 回收（latest-wins）；
- take 时 frame 或 generation 不匹配 → 显式 ack 回收并返回 None；
- 匹配的 descriptor 由宿主管理（widget 的 set_gpu_frame 自带 ack 纪律）；
- 宿主已亡（orphan release）与显式清空（release_pending）都有唯一出口；
- slot_id 无效（-1/None）不触发 ack；ack 回调异常被吞掉并计数；
- 全程计数（stashed/displaced/mismatch/orphan/flushed）可诊断饥饿与泄漏。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.edit.core.source_present_gate import SourcePresentGate

pytestmark = pytest.mark.edit_core


def _present(frame: int, generation: int, slot: int) -> SimpleNamespace:
    return SimpleNamespace(
        frame=frame, generation_id=generation, slot_id=slot)


def test_b02_gate_happy_path_pairs_without_ack():
    acked: list[int] = []
    gate = SourcePresentGate(ack_slot=acked.append)
    p = _present(10, 3, 7)

    gate.stash(p)
    taken = gate.take(frame=10, generation=3)

    assert taken is p
    assert acked == []  # 匹配交付宿主，宿主管理其 slot 生命周期
    assert gate.stats["stashed"] == 1


def test_b02_gate_frame_mismatch_releases():
    acked: list[int] = []
    gate = SourcePresentGate(ack_slot=acked.append)
    gate.stash(_present(10, 3, 7))

    taken = gate.take(frame=12, generation=3)

    assert taken is None
    assert acked == [7]
    assert gate.stats["mismatch_released"] == 1


def test_b02_gate_generation_mismatch_releases():
    acked: list[int] = []
    gate = SourcePresentGate(ack_slot=acked.append)
    gate.stash(_present(10, 3, 7))

    taken = gate.take(frame=10, generation=4)

    assert taken is None
    assert acked == [7]


def test_b02_gate_displaced_pending_released_immediately():
    """两个 present 相邻到达（frame_ready 迟到）→ 旧的即刻回收。"""
    acked: list[int] = []
    gate = SourcePresentGate(ack_slot=acked.append)

    gate.stash(_present(10, 3, 7))
    gate.stash(_present(11, 3, 8))

    assert acked == [7]
    assert gate.stats["displaced_released"] == 1
    assert gate.take(frame=11, generation=3).slot_id == 8
    assert acked == [7]


def test_b02_gate_orphan_release_for_closed_widget():
    """frame_ready 到达时宿主已关：匹配的 descriptor 仍要归还。"""
    acked: list[int] = []
    gate = SourcePresentGate(ack_slot=acked.append)
    gate.stash(_present(10, 3, 7))
    present = gate.take(frame=10, generation=3)

    gate.release(present)

    assert acked == [7]
    assert gate.stats["orphan_released"] == 1


def test_b02_gate_release_pending_flushes():
    acked: list[int] = []
    gate = SourcePresentGate(ack_slot=acked.append)
    gate.stash(_present(10, 3, 7))

    gate.release_pending()

    assert acked == [7]
    assert gate.stats["flushed"] == 1
    assert gate.take(frame=10, generation=3) is None  # 清空后不再配对


def test_b02_gate_invalid_slot_ids_never_ack():
    acked: list[int] = []
    gate = SourcePresentGate(ack_slot=acked.append)

    gate.stash(_present(10, 3, -1))
    assert gate.take(frame=11, generation=3) is None
    gate.release(_present(12, 3, -1))
    gate.release_slot(-1)
    gate.stash(SimpleNamespace(frame=13, generation_id=3, slot_id=None))
    assert gate.take(frame=99, generation=1) is None

    assert acked == []
    # 4 次：mismatch(-1)、release(-1)、release_slot(-1)、mismatch(None)
    assert gate.stats["acks_skipped_invalid_slot"] == 4


def test_b02_gate_ack_exception_swallowed_and_counted():
    def _boom(slot_id: int) -> None:
        raise RuntimeError("renderer gone")

    gate = SourcePresentGate(ack_slot=_boom)
    gate.stash(_present(10, 3, 7))

    assert gate.take(frame=11, generation=3) is None  # 不向调用方传播
    assert gate.stats["ack_failures"] == 1


def test_b02_gate_double_take_returns_none():
    gate = SourcePresentGate()
    gate.stash(_present(10, 3, 7))
    assert gate.take(frame=10, generation=3).slot_id == 7
    assert gate.take(frame=10, generation=3) is None  # 单槽已清
