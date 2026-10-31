"""Regression coverage for packaged builds without an ffmpeg executable."""

from __future__ import annotations

import numpy as np
import pytest

from src.shared.media.proxy import DiskProxyConfig, ProxyFormat
from src.shared.media.proxy_generator import ProxyGenerator


def _write_short_h264_source(path) -> None:
    av = pytest.importorskip("av")
    output = av.open(str(path), "w")
    stream = output.add_stream("libx264", rate=24)
    stream.width = 160
    stream.height = 90
    stream.pix_fmt = "yuv420p"
    for value in range(6):
        frame = av.VideoFrame.from_ndarray(
            np.full((90, 160, 3), value * 20, dtype=np.uint8),
            format="rgb24",
        )
        for packet in stream.encode(frame):
            output.mux(packet)
    for packet in stream.encode():
        output.mux(packet)
    output.close()


def test_pyav_fallback_builds_intraframe_proxy_without_ffmpeg(tmp_path, monkeypatch) -> None:
    av = pytest.importorskip("av")
    try:
        av.codec.Codec("prores_ks", "w")
    except Exception:
        pytest.skip("PyAV build has no ProRes encoder")

    source_path = tmp_path / "source.mp4"
    _write_short_h264_source(source_path)
    generator = ProxyGenerator(DiskProxyConfig(
        format=ProxyFormat.PRORES_PROXY,
        target_height=72,
        cache_dir=str(tmp_path / "proxy-cache"),
    ))
    monkeypatch.setattr(generator, "_check_ffmpeg", lambda: False)

    result = generator.generate(str(source_path))

    assert result.success, result.error_message
    assert result.proxy_resolution == (128, 72)
    assert result.proxy_path
    proxy = av.open(result.proxy_path)
    try:
        assert proxy.streams.video[0].codec_context.name == "prores"
        assert (proxy.streams.video[0].width, proxy.streams.video[0].height) == (128, 72)
    finally:
        proxy.close()
