"""W02：CudaGlInteropSession 生命周期/回滚/池预算/current-context 契约（L1 替身）。

计划 `docs/upgrade_plan_W_windows_2026-10-24.md` W02（W-I03/R12）：

- destroy 必须调用 cuCtxDestroy（旧实现从不销毁，上下文泄漏）且幂等；
- 分阶段创建（纹理对 → 注册Y → 注册UV）失败逆序回滚：第二平面注册失败
  必须撤销第一平面注册与两张纹理（旧实现全泄漏）；
- slot 池按 (计数, 字节) 双预算 + LRU 回收**空闲** slot；活跃租约绝不
  回收；无可回收时诚实拒绝（旧实现 32 个空闲异 key slot 即耗尽）；
- initialize/bind/unregister/destroy 各阶段核验实际 current-context：
  操作前切换到私有上下文、操作后恢复调用者原上下文（同线程 ≠ 同
  current-context，PyAV cuda 会话可在同线程留下别的 current）；
- 销毁后的会话不隐式复活，bind 带 session_destroyed 可诊断 reason。

替身层：fake ``cuda.bindings.driver`` 注入 sys.modules；纹理创建经
monkeypatch 替换（不触 moderngl/GL）。本文件全平台可跑（W02 L1 层）；
真实资源行为由 L2（md5 对拍）/L3（渲染线程端到端）覆盖，分列不互代。
"""
from __future__ import annotations

import sys
import threading
import types
from typing import Any, List, Optional

import pytest

from src.shared.media.windows_hw import cuda_resident as cr
from src.shared.media.windows_hw.cuda_resident import (
    CudaGlInteropSession,
    CudaResidentFrame,
    CudaResidentPlane,
)


class _FakeCtx:
    """int-able 句柄（真绑定经 int(CUcontext) 取句柄值比较）。"""

    _next = [0x1000]

    def __init__(self, handle: Optional[int] = None) -> None:
        if handle is None:
            _FakeCtx._next[0] += 1
            handle = _FakeCtx._next[0]
        self.handle = handle

    def __int__(self) -> int:
        return self.handle

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, _FakeCtx):
            return self.handle == other.handle
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.handle)


class _FakeRes:
    _next = [0]

    def __init__(self) -> None:
        _FakeRes._next[0] += 1
        self.id = _FakeRes._next[0]


class _FakeDriver(types.SimpleNamespace):
    """cuda.bindings.driver 替身：全调用计数 + 失败注入点。

    W04 旋钮：``gl_devices``（cuGLGetDevices 返回的设备序号列表，默认
    None=查询失败模拟无 current GL 上下文）、``device_count``（全机 CUDA
    设备数，默认 1）、``ptr_fail``（cuPointerGetAttribute 失败）、
    ``ptr_ordinal``（平面指针所在设备序号，默认跟随会话创建设备）。
    """

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.current: Optional[_FakeCtx] = None
        self.fail_register_at: Optional[int] = None  # 第 N 次 register 失败
        self.fail_copy = False
        self._reg_count = 0
        self.destroyed_ctxs: List[int] = []
        # W04 旋钮
        self.gl_devices: Optional[List[int]] = None
        self.gl_err: int = -1            # cuGLGetDevices 错误码（默认失败）
        self.device_count: int = 1
        self.created_device: Optional[int] = None  # cuCtxCreate 用的序号
        self.ptr_fail = False
        self.ptr_ordinal: Optional[int] = None
        self.ptr_memtype = 1             # CU_MEMORYTYPE_DEVICE
        sd = types.SimpleNamespace

        class _CUmemorytype:
            CU_MEMORYTYPE_DEVICE = 1
            CU_MEMORYTYPE_ARRAY = 3

        class _CUGLDeviceList:
            CU_GL_DEVICE_LIST_ALL = 1
            CU_GL_DEVICE_LIST_CURRENT_FRAME = 2

        class _CUpointer_attribute:
            CU_POINTER_ATTRIBUTE_MEMORY_TYPE = 1
            CU_POINTER_ATTRIBUTE_DEVICE_ORDINAL = 2

        class _MemCpy2D:
            def __init__(self) -> None:
                self.srcMemoryType = 0
                self.srcDevice = 0
                self.srcPitch = 0
                self.dstMemoryType = 0
                self.dstArray = 0
                self.WidthInBytes = 0
                self.Height = 0

        outer = self

        def cuInit(flags):
            outer.calls.append("cuInit")
            return (0,)

        def cuCtxCreate(cfg, flags, dev):
            outer.calls.append("cuCtxCreate")
            outer.created_device = int(dev)
            return (0, _FakeCtx())

        def cuGLGetDevices(n, device_list):
            outer.calls.append("cuGLGetDevices")
            if outer.gl_devices is None:
                return (outer.gl_err, None, None)
            devs = outer.gl_devices[:n]
            return (0, len(devs), [int(d) for d in devs])

        def cuDeviceGetCount():
            outer.calls.append("cuDeviceGetCount")
            return (0, outer.device_count)

        def cuDeviceGetName(length, dev):
            outer.calls.append("cuDeviceGetName")
            return (0, b"Fake CUDA Device\x00")

        def cuDriverGetVersion():
            outer.calls.append("cuDriverGetVersion")
            return (0, 13000)

        def cuPointerGetAttribute(attribute, ptr):
            outer.calls.append("cuPointerGetAttribute")
            if outer.ptr_fail:
                return (-1, None)
            if attribute == _CUpointer_attribute.CU_POINTER_ATTRIBUTE_MEMORY_TYPE:
                return (0, outer.ptr_memtype)
            return (0, outer.created_device
                    if outer.ptr_ordinal is None else outer.ptr_ordinal)

        def cuCtxSetCurrent(ctx):
            outer.calls.append("cuCtxSetCurrent")
            outer.current = ctx
            return (0,)

        def cuCtxGetCurrent():
            # 不计次：守卫高频查询
            return (0, outer.current)

        def cuCtxDestroy(ctx):
            outer.calls.append("cuCtxDestroy")
            outer.destroyed_ctxs.append(int(ctx))
            if outer.current is not None and int(outer.current) == int(ctx):
                outer.current = None
            return (0,)

        def cuCtxSynchronize():
            outer.calls.append("cuCtxSynchronize")
            return (0,)

        def cuGraphicsGLRegisterImage(glo, target, flags):
            outer._reg_count += 1
            outer.calls.append("cuGraphicsGLRegisterImage")
            if outer.fail_register_at == outer._reg_count:
                return (-1,)
            return (0, _FakeRes())

        def cuGraphicsUnregisterResource(res):
            outer.calls.append("cuGraphicsUnregisterResource")
            return (0,)

        def cuGraphicsMapResources(n, res, flags):
            outer.calls.append("cuGraphicsMapResources")
            return (0,)

        def cuGraphicsSubResourceGetMappedArray(res, a, b):
            outer.calls.append("cuGraphicsSubResourceGetMappedArray")
            return (0, object())

        def cuGraphicsUnmapResources(n, res, flags):
            outer.calls.append("cuGraphicsUnmapResources")
            return (0,)

        def cuMemcpy2D(m):
            outer.calls.append("cuMemcpy2D")
            if outer.fail_copy:
                return (-1,)
            return (0,)

        super().__init__(
            cuInit=cuInit,
            cuCtxCreate=cuCtxCreate,
            cuCtxSetCurrent=cuCtxSetCurrent,
            cuCtxGetCurrent=cuCtxGetCurrent,
            cuCtxDestroy=cuCtxDestroy,
            cuCtxSynchronize=cuCtxSynchronize,
            cuGLGetDevices=cuGLGetDevices,
            cuDeviceGetCount=cuDeviceGetCount,
            cuDeviceGetName=cuDeviceGetName,
            cuDriverGetVersion=cuDriverGetVersion,
            cuPointerGetAttribute=cuPointerGetAttribute,
            cuGraphicsGLRegisterImage=cuGraphicsGLRegisterImage,
            cuGraphicsUnregisterResource=cuGraphicsUnregisterResource,
            cuGraphicsMapResources=cuGraphicsMapResources,
            cuGraphicsSubResourceGetMappedArray=(
                cuGraphicsSubResourceGetMappedArray),
            cuGraphicsUnmapResources=cuGraphicsUnmapResources,
            cuMemcpy2D=cuMemcpy2D,
            CUmemorytype=_CUmemorytype,
            CUGLDeviceList=_CUGLDeviceList,
            CUpointer_attribute=_CUpointer_attribute,
            CUDA_MEMCPY2D=_MemCpy2D,
            CUdevice=int,
            CUdeviceptr=int,
        )


class _FakeTex:
    _next = [0]

    def __init__(self) -> None:
        _FakeTex._next[0] += 1
        self.glo = _FakeTex._next[0]
        self.released = 0

    def release(self) -> None:
        if self.released:
            return
        self.released += 1


def _install_fake_driver(monkeypatch, **kw) -> _FakeDriver:
    drv, _ns = _install_fake_driver_ns(monkeypatch)
    return drv


def _install_fake_driver_ns(monkeypatch) -> tuple:
    """安装替身并返回 (driver 实例, 命名空间)，供个别用例二次替换函数。"""
    drv = _FakeDriver()
    pkg = types.ModuleType("cuda.bindings")
    pkg.driver = drv
    ns = types.SimpleNamespace(**{
        k: getattr(drv, k) for k in dir(drv) if not k.startswith("_")})
    pkg.driver = ns
    monkeypatch.setitem(sys.modules, "cuda.bindings", pkg)
    monkeypatch.setitem(sys.modules, "cuda.bindings.driver", ns)
    return drv, ns


def _make_session(monkeypatch, *, ring_size=4, max_ring_size=32,
                  max_pool_bytes=None, tex_factory=None) -> tuple:
    # 注意：构造器把 max_ring_size 钳到 ≥ ring_size——小上限用例须同时
    # 传小 ring_size（W02 实测踩坑）。
    drv = _install_fake_driver(monkeypatch)
    session = CudaGlInteropSession(
        ring_size=ring_size, max_ring_size=max(max_ring_size, ring_size),
        max_pool_bytes=max_pool_bytes)

    def fake_create(width, height, pixel_format):
        if tex_factory is not None:
            return tex_factory(width, height, pixel_format)
        return _FakeTex(), _FakeTex()

    monkeypatch.setattr(
        session, "_create_plane_textures", fake_create)
    return session, drv


def _frame(w=64, h=64, fmt="nv12", index=0) -> CudaResidentFrame:
    itemsize = 2 if fmt != "nv12" else 1
    return CudaResidentFrame(
        frame_index=index, pts=index, time_seconds=0.0,
        width=w, height=h, pixel_format=fmt,
        bit_depth=8 if fmt == "nv12" else 16,
        planes=(
            CudaResidentPlane("Y", 0x1000 + index, w * itemsize,
                              w * itemsize, h),
            CudaResidentPlane("UV", 0x2000 + index, w * itemsize,
                              w * itemsize, h // 2),
        ),
        av_frame=object(),
    )


# ---------------------------------------------------------------------------
# destroy 闭环
# ---------------------------------------------------------------------------

class TestDestroyLifecycleW02:
    def test_destroy_destroys_private_ctx_once_and_is_idempotent(
            self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize() is True
        ctx = session._ctx
        session.destroy()
        assert drv.destroyed_ctxs == [int(ctx)]
        session.destroy()  # 幂等：不再销毁
        assert len(drv.destroyed_ctxs) == 1
        assert session.stats["destroyed"] is True
        assert session.stats["slots"] == 0

    def test_destroy_unregisters_all_resources_and_releases_textures(
            self, monkeypatch):
        tex_pairs: list = []

        def factory(w, h, fmt):
            pair = (_FakeTex(), _FakeTex())
            tex_pairs.append(pair)
            return pair

        session, drv = _make_session(monkeypatch, tex_factory=factory)
        assert session.initialize()
        r1 = session.bind_resident_frame(_frame())
        r2 = session.bind_resident_frame(_frame(w=32, h=32))
        assert r1 is not None and r2 is not None
        n_resources = drv.calls.count("cuGraphicsGLRegisterImage")
        assert n_resources == 4  # 两 slot × 两平面
        session.destroy()
        assert drv.calls.count("cuGraphicsUnregisterResource") == 4
        for pair in tex_pairs:
            for tex in pair:
                assert tex.released == 1

    def test_destroy_restores_caller_context(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        caller_ctx = _FakeCtx(0xABC)
        drv.cuCtxSetCurrent(caller_ctx)
        session.destroy()
        assert drv.current is caller_ctx, "destroy 后必须恢复调用者上下文"

    def test_initialize_after_destroy_does_not_resurrect(
            self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        session.destroy()
        assert session.initialize() is False
        assert session.last_fallback_reason == "session_destroyed"
        r = session.bind_resident_frame(_frame())
        assert r is None
        assert session.last_fallback_reason == "session_destroyed"


# ---------------------------------------------------------------------------
# 分阶段创建 + 逆序回滚
# ---------------------------------------------------------------------------

class TestStagedCreationRollbackW02:
    def test_second_plane_register_failure_rolls_back_first(
            self, monkeypatch):
        tex_pairs: list = []

        def factory(w, h, fmt):
            pair = (_FakeTex(), _FakeTex())
            tex_pairs.append(pair)
            return pair

        session, drv = _make_session(monkeypatch, tex_factory=factory)
        assert session.initialize()
        drv.fail_register_at = 2  # Y 注册成功，UV 注册失败
        r = session.bind_resident_frame(_frame())
        assert r is None
        assert "slot_create_failed" in session.last_fallback_reason
        # 第一平面注册被撤销（1 次注册成功 → 1 次注销）
        assert drv.calls.count("cuGraphicsUnregisterResource") == 1
        # 两张纹理都已释放（含未注册成功的第二张）
        assert len(tex_pairs) == 1
        for tex in tex_pairs[0]:
            assert tex.released == 1
        # 池状态一致：无 slot 无占用
        assert session.stats["slots"] == 0
        assert session.stats["pool_bytes"] == 0

    def test_texture_creation_failure_leaves_no_slots(self, monkeypatch):
        def factory(w, h, fmt):
            raise RuntimeError("gl texture create failed")

        session, drv = _make_session(monkeypatch, tex_factory=factory)
        assert session.initialize()
        r = session.bind_resident_frame(_frame())
        assert r is None
        assert "slot_create_failed" in session.last_fallback_reason
        assert session.stats["slots"] == 0

    def test_partial_texture_creation_releases_created_half(
            self, monkeypatch):
        # _create_plane_textures 自身的部分失败由内部回滚保证：这里验证
        # 会话层只见异常、无资源残留
        def factory(w, h, fmt):
            first = _FakeTex()
            try:
                _ = 1 / 0  # 第二平面失败
            except ZeroDivisionError:
                first.release()
                raise
            return first, _FakeTex()

        session, drv = _make_session(monkeypatch, tex_factory=factory)
        assert session.initialize()
        r = session.bind_resident_frame(_frame())
        assert r is None
        assert session.stats["slots"] == 0


# ---------------------------------------------------------------------------
# 池预算（计数 + 字节）与 LRU
# ---------------------------------------------------------------------------

class TestPoolBudgetW02:
    def test_byte_budget_evicts_lru_idle_slot(self, monkeypatch):
        # 64x64 nv12 slot = 1.5*64*64*1 = 6144 B；96x64 = 9216 B。
        # 预算 16000：两个 64x64 后 12288，再进 96x64 需回收一个 LRU
        # （12288-6144+9216=15360 ≤ 16000，恰好回收一个）。
        session, drv = _make_session(
            monkeypatch, max_ring_size=32, max_pool_bytes=16000)
        assert session.initialize()
        r1 = session.bind_resident_frame(_frame(w=64, h=64, index=1))
        r2 = session.bind_resident_frame(_frame(w=64, h=64, index=2))
        assert r1 is not None and r2 is not None
        r1[1].release()  # slot1 归还 → 空闲（LRU 最旧）

        r3 = session.bind_resident_frame(_frame(w=96, h=64, index=3))
        assert r3 is not None
        assert session.stats["slots"] == 2
        assert drv.calls.count("cuGraphicsUnregisterResource") == 2  # evict

    def test_active_lease_never_evicted(self, monkeypatch):
        session, drv = _make_session(
            monkeypatch, max_ring_size=32, max_pool_bytes=13000)
        assert session.initialize()
        r1 = session.bind_resident_frame(_frame(w=64, h=64, index=1))
        r2 = session.bind_resident_frame(_frame(w=64, h=64, index=2))
        assert r1 is not None and r2 is not None
        # 两 slot 都活跃（租约未归还）：无可回收 → 诚实拒绝
        r3 = session.bind_resident_frame(_frame(w=64, h=64, index=3))
        assert r3 is None
        assert "slot_pool_exhausted" in session.last_fallback_reason
        assert "bytes=" in session.last_fallback_reason
        assert drv.calls.count("cuGraphicsUnregisterResource") == 0
        r1[1].release()  # 归还后可复用（同 key 命中空闲）
        r4 = session.bind_resident_frame(_frame(w=64, h=64, index=4))
        assert r4 is not None

    def test_count_cap_evicts_idle_before_rejecting(self, monkeypatch):
        session, drv = _make_session(
            monkeypatch, ring_size=1, max_ring_size=2)
        assert session.initialize()
        r1 = session.bind_resident_frame(_frame(w=16, h=16, index=1))
        r2 = session.bind_resident_frame(_frame(w=32, h=32, index=2))
        assert r1 is not None and r2 is not None
        r1[1].release()
        # 计数满 2：第三个异 key slot 应回收空闲的 #1 而不是拒绝
        r3 = session.bind_resident_frame(_frame(w=48, h=48, index=3))
        assert r3 is not None
        assert session.stats["slots"] == 2
        # 计数满且全活跃 → 拒绝
        r4 = session.bind_resident_frame(_frame(w=64, h=64, index=4))
        assert r4 is None
        assert "slot_pool_exhausted" in session.last_fallback_reason
        assert f"count=2/2" in session.last_fallback_reason

    def test_pool_bytes_accounting_round_trips(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        leases = []
        for i in range(4):
            r = session.bind_resident_frame(_frame(w=64 + 64 * i,
                                                   h=64, index=i))
            assert r is not None
            leases.append(r)
        expected = sum(
            CudaGlInteropSession._slot_byte_cost((64 + 64 * i, 64, "nv12"))
            for i in range(4))
        assert session.stats["pool_bytes"] == expected
        for _, release in leases:
            release.release()
        session.destroy()
        assert session.stats["pool_bytes"] == 0

    def test_slot_reuse_updates_lru_order(self, monkeypatch):
        session, drv = _make_session(
            monkeypatch, ring_size=2, max_ring_size=3, max_pool_bytes=None)
        assert session.initialize()
        # max_pool_bytes=None → 走默认 1GiB（不构成约束）
        keys = [(16, 16), (32, 32), (48, 48)]
        releases = []
        for i, (w, h) in enumerate(keys):
            r = session.bind_resident_frame(_frame(w=w, h=h, index=i))
            assert r is not None
            releases.append(r)
        for _planes, rel in releases:
            rel.release()
        # 复用 slot0（16x16，变为最新），随后 count=3 满时最旧应为 slot1
        r0 = session.bind_resident_frame(_frame(w=16, h=16, index=9))
        assert r0 is not None
        r4 = session.bind_resident_frame(_frame(w=64, h=64, index=4))
        assert r4 is not None
        remaining_keys = {s.key for s in session._slots}
        assert (16, 16, "nv12") in remaining_keys  # 被复用过的未被回收
        assert (32, 32, "nv12") not in remaining_keys  # LRU 被回收


# ---------------------------------------------------------------------------
# current-context 契约（R12）
# ---------------------------------------------------------------------------

class TestCurrentContextGuardW02:
    def test_bind_restores_caller_context_on_success(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        caller_ctx = _FakeCtx(0x777)
        drv.cuCtxSetCurrent(caller_ctx)  # 同线程留下其它 current
        r = session.bind_resident_frame(_frame())
        assert r is not None
        assert drv.current is caller_ctx, "bind 后必须恢复调用者上下文"

    def test_bind_restores_caller_context_on_copy_failure(
            self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        caller_ctx = _FakeCtx(0x778)
        drv.cuCtxSetCurrent(caller_ctx)
        drv.fail_copy = True
        r = session.bind_resident_frame(_frame())
        assert r is None
        assert "copy_failed" in session.last_fallback_reason
        assert drv.current is caller_ctx

    def test_no_current_before_bind_stays_none_after(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        drv.cuCtxSetCurrent(_FakeCtx(0))  # 模拟无 current（0 句柄）
        drv.current = None
        r = session.bind_resident_frame(_frame())
        assert r is not None
        # 调用者原本无 current：守卫退出后不留我们的私有上下文
        assert drv.current is None or int(drv.current) == 0

    def test_owner_thread_still_enforced(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        result: dict = {}

        def _other_thread():
            result["r"] = session.bind_resident_frame(_frame())

        t = threading.Thread(target=_other_thread)
        t.start()
        t.join()
        assert result["r"] is None
        assert session.last_fallback_reason == "session_thread_mismatch"

    def test_initialize_verifies_current_context(self, monkeypatch):
        # cuCtxGetCurrent 恒报别的上下文（驱动异常）→ initialize 拒绝并
        # 销毁刚创建的上下文。需要直接替换命名空间的 cuCtxGetCurrent，
        # 否则 SetCurrent 副作用会让校验自然通过。
        drv, ns = _install_fake_driver_ns(monkeypatch)

        def lying_get():
            return (0, _FakeCtx(0xDEAD))
        ns.cuCtxGetCurrent = lying_get

        session = CudaGlInteropSession()
        monkeypatch.setattr(
            session, "_create_plane_textures",
            lambda w, h, f: (_FakeTex(), _FakeTex()))
        ok = session.initialize()
        assert ok is False
        assert "cuCtxGetCurrent_verify" in session.last_fallback_reason
        assert drv.destroyed_ctxs, "创建成功但校验失败必须销毁上下文"


# ---------------------------------------------------------------------------
# 租约与状态
# ---------------------------------------------------------------------------

class TestLeaseAndStatsW02:
    def test_release_exactly_once_and_slot_reusable(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        r = session.bind_resident_frame(_frame())
        planes, release = r
        release.release()
        release.release()  # 幂等
        assert session.stats["slots_in_use"] == 0
        r2 = session.bind_resident_frame(_frame())
        assert r2 is not None
        assert session.stats["slots"] == 1  # 复用而非新建
        assert drv.calls.count("cuGraphicsGLRegisterImage") == 2

    def test_stats_exposes_pool_and_ctx_fields(self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        assert session.initialize()
        st = session.stats
        for field in ("initialized", "destroyed", "slots", "slots_in_use",
                      "max_ring_size", "pool_bytes", "max_pool_bytes",
                      "ctx", "bind_count", "fail_count"):
            assert field in st
        assert st["ctx"].startswith("0x")

    def test_hundred_create_destroy_cycles_no_ctx_accumulation(
            self, monkeypatch):
        session, drv = _make_session(monkeypatch)
        for _ in range(100):
            assert session.initialize()
            r = session.bind_resident_frame(_frame())
            assert r is not None
            r[1].release()
            session.destroy()
            session = CudaGlInteropSession()
            monkeypatch.setattr(
                session, "_create_plane_textures",
                lambda w, h, f: (_FakeTex(), _FakeTex()))
        assert len(drv.destroyed_ctxs) == 100
