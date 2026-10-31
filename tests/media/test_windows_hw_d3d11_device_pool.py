"""D3D11 device 生命周期 / 定长纹理池 / GPU 帧契约测试（计划步骤 1）。

注入式测试用假纹理工厂覆盖池的全部治理语义（封口、耗尽、反压、恰一次
归还、拒绝跨池归还）；真机类（Windows）验证 ctypes 走查层与真实
CreateTexture2D。1,000 帧完整门禁由 scripts/probe_d3d11_device_pool_windows.py
在真机执行并归档。
"""

from __future__ import annotations

import platform
import threading
from typing import List

import pytest

from src.shared.media.windows_hw.d3d11_device import (
    DXGI_FORMAT_NV12,
    DXGI_FORMAT_P010,
    D3D11Device,
    D3D11DeviceIdentity,
    VTABLE_SLOT_CREATE_TEXTURE2D,
    VTABLE_SLOT_GET_FEATURE_LEVEL,
    create_d3d11_device,
)
from src.shared.media.windows_hw.d3d11_frame import D3D11GpuFrame
from src.shared.media.windows_hw.d3d11_texture_pool import (
    D3D11TexturePool,
    create_d3d11_texture_pool,
)


class FakeTextureFactory:
    """返回递增伪指针的工厂；记录调用次数以钉死"封口"语义。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return 0xC0FFEE00 + self.calls


def make_pool(depth: int = 4, **kwargs: object) -> tuple:
    factory = FakeTextureFactory()
    released: List[int] = []

    def fake_releaser(ptr: int) -> None:
        released.append(ptr)

    kwargs.setdefault("dxgi_format", DXGI_FORMAT_NV12)
    pool = D3D11TexturePool(
        depth=depth,
        width=1920,
        height=1080,
        texture_factory=factory,
        texture_releaser=fake_releaser,
        **kwargs,
    )
    pool._test_released = released  # type: ignore[attr-defined]
    return pool, factory


class TestPoolSealing:
    def test_constructs_all_textures_upfront(self) -> None:
        pool, factory = make_pool(depth=4)
        assert factory.calls == 4
        assert pool.stats().created == 4
        assert pool.stats().in_use == 0
        pool.close()

    def test_factory_never_called_after_seal(self) -> None:
        """计划门禁：封口后逐帧借还 1000 次，工厂调用数不变（无增长）。"""
        pool, factory = make_pool(depth=4)
        for _ in range(1000):
            lease = pool.try_acquire()
            assert lease is not None
            lease.release()
        assert factory.calls == 4
        stats = pool.stats()
        assert stats.serviced == 1000
        assert stats.peak_in_use == 1
        assert stats.created == 4
        pool.close()

    def test_invalid_format_rejected(self) -> None:
        with pytest.raises(ValueError, match="半平面格式"):
            D3D11TexturePool(
                depth=2, width=64, height=64, dxgi_format=87,
                texture_factory=FakeTextureFactory(),
            )

    def test_odd_height_rejected(self) -> None:
        with pytest.raises(ValueError, match="偶数"):
            D3D11TexturePool(
                depth=2, width=64, height=65, dxgi_format=DXGI_FORMAT_NV12,
                texture_factory=FakeTextureFactory(),
            )

    def test_failed_creation_rolls_back(self) -> None:
        calls: List[int] = []
        released: List[int] = []

        def failing_factory() -> int:
            calls.append(1)
            if len(calls) >= 3:
                raise RuntimeError("CreateTexture2D hr=0x887A0005")
            return 0xDEAD00 + len(calls)

        with pytest.raises(RuntimeError):
            D3D11TexturePool(
                depth=4, width=64, height=64,
                dxgi_format=DXGI_FORMAT_NV12,
                texture_factory=failing_factory,
                texture_releaser=released.append,
            )
        assert len(calls) == 3
        # 已创建的 2 张在回滚中被"释放"，绝不交付残池。
        assert len(released) == 2


class TestLeaseSemantics:
    def test_exhaustion_returns_none(self) -> None:
        pool, _ = make_pool(depth=2)
        a = pool.try_acquire()
        b = pool.try_acquire()
        assert a is not None and b is not None
        assert pool.try_acquire() is None
        assert pool.stats().acquire_timeouts == 1
        a.release()
        b.release()
        pool.close()

    def test_blocking_acquire_times_out(self) -> None:
        pool, _ = make_pool(depth=1)
        lease = pool.acquire(timeout_s=0.01)
        assert lease is not None
        start = threading.Event()
        results: list = []

        def waiter() -> None:
            start.set()
            results.append(pool.acquire(timeout_s=0.05))

        thread = threading.Thread(target=waiter)
        thread.start()
        start.wait()
        thread.join(timeout=2.0)
        assert results == [None]
        lease.release()
        pool.close()

    def test_blocking_acquire_wakes_on_return(self) -> None:
        pool, _ = make_pool(depth=1)
        lease = pool.acquire(timeout_s=0.01)
        assert lease is not None
        results: list = []

        def waiter() -> None:
            results.append(pool.acquire(timeout_s=2.0))

        thread = threading.Thread(target=waiter)
        thread.start()
        lease.release()
        thread.join(timeout=2.0)
        assert len(results) == 1 and results[0] is not None
        results[0].release()
        pool.close()

    def test_double_release_rejected(self) -> None:
        pool, _ = make_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        lease.release()
        with pytest.raises(ValueError, match="重复归还"):
            lease.release()
        pool.close()

    def test_texture_ptr_invalid_after_release(self) -> None:
        pool, _ = make_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        ptr = lease.texture_ptr
        lease.release()
        with pytest.raises(RuntimeError, match="已归还"):
            _ = lease.texture_ptr
        assert ptr
        pool.close()

    def test_close_rejected_with_outstanding_lease(self) -> None:
        pool, _ = make_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        with pytest.raises(ValueError, match="在途 lease"):
            pool.close()
        lease.release()
        pool.close()

    def test_close_is_idempotent(self) -> None:
        pool, _ = make_pool(depth=1)
        pool.close()
        pool.close()
        assert len(pool._test_released) == 1  # type: ignore[attr-defined]

    def test_created_counter_survives_close(self) -> None:
        """审计语义：close 清空 free 后 created 仍等于池深，不归零。"""
        pool, _ = make_pool(depth=3)
        assert pool.stats().created == 3
        pool.close()
        stats = pool.stats()
        assert stats.created == 3
        assert stats.closed is True

    def test_concurrent_acquire_release(self) -> None:
        pool, factory = make_pool(depth=4)
        errors: list = []

        def worker() -> None:
            for _ in range(100):
                lease = pool.try_acquire()
                if lease is None:
                    continue
                try:
                    lease.release()
                except ValueError as exc:  # pragma: no cover
                    errors.append(exc)
                    return

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert factory.calls == 4
        assert pool.stats().in_use == 0
        pool.close()


class TestConvenienceEntry:
    def test_create_pool_rejects_bad_params(self) -> None:
        pool, reason = create_d3d11_texture_pool(
            0xBEEF, depth=2, width=64, height=65,
        )
        assert pool is None
        assert "invalid_pool" in reason

    def test_p010_pool_accepted(self) -> None:
        pool, _ = make_pool(depth=2, dxgi_format=DXGI_FORMAT_P010)
        assert pool.dxgi_format == DXGI_FORMAT_P010
        lease = pool.try_acquire()
        assert lease is not None
        lease.release()
        pool.close()


class TestD3D11GpuFrame:
    def make_frame(self, **overrides: object) -> D3D11GpuFrame:
        pool, _ = make_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        defaults: dict = dict(
            texture_ptr=lease.texture_ptr,
            dxgi_format=DXGI_FORMAT_NV12,
            width=1920,
            height=1080,
            lease=lease,
            adapter_luid="0x1-0x2",
            frame_index=7,
            pts_us=280_000,
        )
        defaults.update(overrides)
        frame = D3D11GpuFrame(**defaults)  # type: ignore[arg-type]
        return frame

    def test_valid_frame_and_release_delegation(self) -> None:
        pool, _ = make_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        frame = D3D11GpuFrame(
            texture_ptr=lease.texture_ptr,
            dxgi_format=DXGI_FORMAT_NV12,
            width=1920, height=1080,
            lease=lease, adapter_luid="0x1-0x2",
        )
        frame.release()
        assert lease.released
        assert pool.stats().in_use == 0

    def test_no_cpu_planes_field(self) -> None:
        """契约：D3D11 帧不暴露 CPU planes（与 Metal 版 GpuDecodedFrame 区别）。"""
        fields = {f for f in D3D11GpuFrame.__dataclass_fields__}
        assert not ({"planes", "data", "planes_uv"} & fields)

    def test_empty_luid_rejected(self) -> None:
        with pytest.raises(ValueError, match="adapter_luid"):
            self.make_frame(adapter_luid="")

    def test_bit_depth_format_consistency(self) -> None:
        with pytest.raises(ValueError, match="bit_depth"):
            self.make_frame(bit_depth=10, dxgi_format=DXGI_FORMAT_NV12)

    def test_p010_frame_ok(self) -> None:
        frame = self.make_frame(dxgi_format=DXGI_FORMAT_P010, bit_depth=10)
        assert frame.is_10bit
        frame.release()

    def test_trail_dict_has_no_pointers(self) -> None:
        frame = self.make_frame()
        trail = frame.to_trail_dict()
        assert "texture_ptr" not in trail
        assert trail["kind"] == "d3d11_gpu_frame"
        frame.release()


class TestDeviceLifecycle:
    def _make_device(self) -> D3D11Device:
        return D3D11Device(
            0xD1CE00,
            0xC0DE7,
            D3D11DeviceIdentity(
                feature_level=0xC100,
                adapter_luid="0x1-0x2",
                adapter_description="Fake GPU",
                vendor_name="amd",
                bgra_support=True,
            ),
        )

    def test_release_frees_context_then_device(self, monkeypatch) -> None:
        """context 持有 device 引用：release 必须先 context 后 device，缺一即泄漏。"""
        from src.shared.media.windows_hw import d3d11_device as device_module

        released: List[int] = []
        monkeypatch.setattr(device_module, "release_com_object", released.append)
        device = self._make_device()
        device.release()
        assert released == [0xC0DE7, 0xD1CE00]
        with pytest.raises(ValueError):
            device.release()


@pytest.mark.skipif(
    platform.system() != "Windows",
    reason="需要真机 D3D11（计划步骤 1 真机门禁）",
)
class TestRealDeviceOnWindows:
    def test_device_creation_and_luid(self) -> None:
        device, reason = create_d3d11_device()
        assert device is not None, f"device 创建失败: {reason}"
        try:
            identity = device.identity
            # FL 阶梯显式请求 12.1→11.0；真机不得低于 11.0。
            assert identity.feature_level >= 0xB000  # D3D 11.0 = 0xB000
            # 本机 RX 6800：adapter LUID 必须走查成功（计划步骤 1 门禁）
            assert identity.adapter_luid, "真机 LUID 走查不能为空"
            assert identity.adapter_description
        finally:
            device.release()
        with pytest.raises(RuntimeError):
            _ = device.device_ptr
        with pytest.raises(ValueError):
            device.release()

    def test_nv12_pool_lease_loop_real_textures(self) -> None:
        device, reason = create_d3d11_device()
        assert device is not None, reason
        try:
            pool, pool_reason = create_d3d11_texture_pool(
                device.device_ptr, depth=8, width=1920, height=1080,
                dxgi_format=DXGI_FORMAT_NV12,
            )
            assert pool is not None, pool_reason
            try:
                for _ in range(200):
                    lease = pool.try_acquire()
                    assert lease is not None
                    assert lease.texture_ptr
                    lease.release()
                stats = pool.stats()
                assert stats.created == 8
                assert stats.serviced == 200
                assert stats.peak_in_use == 1
                assert stats.in_use == 0
            finally:
                pool.close()
        finally:
            device.release()

    def test_format_support_and_p010_pool_fail_closed(self) -> None:
        """格式预检必须真实：NV12 可用；P010 按真机能力 fail-closed。

        RX 6800 实测：CheckFormatSupport(P010) = E_FAIL，手动创建 P010
        纹理池必须得到结构化 reason 而不是半初始化池或崩溃。
        """
        from src.shared.media.windows_hw.d3d11_device import check_format_support

        device, reason = create_d3d11_device()
        assert device is not None, reason
        try:
            ok, bits, why = check_format_support(device, DXGI_FORMAT_NV12)
            assert ok, why
            # W01 修正：0x80 是 D3D11_FORMAT_SUPPORT_TEXTURECUBE（把
            # D3D11_BIND_SHADER_RESOURCE=0x8 的 bind 枚举与 format-support
            # 枚举混用的笔误）。NV12 作为可采样 2D 纹理的正确断言：
            # TEXTURE2D(0x20) + SHADER_SAMPLE(0x200)（4090/621.01 实测
            # bits=0xfa82c320，含两者、无 CUBE——视频平面格式本就无 cube）。
            assert bits & 0x20, "NV12 应支持 TEXTURE2D"
            assert bits & 0x200, "NV12 应支持 SHADER_SAMPLE"

            ok_p010, bits_p010, _ = check_format_support(device, DXGI_FORMAT_P010)
            pool, p010_reason = create_d3d11_texture_pool(
                device.device_ptr, depth=2, width=1920, height=1080,
                dxgi_format=DXGI_FORMAT_P010,
            )
            if ok_p010 and pool is None:
                pytest.fail(
                    f"格式预检通过但池创建失败: {p010_reason}"
                )
            if not ok_p010:
                # 预检已判死：池必须拒绝（fail-closed），reason 非空。
                assert pool is None
                assert "create_texture" in p010_reason
            else:
                assert pool is not None
                pool.close()
        finally:
            device.release()

    def test_vtable_slots(self) -> None:
        from src.shared.media.windows_hw.d3d11_device import (
            VTABLE_SLOT_GET_IMMEDIATE_CONTEXT,
        )

        # 槽位钉死契约：错位会在此立即失败，而不是在生产里崩溃。
        assert VTABLE_SLOT_CREATE_TEXTURE2D == 5
        assert VTABLE_SLOT_GET_FEATURE_LEVEL == 37
        # AUD-36000：27 是 CreateDeferredContext；GetImmediateContext 按
        # SDK d3d11.h 实序为 40（真机探针只实测过 [5]/[37]，未覆盖此槽）。
        assert VTABLE_SLOT_GET_IMMEDIATE_CONTEXT == 40
