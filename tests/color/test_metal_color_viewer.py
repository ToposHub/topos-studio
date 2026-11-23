"""4.4/4.5 Color Viewer Metal 呈现端：开关/EDR 决策/适配矩形（逻辑层），
以及真实 Cocoa 窗口 + Metal 设备上的端到端呈现（cocoa 门控）。
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.color.ui.metal_color_viewer import (
    MTL_PIXEL_FORMAT_BGRA8_UNORM,
    MTL_PIXEL_FORMAT_RGBA16_FLOAT,
    ColorMetalViewportHost,
    ColorMetalViewerPresenter,
    color_metal_viewer_enabled,
    decide_edr_layer,
    fit_display_rect,
)


# ── 开关（默认关闭；显式开启才生效） ─────────────────────────────────────

class TestOptInSwitch:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("TOPOS_COLOR_METAL", raising=False)
        monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
        enabled, reason = color_metal_viewer_enabled()
        assert enabled is False
        assert reason == "opt_in_missing"

    def test_opt_in_enables(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
        monkeypatch.delenv("TOPOS_METAL_DISABLE", raising=False)
        assert color_metal_viewer_enabled() == (True, "")

    def test_unified_kill_switch_wins(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
        monkeypatch.setenv("TOPOS_METAL_DISABLE", "color_graph_metal")
        enabled, reason = color_metal_viewer_enabled()
        assert enabled is False
        assert reason == "disabled_by_switch:color_graph_metal"

    def test_unrelated_kill_switch_does_not_affect(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
        monkeypatch.setenv("TOPOS_METAL_DISABLE", "native_viewport")
        assert color_metal_viewer_enabled() == (True, "")


# ── EDR 决策（随屏幕变更重新配置的判定逻辑） ────────────────────────────

class TestEdrDecision:
    def test_no_edr_capability_falls_back_to_sdr(self):
        decision = decide_edr_layer(1.0, hdr_desired=True)
        assert decision.pixel_format == MTL_PIXEL_FORMAT_BGRA8_UNORM
        assert not decision.wants_edr
        assert decision.colorspace == "srgb"
        assert not decision.is_float

    def test_edr_capable_screen_gets_float_layer(self):
        decision = decide_edr_layer(4.0, hdr_desired=True)
        assert decision.pixel_format == MTL_PIXEL_FORMAT_RGBA16_FLOAT
        assert decision.wants_edr
        assert decision.max_edr == 4.0
        assert decision.colorspace == "extended_linear_srgb"
        assert decision.is_float

    def test_sdr_content_never_asks_for_edr(self):
        decision = decide_edr_layer(16.0, hdr_desired=False)
        assert not decision.wants_edr
        assert decision.pixel_format == MTL_PIXEL_FORMAT_BGRA8_UNORM


# ── 适配矩形（与 GL 视口同语义：等比 contain + zoom/pan） ───────────────

class TestFitDisplayRect:
    def test_contain_16x9_in_16x10(self):
        x, y, w, h = fit_display_rect((1920, 1080), (1600, 1000))
        assert w == pytest.approx(1600.0)
        assert h == pytest.approx(900.0)
        assert y == pytest.approx(50.0)
        assert x == pytest.approx(0.0)

    def test_zoom_scales_around_center(self):
        x, y, w, h = fit_display_rect((1000, 1000), (1000, 1000), zoom=2.0)
        assert w == pytest.approx(2000.0)
        assert x == pytest.approx(-500.0)

    def test_pan_shifts_by_fitted_size(self):
        x, y, w, h = fit_display_rect((1000, 500), (1000, 1000),
                                      pan=(0.5, -0.25))
        # 适配后 1000x500，居中 y=250；pan 单位 = 适配图幅
        assert x == pytest.approx(500.0)
        assert y == pytest.approx(250.0 - 125.0)

    def test_degenerate_image_fills_surface(self):
        assert fit_display_rect((0, 0), (320, 240)) == (0.0, 0.0, 320.0, 240.0)


# ── 宿主（offscreen：attach 拒绝且可见诊断；cocoa：真实层） ─────────────

class TestHost:
    def test_offscreen_refuses_with_reason(self, qapp):
        if qapp.platformName() != "offscreen":
            pytest.skip("本用例验证 offscreen 拒绝路径")
        host = ColorMetalViewportHost()
        host.resize(320, 180)
        host.show()
        try:
            assert host.attach() is False
            assert host.failure.startswith("platform_unsupported")
            assert host.configure_display(hdr_desired=True) is None
        finally:
            host.detach()
            host.close()

    def test_edr_capability_probe_never_raises_offscreen(self, qapp):
        host = ColorMetalViewportHost()
        # 非 cocoa 探测按「无 EDR」处理（不冒充、不抛出）
        assert host.screen_edr_capability() == 1.0


# ── 端到端（cocoa + Metal 真机）：发布 → present port → CAMetalLayer ────

def _end_to_end_enabled() -> bool:
    import sys

    if sys.platform != "darwin":
        return False
    from PyQt6.QtGui import QGuiApplication

    if QGuiApplication.platformName() != "cocoa":
        return False
    from src.shared.gpu.metal.context import MetalComputeContext

    return MetalComputeContext.is_supported()


@pytest.mark.skipif(not _end_to_end_enabled(),
                    reason="需要 cocoa 平台 + Metal 设备（NOT RUN）")
class TestPresenterEndToEnd:
    def test_publish_present_and_ack(self, qapp):
        """合成帧经发布者/呈现端口送到真实 CAMetalLayer 并归还槽位。"""
        if qapp.platformName() != "cocoa":
            # The host refuses every other platform by design.
            pytest.skip("a CAMetalLayer needs the cocoa Qt platform")
        import numpy as np
        from PyQt6.QtCore import QByteArray

        from src.features.color.core.frame_color_description import (
            ColorFrameDescription,
            DisplayOutputDescription,
        )
        from src.features.color.core.metal_color_io import MetalColorIO
        from src.features.color.core.metal_frame_publish import (
            SUBMITTED,
            ColorFrameMetalPublisher,
        )
        from src.shared.gpu.metal.context import MetalComputeContext

        ctx = MetalComputeContext()
        io = MetalColorIO(ctx)
        publisher = ColorFrameMetalPublisher(ctx, pool_size=2, max_in_flight=1)

        host = ColorMetalViewportHost()
        host.resize(320, 180)
        host.show()
        qapp.processEvents()
        try:
            assert host.attach(device=ctx.device) is True, host.failure
            decision = host.configure_display(hdr_desired=False)
            assert decision is not None
            presenter = ColorMetalViewerPresenter(ctx, host)
            assert presenter.attach_layer() is True
            assert presenter is not None

            size = (64, 48)
            desc = ColorFrameDescription("rgba16f", *size, bit_depth=16)
            source = np.full((size[1], size[0], 4),
                             (0.25, 0.5, 0.75, 1.0), np.float32)
            working = io.decode_to_working(io.upload_rgba16f(source), desc)

            released = []
            outcome = publisher.render_frame(
                size,
                lambda slot: io.encode_for_display(
                    working, DisplayOutputDescription("sdr_passthrough"),
                    target=slot),
                frame=7, generation_id=1,
                color_info=desc.present_color_info("sdr"),
                working_release=None)
            assert outcome == SUBMITTED

            published = []
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not published:
                published = publisher.poll()
                presenter.poll()
                time.sleep(0.005)
            assert len(published) == 1
            frame = published[0]
            assert frame.present.is_native
            assert frame.present.native.color.color_transfer == "bt709"

            presented = []
            acked = []
            assert presenter.submit(
                frame, image_size=size,
                on_presented=lambda f: presented.append(f),
                on_released=lambda f, r: acked.append((f, r))) is True
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not presented:
                publisher.poll()
                presenter.poll()
                time.sleep(0.005)
                qapp.processEvents()
            assert presented, "帧未到达屏幕（present port 未呈现）"
            # 在屏帧按端口语义保持到被替换/shutdown；shutdown 触发释放
            outcomes = presenter.shutdown()
            publisher.poll()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not acked:
                publisher.poll()
                time.sleep(0.005)
            assert acked, "shutdown 后帧租约未归还"
            publisher.ack(acked[0][0], acked[0][1])
            slots = publisher.stats_snapshot().get("slots", {})
            assert slots.get("reuse_violations", 0) == 0
            assert slots.get("stale_acks", 0) == 0
            return
        finally:
            if "presenter" in locals():
                presenter.shutdown()
            publisher.shutdown()
            host.detach()
            host.close()
