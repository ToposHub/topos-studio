"""VP-B02：Source present descriptor 配对与回收（纯领域件）。

计划：docs/plans/2026-11-09-edit-viewport-backend-plan.md §VP-B02 第 3 条。

背景缺口：GPU 渲染线程对每个任务先 emit ``present_frame_ready(descriptor)``
再 emit ``frame_ready(frame, gen, tex, w, h)``；UI 侧用单槽暂存 descriptor、
在 frame_ready 里按 frame+generation 配对。此前"被顶掉的 pending /
不匹配 / 宿主已关 / 迟到回调"这些路径直接丢弃 descriptor 而**不 ack**——
OutputSlotPool 的不变量是"未 ack 的 slot 永不复用"（见
``src/shared/gpu/present_frame.py``），漏 ack 的 slot 永久滞留，池耗尽后
``present_slot_unavailable``、Source 画面冻结。

本类把配对与回收收敛为唯一出口：

- ``stash``：latest-wins，被顶掉的旧 pending **即刻** ack 回收；
- ``take``：frame 或 generation 不匹配 → 显式 ack 回收并返回 None；
  匹配 → 交给宿主（widget 的 set_gpu_frame 自带 ack 纪律）；
- ``release`` / ``release_slot``：宿主已亡（widget C++ 析构的关窗竞态）
  的孤儿归还；``release_pending``：换源/关闭时清空暂存；
- slot_id 无效（-1/None）不触发 ack；ack 回调异常吞掉并计数（旧
  renderer 被释放后的迟到 ack 是已知竞态，不得向调用方传播）；
- 全程计数（``stats``）供 B02 的饥饿/泄漏诊断。
"""

from __future__ import annotations

from typing import Any, Callable


class SourcePresentGate:
    """单槽 present descriptor 配对器（UI 线程亲和由调用方保证）。"""

    def __init__(self,
                 ack_slot: Callable[[int], None] | None = None) -> None:
        self._ack_slot = ack_slot
        self._pending: Any = None
        self._stats = {
            "stashed": 0,
            "displaced_released": 0,
            "mismatch_released": 0,
            "orphan_released": 0,
            "flushed": 0,
            "acks_skipped_invalid_slot": 0,
            "ack_failures": 0,
        }

    # ── 主流程 ──────────────────────────────────────────────────────

    def stash(self, present: Any) -> None:
        """暂存 descriptor（latest-wins：被顶掉的旧 pending 即刻回收）。"""
        if self._pending is not None:
            self._release(self._pending, "displaced_released")
        self._pending = present
        self._stats["stashed"] += 1

    def take(self, frame: int, generation: int) -> Any:
        """按 frame+generation 配对取走 descriptor。

        不匹配（迟到/过期回调）→ 显式回收并返回 None；
        匹配 → 返回 descriptor，slot 生命周期移交宿主。
        """
        present = self._pending
        self._pending = None
        if present is None:
            return None
        if (getattr(present, "frame", -1) != frame
                or getattr(present, "generation_id", -1) != generation):
            self._release(present, "mismatch_released")
            return None
        return present

    # ── 归还出口 ────────────────────────────────────────────────────

    def release(self, present: Any) -> None:
        """宿主已亡（widget 关闭/析构竞态）时归还匹配出的 descriptor。"""
        self._release(present, "orphan_released")

    def release_slot(self, slot_id: int) -> None:
        """按 slot id 归还（widget 抛 RuntimeError 的兜底路径）。"""
        if slot_id is None or slot_id < 0:
            self._stats["acks_skipped_invalid_slot"] += 1
            return
        self._try_ack(int(slot_id))

    def release_pending(self) -> None:
        """换源/关闭：清空暂存并回收当前 pending。"""
        if self._pending is not None:
            self._release(self._pending, "flushed")
            self._pending = None
        elif self._stats["stashed"] > 0:
            self._stats["flushed"] += 1

    # ── 诊断 ────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ── 内部 ────────────────────────────────────────────────────────

    def _release(self, present: Any, counter: str) -> None:
        self._stats[counter] += 1
        slot_id = getattr(present, "slot_id", -1)
        if slot_id is None or slot_id < 0:
            self._stats["acks_skipped_invalid_slot"] += 1
            return
        self._try_ack(int(slot_id))

    def _try_ack(self, slot_id: int) -> None:
        if self._ack_slot is None:
            return
        try:
            self._ack_slot(slot_id)
        except Exception:  # 旧 renderer 已释放等竞态：不向调用方传播
            self._stats["ack_failures"] += 1
