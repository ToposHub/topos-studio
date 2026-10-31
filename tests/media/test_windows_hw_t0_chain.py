"""T0 零回读直通链 + 编码 surface 契约测试（计划步骤 2）。

注入式测试钉死：
- 三段 LUID 一致性（跨 adapter 必须得到 adapter_mismatch reason）；
- 原生桥边界的 fail-closed：解码 surface 缺位 / 硬编提交缺位时链路
  如实中止，绝不静默回 CPU（trail 计数保持 0 是"没有隐藏 CPU 路径"
  的证据，中止 reason 是"为什么不能继续"的证据）；
- 编码 surface 词表与同卡校验；
- 分层 zero-copy probe（真机类）。
"""

from __future__ import annotations

import platform
from typing import Optional, Tuple

import pytest

from src.shared.export.gpu_trail import ExportGpuTrail
from src.shared.media.windows_hw.d3d11_device import (
    DXGI_FORMAT_NV12,
    DXGI_FORMAT_P010,
    D3D11Device,
    D3D11DeviceIdentity,
    check_format_support,
    create_d3d11_device,
)
from src.shared.media.windows_hw.d3d11_frame import D3D11GpuFrame
from src.shared.media.windows_hw.d3d11_passthrough_chain import (
    D3D11PassthroughChain,
)
from src.shared.media.windows_hw.d3d11_texture_pool import D3D11TexturePool
from src.shared.media.windows_hw.gpu_encode_surface import (
    D3D11EncodeSurfaceProvider,
    GpuEncodeSurface,
)
from src.shared.media.windows_hw.reasons import FallbackReason

DEVICE_LUID = "00011E23-00000000"
DECODE_LUID = "00011E23-00000000"
OTHER_LUID = "0002ABCD-00000000"


class FakeReleaser:
    def __init__(self) -> None:
        self.released: list = []

    def __call__(self, ptr: int) -> None:
        self.released.append(ptr)


def make_fake_pool(depth: int = 4) -> tuple:
    calls = {"n": 0}
    releaser = FakeReleaser()

    def factory() -> int:
        calls["n"] += 1
        return 0xBEEF0000 + calls["n"]

    pool = D3D11TexturePool(
        depth=depth, width=1920, height=1080,
        dxgi_format=DXGI_FORMAT_NV12,
        texture_factory=factory, texture_releaser=releaser,
    )
    return pool, releaser


def make_fake_device(luid: str = DEVICE_LUID) -> D3D11Device:
    return D3D11Device(
        0xABCDEF, 0x123456,
        D3D11DeviceIdentity(
            feature_level=0xC100,
            adapter_luid=luid,
            adapter_description="Fake AMD Radeon",
            vendor_name="amd",
            bgra_support=True,
        ),
    )


def ok_format_support(device: D3D11Device, fmt: int) -> Tuple[bool, int, str]:
    return True, 0x80, ""


class FakeDecodeSource:
    """按提供的帧序列供帧；耗尽后返回 (None, end_of_stream)。

    close() 释放队列中未读的帧——这是解码源的生命周期契约（原生桥
    实现必须遵守）：链中止时链只归还它拿到的帧，源队列里的帧归源。
    """

    def __init__(self, frames: list, luid: str = DECODE_LUID) -> None:
        self._frames = list(frames)
        self._luid = luid
        self.closed = False

    def read_frame(self) -> Tuple[Optional[D3D11GpuFrame], str]:
        if self._frames:
            return self._frames.pop(0), ""
        return None, "end_of_stream"

    def adapter_luid(self) -> str:
        return self._luid

    def close(self) -> None:
        for frame in self._frames:
            frame.release()
        self._frames.clear()
        self.closed = True


def make_frame(pool: D3D11TexturePool, luid: str = DECODE_LUID) -> D3D11GpuFrame:
    lease = pool.try_acquire()
    assert lease is not None
    return D3D11GpuFrame(
        texture_ptr=lease.texture_ptr,
        dxgi_format=DXGI_FORMAT_NV12,
        width=1920, height=1080,
        lease=lease, adapter_luid=luid,
        frame_index=0, pts_us=0,
    )


def make_chain(
    frames: int,
    decode_luid: str = DECODE_LUID,
    device_luid: str = DEVICE_LUID,
) -> tuple:
    pool, releaser = make_fake_pool(depth=max(2, frames))
    device = make_fake_device(device_luid)
    provider = D3D11EncodeSurfaceProvider(
        device, "amf", 1920, 1080,
        format_support_fn=ok_format_support,
    )
    trail = ExportGpuTrail("t0-test", adapter_luid=device_luid)
    source = FakeDecodeSource(
        [make_frame(pool, decode_luid) for _ in range(frames)],
        luid=decode_luid,
    )
    chain = D3D11PassthroughChain(device, source, provider, trail)
    return chain, pool, releaser, device, provider, trail, source


class TestEncodeSurfaceContract:
    def test_encoder_kind_vocabulary(self) -> None:
        device = make_fake_device()
        with pytest.raises(ValueError, match="词表"):
            D3D11EncodeSurfaceProvider(device, "ffmpeg_cpu", 1920, 1080)

    def test_prepare_requires_format_support(self) -> None:
        device = make_fake_device()

        def failing(device: D3D11Device, fmt: int) -> Tuple[bool, int, str]:
            return False, 0, "gpu_probe_failed:check_format_support E_FAIL"

        provider = D3D11EncodeSurfaceProvider(
            device, "amf", 1920, 1080, format_support_fn=failing,
        )
        ok, why = provider.prepare()
        assert not ok
        assert "check_format_support" in why
        assert not provider.prepared

    def test_surface_rejects_cross_adapter_frame(self) -> None:
        pool, _ = make_fake_pool(depth=1)
        device = make_fake_device(DEVICE_LUID)
        provider = D3D11EncodeSurfaceProvider(
            device, "nvenc", 1920, 1080,
            format_support_fn=ok_format_support,
        )
        provider.prepare()
        frame = make_frame(pool, luid=OTHER_LUID)
        surface, why = provider.surface_from_frame(frame)
        assert surface is None
        assert FallbackReason.ADAPTER_MISMATCH.value in why
        assert OTHER_LUID in why
        frame.release()

    def test_surface_rejects_format_and_size_mismatch(self) -> None:
        pool, _ = make_fake_pool(depth=1)
        device = make_fake_device()
        provider = D3D11EncodeSurfaceProvider(
            device, "amf", 3840, 2160,
            format_support_fn=ok_format_support,
        )
        provider.prepare()
        frame = make_frame(pool)
        surface, why = provider.surface_from_frame(frame)
        assert surface is None
        assert "surface_size_mismatch" in why
        frame.release()

    def test_gpu_encode_surface_vocabulary(self) -> None:
        pool, _ = make_fake_pool(depth=1)
        lease = pool.try_acquire()
        assert lease is not None
        with pytest.raises(ValueError, match="词表"):
            GpuEncodeSurface(
                texture_ptr=lease.texture_ptr, dxgi_format=DXGI_FORMAT_NV12,
                width=1920, height=1080, encoder_kind="who",
                adapter_luid=DEVICE_LUID, lease=lease,
            )
        lease.release()


class TestPassthroughChain:
    def test_run_without_prepare_fails_closed(self) -> None:
        chain, pool, releaser, device, provider, trail, source = make_chain(2)
        outcome = chain.run(2)
        assert not outcome.ok
        assert outcome.reason == FallbackReason.NOT_PROBED.value

    def test_luid_mismatch_blocks_prepare(self) -> None:
        chain, pool, releaser, device, provider, trail, source = make_chain(
            2, decode_luid=OTHER_LUID,
        )
        ok, why = chain.prepare()
        assert not ok
        assert FallbackReason.ADAPTER_MISMATCH.value in why
        assert OTHER_LUID in why and DEVICE_LUID in why

    def test_encode_submit_boundary_fails_closed(self) -> None:
        """原生桥缺位：链路在 encode_submit 显式中止，不伪报成功。"""
        chain, pool, releaser, device, provider, trail, source = make_chain(4)
        ok, why = chain.prepare()
        assert ok, why
        outcome = chain.run(4)
        assert not outcome.ok
        assert FallbackReason.NOT_YET_IMPLEMENTED.value in outcome.reason
        assert "encode_submit" in outcome.reason
        # 帧在提交失败前已被归还；源队列里未读的帧由 source.close() 释放
        # （所有权边界：链只负责它拿到的帧）。
        assert pool.stats().in_use == 3
        chain.close()
        assert pool.stats().in_use == 0
        # 零 CPU 像素搬运保持成立：链路从未走 CPU 路径。
        assert outcome.zero_pixel_transfer
        assert outcome.cpu_upload_bytes == 0
        assert outcome.cpu_download_bytes == 0
        snap = trail.snapshot()
        assert snap.fallback_reason.startswith(
            "encode_submit:" + FallbackReason.NOT_YET_IMPLEMENTED.value
        )

    def test_decode_exhaustion_stops_chain(self) -> None:
        """解码源供帧耗尽（帧 0）时链路以 decode_read_frame 显式中止。

        注：有帧可读时链路会先停在 encode_submit 边界（原生桥缺位），
        解码耗尽路径只能用空帧源触达——这正是 fail-closed 边界的顺序。
        """
        chain, pool, releaser, device, provider, trail, source = make_chain(0)
        ok, why = chain.prepare()
        assert ok, why
        outcome = chain.run(4)
        assert not outcome.ok
        assert "decode_read_frame" in outcome.reason
        assert "decode_read_frame" in trail.snapshot().fallback_reason
        assert outcome.frames_completed == 0
        assert outcome.zero_pixel_transfer
        assert pool.stats().in_use == 0
        chain.close()
        assert source.closed

    def test_invalid_frame_count_rejected(self) -> None:
        chain, *_ = make_chain(1)
        with pytest.raises(ValueError):
            chain.run(0)


@pytest.mark.skipif(
    platform.system() != "Windows",
    reason="需要真机 D3D11（计划步骤 2 分层 probe）",
)
class TestLayeredProbeOnWindows:
    def test_run_probe_layered_evidence(self) -> None:
        from src.shared.media.windows_hw.windows_zero_copy_probe import run_probe

        result = run_probe(frames=16)
        assert result.probe_success, result.fallback_reason
        assert result.platform == "Windows"
        # device / 格式 / 纹理池层全部真实通过
        assert result.layers["device"]["ok"] is True
        assert result.layers["device"]["adapter_luid"]
        assert result.layers["format_support_nv12"]["ok"] is True
        assert result.layers["texture_pool"]["ok"] is True
        # 解码 surface 层仍是 fail-closed 边界：不伪报零拷贝
        assert result.d3d11_decoder_available is False
        assert result.zero_copy is False
        assert result.fallback_reason is not None
        assert "decode_surface_layer:not_yet_implemented" in (
            result.fallback_reason
        )

    def test_provider_prepare_with_real_format_check(self) -> None:
        device, reason = create_d3d11_device()
        assert device is not None, reason
        try:
            provider = D3D11EncodeSurfaceProvider(
                device, "amf", 1920, 1080, dxgi_format=DXGI_FORMAT_NV12,
            )
            ok, why = provider.prepare()
            assert ok, why
            assert provider.prepared
        finally:
            device.release()
