"""Bounded pipeline, effect coverage and vendor policy tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.shared.export.gpu_trail import ExportGpuTrail
from src.shared.media.windows_hw.d3d11_format_contract import (
    D3D11FormatContract,
    same_d3d11_adapter,
    validate_d3d11_format,
)
from src.shared.media.windows_hw.d3d11_effects import D3D11_EFFECTS, d3d11_effect_capability
from src.shared.media.windows_hw.d3d11_frame import D3D11GpuFrame
from src.shared.media.windows_hw.d3d11_pipeline import D3D11PipelineScheduler
from src.shared.media.windows_hw.d3d11_render_graph import D3D11RenderResult, D3D11RenderTarget
from src.shared.media.windows_hw.d3d11_sync import ImmediateD3D11Fence
from src.shared.media.windows_hw.d3d11_texture_pool import D3D11TexturePool
from src.shared.media.windows_hw.native_bridge import NativeEncodeSubmitResult
from src.shared.media.windows_hw.vendor_policy import evaluate_vendor_ab, select_single_adapter


def make_frame(index: int, pool: D3D11TexturePool) -> D3D11GpuFrame:
    lease = pool.acquire()
    assert lease is not None
    return D3D11GpuFrame(
        texture_ptr=lease.texture_ptr,
        dxgi_format=103,
        width=1920,
        height=1080,
        lease=lease,
        adapter_luid="luid",
        frame_index=index,
    )


class Source:
    def __init__(self, frames: list[D3D11GpuFrame]) -> None:
        self.frames = frames

    def read_frame(self):
        return (self.frames.pop(0), "") if self.frames else (None, "end_of_stream")


def test_pipeline_is_bounded_and_releases_render_and_encode_leases() -> None:
    handles = iter(range(101, 109))
    pool = D3D11TexturePool(
        depth=8, width=1920, height=1080, dxgi_format=103,
        texture_factory=lambda: next(handles), texture_releaser=lambda _ptr: None,
    )
    target_handles = iter(range(201, 209))
    target_pool = D3D11TexturePool(
        depth=8, width=1920, height=1080, dxgi_format=103,
        texture_factory=lambda: next(target_handles), texture_releaser=lambda _ptr: None,
    )
    source = Source([make_frame(i, pool) for i in range(8)])

    def render(frame: D3D11GpuFrame) -> D3D11RenderResult:
        target_lease = target_pool.acquire()
        assert target_lease is not None
        return D3D11RenderResult(
            D3D11RenderTarget(
                texture_ptr=target_lease.texture_ptr,
                dxgi_format=103,
                width=frame.width,
                height=frame.height,
                adapter_luid=frame.adapter_luid,
                lease=target_lease,
                completion_fence=ImmediateD3D11Fence(),
            ),
            gpu_render_ms=0.1,
            gpu_yuv_ms=0.1,
        )

    # Render target intentionally shares the lease in this injected test;
    # immediate completion makes the ownership handoff observable.
    def encode(target: D3D11RenderTarget) -> NativeEncodeSubmitResult:
        return NativeEncodeSubmitResult(
            accepted=True,
            gpu_submit_ms=0.1,
            completion_fence=ImmediateD3D11Fence(),
        )

    scheduler = D3D11PipelineScheduler(render, encode, surface_depth=4)
    try:
        completed, telemetry = scheduler.run(source, 8)
        assert completed == 8
        assert telemetry.submitted == 8
        assert telemetry.completed == 8
        assert telemetry.peak_in_flight <= 4
    finally:
        scheduler.close()
        pool.close()
        target_pool.close()


def test_pipeline_records_gpu_stage_evidence_in_trail() -> None:
    source_handles = iter(range(301, 305))
    pool = D3D11TexturePool(
        depth=4, width=1920, height=1080, dxgi_format=103,
        texture_factory=lambda: next(source_handles), texture_releaser=lambda _ptr: None,
    )
    target_handles = iter(range(401, 405))
    target_pool = D3D11TexturePool(
        depth=4, width=1920, height=1080, dxgi_format=103,
        texture_factory=lambda: next(target_handles), texture_releaser=lambda _ptr: None,
    )
    lease = pool.acquire()
    assert lease is not None
    frame = D3D11GpuFrame(
        texture_ptr=lease.texture_ptr, dxgi_format=103, width=1920, height=1080,
        lease=lease, adapter_luid="luid", frame_index=0,
        gpu_decode_ms=0.2,
    )
    source = Source([frame])
    trail = ExportGpuTrail("pipeline-test", adapter_luid="luid")

    def render(item: D3D11GpuFrame) -> D3D11RenderResult:
        target_lease = target_pool.acquire()
        assert target_lease is not None
        return D3D11RenderResult(
            D3D11RenderTarget(
                texture_ptr=target_lease.texture_ptr, dxgi_format=103,
                width=item.width, height=item.height, adapter_luid=item.adapter_luid,
                lease=target_lease, completion_fence=ImmediateD3D11Fence(),
            ), gpu_render_ms=0.2, gpu_yuv_ms=0.2,
        )

    def encode(_target: D3D11RenderTarget) -> NativeEncodeSubmitResult:
        return NativeEncodeSubmitResult(
            accepted=True, gpu_submit_ms=0.2, completion_fence=ImmediateD3D11Fence(),
        )

    scheduler = D3D11PipelineScheduler(render, encode, trail=trail)
    try:
        completed, _telemetry = scheduler.run(source, 1)
        assert completed == 1
        snapshot = trail.snapshot()
        assert snapshot.full_gpu_claim_reason() == ""
        assert snapshot.zero_pixel_transfer
    finally:
        scheduler.close()
        pool.close()
        target_pool.close()


def test_pipeline_render_fence_contract_error_releases_frame_lease() -> None:
    """fence 缺 wait() 时：target 与 frame 两个 lease 都必须归还，池容量不损失。"""
    handles = iter(range(501, 505))
    pool = D3D11TexturePool(
        depth=4, width=1920, height=1080, dxgi_format=103,
        texture_factory=lambda: next(handles), texture_releaser=lambda _ptr: None,
    )
    target_pool = D3D11TexturePool(
        depth=4, width=1920, height=1080, dxgi_format=103,
        texture_factory=lambda: 0xAAA, texture_releaser=lambda _ptr: None,
    )
    frame = make_frame(0, pool)
    source = Source([frame])

    def render(_frame: D3D11GpuFrame) -> D3D11RenderResult:
        target_lease = target_pool.acquire()
        assert target_lease is not None
        return D3D11RenderResult(
            D3D11RenderTarget(
                texture_ptr=target_lease.texture_ptr, dxgi_format=103,
                width=1920, height=1080, adapter_luid="luid",
                lease=target_lease,
                completion_fence=object(),  # 缺 wait()/signalled 的违约 fence
            ),
            gpu_render_ms=0.1, gpu_yuv_ms=0.1,
        )

    def encode(_target: D3D11RenderTarget) -> NativeEncodeSubmitResult:  # pragma: no cover
        raise AssertionError("encode 不应在 render fence 失败后执行")

    scheduler = D3D11PipelineScheduler(render, encode)
    try:
        completed, telemetry = scheduler.run(source, 1)
        assert completed == 0
        assert telemetry.failed == 1
        assert telemetry.fallback_reasons[0].startswith("render_fence:")
        # 池与目标池都回到无在途状态：解码纹理槽位没有永久泄漏。
        assert pool.stats().in_use == 0
        assert target_pool.stats().in_use == 0
    finally:
        scheduler.close()
        pool.close()
        target_pool.close()


def test_d3d11_format_matrix_rejects_unsafe_hdr_alpha_and_manual_p010() -> None:
    hdr = D3D11FormatContract(
        "p010", 10, color_matrix="bt2020nc", color_transfer="smpte2084",
        color_primaries="bt2020", surface_owner="decoder",
    )
    assert validate_d3d11_format(hdr, for_encode=True, encoder_kind="amf").supported
    alpha = D3D11FormatContract("nv12", 8, has_alpha=True)
    assert validate_d3d11_format(alpha).reason == "unsupported_format:alpha_on_yuv_surface"
    manual_p010 = D3D11FormatContract("p010", 10, surface_owner="pool")
    decision = validate_d3d11_format(manual_p010, for_encode=True)
    assert not decision.supported
    assert decision.requires_native_owned_surface


def test_d3d11_adapter_policy_requires_one_luid() -> None:
    assert same_d3d11_adapter("luid", "luid") == (True, "")
    ok, reason = same_d3d11_adapter("luid", "other")
    assert not ok and reason == "adapter_mismatch:multiple_luids"


def test_effect_catalog_declares_all_planned_effects_and_explicit_fallback() -> None:
    assert {"blur", "sharpen", "glow", "erode", "dilate", "edge", "denoise", "defocus"} <= set(D3D11_EFFECTS)
    assert d3d11_effect_capability("blur", "gaussian")["supported"] is True
    result = d3d11_effect_capability("blur", "unknown")
    assert result["supported"] is False
    assert result["reason"] == "effect_variant_not_declared"


def test_vendor_ab_requires_generic_gate_and_fifteen_percent_gain() -> None:
    blocked = evaluate_vendor_ab("amd", "smartaccess_video", 100.0, 130.0, generic_d3d11_gate=False)
    assert not blocked.enabled and blocked.reason == "not_probed:generic_d3d11_gate"
    enabled = evaluate_vendor_ab("amd", "smartaccess_video", 100.0, 115.0, generic_d3d11_gate=True)
    assert enabled.enabled
    copied = evaluate_vendor_ab("nvidia", "cuda_ai", 100.0, 140.0, generic_d3d11_gate=True, has_staging_copy=True)
    assert not copied.enabled and copied.reason == "zero_copy_violation"


def test_multi_gpu_defaults_to_one_luid_and_requires_safe_partition() -> None:
    single = select_single_adapter(["a", "b"])
    assert single.selected_luid == "a"
    assert single.enabled is False
    assert select_single_adapter(
        ["a", "b"], independent_partition=True, measured_gain_ratio=0.2
    ).enabled is True
    mismatch = select_single_adapter(["a"], requested_luid="b")
    assert "adapter_mismatch" in mismatch.reason
