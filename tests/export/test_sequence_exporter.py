"""P5-T6：sequence_exporter 专测。

ImageSequenceExporter / VerifiedFrameManifest / 配置枚举与工厂的
公共 API 覆盖。不依赖真实媒体与可选后端（OpenEXR/tifffile/imageio
本环境未安装，能力探测路径用 monkeypatch 固定），全部写盘落
tmp_path：

- 构造 / 默认色彩策略 / 扩展名 / 命名（含坏模式回退）
- VerifiedFrameManifest：record / is_verified / save / load / 损坏
- export_frame：PNG 8-bit、PNG 16-bit（内置编码器）、TIFF 8-bit、
  JPG；overwrite=False 的 F6.4 指纹跳过与"坏文件重导"
- _extract_frame_data：packed uint8/uint16 归一化、平面 YUV→RGBA、
  双平面灰度、无数据兜底
- _apply_color_policy：sRGB / scene-linear / alpha 不动
- 统计 / cleanup / create_sequence_exporter 用例预设
- 失败路径：后端缺失显式报错（包装为 IOError）、底层异常包装
"""

from __future__ import annotations

import os
import struct
import zlib
from types import SimpleNamespace

import numpy as np
import pytest

from src.shared.export import sequence_exporter as seq_mod
from src.shared.export.sequence_exporter import (
    BitDepthMode,
    ColorPolicy,
    ImageFormat,
    ImageSequenceExporter,
    SequenceExportConfig,
    VerifiedFrameManifest,
    create_sequence_exporter,
)


# ── 测试替身 ───────────────────────────────────────────────────────────────

def make_packed_frame(data: np.ndarray, index: int = 0) -> SimpleNamespace:
    """DecodedFrame 的鸭子类型替身（打包格式）。"""
    return SimpleNamespace(
        frame_index=index, data=data, is_packed=True, is_planar=False,
        planes=(), height=data.shape[0], width=data.shape[1], bit_depth=8)


def make_planar_frame(planes: tuple, h: int, w: int,
                      bit_depth: int = 8, index: int = 0) -> SimpleNamespace:
    """DecodedFrame 的鸭子类型替身（平面 YUV/YUVA）。"""
    return SimpleNamespace(
        frame_index=index, data=None, is_packed=False, is_planar=True,
        planes=planes, height=h, width=w, bit_depth=bit_depth)


def solid_rgba(value: float, h: int = 4, w: int = 6) -> np.ndarray:
    data = np.full((h, w, 4), value, dtype=np.float32)
    data[..., 3] = 1.0
    return data


def parse_png(path: str) -> dict:
    """解析 PNG（签名/IHDR/IDAT），供 16-bit 内置编码器验证。"""
    raw = open(path, "rb").read()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, pos, idat = {}, 8, b""
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        ctype = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            chunks["ihdr"] = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        pos += 12 + length
    chunks["idat"] = zlib.decompress(idat)
    return chunks


# ── 配置与命名 ─────────────────────────────────────────────────────────────

class TestConfigAndNaming:
    def test_config_defaults(self) -> None:
        cfg = SequenceExportConfig(output_dir="/tmp/x")
        assert cfg.format is ImageFormat.EXR
        assert cfg.naming == "frame_{:04d}"
        assert cfg.bit_depth is BitDepthMode.BIT16
        assert cfg.color_policy is ColorPolicy.SCENE_LINEAR
        assert cfg.overwrite is True
        assert cfg.start_frame == 0
        assert cfg.metadata == {}

    def test_constructor_creates_output_dir(self, tmp_path) -> None:
        target = tmp_path / "deep" / "nested" / "out"
        ImageSequenceExporter(str(target), format="png")
        assert target.is_dir()

    @pytest.mark.parametrize("fmt,policy", [
        ("exr", "scene_linear"),
        ("tiff", "scene_linear"),   # 16-bit 默认
        ("png", "srgb"),
        ("jpg", "srgb"),
        ("dpx", "log"),
        ("unknownfmt", "srgb"),     # 未知格式回退 sRGB
    ])
    def test_default_color_policy(self, tmp_path, fmt, policy) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format=fmt, bit_depth=16)
        assert ex.get_color_policy() == policy

    def test_tiff8bit_defaults_to_srgb(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="tiff", bit_depth=8)
        assert ex.get_color_policy() == "srgb"

    def test_color_policy_override(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png",
                                   color_policy="rec709")
        assert ex.get_color_policy() == "rec709"

    def test_invalid_color_policy_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            ImageSequenceExporter(str(tmp_path), format="png",
                                  color_policy="nope")

    @pytest.mark.parametrize("fmt,ext", [
        ("exr", ".exr"), ("tiff", ".tiff"), ("tif", ".tif"),
        ("png", ".png"), ("jpg", ".jpg"), ("jpeg", ".jpg"),
        ("dpx", ".dpx"), ("weird", ".weird"),
    ])
    def test_get_extension(self, tmp_path, fmt, ext) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format=fmt)
        assert ex.get_extension() == ext

    def test_generate_filename_custom_pattern(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png",
                                   naming="shot_{:05d}")
        assert ex.generate_filename(7) == "shot_00007.png"

    def test_generate_filename_fallback_on_bad_pattern(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png",
                                   naming="shot_{bad")
        assert ex.generate_filename(3) == "frame_0003.png"


# ── VerifiedFrameManifest ─────────────────────────────────────────────────

class TestVerifiedFrameManifest:
    def test_record_then_verified(self, tmp_path) -> None:
        f = tmp_path / "frame.png"
        f.write_bytes(b"data-v1")
        m = VerifiedFrameManifest()
        assert m.is_verified(str(f)) is False  # 未记录
        m.record(str(f))
        assert len(m) == 1
        assert m.is_verified(str(f)) is True

    def test_modified_file_fails_verification(self, tmp_path) -> None:
        f = tmp_path / "frame.png"
        f.write_bytes(b"data-v1")
        m = VerifiedFrameManifest()
        m.record(str(f))
        f.write_bytes(b"data-v2-tampered")
        assert m.is_verified(str(f)) is False

    def test_missing_file_fails_verification(self, tmp_path) -> None:
        f = tmp_path / "frame.png"
        f.write_bytes(b"data")
        m = VerifiedFrameManifest()
        m.record(str(f))
        f.unlink()
        assert m.is_verified(str(f)) is False

    def test_record_missing_file_is_ignored(self, tmp_path) -> None:
        m = VerifiedFrameManifest()
        m.record(str(tmp_path / "ghost.png"))  # 不应抛
        assert len(m) == 0

    def test_save_load_roundtrip(self, tmp_path) -> None:
        f = tmp_path / "frame.png"
        f.write_bytes(b"payload")
        m = VerifiedFrameManifest()
        m.record(str(f))
        path = tmp_path / "manifest.json"
        assert m.save(str(path)) is True

        loaded = VerifiedFrameManifest.load(str(path))
        assert len(loaded) == 1
        assert loaded.is_verified(str(f)) is True

    def test_load_missing_returns_empty(self, tmp_path) -> None:
        m = VerifiedFrameManifest.load(str(tmp_path / "absent.json"))
        assert len(m) == 0

    def test_load_corrupt_returns_empty(self, tmp_path) -> None:
        p = tmp_path / "manifest.json"
        p.write_text("{{{ not json", encoding="utf-8")
        assert len(VerifiedFrameManifest.load(str(p))) == 0

    def test_load_wrong_schema_ignored(self, tmp_path) -> None:
        import json
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps({"schema": "other/9", "frames": {}}),
                     encoding="utf-8")
        assert len(VerifiedFrameManifest.load(str(p))) == 0

    def test_save_to_unwritable_path_returns_false(self, tmp_path) -> None:
        m = VerifiedFrameManifest()
        assert m.save(str(tmp_path / "no" / "such" / "dir.json")) is False


# ── export_frame 实际写盘路径 ──────────────────────────────────────────────

class TestExportFrameFormats:
    def test_png8_roundtrip_pixels(self, tmp_path) -> None:
        from PIL import Image
        ex = ImageSequenceExporter(str(tmp_path), format="png", bit_depth=8,
                                   color_policy="scene_linear")
        data = solid_rgba(0.0)
        data[0, 0] = (1.0, 1.0, 1.0, 1.0)  # 精确可量化的角点
        path = ex.export_frame(None, 0, processed_data=data)

        assert os.path.exists(path)
        assert path.endswith("frame_0000.png")
        img = Image.open(path)
        assert img.mode == "RGBA"
        assert img.getpixel((0, 0)) == (255, 255, 255, 255)
        assert img.getpixel((1, 1)) == (0, 0, 0, 255)
        assert ex.get_exported_files() == [path]

    def test_png16_builtin_encoder_header_and_pixels(self, tmp_path) -> None:
        """AUD-33322：16-bit RGBA PNG 走内置编码器（bit depth 16 / RGBA）。"""
        ex = ImageSequenceExporter(str(tmp_path), format="png", bit_depth=16,
                                   color_policy="scene_linear")
        data = solid_rgba(0.0)
        data[0, 0] = (1.0, 1.0, 1.0, 1.0)
        path = ex.export_frame(None, 0, processed_data=data)

        png = parse_png(path)
        w, h, depth, ctype, comp, filt, inter = png["ihdr"]
        assert (w, h, depth, ctype) == (data.shape[1], data.shape[0], 16, 6)
        # 大端 16-bit 样本 + 行首 filter 字节 0；全 1.0 像素 → 0xFFFF
        row0 = png["idat"][1:1 + 8]
        assert row0 == b"\xff" * 8
        # 尾行 = filter 0 + 6 个黑像素（RGB 0 / A 0xFFFF）
        last_row = png["idat"][-(1 + w * 8):]
        assert last_row == b"\x00" + (b"\x00" * 6 + b"\xff\xff") * w

    def test_write_png16_rejects_bad_input(self, tmp_path) -> None:
        out = str(tmp_path / "x.png")
        with pytest.raises(ValueError, match="uint16"):
            ImageSequenceExporter._write_png16_rgba(
                out, np.zeros((2, 2, 4), dtype=np.float32), 6)
        with pytest.raises(ValueError, match="shape"):
            ImageSequenceExporter._write_png16_rgba(
                out, np.zeros((2, 2, 3), dtype=np.uint16), 6)

    def test_tiff8_roundtrip(self, tmp_path) -> None:
        from PIL import Image
        ex = ImageSequenceExporter(str(tmp_path), format="tiff",
                                   bit_depth=8, color_policy="scene_linear")
        path = ex.export_frame(None, 0, processed_data=solid_rgba(0.5))
        img = Image.open(path)
        assert img.size == (6, 4)

    def test_jpg_roundtrip_rgb(self, tmp_path) -> None:
        from PIL import Image
        ex = ImageSequenceExporter(str(tmp_path), format="jpg",
                                   bit_depth=8, color_policy="scene_linear",
                                   compression="90")
        path = ex.export_frame(None, 0, processed_data=solid_rgba(0.5))
        img = Image.open(path)
        assert img.mode == "RGB"  # JPG 无 alpha
        assert img.size == (6, 4)

    def test_exr_without_backend_raises_ioerror(self, tmp_path, monkeypatch) -> None:
        """AUD-33323：后端缺失显式报"不支持"（export_frame 包为 IOError）。"""
        monkeypatch.setattr(seq_mod, "_optional_module_available",
                            lambda name: False)
        ex = ImageSequenceExporter(str(tmp_path), format="exr")
        with pytest.raises(IOError, match="EXR"):
            ex.export_frame(None, 0, processed_data=solid_rgba(0.5))
        assert ex.get_exported_files() == []  # 失败不登记

    def test_encoder_crash_wrapped_as_ioerror(self, tmp_path, monkeypatch) -> None:
        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        ex = ImageSequenceExporter(str(tmp_path), format="png", bit_depth=8)
        monkeypatch.setattr(ex, "_export_png", _boom)
        with pytest.raises(IOError, match="boom"):
            ex.export_frame(None, 0, processed_data=solid_rgba(0.5))
        assert ex.get_exported_files() == []


class TestOverwriteAndResume:
    def test_overwrite_false_skips_verified_frame(self, tmp_path) -> None:
        first = ImageSequenceExporter(str(tmp_path), format="png",
                                      bit_depth=8, overwrite=True)
        path = first.export_frame(None, 0, processed_data=solid_rgba(0.5))
        original = open(path, "rb").read()

        second = ImageSequenceExporter(str(tmp_path), format="png",
                                       bit_depth=8, overwrite=False)
        returned = second.export_frame(None, 0,
                                       processed_data=solid_rgba(0.25))
        assert returned == path
        assert open(path, "rb").read() == original  # 未重写
        assert second.get_exported_files() == []    # 跳过不登记

    def test_overwrite_false_reexports_tampered_frame(self, tmp_path) -> None:
        """F6.4：文件存在但指纹不匹配 → 重导（不跳过坏文件）。"""
        first = ImageSequenceExporter(str(tmp_path), format="png",
                                      bit_depth=8, overwrite=True)
        path = first.export_frame(None, 0, processed_data=solid_rgba(0.5))
        with open(path, "ab") as fh:  # 截断/篡改 → 指纹失配
            fh.write(b"junk")

        second = ImageSequenceExporter(str(tmp_path), format="png",
                                       bit_depth=8, overwrite=False)
        second.export_frame(None, 0, processed_data=solid_rgba(0.5))
        assert second.get_exported_files() == [path]  # 触发了重导

    def test_overwrite_true_reexports_verified_frame(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png", bit_depth=8,
                                   overwrite=True)
        data = solid_rgba(0.5)
        ex.export_frame(None, 0, processed_data=data)
        ex.export_frame(None, 0, processed_data=data)  # overwrite=True 不跳过
        assert len(ex.get_exported_files()) == 2


# ── 帧数据提取与色彩策略 ───────────────────────────────────────────────────

class TestExtractFrameData:
    def test_packed_uint8_normalized_to_float(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png")
        raw = np.full((2, 2, 3), 255, dtype=np.uint8)
        out = ex._extract_frame_data(make_packed_frame(raw))
        assert out.dtype == np.float32
        assert out.shape == (2, 2, 4)  # 自动补 alpha
        assert out[..., :3] == pytest.approx(1.0)
        assert out[..., 3] == pytest.approx(1.0)

    def test_packed_uint16_normalized(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png")
        raw = np.zeros((2, 2, 4), dtype=np.uint16)
        raw[..., :3] = 65535
        raw[..., 3] = 32768
        out = ex._extract_frame_data(make_packed_frame(raw))
        assert out[..., 0] == pytest.approx(1.0)
        assert out[..., 3] == pytest.approx(32768 / 65535, abs=1e-6)

    def test_planar_yuv_bt709_neutral_gray(self, tmp_path) -> None:
        """128/128/128 平面经 BT.709 系数转换（128/255 ≠ 0.5，按公式断言）。"""
        ex = ImageSequenceExporter(str(tmp_path), format="png")
        h = w = 2
        planes = (np.full((h, w), 128, dtype=np.uint8),
                  np.full((h, w), 128, dtype=np.uint8),
                  np.full((h, w), 128, dtype=np.uint8))
        out = ex._extract_frame_data(make_planar_frame(planes, h, w))
        y = 128 / 255
        u = 128 / 255 - 0.5
        v = 128 / 255 - 0.5
        expected = (y + 1.5748 * v,
                    y - 0.1873 * u - 0.4681 * v,
                    y + 1.8556 * u)
        assert out[..., 0] == pytest.approx(expected[0], abs=1e-6)
        assert out[..., 1] == pytest.approx(expected[1], abs=1e-6)
        assert out[..., 2] == pytest.approx(expected[2], abs=1e-6)
        assert out[..., 3] == pytest.approx(1.0)  # 无 alpha 平面 → 1.0

    def test_planar_two_planes_treated_as_grayscale(self, tmp_path) -> None:
        """AUD-33321：<3 平面无色度 → 中性色度灰度转换（不 UnboundLocalError）。"""
        ex = ImageSequenceExporter(str(tmp_path), format="png")
        h = w = 2
        planes = (np.full((h, w), 255, dtype=np.uint8),
                  np.full((h, w), 255, dtype=np.uint8))
        out = ex._extract_frame_data(make_planar_frame(planes, h, w))
        assert out[..., 0] == pytest.approx(1.0, abs=1e-6)  # R=G=B=Y
        assert out[..., 3] == pytest.approx(1.0)

    def test_planar_with_alpha_plane(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png")
        h = w = 2
        planes = (np.full((h, w), 128, dtype=np.uint8),
                  np.full((h, w), 128, dtype=np.uint8),
                  np.full((h, w), 128, dtype=np.uint8),
                  np.zeros((h, w), dtype=np.uint8))  # alpha=0
        out = ex._extract_frame_data(make_planar_frame(planes, h, w))
        assert out[..., 3] == pytest.approx(0.0, abs=1e-6)

    def test_no_valid_data_returns_zeros(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png")
        frame = SimpleNamespace(frame_index=0, data=None, is_packed=False,
                                is_planar=False, planes=(), height=3,
                                width=5, bit_depth=8)
        out = ex._extract_frame_data(frame)
        assert out.shape == (3, 5, 4)
        assert not out.any()


class TestColorPolicyApplication:
    def test_scene_linear_passthrough(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png",
                                   color_policy="scene_linear")
        data = solid_rgba(0.25)
        out = ex._apply_color_policy(data)
        np.testing.assert_array_equal(out, data)

    def test_srgb_endpoints_and_alpha_untouched(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png",
                                   color_policy="srgb")
        data = solid_rgba(0.25)
        data[0, 0, :3] = 1.0
        data[0, 0, 3] = 0.5  # alpha 不参与 gamma
        data[1, 1, :3] = 0.0
        out = ex._apply_color_policy(data)
        assert out[0, 0, 0] == pytest.approx(1.055 * 1.0 ** (1 / 2.4) - 0.055)
        assert out[1, 1, 0] == pytest.approx(0.0)  # 12.92 × 0
        assert out[0, 0, 3] == pytest.approx(0.5)  # alpha 原样
        assert out[2, 2, 0] > data[2, 2, 0]        # 中间调提亮


# ── 统计 / 清理 / 工厂 ─────────────────────────────────────────────────────

class TestStatsCleanupFactory:
    def test_stats_empty(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png")
        stats = ex.get_export_stats()
        assert stats["count"] == 0
        assert stats["total_bytes"] == 0
        assert stats["format"] == "png"
        assert "output_dir" not in stats

    def test_stats_after_exports(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png",
                                   bit_depth=8, color_policy="scene_linear")
        p0 = ex.export_frame(None, 0, processed_data=solid_rgba(0.5))
        p1 = ex.export_frame(None, 1, processed_data=solid_rgba(0.5))
        stats = ex.get_export_stats()
        total = os.path.getsize(p0) + os.path.getsize(p1)
        assert stats == {
            "count": 2, "total_bytes": total, "avg_bytes": total / 2,
            "format": "png", "bit_depth": 8,
            "color_policy": "scene_linear", "output_dir": str(tmp_path)}

    def test_cleanup_removes_exported_files(self, tmp_path) -> None:
        ex = ImageSequenceExporter(str(tmp_path), format="png",
                                   bit_depth=8, color_policy="scene_linear")
        p0 = ex.export_frame(None, 0, processed_data=solid_rgba(0.5))
        p1 = ex.export_frame(None, 1, processed_data=solid_rgba(0.5))
        ex.cleanup()
        assert not os.path.exists(p0)
        assert not os.path.exists(p1)
        assert ex.get_exported_files() == []

    def test_factory_vfx_preset(self, tmp_path) -> None:
        ex = create_sequence_exporter(str(tmp_path), use_case="vfx")
        assert isinstance(ex, ImageSequenceExporter)
        assert ex._format == "exr"
        assert ex._bit_depth == 16
        assert ex.get_color_policy() == "scene_linear"

    def test_factory_web_preset(self, tmp_path) -> None:
        ex = create_sequence_exporter(str(tmp_path), use_case="web")
        assert ex._format == "png"
        assert ex._bit_depth == 8
        assert ex._compression == "fast"
        assert ex.get_color_policy() == "srgb"

    def test_factory_archive_preset(self, tmp_path) -> None:
        ex = create_sequence_exporter(str(tmp_path), use_case="archive")
        assert ex._format == "tiff"
        assert ex._bit_depth == 16
        assert ex._compression == "lzw"

    def test_factory_unknown_use_case_uses_format(self, tmp_path) -> None:
        ex = create_sequence_exporter(str(tmp_path), format="png",
                                      use_case="mystery")
        assert ex._format == "png"
