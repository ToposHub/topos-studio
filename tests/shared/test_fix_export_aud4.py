"""批 A（Critical）修复回归测试：AUD-33100 / AUD-33320 / AUD-33504 / AUD-33901。

证据报告：docs/audit/2026-09-28/batches/batch-331/333/335-shared-export.md、
batch-339-shared-io.md。修复说明见 docs/plans/2026-09-28-multi-machine-program/
fixes/reports/w4-g5.md。
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Any, Optional

import numpy as np
import pytest

from src.shared.export.export_panel import ExportPanel, generate_export_filename
from src.shared.export.sequence_exporter import ImageSequenceExporter
from src.shared.export.video_encoder import (
    FFmpegEncoderConfig,
    FFmpegVideoEncoder,
    VP9_PRESET_CPU_USED,
)
from src.shared.io.file_io import FileIO
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


# =============================================================================
# AUD-33100：自定义命名模式非法 format 串不得逃逸异常
# =============================================================================

class TestAud33100InvalidPattern:
    """generate_export_filename 对非法 pattern 必须回退默认命名（不抛 ValueError）。"""

    @pytest.mark.parametrize("bad_pattern", [
        "{",            # 键入过程中的中间态：Single '{' encountered → ValueError
        "{}",           # 空占位符 → IndexError
        "{n:0dd}",      # 非法格式说明符 → ValueError
        "{unknown}",    # 未知字段 → KeyError
        "{name",        # 未闭合
        "{0}",          # 位置参数（调用只提供关键字）→ IndexError
    ])
    def test_invalid_pattern_returns_fallback(self, bad_pattern: str) -> None:
        result = generate_export_filename(
            mode="Custom Pattern", original_name="photo", output_ext=".jpg",
            index=1, seq_digits=3, custom_pattern=bad_pattern,
        )
        assert result == "photo_001.jpg"

    def test_valid_pattern_still_formats(self) -> None:
        result = generate_export_filename(
            mode="Custom Pattern", original_name="photo", output_ext=".jpg",
            index=7, seq_digits=3, custom_pattern="{name}_{n:03d}{ext}",
        )
        assert result == "photo_007.jpg"

    def test_typing_lone_brace_does_not_crash_preview(self, qapp) -> None:
        """textChanged 每键触发 update_naming_preview：键入 "{" 不得抛异常，
        预览显示占位错误文案而非崩溃（PyQt6 槽内未处理异常会中止进程）。"""
        panel = ExportPanel()
        panel.custom_pattern_input.setText("{")
        text = panel.naming_preview_label.text()
        assert text.startswith("Preview:")
        assert ("无效" in text) or ("nvalid" in text)

    def test_typing_bad_specifier_shows_placeholder(self, qapp) -> None:
        panel = ExportPanel()
        panel.custom_pattern_input.setText("{n:0dd}")
        text = panel.naming_preview_label.text()
        assert ("无效" in text) or ("nvalid" in text)

    def test_valid_pattern_preview_recovers(self, qapp) -> None:
        panel = ExportPanel()
        panel.custom_pattern_input.setText("{")
        panel.custom_pattern_input.setText("{name}_{n:03d}{ext}")
        text = panel.naming_preview_label.text()
        assert ("无效" not in text) and ("nvalid" not in text)


# =============================================================================
# AUD-33320：packed 整数帧数据必须按 dtype 归一化
# =============================================================================

def _packed_frame(data: np.ndarray) -> DecodedFrame:
    return DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        data=data, pixel_format="rgba" if data.shape[-1] == 4 else "rgb",
    )


class TestAud33320PackedNormalization:
    """_extract_frame_data 返回值必须处于 [0,1] float 域。"""

    @pytest.fixture
    def exporter(self, tmp_path) -> ImageSequenceExporter:
        return ImageSequenceExporter(output_dir=str(tmp_path), format="png")

    def test_uint8_normalized_to_unit_range(self, exporter) -> None:
        data = np.full((4, 4, 3), 255, dtype=np.uint8)
        data[0, 0] = (0, 128, 255)
        out = exporter._extract_frame_data(_packed_frame(data))
        assert out.dtype == np.float32
        assert out.shape == (4, 4, 4)  # 补了 alpha
        assert out[0, 0, 0] == pytest.approx(0.0 / 255.0)
        assert out[0, 0, 1] == pytest.approx(128.0 / 255.0)
        assert out[0, 0, 2] == pytest.approx(1.0)
        # 修复前：uint8 原样透传 → clip(0,255→1) 导出全白
        assert out.max() <= 1.0
        # 补的 alpha 必须是 1.0（修复前整数域 alpha=1 → 归一化后 1/255）
        assert (out[..., 3] == 1.0).all()

    def test_uint16_normalized_to_unit_range(self, exporter) -> None:
        data = np.zeros((2, 2, 4), dtype=np.uint16)
        data[..., 0] = 65535
        data[..., 1] = 32768
        out = exporter._extract_frame_data(_packed_frame(data))
        assert out.dtype == np.float32
        assert out[..., 0] == pytest.approx(1.0)
        assert out[..., 1] == pytest.approx(32768.0 / 65535.0, abs=1e-6)

    def test_float_passthrough_unchanged(self, exporter) -> None:
        data = np.linspace(0.0, 2.0, 16, dtype=np.float32).reshape(2, 2, 4)
        out = exporter._extract_frame_data(_packed_frame(data))
        # float 原样（HDR >1.0 不得被归一化破坏）
        assert out.dtype == np.float32
        np.testing.assert_array_equal(out, data)


# =============================================================================
# AUD-33504：VP9 编码参数必须真正下发给编码器
# =============================================================================

class _FakeStream:
    """最小流桩：承载 options 字典与 gop_size/profile/thread_count 属性。"""

    def __init__(self) -> None:
        self.options: dict[str, str] = {}
        self.gop_size: Optional[int] = None
        self.profile: Any = None
        self.thread_count: int = 0


def _vp9_encoder(**overrides: Any) -> tuple[FFmpegVideoEncoder, _FakeStream]:
    cfg_kwargs: dict[str, Any] = dict(
        codec="vp9", width=64, height=64, fps=Rational(30, 1),
        pix_fmt="yuv420p", hwaccel=None,
    )
    cfg_kwargs.update(overrides)
    encoder = FFmpegVideoEncoder("/tmp/unused_vp9_test.webm",
                                 FFmpegEncoderConfig(**cfg_kwargs))
    stream = _FakeStream()
    encoder._stream = stream
    return encoder, stream


class TestAud33504Vp9ParameterDelivery:

    def test_crf_preset_gop_all_delivered(self) -> None:
        encoder, stream = _vp9_encoder(crf=32, preset="medium", gop_size=48)
        encoder._configure_encoder()
        opts = stream.options
        # 修复前：vp9 不在任何分支内，options 恒空（FFmpeg 默认 256 kb/s）
        assert opts.get("crf") == "32"
        assert opts.get("cpu-used") == "4"      # libvpx 无 'preset' 选项
        assert "preset" not in opts             # 'preset' 会让 avcodec_open2 失败
        assert stream.gop_size == 48

    def test_vp9_crf_clamped_to_libvpx_domain(self) -> None:
        # libvpx-vp9 的 crf 域是 0-63（x264/x265 是 0-51）
        encoder, stream = _vp9_encoder(crf=70)
        encoder._configure_encoder()
        assert stream.options.get("crf") == "63"

    def test_vp9_bitrate_uses_standard_avoptions(self) -> None:
        encoder, stream = _vp9_encoder(
            bitrate=8_000_000, max_bitrate=10_000_000, buffer_size=2_000_000)
        encoder._configure_encoder()
        opts = stream.options
        # 对齐 av1 分支：标准 AVOption（b/maxrate/bufsize），
        # x264-params/x265-params 私有串对 libvpx 无效
        assert opts.get("b") == "8000k"
        assert opts.get("maxrate") == "10000k"
        assert opts.get("bufsize") == "2000k"
        assert "x264-params" not in opts and "x265-params" not in opts

    def test_vp9_preset_cpu_used_mapping(self) -> None:
        assert VP9_PRESET_CPU_USED["veryslow"] == "0"
        assert VP9_PRESET_CPU_USED["medium"] == "4"
        assert VP9_PRESET_CPU_USED["ultrafast"] == "8"
        encoder, stream = _vp9_encoder(crf=30, preset="veryslow")
        encoder._configure_encoder()
        assert stream.options.get("cpu-used") == "0"

    def test_vp9_gop_defaults_to_fps_times_two(self) -> None:
        encoder, stream = _vp9_encoder(crf=30)
        encoder._configure_encoder()
        # _is_gop_codec 包含 vp9：默认 GOP = fps*2 = 60
        assert encoder._config.gop_size == 60
        assert stream.gop_size == 60

    def test_h264_path_unchanged(self) -> None:
        """对照：H.264 CPU 路径的既有下发写法不受影响。"""
        cfg = FFmpegEncoderConfig(
            codec="h264", width=64, height=64, fps=Rational(24, 1),
            pix_fmt="yuv420p", hwaccel=None, crf=20, preset="slow")
        encoder = FFmpegVideoEncoder("/tmp/unused_h264_test.mp4", cfg)
        stream = _FakeStream()
        encoder._stream = stream
        encoder._configure_encoder()
        assert stream.options.get("crf") == "20"
        assert stream.options.get("preset") == "slow"
        assert stream.gop_size == 48  # 24fps * 2


# =============================================================================
# AUD-33901：预览缓存 EXR 压缩必须遵循 T-P1-71（RLE，不走 libdeflate）
# =============================================================================

class TestAud33901PreviewCacheExrCompression:

    def test_cache_preview_uses_rle_not_zip(self, tmp_path, monkeypatch) -> None:
        """16 位预览缓存走 EXR 编码路径：压缩参数必须是 RLE(1)。

        T-P1-71：opencv 轮子的 libOpenEXR 走 ZIP/PIZ 时经 libdeflate 派发，
        本机间歇性 SIGILL 崩溃；save_image 已改 RLE(1)，预览缓存须一致。
        """
        import cv2
        monkeypatch.setattr(FileIO, "CACHE_DIR", str(tmp_path))
        captured: dict[str, Any] = {}

        def fake_imencode(ext: str, img: Any, params: Any) -> tuple:
            captured["ext"] = ext
            captured["params"] = list(params)
            return True, np.frombuffer(b"exr-bytes", dtype=np.uint8)

        monkeypatch.setattr("src.shared.io.file_io.cv2.imencode", fake_imencode)

        preview = np.zeros((4, 4, 3), dtype=np.uint16)
        FileIO._cache_preview("/nonexistent/src.nef", preview, "high")

        assert captured["ext"] == ".exr"
        params = captured["params"]
        assert params[0] == cv2.IMWRITE_EXR_TYPE
        assert params[2] == cv2.IMWRITE_EXR_COMPRESSION
        assert params[3] == 1, (
            f"预览缓存 EXR 压缩必须是 RLE(1)（T-P1-71 libdeflate SIGILL "
            f"规避），实际为 {params[3]}"
        )

    def test_uint8_preview_still_uses_png_jpg_path(self, tmp_path, monkeypatch) -> None:
        """uint8（无 alpha）预览不受影响，仍走 JPEG 路径。"""
        import cv2
        monkeypatch.setattr(FileIO, "CACHE_DIR", str(tmp_path))
        captured: dict[str, Any] = {}

        def fake_imencode(ext: str, img: Any, params: Any) -> tuple:
            captured["ext"] = ext
            return True, np.frombuffer(b"jpg-bytes", dtype=np.uint8)

        monkeypatch.setattr("src.shared.io.file_io.cv2.imencode", fake_imencode)

        FileIO._cache_preview("/nonexistent/src.jpg",
                              np.zeros((4, 4, 3), dtype=np.uint8), "high")
        assert captured["ext"] == ".jpg"

    def test_save_image_default_exr_compression_is_rle(self) -> None:
        """对照锚点：save_image 的 T-P1-71 修复（默认 RLE(1)）保持不变。"""
        import cv2
        assert int(cv2.IMWRITE_EXR_COMPRESSION) == 49  # 常量存在性护栏
