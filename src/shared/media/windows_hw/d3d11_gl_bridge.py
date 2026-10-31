"""Optional D3D11-to-OpenGL compatibility bridge.

The bridge is intentionally not part of the D3D11 export main path.  An
injected interop implementation may expose a bounded ring for preview or
legacy GLSL plugins; if it is unavailable, callers receive an explicit
``interop_not_yet_implemented`` reason and must use the normal fallback.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Sequence, Tuple

from src.shared.media.gpu_frame import GpuFramePlane, GpuFrameRelease

from .reasons import FallbackReason, param

logger = logging.getLogger(__name__)

InteropFactory = Callable[
    [int, int, str, int], Tuple[Sequence[GpuFramePlane], GpuFrameRelease]
]


class D3D11GLBridge:
    """Bounded, injectable bridge used only for compatibility preview."""

    def __init__(
        self,
        ring_size: int = 4,
        interop_factory: Optional[InteropFactory] = None,
    ) -> None:
        if ring_size < 1:
            raise ValueError("D3D11GLBridge ring_size 至少为 1")
        self._ring_size = ring_size
        self._interop_factory = interop_factory
        self._initialized = False
        self._fail_count = 0
        self._active_slots: set[int] = set()
        self._lock = threading.Lock()
        self._last_fallback_reason: str | None = None

    def initialize(self) -> bool:
        """Initialize only when a real interop factory was supplied."""
        if self._interop_factory is None:
            logger.warning("D3D11GLBridge: native interop implementation unavailable")
            self._last_fallback_reason = FallbackReason.INTEROP_NOT_YET_IMPLEMENTED.value
            return False
        with self._lock:
            self._initialized = True
            self._last_fallback_reason = None
        return True

    def bind_texture(
        self,
        d3d_texture: int,
        frame_index: int,
        pixel_format: str = "nv12",
        bit_depth: int = 8,
    ) -> tuple[Sequence[GpuFramePlane], GpuFrameRelease] | None:
        """Import one texture into a free ring slot, without CPU staging."""
        def _count_failure(reason: str) -> None:
            # AUD-36115：reason 写入与计数同锁——原实现锁外写，并发失败时
            # 可能乱序覆盖，诊断口径与计数不一致。
            with self._lock:
                self._fail_count += 1
                self._last_fallback_reason = reason

        # AUD-36115：负 frame_index 会取模出负槽键，环语义失效且永不与
        # 正常槽冲突（问题被掩盖）——显式拒绝并计入诊断口径。
        if frame_index < 0:
            _count_failure(param(
                FallbackReason.INTEROP_INIT_FAILED, "negative_frame_index"
            ))
            return None

        slot = frame_index % self._ring_size
        with self._lock:
            # AUD-36114：初始化态检查与槽位登记必须在同一临界区。否则
            # destroy() 可在检查之后、登记之前清场，bind 在已销毁的桥上
            # 继续占用 ring 槽（与 destroy 后的重建形成双重占用）。
            # reason 优先级保持原契约：未初始化 > 空纹理 > 槽耗尽。
            if not self._initialized or self._interop_factory is None:
                self._fail_count += 1
                self._last_fallback_reason = (
                    FallbackReason.INTEROP_NOT_YET_IMPLEMENTED.value
                )
                return None
            if not d3d_texture:
                self._fail_count += 1
                self._last_fallback_reason = param(
                    FallbackReason.INTEROP_INIT_FAILED, "empty_d3d_texture"
                )
                return None
            if slot in self._active_slots:
                self._fail_count += 1
                self._last_fallback_reason = param(
                    FallbackReason.INTEROP_INIT_FAILED, "ring_exhausted"
                )
                return None
            self._active_slots.add(slot)
        try:
            planes, native_release = self._interop_factory(
                d3d_texture, frame_index, pixel_format, bit_depth
            )
            if not planes or not isinstance(native_release, GpuFrameRelease):
                raise ValueError("interop factory 返回了无效 plane/release")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            with self._lock:
                self._active_slots.discard(slot)
            _count_failure(param(FallbackReason.INTEROP_INIT_FAILED, str(exc)))
            return None

        def _release_slot() -> None:
            try:
                native_release.release()
            finally:
                with self._lock:
                    self._active_slots.discard(slot)

        return planes, GpuFrameRelease(
            retain_token=(d3d_texture, slot),
            release_callback=_release_slot,
            owner_backend="opengl",
            destructor_policy="warn_only",
        )

    def destroy(self) -> None:
        """Drop the bridge state; in-flight releases still remain idempotent.

        AUD-36114：只撤销初始化态，**绝不清空** ``_active_slots``——
        in-flight ``bind_texture`` 可能已登记槽位且其 release 句柄仍会
        到来；清空会让后续 bind 拿到同一 ring 槽，与在途句柄双重占用。
        槽位由 ``_release_slot`` 在原生释放后幂等归还（fail-safe：宁可
        ``ring_exhausted`` 也不双占）。失败计数与最后 reason 同样保留
        （destroy 不是诊断擦除）。
        """
        with self._lock:
            self._initialized = False

    @property
    def is_initialized(self) -> bool:
        # AUD-36115：诊断状态读也持锁（与写侧同一临界区）
        with self._lock:
            return self._initialized

    @property
    def last_fallback_reason(self) -> str | None:
        with self._lock:
            return self._last_fallback_reason

    @property
    def fail_count(self) -> int:
        with self._lock:
            return self._fail_count

