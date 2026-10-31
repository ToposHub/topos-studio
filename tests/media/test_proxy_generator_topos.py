"""TOPOS_PROXY 代理档（普通源）回归测试（2026-09-19 缓存审计补齐）。

覆盖：
- h264 源 → ``ProxyFormat.TOPOS_PROXY``：PyAV 解码 → native topos 编码，
  产物为 TPIC MOV，工厂内容嗅探自动路由回 ToposMediaSource；
- 代理键随 format 变化（换档后旧代理自动失效）；
- 取消语义：cancel 后不留半成品文件。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.shared.media.proxy import DiskProxyConfig, ProxyFormat, generate_proxy_key
from src.shared.media.proxy_generator import ProxyGenerator


def _write_short_h264_source(path: Path, frames: int = 12) -> None:
    av = pytest.importorskip("av")
    output = av.open(str(path), "w")
    stream = output.add_stream("libx264", rate=24)
    stream.width = 160
    stream.height = 90
    stream.pix_fmt = "yuv420p"
    stream.options = {"crf": "20", "g": "1"}
    for value in range(frames):
        img = np.full((90, 160, 3), (value * 20) % 255, dtype=np.uint8)
        for packet in stream.encode(
                av.VideoFrame.from_ndarray(img, format="rgb24")):
            output.mux(packet)
    for packet in stream.encode():
        output.mux(packet)
    output.close()


def _test_topos_codec_available() -> None:
    from src.shared.media.topos_source import check_topos_codec_available
    if not check_topos_codec_available():
        pytest.skip("native topos codec unavailable")


def test_topos_proxy_from_h264_source_roundtrip(tmp_path) -> None:
    av = pytest.importorskip("av")
    _test_topos_codec_available()

    source_path = tmp_path / "source.mp4"
    _write_short_h264_source(source_path)
    generator = ProxyGenerator(DiskProxyConfig(
        format=ProxyFormat.TOPOS_PROXY,
        target_height=72,
        cache_dir=str(tmp_path / "proxy-cache"),
    ))

    result = generator.generate(str(source_path))
    assert result.success, result.error_message
    assert result.proxy_resolution == (128, 72)
    assert result.proxy_path and Path(result.proxy_path).exists()

    # 产物是 TPIC：PyAV 打不开是预期；工厂 probe 走自研解码器
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import is_topos_movie, ToposMediaSource

    assert is_topos_movie(result.proxy_path)
    vm = MediaSourceFactory.probe_video_metadata_cached(
        result.proxy_path).metadata
    assert vm.codec_name == "topos"
    assert vm.pixel_format == "yuv422p10le"
    assert (int(vm.width), int(vm.height)) == (128, 72)
    assert int(vm.total_frames or 0) == 12

    # 内容 sanity：源为灰度递增序列，代理 luma 须单调上升
    src = ToposMediaSource(result.proxy_path)
    src.open()
    try:
        lumas = [
            float(np.asarray(src.read_frame(i).planes[0]).mean())
            for i in (0, 5, 11)
        ]
    finally:
        src.close()
    assert lumas[0] < lumas[1] < lumas[2], lumas


def test_topos_proxy_key_differs_from_prores(tmp_path) -> None:
    """换代理档必须换键——不同编码产物不允许互相命中。"""
    config_topos = DiskProxyConfig(
        format=ProxyFormat.TOPOS_PROXY, cache_dir=str(tmp_path / "a"))
    config_prores = DiskProxyConfig(
        format=ProxyFormat.PRORES_PROXY, cache_dir=str(tmp_path / "a"))
    assert generate_proxy_key("s", 1.0, 2, config_topos) != \
        generate_proxy_key("s", 1.0, 2, config_prores)


def test_topos_proxy_cancel_leaves_no_residue(tmp_path, monkeypatch) -> None:
    _test_topos_codec_available()

    source_path = tmp_path / "source.mp4"
    _write_short_h264_source(source_path, frames=24)
    generator = ProxyGenerator(DiskProxyConfig(
        format=ProxyFormat.TOPOS_PROXY,
        target_height=72,
        cache_dir=str(tmp_path / "proxy-cache"),
    ))

    # 编码中途取消（generate() 会重置预先置位的取消标志——与 ProRes
    # 路径同语义，故在 encode_sized 第 3 次调用时触发）
    import src.shared.codec.topos_binding as topos_binding

    real_encode_sized = topos_binding.ToposCodec.encode_sized
    real_encode_frame = topos_binding.ToposCodec.encode_frame
    calls = {"n": 0}

    def cancelling_encode(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            generator.cancel()
        if kwargs.get("target_bytes") is not None or len(args) >= 3:
            return real_encode_sized(self, *args, **kwargs)
        return real_encode_frame(self, *args, **kwargs)

    monkeypatch.setattr(
        topos_binding.ToposCodec, "encode_sized", cancelling_encode)
    monkeypatch.setattr(
        topos_binding.ToposCodec, "encode_frame", cancelling_encode)

    result = generator.generate(str(source_path))
    assert not result.success and result.cancelled, result
    # 半成品 TPIC（无 moov 的 MOV）不可播放——_fail 路径必须删除
    cache_dir = tmp_path / "proxy-cache"
    if cache_dir.exists():
        assert not list(cache_dir.glob("*.mov")), "取消后不得残留 .mov"
