"""D3D11 定长纹理池与 lease 所有权（计划 2026-09-15 §6 步骤 1）。

对应计划 §3.3 契约："所有资源来自定长池；逐帧不得创建纹理或等待 GPU"
以及 §6 步骤 1 门禁："1,000 帧循环无资源增长"。

硬规则：

- 池在构造期一次性创建全部 ``depth`` 张纹理并封口（sealed）；封口后
  纹理工厂永远不再被调用——资源增长在结构上不可能，而非靠纪律；
- 帧以 :class:`D3D11TextureLease` 借出，恰好归还一次；跨池归还、
  重复归还在词表层显式报错；
- 池耗尽时 ``try_acquire`` 返回 ``None``、``acquire(timeout_s)`` 阻塞
  等待（有界 in-flight 的反压原语），绝不超发；
- 统计（created / in_use / peak / serviced / timeouts）随时可审计，
  供 ``ExportGpuTrail`` 与真机 probe 门禁消费。

真机验证：scripts/probe_d3d11_device_pool_windows.py 在 AMD RX 6800 上
跑 device 创建 + NV12 1000 帧 lease 循环门禁。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Protocol, Tuple

from .d3d11_device import (
    DXGI_FORMAT_NV12,
    DXGI_FORMAT_P010,
    VTABLE_SLOT_CREATE_TEXTURE2D,
    release_com_object,
    vtable_fn,
)
from .reasons import FallbackReason, param

logger = logging.getLogger(__name__)

__all__ = [
    "D3D11_BIND_DECODER",
    "D3D11_BIND_SHADER_RESOURCE",
    "D3D11_BIND_RENDER_TARGET",
    "D3D11_USAGE_DEFAULT",
    "SEMI_PLANAR_SUBRESOURCE_COUNT",
    "D3D11TextureLease",
    "D3D11ExternalTextureLease",
    "TextureLease",
    "D3D11TexturePoolStats",
    "D3D11TexturePool",
    "create_d3d11_texture_pool",
    "d3d11_texture_factory",
]

# --- D3D11 常量（SDK d3d11.h 钉死） ---------------------------------------
# 默认 bind_flags 只用 SHADER_RESOURCE：DECODER|SHADER_RESOURCE 组合在
# AMD RX 6800 真机上 CreateTexture2D 直接 E_INVALIDARG（DECODER 旗标是
# MF 解码器自有 surface 的属性，池纹理不该带）。
D3D11_BIND_RENDER_TARGET = 0x20
D3D11_BIND_SHADER_RESOURCE = 0x80
D3D11_BIND_DECODER = 0x200
D3D11_USAGE_DEFAULT = 0

#: NV12 / P010 半平面纹理的子资源数（Y 平面 = 0，UV 交错平面 = 1）。
SEMI_PLANAR_SUBRESOURCE_COUNT = 2

#: AUD-36109：fence 回收线程的有界等待上限（秒）。completion fence 在该
#: 窗口内未 signal 即视为管线 wedge（设备故障 / fence 被原生侧丢弃），
#: lease 走兜底同步归还——池槽绝不能因等待失败而静默蒸发；回收线程也
#: 因此保证终止，fence 永不 signal 时不再无限堆积常驻阻塞线程。
_FENCE_RECLAIM_TIMEOUT_S = 10.0

TextureFactory = Callable[[], int]


def _spawn_fence_reclaimer(
    release: Callable[[], None],
    wait: Callable[[Optional[float]], bool],
    label: str,
    thread_name: str,
) -> None:
    """启动有界 fence 回收线程（AUD-36109）。

    - ``wait(timeout)`` 返回 True：fence 已 signal，正常归还 lease；
    - ``wait`` 返回 False（超时）或抛出 ``OSError`` 等异常：等待失败
      意味着管线已 wedge，此时"槽位永久泄漏"比"保守同步归还"更危险
      （泄漏累积到池耗尽会让导出整体死锁），因此兜底同步归还并留痕；
    - 归还自身抛错（重复归还等所有权缺陷）按错误级留痕，不再向上抛。
    """
    def _wait_and_release() -> None:
        signalled = False
        try:
            signalled = bool(wait(_FENCE_RECLAIM_TIMEOUT_S))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "%s: completion fence wait 异常，lease 兜底同步归还: %s",
                label, exc,
            )
        if not signalled:
            logger.warning(
                "%s: completion fence 未在 %.1fs 内 signal，lease 兜底同步归还",
                label, _FENCE_RECLAIM_TIMEOUT_S,
            )
        try:
            release()
        except (RuntimeError, ValueError) as exc:
            logger.error("%s: lease 归还失败（池槽位可能泄漏）: %s", label, exc)

    threading.Thread(target=_wait_and_release, name=thread_name, daemon=True).start()


class TextureLease(Protocol):
    """Common lease API for pooled and decoder-owned textures."""

    @property
    def texture_ptr(self) -> int:
        ...

    @property
    def released(self) -> bool:
        ...

    def release(self) -> None:
        ...

    def release_after(self, fence: object) -> None:
        ...

_POOL_ACCEPTED_FORMATS: Tuple[int, ...] = (DXGI_FORMAT_NV12, DXGI_FORMAT_P010)


class D3D11TextureLease:
    """单张池纹理的借出凭证（恰一次归还）。"""

    def __init__(self, texture_ptr: int, pool: "D3D11TexturePool") -> None:
        self._texture_ptr = texture_ptr
        self._pool = pool
        self._released = False
        self._release_lock = threading.Lock()

    @property
    def texture_ptr(self) -> int:
        if self._released:
            raise RuntimeError("lease 已归还，texture 指针所有权已交还池")
        return self._texture_ptr

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                raise ValueError("lease 重复归还（资源治理缺陷，必须显式修）")
            self._released = True
        self._pool._return_texture(self._texture_ptr)

    def release_after(self, fence: object) -> None:
        """在 native completion fence 后异步归还，避免复用 in-flight texture。"""
        wait = getattr(fence, "wait", None)
        if not callable(wait):
            raise ValueError("D3D11 completion fence 缺少 wait()")
        if bool(getattr(fence, "signalled", False)):
            self.release()
            return
        _spawn_fence_reclaimer(
            release=self.release,
            wait=wait,
            label="D3D11 fence lease",
            thread_name="topos-d3d11-lease-reclaimer",
        )


class D3D11ExternalTextureLease:
    """Decoder-owned D3D11 texture lease returned through the native bridge."""

    def __init__(self, texture_ptr: int, release_callback: Callable[[], None]) -> None:
        if not texture_ptr:
            raise ValueError("外部 D3D11 texture lease 不接受空指针")
        self._texture_ptr = texture_ptr
        self._release_callback = release_callback
        self._released = False
        self._release_lock = threading.Lock()

    @property
    def texture_ptr(self) -> int:
        with self._release_lock:
            if self._released:
                raise RuntimeError("lease 已归还，texture 指针所有权已交还解码器")
            return self._texture_ptr

    @property
    def released(self) -> bool:
        with self._release_lock:
            return self._released

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                raise ValueError("外部 texture lease 重复归还")
            self._released = True
        self._release_callback()

    def release_after(self, fence: object) -> None:
        wait = getattr(fence, "wait", None)
        if not callable(wait):
            raise ValueError("D3D11 completion fence 缺少 wait()")
        if bool(getattr(fence, "signalled", False)):
            self.release()
            return
        _spawn_fence_reclaimer(
            release=self.release,
            wait=wait,
            label="外部 D3D11 fence lease",
            thread_name="topos-d3d11-external-lease-reclaimer",
        )

    def __enter__(self) -> "D3D11ExternalTextureLease":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if not self._released:
            self.release()


@dataclass(frozen=True)
class D3D11TexturePoolStats:
    """池的审计快照（真机门禁与 ExportGpuTrail 消费）。"""

    depth: int
    created: int
    in_use: int
    peak_in_use: int
    serviced: int
    acquire_calls: int
    acquire_timeouts: int
    sealed: bool
    closed: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "depth": self.depth,
            "created": self.created,
            "in_use": self.in_use,
            "peak_in_use": self.peak_in_use,
            "serviced": self.serviced,
            "acquire_calls": self.acquire_calls,
            "acquire_timeouts": self.acquire_timeouts,
            "sealed": self.sealed,
            "closed": self.closed,
        }


@dataclass
class _PoolState:
    free: set = field(default_factory=set)
    in_use: int = 0
    peak_in_use: int = 0
    serviced: int = 0
    acquire_calls: int = 0
    acquire_timeouts: int = 0
    closed: bool = False
    # 历史创建数：close 后 free 清空，created 仍须可审计（不随关闭归零）。
    created: int = 0


class D3D11TexturePool:
    """定长 D3D11 纹理池（构造期创建、封口、lease 归还）。"""

    def __init__(
        self,
        *,
        depth: int,
        width: int,
        height: int,
        dxgi_format: int,
        texture_factory: TextureFactory,
        bind_flags: int = D3D11_BIND_SHADER_RESOURCE,
        label: str = "decode",
        texture_releaser: Optional[Callable[[int], None]] = None,
    ) -> None:
        if depth < 1:
            raise ValueError("纹理池 depth 至少为 1")
        if dxgi_format not in _POOL_ACCEPTED_FORMATS:
            raise ValueError(
                f"纹理池只接受半平面格式 { _POOL_ACCEPTED_FORMATS }，"
                f"得到 dxgi_format={dxgi_format}"
            )
        if width <= 0 or height <= 0 or height % 2 != 0:
            raise ValueError("宽高必须为正且高为偶数（4:2:0 半平面）")
        self._depth = depth
        self._width = width
        self._height = height
        self._dxgi_format = dxgi_format
        self._bind_flags = bind_flags
        self._label = label
        # 释放路径与创建路径同样可注入：假工厂测试绝不触碰真实 COM。
        self._releaser = texture_releaser or release_com_object
        self._condition = threading.Condition()
        self._state = _PoolState()

        # 构造期一次性建满并封口；任何一张失败都整体失败（不交付残池）。
        created: list[int] = []
        try:
            for _ in range(depth):
                texture_ptr = texture_factory()
                if not texture_ptr:
                    raise ValueError("纹理工厂返回空指针")
                created.append(texture_ptr)
        except Exception:
            for texture_ptr in created:
                try:
                    self._releaser(texture_ptr)
                except Exception:  # noqa: S110 - 清理路径尽力而为
                    pass
            raise
        self._state.free = set(created)
        self._state.created = len(created)
        self._sealed = True
        self._closed = False

    # --- 属性 ------------------------------------------------------------
    @property
    def depth(self) -> int:
        return self._depth

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def dxgi_format(self) -> int:
        return self._dxgi_format

    def stats(self) -> D3D11TexturePoolStats:
        with self._condition:
            return D3D11TexturePoolStats(
                depth=self._depth,
                created=self._state.created,
                in_use=self._state.in_use,
                peak_in_use=self._state.peak_in_use,
                serviced=self._state.serviced,
                acquire_calls=self._state.acquire_calls,
                acquire_timeouts=self._state.acquire_timeouts,
                sealed=self._sealed,
                closed=self._state.closed,
            )

    # --- 借出 / 归还 -------------------------------------------------------
    def try_acquire(self) -> Optional[D3D11TextureLease]:
        """非阻塞借出；池空返回 None（调用方决定跳帧或反压）。"""
        with self._condition:
            self._state.acquire_calls += 1
            if self._state.closed or not self._state.free:
                if not self._state.free and not self._state.closed:
                    self._state.acquire_timeouts += 1
                return None
            texture_ptr = self._state.free.pop()
            self._state.in_use += 1
            self._state.peak_in_use = max(
                self._state.peak_in_use, self._state.in_use,
            )
            self._state.serviced += 1
            return D3D11TextureLease(texture_ptr, self)

    def acquire(self, timeout_s: float = 1.0) -> Optional[D3D11TextureLease]:
        """阻塞借出（有界等待 = 反压原语）；超时返回 None 并计数。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            self._state.acquire_calls += 1
            while not self._state.free:
                if self._state.closed:
                    self._state.acquire_timeouts += 1
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._state.acquire_timeouts += 1
                    return None
                self._condition.wait(timeout=remaining)
            texture_ptr = self._state.free.pop()
            self._state.in_use += 1
            self._state.peak_in_use = max(
                self._state.peak_in_use, self._state.in_use,
            )
            self._state.serviced += 1
            return D3D11TextureLease(texture_ptr, self)

    def _return_texture(self, texture_ptr: int) -> None:
        # AUD-36111：与 close() 对齐——锁内只改状态并收集待释放指针，
        # 锁外调用注入的 releaser。原实现持 self._condition 调用外部
        # releaser：关闭后每笔在途归还都在池锁内执行 COM Release，
        # releaser 阻塞/抛错会拖住全部 acquire/stats，异常还会被送进
        # fence 回收线程。
        release_after_unlock: Optional[int] = None
        with self._condition:
            if texture_ptr in self._state.free:
                raise ValueError("纹理重复归还（不属于任何在用 lease）")
            if self._state.closed:
                # 池已关闭时归还在途纹理：就地释放，不再回池（in_use 同步
                # 扣减，审计计数不留幽灵占用）。
                self._state.in_use -= 1
                release_after_unlock = texture_ptr
            else:
                self._state.free.add(texture_ptr)
                self._state.in_use -= 1
                self._condition.notify()
        if release_after_unlock is not None:
            self._releaser(release_after_unlock)

    # --- 关闭 --------------------------------------------------------------
    def close(self) -> None:
        """关闭池并释放全部空闲纹理；仍有在途 lease 时拒绝（fail-closed）。"""
        with self._condition:
            if self._state.closed:
                return
            if self._state.in_use > 0:
                raise ValueError(
                    f"池仍有 {self._state.in_use} 个在途 lease，拒绝关闭"
                    "（先归还，或按计划修正生命周期）"
                )
            self._state.closed = True
            to_release = list(self._state.free)
            self._state.free.clear()
        for texture_ptr in to_release:
            self._releaser(texture_ptr)

    def __enter__(self) -> "D3D11TexturePool":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 真实纹理工厂（ctypes CreateTexture2D，vtable[5] 真机验证槽位）
# ---------------------------------------------------------------------------

def d3d11_texture_factory(
    device_ptr: int,
    width: int,
    height: int,
    dxgi_format: int,
    bind_flags: int = D3D11_BIND_SHADER_RESOURCE,
) -> TextureFactory:
    """构造闭包：每次调用在 device 上创建一张半平面 NV12/P010 纹理。"""

    def _create() -> int:
        # D3D11_TEXTURE2D_DESC：Width,Height,MipLevels,ArraySize,Format,
        # SampleDesc{Count,Quality},Usage,BindFlags,CPUAccessFlags,MiscFlags
        desc = (ctypes.c_uint * 11)(
            width, height, 1, 1, dxgi_format,
            1, 0,
            D3D11_USAGE_DEFAULT,
            bind_flags,
            0, 0,
        )
        texture = ctypes.c_void_p(0)
        proto = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        )
        hr = vtable_fn(
            device_ptr, VTABLE_SLOT_CREATE_TEXTURE2D, proto,
        )(device_ptr, ctypes.byref(desc), None, ctypes.byref(texture))
        if hr != 0 or not texture.value:
            raise RuntimeError(
                f"CreateTexture2D({width}x{height} fmt={dxgi_format}) "
                f"hr=0x{hr & 0xFFFFFFFF:08X}"
            )
        return texture.value

    return _create


def create_d3d11_texture_pool(
    device_ptr: int,
    *,
    depth: int,
    width: int,
    height: int,
    dxgi_format: int = DXGI_FORMAT_NV12,
    bind_flags: int = D3D11_BIND_SHADER_RESOURCE,
    label: str = "decode",
) -> Tuple[Optional[D3D11TexturePool], str]:
    """便捷入口：失败返回 ``(None, reason)``，词表与 device 层一致。"""
    try:
        pool = D3D11TexturePool(
            depth=depth,
            width=width,
            height=height,
            dxgi_format=dxgi_format,
            texture_factory=d3d11_texture_factory(
                device_ptr, width, height, dxgi_format, bind_flags,
            ),
            bind_flags=bind_flags,
            label=label,
        )
    except RuntimeError as exc:
        return None, param(FallbackReason.GPU_PROBE_FAILED, f"create_texture:{exc}")
    except ValueError as exc:
        return None, param(FallbackReason.GPU_PROBE_FAILED, f"invalid_pool:{exc}")
    return pool, ""
