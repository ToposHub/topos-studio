"""F6.4 · 通用图像序列输出——已验证帧 manifest 验收。

验收锚点：暂停/取消/缺帧/损坏帧/断电恢复不跳过坏文件、不混不同配置
（恢复依据内容指纹 manifest，不只看文件名存在）；外部回导帧数/帧号
一致（Topos 自有序列兼容——.toos 路径零改动）。
"""
from __future__ import annotations

import os


import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.export.sequence_exporter import (
    ImageSequenceExporter,
    VerifiedFrameManifest,
)


def _exporter(tmp_path, **kw):
    return ImageSequenceExporter(output_dir=str(tmp_path), format="png",
                                 **kw)


def _fake_frame(size: int = 8, seed: int = 0):
    from types import SimpleNamespace

    data = ((np.arange(size * size * 4, dtype=np.uint8) + seed)
            .reshape(size, size, 4))
    return SimpleNamespace(data=data, is_packed=True)


class TestVerifiedFrameManifest:
    def test_record_and_verify_roundtrip(self, tmp_path):
        f = tmp_path / "f0.png"
        f.write_bytes(b"hello")
        m = VerifiedFrameManifest()
        m.record(str(f))
        assert m.is_verified(str(f))
        # 保存/加载往返
        mfile = tmp_path / "manifest.json"
        assert m.save(str(mfile))
        loaded = VerifiedFrameManifest.load(str(mfile))
        assert loaded.is_verified(str(f))
        assert len(loaded) == 1

    def test_corrupted_file_not_verified(self, tmp_path):
        """损坏帧不被"文件名存在"骗过——内容变了即失效。"""
        f = tmp_path / "f0.png"
        f.write_bytes(b"hello")
        m = VerifiedFrameManifest()
        m.record(str(f))
        f.write_bytes(b"hellO")  # 篡改 1 字节
        assert m.is_verified(str(f)) is False

    def test_truncated_file_not_verified(self, tmp_path):
        f = tmp_path / "f0.png"
        f.write_bytes(b"0123456789")
        m = VerifiedFrameManifest()
        m.record(str(f))
        f.write_bytes(b"012")  # 断电截断
        assert m.is_verified(str(f)) is False

    def test_missing_manifest_exports_all(self, tmp_path):
        assert len(VerifiedFrameManifest.load(
            str(tmp_path / "none.json"))) == 0

    def test_corrupt_manifest_exports_all(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert len(VerifiedFrameManifest.load(str(bad))) == 0


class TestExporterResume:
    def test_resume_skips_only_verified_frames(self, tmp_path):
        """恢复：完好帧跳过、损坏帧重导（不跳过坏文件）。"""
        exp = _exporter(tmp_path)
        p0 = exp.export_frame(_fake_frame(seed=1), 0)
        p1 = exp.export_frame(_fake_frame(seed=2), 1)
        mfile = tmp_path / "manifest.json"
        exp._frame_manifest.save(str(mfile))

        # 篡改 frame 1（模拟断电损坏）
        Path(p1).write_bytes(b"corrupt")

        # 恢复会话：加载 manifest 的 exporter
        exp2 = _exporter(tmp_path)
        exp2._frame_manifest = VerifiedFrameManifest.load(str(mfile))
        out0 = exp2.export_frame(_fake_frame(seed=1), 0)  # 已验证 → 跳过
        out1 = exp2.export_frame(_fake_frame(seed=2), 1)  # 损坏 → 重导
        assert out0 == p0
        assert out1 == p1
        assert Path(p1).read_bytes() != b"corrupt"  # 坏文件被替换

    def test_different_config_residual_reexported(self, tmp_path):
        """异配置残留文件（大小不同）不被旧文件名骗过。"""
        exp = _exporter(tmp_path)
        residual = Path(exp.generate_filename(0))
        residual.write_bytes(b"tiny-stale-file")  # 旧配置残留
        out = exp.export_frame(_fake_frame(seed=5), 0)
        assert Path(out).stat().st_size != len(b"tiny-stale-file")

    def test_manifest_records_grow_with_exports(self, tmp_path):
        exp = _exporter(tmp_path)
        for i in range(3):
            exp.export_frame(_fake_frame(seed=i), i)
        assert len(exp._frame_manifest) == 3
        assert exp._frame_manifest.is_verified(
            str(tmp_path / exp.generate_filename(2)))


# ================================================================
# M17 · 主机侧 UYVY 打包（监看候选路径）
# ================================================================

class TestBgraToUyvy:
    def test_roundtrip_against_decoder(self):
        """打包→解码往返：像素级还原（Y/UV 在容忍带内）。"""
        import numpy as np
        from src.shared.video.monitor_output import bgra_frame_to_uyvy
        try:
            import cv2
        except ImportError:
            pytest.skip("cv2 unavailable")
        # 常量色（4:2:2 对常量内容无损——色度无空间变化）
        frame = np.full((8, 6, 4), 0, dtype=np.uint8)
        frame[..., 0] = 200  # B
        frame[..., 1] = 120  # G
        frame[..., 2] = 80   # R
        frame[..., 3] = 255
        packed = bgra_frame_to_uyvy(
            frame.tobytes(), width=6, height=8)
        # UYVY 字节数 = w*h*2
        assert len(packed) == 6 * 8 * 2
        arr = np.frombuffer(packed, dtype=np.uint8).reshape(8, 6, 2)
        back = cv2.cvtColor(arr, cv2.COLOR_YUV2BGRA_Y422)
        # 色度下采样往返误差容忍（4:2:2 有损）
        assert np.abs(back[:, :, :3].astype(int)
                      - frame[:, :, :3].astype(int)).max() <= 48

    def test_odd_width_padded_to_macro_pixel(self):
        import numpy as np
        from src.shared.video.monitor_output import bgra_frame_to_uyvy
        packed = bgra_frame_to_uyvy(bytes(5 * 4 * 4), width=5, height=4)
        assert len(packed) == 4 * 6 * 2  # 5→6 对齐

    def test_failure_returns_original(self):
        from src.shared.video.monitor_output import bgra_frame_to_uyvy
        original = b"\x01\x02\x03\x04"
        out = bgra_frame_to_uyvy(original, width=0, height=0)
        assert out == original


class TestSequenceManifestResume:
    def test_manifest_survives_session_restart(self, tmp_path):
        """M10：manifest 逐帧落盘——新 exporter 实例（重启等价）装载
        既有 manifest，已验证帧按指纹跳过。"""
        import numpy as np
        from src.shared.export.sequence_exporter import ImageSequenceExporter
        from types import SimpleNamespace as NS

        exporter = ImageSequenceExporter(
            output_dir=str(tmp_path), format="png", overwrite=False)
        frame = NS(to_ndarray=lambda: np.zeros(
            (8, 8, 4), np.uint8), index=0)
        # 用真实 API：export_frame
        exp = exporter
        # 构造 DecodedFrame 替身走 processed_data 路径
        out1 = exp.export_frame(
            frame, 0, processed_data=np.zeros((8, 8, 4), np.uint8))
        assert os.path.isfile(out1)
        assert os.path.isfile(os.path.join(
            str(tmp_path), "frame_manifest.json"))

        # "重启"：新实例同目录
        exp2 = ImageSequenceExporter(
            output_dir=str(tmp_path), format="png", overwrite=False)
        out2 = exp2.export_frame(
            frame, 0, processed_data=np.full((8, 8, 4), 255, np.uint8))
        assert out2 == out1  # 已验证帧跳过（文件未重写）
        # 指纹验证：跳过的帧保持第一会话内容（全 0）
        from PIL import Image
        img = np.asarray(Image.open(out1))
        assert img.max() == 0
