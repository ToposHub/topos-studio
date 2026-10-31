"""第七轮审计 0A/0C 回归：图像序列安全与配置贯通（R15–R19）。

R15：同名图序不静默覆盖旧帧；取消只清本次暂存（旧文件哈希不变）；
     授权覆盖时成功提交并清理旧尾帧（长→短不混帧）。
R16：目录目标写到目录内部；manifest 列帧清单/哈希/首尾帧号。
R17：位深/帧号/首帧号/覆盖策略从正式作业配置贯通（16-bit 走高精度
     渲染管线——TimelineExportConfig.bit_depth → pix_fmt rgb48le）。
R18：PNG16 逐值回读正确（from_ndarray 对 be 格式自行处理端序，手动
     byteswap 会把 [30,250,100,200] 变成 ×256）。
R19：supports_audio=False——导出结果显式披露 audio_omitted。
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.export.image_sequence_encoder import (
    StandardImageSequenceEncoder,
    image_sequence_stats,
)

pytestmark = pytest.mark.edit_core


def _decoded(data):
    return SimpleNamespace(data=data)


def _frame(seed: int = 0, channels: int = 3, dtype=np.uint8):
    rng = np.random.default_rng(seed)
    return rng.integers(
        0, 256 if dtype == np.uint8 else 65536,
        size=(8, 12, channels), dtype=dtype)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_old_sequence(directory: Path, base: str, ext: str,
                        indices, digits: int = 6):
    for idx in indices:
        (directory / f"{base}_{idx:0{digits}d}{ext}").write_bytes(
            b"OLDFRAME-SENTINEL")


class TestR15OverwriteAndCancelSafety:
    def test_conflict_fails_without_overwrite_and_keeps_old_files(self, tmp_path):
        """同名旧帧存在且未授权覆盖：open 失败并列出冲突文件，旧文件不动。"""
        _write_old_sequence(tmp_path, "shot", ".png", [0, 1, 2])
        old_hashes = {p.name: _sha(p) for p in tmp_path.iterdir()}
        config = SimpleNamespace(codec="png_sequence",
                                 image_sequence_overwrite=False)
        encoder = StandardImageSequenceEncoder(str(tmp_path / "shot"), config)
        with pytest.raises(ValueError, match="already contains"):
            encoder.open()
        # 旧文件逐字节不变。
        assert {p.name: _sha(p) for p in tmp_path.iterdir()} == old_hashes

    def test_abort_never_touches_existing_old_frames(self, tmp_path):
        """旧序列存在 + 授权覆盖：中途取消只清暂存，旧帧哈希不变
        （旧实现 abort 无条件删除 _written 里的目标路径=旧文件）。"""
        _write_old_sequence(tmp_path, "shot", ".png", [0, 1, 2])
        old_hashes = {p.name: _sha(p) for p in tmp_path.iterdir()}
        config = SimpleNamespace(codec="png_sequence",
                                 image_sequence_overwrite=True)
        encoder = StandardImageSequenceEncoder(str(tmp_path / "shot"), config)
        encoder.open()
        for i in range(2):
            encoder.encode_frame(_decoded(_frame(seed=i)))
        encoder.abort()

        assert {p.name: _sha(p) for p in tmp_path.iterdir()} == old_hashes, \
            "取消后旧序列哈希必须逐文件不变"
        assert not list(tmp_path.glob(".shot.topos_stage*")), "暂存目录应清除"

    def test_overwrite_commits_and_clears_stale_tail(self, tmp_path):
        """授权覆盖：成功提交新帧；长→短重导出后旧尾帧被清理不混帧。"""
        _write_old_sequence(tmp_path, "shot", ".png", [0, 1, 2, 3, 4])
        config = SimpleNamespace(codec="png_sequence",
                                 image_sequence_overwrite=True)
        encoder = StandardImageSequenceEncoder(str(tmp_path / "shot"), config)
        encoder.open()
        for i in range(3):
            encoder.encode_frame(_decoded(_frame(seed=i)))
        encoder.close()

        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == [f"shot_{i:06d}.png" for i in range(3)], \
            f"长→短重导出不得残留旧尾帧：{names}"
        # 新帧是真实 PNG（不是哨兵字节）。
        assert (tmp_path / "shot_000000.png").read_bytes() != b"OLDFRAME-SENTINEL"

    def test_fresh_directory_exports_normally(self, tmp_path):
        """无冲突目录：默认配置直接成功（非破坏默认不影响正常交付）。"""
        config = SimpleNamespace(codec="tiff_sequence")
        encoder = StandardImageSequenceEncoder(str(tmp_path / "seq"), config)
        encoder.open()
        for i in range(2):
            encoder.encode_frame(_decoded(_frame(seed=i)))
        encoder.close()
        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == ["seq_000000.tif", "seq_000001.tif"]


class TestR16DirectoryTargetAndManifest:
    def test_directory_target_writes_inside(self, tmp_path):
        """output_path 是已存在目录：帧写进目录内部（旧实现写到父目录
        并把目录名当基名——目录保持空）。"""
        target_dir = tmp_path / "delivery"
        target_dir.mkdir()
        config = SimpleNamespace(codec="png_sequence", custom_name="shot")
        encoder = StandardImageSequenceEncoder(str(target_dir), config)
        encoder.open()
        for i in range(2):
            encoder.encode_frame(_decoded(_frame(seed=i)))
        encoder.close()
        inside = sorted(p.name for p in target_dir.iterdir())
        assert inside == ["shot_000000.png", "shot_000001.png"]
        assert list(tmp_path.glob("delivery_*.png")) == [], "不得写到父目录"

    def test_manifest_snapshot_lists_frame_inventory(self, tmp_path):
        """manifest 对目录式序列列帧清单/哈希/首尾帧号（不是 Is a
        directory 错误）。"""
        from src.features.edit.core.export_manifest import _output_snapshot

        config = SimpleNamespace(codec="png_sequence")
        encoder = StandardImageSequenceEncoder(str(tmp_path / "seq"), config)
        encoder.open()
        for i in range(3):
            encoder.encode_frame(_decoded(_frame(seed=i)))
        encoder.close()

        snap = _output_snapshot(str(tmp_path))
        assert snap["present"] and "hash_error" not in snap
        seq = snap["sequence"]
        assert seq["frame_files"] == 3
        assert seq["first_frame"] == 0 and seq["last_frame"] == 2
        assert len(seq["frames"]) == 3
        assert all("sha256" in f and f["sha256"] for f in seq["frames"])

    def test_sequence_info_reports_inventory(self, tmp_path):
        config = SimpleNamespace(codec="png_sequence",
                                 image_sequence_start_index=1001)
        encoder = StandardImageSequenceEncoder(str(tmp_path / "seq"), config)
        encoder.open()
        encoder.encode_frame(_decoded(_frame()))
        encoder.close()
        info = encoder.sequence_info()
        assert info["first_frame"] == 1001 and info["last_frame"] == 1001
        assert len(info["frame_files"]) == 1
        assert info["directory"] == str(tmp_path)


class TestR17JobConfigPlumbing:
    def test_digits_and_start_from_explicit_fields(self, tmp_path):
        """帧号 padding/首帧号来自显式作业字段（正式队列路径）。"""
        config = SimpleNamespace(
            codec="png_sequence",
            image_sequence_digits=4, image_sequence_start_index=1001)
        encoder = StandardImageSequenceEncoder(str(tmp_path / "seq"), config)
        encoder.open()
        encoder.encode_frame(_decoded(_frame()))
        encoder.close()
        assert (tmp_path / "seq_1001.png").exists()

    def test_video_config_roundtrip_carries_sequence_fields(self):
        """VideoConfig 快照往返保留序列字段（队列重开后配置不丢）。"""
        from src.features.deliver.core.video_export_job import VideoConfig

        vc = VideoConfig(
            codec="png_sequence", container="", bit_depth=16,
            image_sequence_digits=5, image_sequence_start_index=100,
            image_sequence_overwrite=True)
        restored = VideoConfig.from_dict(vc.to_dict())
        assert restored.bit_depth == 16
        assert restored.image_sequence_digits == 5
        assert restored.image_sequence_start_index == 100
        assert restored.image_sequence_overwrite is True

    def test_to_timeline_export_config_maps_sequence_fields(self):
        """VideoExportJob → TimelineExportConfig 桥接位深/帧号/覆盖。"""
        from src.features.deliver.core.video_export_job import (
            AudioConfig,
            NamingConfig,
            VideoConfig,
            VideoExportJob,
        )

        job = VideoExportJob(
            timeline_id="t", output_path="/tmp/seq",
            video_config=VideoConfig(
                codec="png_sequence", container="", bit_depth=16,
                image_sequence_digits=5, image_sequence_start_index=100,
                image_sequence_overwrite=True),
            audio_config=AudioConfig(include_audio=True),
            naming_config=NamingConfig(),
        )
        cfg = job.to_timeline_export_config()
        assert cfg.bit_depth == 16
        assert cfg.image_sequence_digits == 5
        assert cfg.image_sequence_start_index == 100
        assert cfg.image_sequence_overwrite is True

    def test_bit_depth_16_selects_high_precision_pipeline(self):
        """codec 解析：bit_depth=16 → pix_fmt rgb48le（高精度渲染管线
        的开关判据——此前正式 TIFF 永远 rgb24）。"""
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            TimelineExporter,
            TimelineExportConfig,
        )

        exporter = TimelineExporter.__new__(TimelineExporter)
        method = TimelineExporter._get_codec_settings.__get__(exporter)
        cfg16 = TimelineExportConfig(
            codec=ExportCodec.PNG_SEQUENCE, bit_depth=16)
        cfg8 = TimelineExportConfig(codec=ExportCodec.TIFF_SEQUENCE)
        assert method(cfg16)[1] == "rgb48le", \
            "16-bit 序列必须选 rgb48le 触发高精度管线"
        assert method(cfg8)[1] == "rgb24"


class TestR18Png16Endianness:
    @pytest.mark.parametrize("codec,ext", [
        ("png_sequence", ".png"), ("tiff_sequence", ".tif"),
    ])
    def test_rgba16_pixel_values_roundtrip(self, tmp_path, codec, ext):
        """RGBA16 逐值回读：[30,250,100,200] 不得 ×256（旧实现手动
        byteswap + be 格式双重交换）。"""
        import av

        config = SimpleNamespace(codec=codec)
        encoder = StandardImageSequenceEncoder(str(tmp_path / "px"), config)
        encoder.open()
        # 输入按编码器契约为 BGRA；期望输出 RGB=[30,250,100] A=200。
        encoder.encode_frame(
            _decoded(np.array([[[100, 250, 30, 200]]], dtype=np.uint16)))
        encoder.close()
        out = next(iter(tmp_path.glob(f"px_*{ext}")))
        container = av.open(str(out))
        try:
            frame = next(container.decode(video=0))
            arr = frame.to_ndarray().reshape(-1)
        finally:
            container.close()
        assert list(arr[:4]) == [30, 250, 100, 200], \
            f"{codec} 16-bit 像素值回读损坏: {list(arr[:4])}"

    def test_rgb16_ramp_roundtrip(self, tmp_path):
        """RGB16 随机值逐值回读正确（8-bit 不回归）。"""
        import av

        for dtype, scale in ((np.uint8, 256), (np.uint16, 65536)):
            sub = tmp_path / str(np.dtype(dtype))
            sub.mkdir()
            data = _frame(seed=7, dtype=dtype)
            config = SimpleNamespace(codec="png_sequence")
            encoder = StandardImageSequenceEncoder(str(sub / "r"), config)
            encoder.open()
            encoder.encode_frame(_decoded(data))
            encoder.close()
            out = next(iter(sub.glob("r_*.png")))
            container = av.open(str(out))
            try:
                frame = next(container.decode(video=0))
                arr = frame.to_ndarray()
            finally:
                container.close()
            # PNG 无损：源值（BGR→RGB 交换通道）逐值一致。
            expect = data[:, :, [2, 1, 0]]
            np.testing.assert_array_equal(arr, expect)


class TestR19AudioCapability:
    def test_supports_audio_false(self):
        config = SimpleNamespace(codec="png_sequence")
        encoder = StandardImageSequenceEncoder("/tmp/unused", config)
        assert encoder.supports_audio is False
