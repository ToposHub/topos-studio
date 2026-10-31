"""TRAW 序列源端到端测试（批 2）：CFA .toos 序列 → MediaSource 协议。

覆盖：pf=3 放行、帧/平面几何（平面 = W/2×H/2）、CFA 元数据透传、
target_size 下 CFA 恒全尺寸解码（preview 路径未承载 pf=3，批 3 接入）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.media_core

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "native/topos_codec/build/release/toos"

W, H = 64, 48


def _cfa_planes(f: int, seed_base: int) -> list[np.ndarray]:
    rng = np.random.default_rng(seed_base + f)
    pw, ph = W // 2, H // 2
    planes = []
    for i in range(4):
        p = np.clip(500 + i * 200 + f * 40
                    + rng.integers(-50, 50, (ph, pw)), 0, 4095).astype("<u2")
        planes.append(p)
    return planes


def _write_sequence(td: Path, n: int = 3) -> list[np.ndarray]:
    """编码 n 帧 TRAW 序列，返回编码前相位平面（供比对）。"""
    all_planes = []
    for f in range(n):
        planes = _cfa_planes(f, 700)
        all_planes.append(planes)
        raw = td / f"f{f}.raw"
        np.concatenate([p.reshape(-1) for p in planes]).astype("<u2").tofile(raw)
        r = subprocess.run(
            [str(CLI), "encode", str(raw), "-o", str(td / f"frame_{f:06d}.toos"),
             "--width", str(W), "--height", str(H), "--tier", "raw12-4",
             "--overwrite"],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        raw.unlink()
    return all_planes


@pytest.fixture
def traw_sequence(tmp_path):
    if not CLI.exists():
        pytest.skip("toos CLI 未构建")
    frames = _write_sequence(tmp_path)
    return tmp_path, frames


class TestTrawSequenceSource:
    def test_probe_and_frame_geometry(self, traw_sequence) -> None:
        td, _ = traw_sequence
        from src.shared.media.topos_image_sequence import (
            SequenceOptions, open_sequence)

        src = open_sequence(str(td / "frame_000000.toos"), SequenceOptions())
        meta = src.probe()
        assert meta.width == W and meta.height == H
        assert meta.pixel_format == "topos_cfa12le"
        # M3-R1：TMET 现写 N-6 展示名（'toos RAW <bd>bit <N>:1'）；无 TMET
        # 旧文件仍回退类名 'RAW'（显示层拼族名 → 'Topos Image RAW'）
        assert meta.codec_profile in ("RAW", "toos RAW 12bit 4:1")
        df = src.read_frame(1)
        assert df.width == W and df.height == H  # 帧尺寸 = 声明值（非平面尺寸）
        assert df.pixel_format == "topos_cfa12le"
        assert len(df.planes) == 4
        for p in df.planes:
            assert p.shape == (H // 2, W // 2)
        assert [pi.component for pi in df.plane_infos] == ["R", "Gr", "Gb", "B"]
        assert all(pi.bit_depth == 12 for pi in df.plane_infos)

    def test_cfa_metadata_passthrough(self, traw_sequence) -> None:
        """解码器透传契约：CFA 标记/相位序/transfer 进 extra（不解释语义）。"""
        td, _ = traw_sequence
        from src.shared.media.topos_image_sequence import (
            SequenceOptions, open_sequence)

        src = open_sequence(str(td / "frame_000000.toos"), SequenceOptions())
        df = src.read_frame(0)
        assert df.extra["topos_cfa"] is True
        assert df.extra["cfa_pattern"] == "rggb"
        assert df.extra["color_transfer"] == 20  # TC_TRANSFER_TRAW_LOG0
        assert df.extra["image_profile"] == 3

    def test_decode_matches_source_planes(self, traw_sequence) -> None:
        """同码流解码确定性：解码平面与编码输入误差有界（raw12-4 透明域）。"""
        td, frames = traw_sequence
        from src.shared.media.topos_image_sequence import (
            SequenceOptions, open_sequence)

        src = open_sequence(str(td / "frame_000000.toos"), SequenceOptions())
        df = src.read_frame(2)
        for got, ref in zip(df.planes, frames[2]):
            assert np.abs(got.astype(int) - ref.astype(int)).max() <= 24

    def test_target_size_scaled_decode_for_cfa(self, traw_sequence) -> None:
        """target_size 缩放解码（批 3 解锁 + 2026-09-13 修复钉）：
        pf=3 预览缓冲按 ceil(target/2) 分配（此前按整帧分配 → 3/4 面积
        零值残留 + 帧尺寸推错 2×）；数据非全零且与全尺寸解码同源有界。"""
        td, _ = traw_sequence
        from src.shared.media.topos_image_sequence import (
            SequenceOptions, open_sequence)

        src = open_sequence(str(td / "frame_000000.toos"),
                            SequenceOptions(target_size=(32, 24)))
        df = src.read_frame(0)
        assert df.planes[0].shape == (12, 16)  # ceil(24/2) × ceil(32/2)
        for p in df.planes:
            assert int(p.max()) > 0, "缩放平面不应全零（旧 bug：右/下 3/4 为零）"
            assert int(p.min()) >= 0
        # 帧尺寸 = 有效输出尺寸（与平面/元数据一致；此前取 native 尺寸）
        assert (df.width, df.height) == (32, 24)

    def test_full_size_decode_equivalence(self, traw_sequence) -> None:
        """缩放解码与全尺寸解码的数据同源性（中心下采样有界逼近）。"""
        td, _ = traw_sequence
        from src.shared.media.topos_image_sequence import (
            SequenceOptions, open_sequence)

        full = open_sequence(str(td / "frame_000000.toos"), SequenceOptions())
        scaled = open_sequence(str(td / "frame_000000.toos"),
                               SequenceOptions(target_size=(32, 24)))
        fp = full.read_frame(0).planes
        sp = scaled.read_frame(0).planes
        # raw12-4 透明域 + 金字塔缩放：统计中心有界偏移（非逐像素等值）
        for f, s in zip(fp, sp):
            assert abs(float(f.mean()) - float(s.mean())) <= 64.0

    def test_factory_routes_toos_sequence(self, traw_sequence) -> None:
        """MediaSourceFactory 经 'toos_sequence' 后端打开 TRAW 序列。"""
        td, _ = traw_sequence
        from src.shared.media.factory import MediaSourceFactory

        src = MediaSourceFactory.create(str(td / "frame_000000.toos"))
        try:
            meta = src.probe()
            assert meta.pixel_format == "topos_cfa12le"
        finally:
            src.close()


class TestTrawSingleImageSource:
    """单帧 .toos CFA 源（ToposImageSource）——2026-09-13 修复钉：
    pf=3 的 4 平面此前只建 3 个 bit_depths → plane_infos[3] 越界崩溃。"""

    def test_read_frame_cfa(self, traw_sequence) -> None:
        td, frames = traw_sequence
        from src.shared.media.topos_image_source import ToposImageSource

        src = ToposImageSource(str(td / "frame_000000.toos"))
        src.open()
        meta = src.probe()
        assert meta.pixel_format == "topos_cfa12le"
        df = src.read_frame(0)
        assert (df.width, df.height) == (W, H)
        assert len(df.planes) == 4
        assert [pi.component for pi in df.plane_infos] == ["R", "Gr", "Gb", "B"]
        assert all(pi.bit_depth == 12 for pi in df.plane_infos)
        for got, ref in zip(df.planes, frames[0]):
            assert np.abs(got.astype(int) - ref.astype(int)).max() <= 24
        src.close()
