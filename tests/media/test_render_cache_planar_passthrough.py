"""
T-03 后半段：渲染缓存 planar 直通产出（真实素材门）。

覆盖：
- Topos 真实素材解码契约：planar yuv422p10le（直通的前提）
- RenderCacheEncoder write_frame_planar 往返：格式/几何恒等 + 有损容差
- 平面直通 vs 旧 BGR 链路的保真度对拍（直通必须不差于旧链）

实测基线（2026-09-17，macOS Intel x86_64，1080p topos_proxy，12-24 帧）：
- 旧链 planar→BGR→packed：31.3ms/帧（31.9fps），mean abs err 14.87/1023
- 新链 planar 直入：9.4ms/帧（106.7fps，3.35x），mean abs err 1.986/1023
"""

from __future__ import annotations

import os
import tempfile
import time

import numpy as np
import pytest

from src.features.edit.core.render_cache_encoder import (
    RenderCacheEncoder,
    RenderCacheEncoderConfig,
)
from src.shared.media.topos_source import ToposMediaSource

TEST_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOPOS_PROXY_PATH = os.path.join(TEST_ROOT, "tests", "test_videos", "2K_topos_422_Proxy.mov")


def _require_real_video(path: str) -> str:
    from tests.real_media import STRICT
    if not os.path.exists(path):
        if STRICT:
            pytest.fail(
                f"real-media 作业素材缺失: {path}（TOPOS_REAL_MEDIA_STRICT=1 "
                f"禁止以 skip 缩水覆盖）")
        pytest.skip(f"real test video not available: {path}")
    return path


def _make_encoder(tmpdir: str, name: str) -> tuple[RenderCacheEncoder, str]:
    output_path = os.path.join(tmpdir, name)
    config = RenderCacheEncoderConfig(
        output_path=output_path,
        temp_path=output_path + ".tmp.mov",
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
        codec="topos_proxy",
    )
    encoder = RenderCacheEncoder(config)
    encoder.open()
    return encoder, output_path


class TestToposDecodePlanarContract:
    """直通的前提：Topos 源解码就是平面帧。"""

    def test_real_topos_decode_is_planar_422_10bit(self):
        _require_real_video(TOPOS_PROXY_PATH)
        source = ToposMediaSource(TOPOS_PROXY_PATH)
        source.open()
        try:
            frame = source.read_frame(0)
        finally:
            source.close()
        assert frame.is_planar
        assert frame.pixel_format == "yuv422p10le"
        assert frame.planes[0].dtype == np.uint16
        assert (frame.width, frame.height) == (1920, 1080)
        assert len(frame.planes) == 3


class TestPlanarPassthroughRoundTrip:
    """write_frame_planar 真实素材往返。"""

    def test_roundtrip_preserves_format_geometry_and_fidelity(self):
        _require_real_video(TOPOS_PROXY_PATH)
        source = ToposMediaSource(TOPOS_PROXY_PATH)
        source.open()
        frames = [source.read_frame(i) for i in range(4)]
        source.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            encoder, output_path = _make_encoder(tmpdir, "planar.mov")
            started = time.perf_counter()
            for frame in frames:
                # 渲染缓存 worker 的直通调用点：DecodedFrame 原样进编码器。
                encoder.write_frame_planar(frame)
            elapsed = time.perf_counter() - started
            assert encoder.close() is True
            # 挂起防护（非常量性能门）：8 帧 1080p 编码不应超过 30s。
            assert elapsed < 30.0, f"planar encode regressed: {elapsed:.1f}s"

            back = ToposMediaSource(output_path)
            back.open()
            try:
                for index, original in enumerate(frames):
                    decoded = back.read_frame(index)
                    assert decoded.is_planar
                    assert decoded.pixel_format == "yuv422p10le"
                    assert (decoded.width, decoded.height) == (1920, 1080)
                    # topos_proxy 有损档：误差有界（量化主导，无 8-bit 量化往返）。
                    for src_plane, out_plane in zip(original.planes, decoded.planes):
                        err = np.abs(
                            src_plane.astype(np.float64) - out_plane.astype(np.float64)
                        )
                        assert err.mean() < 8.0, (
                            f"plane mean error {err.mean():.2f}/1023 exceeds proxy bound"
                        )
            finally:
                back.close()

    def test_passthrough_not_worse_than_legacy_bgr_chain(self):
        """直通链平均误差必须不差于旧 BGR 链（消除 10→8→10 量化往返）。"""
        _require_real_video(TOPOS_PROXY_PATH)
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

        class _Conv:
            _plane_to_uint8 = PreviewRenderWorker._plane_to_uint8
            _convert_planar_to_bgr = PreviewRenderWorker._convert_planar_to_bgr

        source = ToposMediaSource(TOPOS_PROXY_PATH)
        source.open()
        frames = [source.read_frame(i) for i in range(4)]
        source.close()
        converter = _Conv()

        def chain_mean_error(planar_mode: bool) -> float:
            with tempfile.TemporaryDirectory() as tmpdir:
                encoder, output_path = _make_encoder(
                    tmpdir, "chain.mov"
                )
                for frame in frames:
                    if planar_mode:
                        encoder.write_frame_planar(frame)
                    else:
                        bgr = converter._convert_planar_to_bgr(frame)
                        encoder.write_frame_bgr(np.ascontiguousarray(bgr))
                encoder.close()
                back = ToposMediaSource(output_path)
                back.open()
                errors: list[float] = []
                try:
                    for index, original in enumerate(frames):
                        decoded = back.read_frame(index)
                        errors.extend(
                            float(np.abs(
                                a.astype(np.float64) - b.astype(np.float64)
                            ).mean())
                            for a, b in zip(original.planes, decoded.planes)
                        )
                finally:
                    back.close()
                return sum(errors) / len(errors)

        legacy_error = chain_mean_error(False)
        passthrough_error = chain_mean_error(True)
        assert passthrough_error <= legacy_error + 1.0, (
            f"passthrough mean error {passthrough_error:.2f} must not exceed "
            f"legacy BGR chain {legacy_error:.2f}"
        )
