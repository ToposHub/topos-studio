"""
GPU Present Frame Descriptor + Texture Lease Lifecycle
专业 NLE 实时播放的 GPU present 协议（对应缺陷计划 RP-P0-01/02/12）。

## 解决的问题

旧代码在 renderer → ProgramViewer 之间传**裸 texture_id**：
- renderer 写完 output slot 后只 `glFlush()` 就 emit `(tex_id, w, h)`
- Viewer 在另一个 GL context 异步 blit，可能读到半写入纹理（混帧/撕裂）
- output pool 仅靠"延迟复用"避免覆盖，没有"Viewer 已拷贝完成"确认
- 播放快时 texture slot 被下一帧复用，画面出现块状叠加

## 本模块提供

- `GpuPresentFrame`: 一个完整的 present descriptor，携带 texture_id、fence、
  slot_id、generation、release callback、色彩/朝向元数据。
- `TextureLease`: output slot 的生命周期状态机
  `FREE -> WRITING -> READY -> IN_VIEWER -> RELEASABLE -> FREE`。
- `OutputSlotPool`: 管理 slot 池，强制 Viewer ack，未 ack 的 slot 永不复用。

## 禁止（缺陷计划 §5）

- 禁止裸 tex_id 在模块间长期传递
- 禁止 cache GPU texture id（pool 会复用，cache 命中会显示错帧）
- 禁止热路径每帧 glFinish（用 fence + ack 替代）

## 协议流程

```
renderer thread (render context):
  slot = pool.acquire_for_writing()       # FREE -> WRITING
  ... composite into slot.texture ...
  fence = glFenceSync(...)
  glFlush()
  slot.mark_ready(fence_ns=fence, fence_cleanup=cb)   # WRITING -> READY
  present = slot.make_present_frame(frame=..., generation_id=...)
  emit frame_ready(present)

viewer thread (display context):
  present = queue.take()
  glWaitSync(present.fence_ns)  # 等 renderer 写完
  ... blit present.texture -> display ...
  pool.ack(present.slot_id)              # IN_VIEWER -> RELEASABLE -> FREE

丢帧纪律（AUD-1603）：帧 emit 后若 Viewer 队列溢出丢帧、或 Viewer 在
take 之前消失，该 present **必须** ``pool.ack(slot_id)`` 或
``pool.discard(slot_id)`` 二选一回收——否则 slot 永久滞留 READY，池
（典型容量 3）耗尽后 ``acquire_for_writing`` 恒返回 None，画面静默冻结。
```

模块本身不依赖 Qt/GL，fence 用 opaque int 表示（实际是 GLsync 指针），
便于在测试中用 mock 验证状态机。

线程边界与交接契约（renderer → viewer）：

- ``OutputSlotPool`` / ``TextureLease`` 的公开方法内部有锁，渲染线程与
  Viewer（UI）线程都可安全调用；本模块绝不调用 GL。每个 lease 另有
  自身的状态锁：渲染线程持 lease 直接调用的 ``mark_ready`` /
  ``make_present_frame`` 与 shutdown 线程经 pool 调用的
  ``release_all`` / ``acquire_for_writing`` 对同一批字段做的是原子
  check-then-act，不存在"release_all 置 FREE 后 mark_ready 把状态复活
  成 READY"的竞态（AUD-1601；锁序恒为 pool -> slot，无死锁）。
- 渲染线程独占 ``acquire_for_writing`` → ``mark_ready`` 段（写纹理 +
  插 fence 必须在拥有 render context 的线程）。
- 交接点只有 ``GpuPresentFrame``：Viewer 侧在 display context 上先
  ``glWaitSync(fence_ns)`` 再 blit，随后必须 ``pool.ack(slot_id)``——
  ack 之前 pool 保证该 slot 的纹理绝不被复用（ack 即交接完成的唯一
  确认信号）。
- fence 的所有权随 lease：ack / release / discard 时自动经
  ``fence_cleanup`` 回收，Viewer 只消费不删除。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Slot 状态枚举
# =============================================================================


class SlotState(Enum):
    """output slot 的生命周期状态（缺陷计划 §3.1）。"""

    FREE = "free"                  # 可被 renderer 重新 acquire 写入
    WRITING = "writing"            # renderer 正在写入，未 ready
    READY = "ready"                # renderer 写完，等待 Viewer 取走
    IN_VIEWER = "in_viewer"        # Viewer 正在显示/blit
    RELEASABLE = "releasable"      # Viewer 已 ack，可回收


# =============================================================================
# TextureLease / OutputSlot
# =============================================================================


@dataclass
class TextureLease:
    """
    单个 output texture slot 的租约。

    Attributes:
        slot_id: 唯一标识
        texture_glo: ModernGL texture 的 OpenGL 对象名（glo）
        width / height: slot 纹理尺寸
        state: 当前生命周期状态
        generation_id: 当前写入的 timeline generation（seek 后递增）
        frame_index: 当前写入的 timeline frame（debug/trace 用）
        fence_ns: renderer 写完后插入的 fence（opaque，实际是 GLsync 指针）
        acked: Viewer 是否已确认完成拷贝
    """

    slot_id: int
    texture_glo: int
    width: int
    height: int
    state: SlotState = SlotState.FREE
    generation_id: int = 0
    frame_index: int = -1
    fence_ns: int = 0  # opaque fence handle (GLsync*)
    fence_cleanup: Optional[Callable[[int], None]] = None  # 清理 fence 的回调
    acked: bool = False
    acquired_count: int = 0  # 统计：被 acquire 写入的总次数
    # P2-03：后端无关资源描述（可选）。None = 纯 GL slot（texture_glo 即 handle）。
    # 非 GL 后端必须填 resource；texture_glo 仅在 graphics=opengl 时有意义。
    resource: "Optional[object]" = None  # GpuPlaneResource | None（避免循环导入）
    # AUD-1601: 状态机字段（state/fence_ns/fence_cleanup/...）的自旋锁。
    # 渲染线程在 pool 锁之外直接调用 mark_ready/make_present_frame（协议
    # 图），shutdown 线程又在 pool 锁内经 release_all/acquire_for_writing
    # 读写同一批字段——转移必须原子，否则 release_all 置 FREE 后
    # mark_ready 可把 slot "复活"成 READY（纹理已被上层销毁、新 fence
    # 无人清理）。锁序恒为 pool -> slot。
    _lock: threading.Lock = field(default_factory=threading.Lock,
                                  repr=False, compare=False)

    @classmethod
    def from_resource(
        cls,
        slot_id: int,
        resource: object,
        width: int,
        height: int,
        *,
        texture_glo: int = 0,
    ) -> "TextureLease":
        """从后端无关资源描述构造 lease（同一状态机，不建第二套 lease 协议）。

        ``resource`` 是 gpu_frame.GpuPlaneResource（鸭子类型避免 shared 内
        跨模块导入循环）；GL 路径继续用 texture_glo 快路径。
        """
        return cls(
            slot_id=slot_id,
            texture_glo=texture_glo,
            width=width,
            height=height,
            resource=resource,
        )

    def resolved_handle(self) -> int | object:
        """backend 消费者取 handle：非 GL 走 resource.native_handle。"""
        if self.resource is not None:
            return getattr(self.resource, "native_handle", self.texture_glo)
        return self.texture_glo

    def is_opengl_path(self) -> bool:
        """texture_glo 是否为本 slot 的有效 handle。"""
        return self.resource is None or getattr(self.resource, "api", "opengl") == "opengl"

    # ------------------------------------------------------------------
    # 状态转移
    # ------------------------------------------------------------------

    def begin_writing(self, generation_id: int, frame_index: int) -> None:
        """FREE -> WRITING。renderer 开始写入。"""
        with self._lock:
            if self.state not in (SlotState.FREE, SlotState.RELEASABLE):
                raise RuntimeError(
                    f"TextureLease {self.slot_id}: cannot begin_writing in state {self.state.name}"
                )
            self.state = SlotState.WRITING
            self.generation_id = generation_id
            self.frame_index = frame_index
            self.acked = False
            self.fence_ns = 0
            self.acquired_count += 1

    def mark_ready(self, fence_ns: int, fence_cleanup: Optional[Callable[[int], None]] = None) -> None:
        """WRITING -> READY。renderer 写完，附带 fence。

        若 slot 已被 shutdown 路径强制回收（FREE），这里的 RuntimeError
        即拒绝"复活"——调用方应放弃本帧。
        """
        with self._lock:
            if self.state is not SlotState.WRITING:
                raise RuntimeError(
                    f"TextureLease {self.slot_id}: cannot mark_ready in state {self.state.name}"
                )
            self.state = SlotState.READY
            self.fence_ns = fence_ns
            self.fence_cleanup = fence_cleanup

    def begin_viewer_use(self) -> None:
        """READY -> IN_VIEWER。Viewer 取走开始显示。"""
        with self._lock:
            if self.state is not SlotState.READY:
                raise RuntimeError(
                    f"TextureLease {self.slot_id}: cannot begin_viewer_use in state {self.state.name}"
                )
            self.state = SlotState.IN_VIEWER

    def ack(self) -> None:
        """IN_VIEWER -> RELEASABLE。Viewer 完成拷贝。"""
        with self._lock:
            # 容忍 READY 直接 ack（Viewer 同步 blit 完成时可能跳过 in_viewer 中间态）
            if self.state not in (SlotState.IN_VIEWER, SlotState.READY):
                raise RuntimeError(
                    f"TextureLease {self.slot_id}: cannot ack in state {self.state.name}"
                )
            self.acked = True
            self.state = SlotState.RELEASABLE
            # 清理 fence（Viewer 已等待完成）；回调移到 slot 锁外执行。
            cleanup = self.fence_cleanup
            fence = self.fence_ns
            self.fence_ns = 0
            self.fence_cleanup = None
        if cleanup is not None and fence != 0:
            try:
                cleanup(fence)
            except Exception as e:
                logger.debug(f"TextureLease fence cleanup failed: {e}")

    def recover_fence(self) -> None:
        """清理未消费的 fence（不改变状态；重复注册/强制回收路径用）。"""
        with self._lock:
            cleanup = self.fence_cleanup
            fence = self.fence_ns
            self.fence_ns = 0
            self.fence_cleanup = None
        if cleanup is not None and fence != 0:
            try:
                cleanup(fence)
            except Exception as e:
                logger.debug(f"TextureLease fence cleanup failed: {e}")

    def release(self) -> None:
        """RELEASABLE -> FREE。回收（pool 内部调用）。"""
        with self._lock:
            if self.state is not SlotState.RELEASABLE:
                # 允许从任何非 WRITING/IN_VIEWER 状态强制回收（shutdown 时）
                if self.state is SlotState.WRITING:
                    logger.warning(
                        f"TextureLease {self.slot_id}: force release from WRITING "
                        f"(frame={self.frame_index})"
                    )
            # 兜底清理未消费的 fence
            cleanup = self.fence_cleanup
            fence = self.fence_ns
            self.fence_ns = 0
            self.fence_cleanup = None
            self.state = SlotState.FREE
            self.acked = False
        if cleanup is not None and fence != 0:
            try:
                cleanup(fence)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def is_writable(self) -> bool:
        """是否可被 renderer acquire（FREE 或已 ack 的 RELEASABLE）。"""
        return self.state in (SlotState.FREE, SlotState.RELEASABLE)

    @property
    def is_ready_for_viewer(self) -> bool:
        """是否处于可呈现状态（READY 或已被取走的 IN_VIEWER）。"""
        return self.state in (SlotState.READY, SlotState.IN_VIEWER)

    def make_present_frame(
        self,
        *,
        frame: int,
        generation_id: int,
        color_space: Optional[str] = None,
        is_gl_oriented: bool = True,
        source_kinds: tuple = (),
        extra: Optional[dict] = None,
    ) -> "GpuPresentFrame":
        """从 READY slot 构造 GpuPresentFrame（不改变 slot 状态）。"""
        with self._lock:
            if self.state is not SlotState.READY:
                raise RuntimeError(
                    f"TextureLease {self.slot_id}: make_present_frame requires READY, got {self.state.name}"
                )
            fence_ns = self.fence_ns
        # AUD-1602: 浅拷贝切断与调用方 dict 的引用共享——frozen descriptor
        # 会随信号跨线程传引用，emit 后调用方继续改写原 dict 会让消费端
        # 读到半更新状态。
        return GpuPresentFrame(
            texture_id=self.texture_glo,
            width=self.width,
            height=self.height,
            frame=frame,
            generation_id=generation_id,
            slot_id=self.slot_id,
            fence_ns=fence_ns,
            color_space=color_space,
            is_gl_oriented=is_gl_oriented,
            source_kinds=source_kinds,
            extra=dict(extra) if extra else {},
        )


# =============================================================================
# GpuPresentFrame descriptor
# =============================================================================


@dataclass(frozen=True)
class GpuPresentFrame:
    """
    一个完整的 GPU present 帧 descriptor（缺陷计划 §3.1）。

    替代裸 (tex_id, w, h) tuple。携带 fence 让 Viewer 在另一个 context
    等待 renderer 写完，携带 slot_id 让 pool 跟踪 ack。

    Attributes:
        texture_id: OpenGL texture 对象名（共享 context）
        width / height: 纹理尺寸
        frame: timeline frame
        generation_id: timeline generation（stale 检测）
        slot_id: output pool slot 标识（用于 ack）
        fence_ns: opaque fence handle（GLsync*），Viewer 用 glWaitSync 等待
        color_space: 色彩空间标记（可空）
        is_gl_oriented: True=GPU FBO 方向（y=0 底），False=CPU QImage 翻转
        source_kinds: 本帧各 layer 的 source kind（telemetry/trace 用）
        extra: 额外元数据（构造时浅拷贝，AUD-1602：emit 后调用方对原
            dict 的后续修改不会影响本 descriptor；仍不要向 extra 写入）
    """

    texture_id: int
    width: int
    height: int
    frame: int
    generation_id: int
    slot_id: int
    fence_ns: int = 0
    color_space: Optional[str] = None
    is_gl_oriented: bool = True
    source_kinds: tuple = ()
    extra: dict = field(default_factory=dict)

    @property
    def has_fence(self) -> bool:
        """是否携带有效 fence（Viewer 需要等待的写完成信号）。"""
        return self.fence_ns != 0

    def as_legacy_tuple(self) -> tuple:
        """兼容旧 (tex_id, w, h) 接口（过渡期用）。"""
        return (self.texture_id, self.width, self.height)


# =============================================================================
# OutputSlotPool
# =============================================================================


class OutputSlotPool:
    """
    output texture slot 池 + 生命周期管理（缺陷计划 RP-P0-02）。

    核心不变量：
    - **未 ack 的 slot 永不复用**（防混帧）。
    - acquire_for_writing 优先选 FREE/RELEASABLE；若全占用则返回 None
      （调用方应 hold/drop，不要阻塞渲染线程）。

    用法：
        pool = OutputSlotPool(size=3)
        pool.register_slot(0, tex_glo=101, width=1920, height=1080)
        pool.register_slot(1, tex_glo=102, width=1920, height=1080)
        lease = pool.acquire_for_writing(generation_id=1, frame_index=10)
        ... composite ...
        lease.mark_ready(fence_ns=fence_handle, fence_cleanup=cleanup_cb)
        present = lease.make_present_frame(frame=10, generation_id=1)
        pool.mark_in_viewer(present.slot_id)
        ... viewer blits ...
        pool.ack(present.slot_id)
    """

    def __init__(self, size: int = 3) -> None:
        """创建 slot 池。

        Args:
            size: 名义池容量（下限 1）。仅用于容量规划与统计展示，
                实际可同时存在的 slot 数由 ``register_slot`` 次数决定
                （渲染线程创建纹理后注册）。
        """
        self._size = max(1, int(size))
        self._slots: dict[int, TextureLease] = {}
        self._lock = threading.RLock()
        # 统计
        self._acquire_failures = 0
        self._reuse_violations = 0  # 试图复用未 ack 的 slot（必须为 0）

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register_slot(self, slot_id: int, *, texture_glo: int, width: int, height: int) -> TextureLease:
        """注册一个 output slot（纹理对象由调用方创建/管理）。

        AUD-1604：尺寸/句柄做正值校验（坏参数不再沿协议传到 Viewer 端
        才暴露）；重复 slot_id 不再静默覆盖——旧 lease 持有未清理的
        fence 时先回收（否则其 fence_cleanup 永不可达，GLsync 泄漏）
        并告警，再以新 lease 顶替。
        """
        if int(texture_glo) <= 0:
            raise ValueError(
                f"register_slot: texture_glo must be > 0 (got {texture_glo})"
            )
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError(
                f"register_slot: width/height must be > 0 (got {width}x{height})"
            )
        with self._lock:
            old = self._slots.get(slot_id)
            if old is not None:
                if old.state is not SlotState.FREE:
                    logger.warning(
                        f"OutputSlotPool.register_slot: slot {slot_id} "
                        f"re-registered while in state {old.state.name}"
                    )
                    # AUD-1606: 旧 lease 未走完 ack/discard 生命周期就被
                    # 顶替 = 一次"复用未 ack 的 slot"——计入复用违规，
                    # 让 assert_no_reuse_violations 验收真正可失败。
                    self._reuse_violations += 1
                old.recover_fence()
            lease = TextureLease(
                slot_id=slot_id,
                texture_glo=int(texture_glo),
                width=int(width),
                height=int(height),
            )
            self._slots[slot_id] = lease
            return lease

    @property
    def size(self) -> int:
        """名义池容量（构造参数，下限 1）。"""
        return self._size

    @property
    def registered_count(self) -> int:
        """已注册的实际 slot 数（线程安全）。"""
        with self._lock:
            return len(self._slots)

    # ------------------------------------------------------------------
    # acquire / release
    # ------------------------------------------------------------------

    def acquire_for_writing(
        self,
        *,
        generation_id: int,
        frame_index: int,
    ) -> Optional[TextureLease]:
        """
        获取一个可写 slot。FREE/RELEASABLE 优先。

        未 ack 的 READY/IN_VIEWER slot **永不复用**（缺陷计划核心不变量）。
        所有 slot 都被占用时返回 None（调用方应 hold/drop）。
        """
        with self._lock:
            # 优先 RELEASABLE（刚被 Viewer ack 的，热路径友好）
            for slot in self._slots.values():
                if slot.state is SlotState.RELEASABLE:
                    slot.release()  # -> FREE
                    slot.begin_writing(generation_id, frame_index)
                    return slot
            # 其次 FREE
            for slot in self._slots.values():
                if slot.state is SlotState.FREE:
                    slot.begin_writing(generation_id, frame_index)
                    return slot
            # 全占用：记录失败，返回 None
            self._acquire_failures += 1
            # AUD-1610: 槽位耗尽期间（播放反压）此路径每帧触发——debug
            # 未启用时不再急切求值 f-string 与 _state_summary() 遍历。
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "OutputSlotPool: no writable slot (gen=%s, frame=%s); states=%s",
                    generation_id, frame_index, self._state_summary(),
                )
            return None

    def mark_in_viewer(self, slot_id: int) -> None:
        """Viewer 取走 slot（READY -> IN_VIEWER）。"""
        with self._lock:
            slot = self._slots.get(slot_id)
            if slot is None:
                return
            if slot.state is SlotState.READY:
                slot.begin_viewer_use()

    def ack(self, slot_id: int) -> None:
        """Viewer 完成拷贝（IN_VIEWER/READY -> RELEASABLE）。"""
        with self._lock:
            slot = self._slots.get(slot_id)
            if slot is None:
                return
            try:
                slot.ack()
            except RuntimeError as e:
                # 双重 ack 或非法状态：记录但不崩溃
                logger.debug(f"OutputSlotPool ack error: {e}")

    def discard(self, slot_id: int) -> bool:
        """回收一个"消费端已消失/迟到"的 slot（READY/IN_VIEWER -> RELEASABLE）。

        AUD-1603：核心不变量"未 ack 永不复用"的另一面是——帧 emit 后
        Viewer 队列溢出丢帧、或 Viewer 在 take 之前关闭而未补 ack 时，
        slot 会永久滞留 READY；池容量典型为 3，数次之后
        ``acquire_for_writing`` 恒为 None，画面静默冻结。任何被丢弃的
        present **必须** ``ack`` 或 ``discard`` 二选一。discard 与 ack
        一样清理 fence。返回 False 表示 slot 不存在或状态不允许回收。
        """
        with self._lock:
            slot = self._slots.get(slot_id)
            if slot is None:
                return False
            if slot.state not in (SlotState.READY, SlotState.IN_VIEWER):
                logger.debug(
                    f"OutputSlotPool discard: slot {slot_id} in state "
                    f"{slot.state.name} (nothing to discard)"
                )
                return False
            try:
                slot.ack()  # READY/IN_VIEWER -> RELEASABLE（含 fence 清理）
            except RuntimeError as e:
                logger.debug(f"OutputSlotPool discard error: {e}")
                return False
            return True

    def release_all(self) -> None:
        """强制释放所有 slot（shutdown 用）。"""
        with self._lock:
            for slot in self._slots.values():
                if slot.state is not SlotState.FREE:
                    try:
                        slot.ack()
                    except Exception:
                        pass
                    slot.release()

    # ------------------------------------------------------------------
    # 查询/统计
    # ------------------------------------------------------------------

    def get_slot(self, slot_id: int) -> Optional[TextureLease]:
        """按 id 查 slot（诊断用；不改变状态）。"""
        with self._lock:
            return self._slots.get(slot_id)

    def _state_summary(self) -> dict:
        """各 SlotState 的当前计数（调试/日志用，调用方需持锁或容忍瞬时值）。"""
        counts: dict[str, int] = {}
        for slot in self._slots.values():
            counts[slot.state.name] = counts.get(slot.state.name, 0) + 1
        return counts

    def stats(self) -> dict:
        """池统计快照（size / registered / states / acquire_failures /
        reuse_violations），供 telemetry 与验收断言使用。"""
        with self._lock:
            return {
                "size": self._size,
                "registered": len(self._slots),
                "states": self._state_summary(),
                "acquire_failures": self._acquire_failures,
                "reuse_violations": self._reuse_violations,  # 必须为 0
            }

    def report_reuse_violation(self, detail: str) -> None:
        """外部调用方记录一次"复用未 ack 的 slot"违规（AUD-1606）。

        池内唯一的自动检测点是 ``register_slot`` 顶替未释放的 lease；调用
        方若在池外捕获到 lease 状态机拒绝（如直接对 READY lease 调
        ``begin_writing`` 触发 RuntimeError），应经此方法把违规计入统一
        计数，供 ``stats()`` / ``assert_no_reuse_violations`` 验收。

        Args:
            detail: 违规描述（进 warning 日志，便于定位调用点）。
        """
        with self._lock:
            self._reuse_violations += 1
        logger.warning(
            "OutputSlotPool: reuse violation reported: %s", detail)

    def assert_no_reuse_violations(self) -> None:
        """断言没有复用未 ack 的 slot（缺陷计划 §7 验收）。

        AUD-1606: 计数器有真实递增路径（register_slot 顶替未释放 lease、
        report_reuse_violation 上报），不再是恒通过的空转验收。
        """
        with self._lock:
            violations = self._reuse_violations
        if violations != 0:
            raise AssertionError(
                f"OutputSlotPool reuse violations: {violations} (must be 0)"
            )
