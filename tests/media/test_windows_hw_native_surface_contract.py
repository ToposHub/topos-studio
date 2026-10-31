"""Native D3D11 bridge, fence and render-plan contract tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from src.shared.export.gpu_trail import ExportGpuTrail
from src.shared.media.windows_hw.d3d11_device import (
    DXGI_FORMAT_NV12,
    DXGI_FORMAT_P010,
    D3D11Device,
    D3D11DeviceIdentity,
)
from src.shared.media.windows_hw.d3d11_export_capability import (
    D3D11T0Evidence,
    probe_d3d11_export_capability,
)
from src.shared.media.windows_hw.d3d11_frame import D3D11GpuFrame
from src.shared.media.windows_hw.d3d11_render_graph import (
    D3D11RenderResult,
    D3D11RenderTarget,
    D3D11TimelineRenderer,
    compile_d3d11_render_plan,
)
from src.shared.media.windows_hw.d3d11_sync import EventD3D11Fence, ImmediateD3D11Fence
from src.shared.media.windows_hw.d3d11_texture_pool import (
    D3D11ExternalTextureLease,
    D3D11TexturePool,
)
from src.shared.media.windows_hw.d3d11_decoder import D3D11Decoder
from src.shared.media.windows_hw.gpu_encode_surface import D3D11EncodeSurfaceProvider
from src.shared.media.windows_hw.native_bridge import (
    NativeBridgeCapabilities,
    NativeDecodedTexture,
    NativeEncodeSubmitResult,
    load_windows_native_bridge,
)


DEVICE_LUID = "00000001-00000000"


class FakeBridge:
    def __init__(self) -> None:
        self.closed: list[object] = []
        self.submissions: list[int] = []
        self.size = (1920, 1080)
        self.read = 0

    def capabilities(self) -> NativeBridgeCapabilities:
        return NativeBridgeCapabilities(
            api_version=1,
            name="fake-d3d11",
            version="test",
            decode_codecs=("h264", "hevc"),
            encode_kinds=("amf", "nvenc"),
            d3d11_renderer=True,
        )

    def open_decoder(self, path: str, codec: str, device_ptr: int) -> object:
        self.read = 0
        return {"path": path, "codec": codec, "device_ptr": device_ptr}

    def decode_next(self, session: object) -> NativeDecodedTexture | None:
        if self.read:
            return None
        self.read = 1
        return NativeDecodedTexture(
            texture_handle=0xCAFE,
            width=self.size[0],
            height=self.size[1],
            frame_index=3,
            pts_us=120_000,
            pixel_format="nv12",
            bit_depth=8,
            adapter_luid=DEVICE_LUID,
        )

    def close_decoder(self, session: object) -> None:
        self.closed.append(session)

    def submit_encode(
        self, texture_handle: int, dxgi_format: int, width: int, height: int,
        encoder_kind: str, adapter_luid: str,
    ) -> NativeEncodeSubmitResult:
        self.submissions.append(texture_handle)
        return NativeEncodeSubmitResult(
            accepted=True,
            gpu_submit_ms=0.25,
            completion_fence=ImmediateD3D11Fence(),
            packet_count=1,
        )


def make_device() -> D3D11Device:
    return D3D11Device(
        0x1111,
        0x2222,
        D3D11DeviceIdentity(
            feature_level=0xC100,
            adapter_luid=DEVICE_LUID,
            adapter_description="Fake GPU",
            vendor_name="amd",
            bgra_support=True,
        ),
    )


def make_pool(depth: int = 2) -> D3D11TexturePool:
    return D3D11TexturePool(
        depth=depth,
        width=1920,
        height=1080,
        dxgi_format=DXGI_FORMAT_NV12,
        texture_factory=(lambda n=iter(range(1, depth + 1)): next(n)),
        texture_releaser=lambda _ptr: None,
    )


def test_native_bridge_loader_validates_protocol() -> None:
    module = SimpleNamespace(create_bridge=lambda: FakeBridge())
    result = load_windows_native_bridge(module=module)
    assert result.available
    assert result.capabilities is not None
    assert result.capabilities.d3d11_renderer

    malformed = SimpleNamespace(create_bridge=lambda: object())
    invalid = load_windows_native_bridge(module=malformed)
    assert not invalid.available
    assert "protocol_mismatch" in invalid.reason


def test_decoder_wraps_native_texture_without_cpu_fields(tmp_path: Path) -> None:
    source = tmp_path / "input.mp4"
    source.write_bytes(b"test")
    bridge = FakeBridge()
    decoder = D3D11Decoder(bridge=bridge, device_ptr=0x1234)
    assert decoder.open(str(source), "h264")
    frame = decoder.decode_next_gpu_frame(lambda _decoded: lambda: None)
    assert frame is not None
    assert frame.adapter_luid == DEVICE_LUID
    assert frame.pts_us == 120_000
    assert not ({"planes", "data", "bgr", "rgb"} & set(frame.__dataclass_fields__))
    frame.release()
    decoder.close()
    assert bridge.closed


def test_decoder_reopen_with_new_source_resets_size_guard(tmp_path: Path) -> None:
    """同一 decoder 实例切换不同分辨率素材不得误报 decode_size_changed。"""
    source = tmp_path / "input.mp4"
    source.write_bytes(b"test")
    bridge = FakeBridge()
    decoder = D3D11Decoder(bridge=bridge, device_ptr=0x1234)
    assert decoder.open(str(source), "h264")
    first = decoder.decode_next()
    assert first is not None and (first.width, first.height) == (1920, 1080)
    decoder.close()

    bridge.size = (2048, 1080)  # 换源：新会话首帧尺寸不同
    assert decoder.open(str(source), "h264")
    second = decoder.decode_next()
    assert second is not None, decoder.last_fallback_reason
    assert (second.width, second.height) == (2048, 1080)
    decoder.close()


def test_lease_is_not_recycled_before_fence() -> None:
    pool = make_pool(1)
    lease = pool.try_acquire()
    assert lease is not None
    fence = EventD3D11Fence()
    lease.release_after(fence)
    assert pool.try_acquire() is None
    fence.signal()
    for _ in range(100):
        if pool.stats().in_use == 0:
            break
        time.sleep(0.001)
    assert pool.stats().in_use == 0
    recycled = pool.try_acquire()
    assert recycled is not None
    recycled.release()
    pool.close()


def test_native_encode_submission_uses_completion_fence() -> None:
    device = make_device()
    provider = D3D11EncodeSurfaceProvider(
        device,
        "amf",
        1920,
        1080,
        format_support_fn=lambda _device, _fmt: (True, 0x80, ""),
        native_bridge=FakeBridge(),
    )
    assert provider.prepare() == (True, "")
    pool = make_pool(1)
    lease = pool.try_acquire()
    assert lease is not None
    frame = D3D11GpuFrame(
        texture_ptr=lease.texture_ptr,
        dxgi_format=DXGI_FORMAT_NV12,
        width=1920,
        height=1080,
        lease=lease,
        adapter_luid=DEVICE_LUID,
    )
    surface, reason = provider.surface_from_frame(frame)
    assert surface is not None, reason
    assert provider.submit_to_encoder(surface) == (True, "")
    assert provider.last_completion_fence is not None
    surface.release_after(provider.last_completion_fence)
    assert pool.stats().in_use == 0


def test_native_owned_p010_is_not_blocked_by_manual_format_probe() -> None:
    device = make_device()
    provider = D3D11EncodeSurfaceProvider(
        device,
        "amf",
        1920,
        1080,
        dxgi_format=DXGI_FORMAT_P010,
        format_support_fn=lambda _device, _fmt: (False, 0, "manual_p010_rejected"),
        native_bridge=FakeBridge(),
    )
    assert provider.prepare() == (True, "")
    lease = D3D11ExternalTextureLease(0xCAFE, lambda: None)
    frame = D3D11GpuFrame(
        texture_ptr=0xCAFE,
        dxgi_format=DXGI_FORMAT_P010,
        width=1920,
        height=1080,
        lease=lease,
        adapter_luid=DEVICE_LUID,
        bit_depth=10,
    )
    surface, reason = provider.surface_from_frame(frame)
    assert surface is not None, reason
    assert provider.submit_to_encoder(surface) == (True, "")
    assert provider.last_completion_fence is not None
    surface.release_after(provider.last_completion_fence)


def test_render_plan_compiles_once_and_renderer_never_returns_cpu_pixels() -> None:
    plan = compile_d3d11_render_plan(
        timeline_revision="rev-1",
        renderer_version="test-renderer",
        source_format="nv12",
        output_format="nv12",
        layers=2,
        effects=("blur", "sharpen"),
        transitions=("wipe",),
        resize=True,
        primary_grade=True,
    )
    assert plan.passes[0].name == "decode"
    assert plan.passes[-1].name == "yuv_conversion"
    assert plan.cache_key == plan.cache_key
    assert "numpy" not in repr(plan).lower()


def test_d3d11_export_gate_never_claims_ready_from_declaration_only(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.shared.media.windows_hw.d3d11_export_capability.platform.system",
        lambda: "Windows",
    )
    result = probe_d3d11_export_capability(
        device=make_device(),
        bridge_module=SimpleNamespace(create_bridge=lambda: FakeBridge()),
    )
    assert not result.ready
    assert result.zero_copy_probe is False
    assert result.evidence["t0_evidence"] is False


def test_d3d11_export_gate_accepts_only_complete_t0_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.shared.media.windows_hw.d3d11_export_capability.platform.system",
        lambda: "Windows",
    )
    evidence = D3D11T0Evidence(
        adapter_luid=DEVICE_LUID,
        frame_count=480,
        output_playable=True,
        decode_gpu_ms=0.2,
        render_gpu_ms=0.3,
        yuv_gpu_ms=0.2,
        encode_submit_gpu_ms=0.4,
    )
    result = probe_d3d11_export_capability(
        device=make_device(),
        bridge_module=SimpleNamespace(create_bridge=lambda: FakeBridge()),
        t0_evidence=evidence,
    )
    assert result.ready
    assert result.zero_copy_probe
    assert result.evidence["t0_evidence"] is True
