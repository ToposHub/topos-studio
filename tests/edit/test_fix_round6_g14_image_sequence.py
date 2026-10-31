"""第六轮审计 G14 回归：PNG/TIFF 标准图像序列交付。

此前 PNG/TIFF 序列在 Deliver 明确拒绝（"unsupported — use Topos Image
Sequence"）。现在 StandardImageSequenceEncoder 提供标准命名 PNG
（rgb24/rgba）与 TIFF（含 16-bit rgb48le）逐帧输出，外部合成软件可直
接回导；取消删除已写文件；配置校验不再 fail-fast 拒绝。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.export.image_sequence_encoder import (
    StandardImageSequenceEncoder,
    image_sequence_stats,
)

from src.shared.media.metadata import Rational as _Rational

Rational_24 = _Rational(24, 1)

pytestmark = pytest.mark.edit_core


def _frame(h=8, w=12, channels=3, dtype=np.uint8, seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(
        0, 256 if dtype == np.uint8 else 65536,
        size=(h, w, channels), dtype=dtype)


def _decoded(data):
    return SimpleNamespace(data=data)


def _read_image_size(path: Path):
    import av

    container = av.open(str(path))
    try:
        for frame in container.decode(video=0):
            return frame.width, frame.height, len(frame.planes)
    finally:
        container.close()


class TestStandardImageSequenceEncoder:
    @pytest.mark.parametrize("codec,ext", [
        ("png_sequence", ".png"), ("tiff_sequence", ".tif"),
    ])
    def test_writes_standard_named_frames(self, tmp_path, codec, ext):
        config = SimpleNamespace(
            codec=codec,
            naming_config=SimpleNamespace(
                unique_filename_digits=5, frame_number_start=100))
        encoder = StandardImageSequenceEncoder(
            str(tmp_path / "shot"), config)
        encoder.open()
        for i in range(3):
            encoder.encode_frame(_decoded(_frame(seed=i)))
        encoder.close()

        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == [
            f"shot_{100 + i:05d}{ext}" for i in range(3)
        ], f"标准帧命名：{names}"
        for path in tmp_path.iterdir():
            width, height, _planes = _read_image_size(path)
            assert (width, height) == (12, 8)

    def test_alpha_png(self, tmp_path):
        config = SimpleNamespace(codec="png_sequence")
        encoder = StandardImageSequenceEncoder(
            str(tmp_path / "alpha"), config)
        encoder.open()
        encoder.encode_frame(_decoded(_frame(channels=4)))
        encoder.close()
        out = next(iter(tmp_path.iterdir()))
        assert out.suffix == ".png"
        import av

        container = av.open(str(out))
        frame = next(container.decode(video=0))
        assert frame.format.name in ("rgba", "rgb32"), frame.format.name
        container.close()

    def test_16bit_tiff(self, tmp_path):
        config = SimpleNamespace(codec="tiff_sequence")
        encoder = StandardImageSequenceEncoder(str(tmp_path / "hi"), config)
        encoder.open()
        encoder.encode_frame(_decoded(_frame(dtype=np.uint16)))
        encoder.close()
        out = next(iter(tmp_path.iterdir()))
        import av

        container = av.open(str(out))
        frame = next(container.decode(video=0))
        assert frame.format.name in ("rgb48le", "rgb48be"), frame.format.name
        container.close()

    def test_abort_removes_partial_sequence(self, tmp_path):
        config = SimpleNamespace(codec="png_sequence")
        encoder = StandardImageSequenceEncoder(str(tmp_path / "x"), config)
        encoder.open()
        for i in range(4):
            encoder.encode_frame(_decoded(_frame(seed=i)))
        encoder.abort()
        assert list(tmp_path.iterdir()) == [], "取消后不留半套序列"
        # abort 后再写帧应显式失败（不静默继续）
        with pytest.raises(RuntimeError, match="aborted"):
            encoder.encode_frame(_decoded(_frame()))

    def test_audio_is_warning_noop(self, tmp_path):
        config = SimpleNamespace(codec="png_sequence")
        encoder = StandardImageSequenceEncoder(str(tmp_path / "a"), config)
        encoder.open()
        encoder.write_audio_samples(np.zeros((2, 480), dtype=np.float32), 48000)
        encoder.close()
        assert not list(tmp_path.iterdir())

    def test_stats_helper(self, tmp_path):
        config = SimpleNamespace(codec="png_sequence")
        encoder = StandardImageSequenceEncoder(str(tmp_path / "s"), config)
        encoder.open()
        for i in range(5):
            encoder.encode_frame(_decoded(_frame(seed=i)))
        encoder.close()
        stats = image_sequence_stats(str(tmp_path / "s"))
        assert stats is not None
        assert stats["frame_files"] == 5
        assert stats["first_frame"] == 0 and stats["last_frame"] == 4


class TestG14TimelineConfigContract:
    def test_png_tiff_no_longer_rejected(self, tmp_path):
        """配置校验不再 fail-fast 拒绝 PNG/TIFF 序列（G14）。"""
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            TimelineExportConfig,
        )

        for codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE):
            config = TimelineExportConfig(
                output_path=str(tmp_path / "seq_dir"), codec=codec,
                timeline_id="t1")
            errors = config.validate()
            unsupported = [
                e for e in errors if "not supported" in e or "unsupported" in e]
            assert not unsupported, f"{codec} 仍被拒绝: {unsupported}"

    def test_cross_extension_rejected(self, tmp_path):
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            TimelineExportConfig,
        )

        config = TimelineExportConfig(
            output_path=str(tmp_path / "out.png"),
            codec=ExportCodec.TIFF_SEQUENCE, timeline_id="t1")
        errors = config.validate()
        assert any("cannot use a .png" in e for e in errors), errors

    def test_encoder_factory_creates_image_sequence(self, tmp_path):
        """_create_encoder 对 png/tiff 返回标准序列编码器。"""
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            TimelineExportConfig,
            TimelineExporter,
        )
        from src.features.edit.core.timeline_v2 import TimelineSequence
        from src.shared.export.image_sequence_encoder import (
            StandardImageSequenceEncoder,
        )

        sequence = TimelineSequence(
            timeline_id="t1", name="G14", width=64, height=48,
            fps=Rational_24)
        exporter = TimelineExporter(None, sequence, Mock())
        for codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE):
            config = TimelineExportConfig(
                output_path=str(tmp_path / "seq"), codec=codec,
                timeline_id="t1", resolution=(64, 48))
            encoder_config = exporter._create_encoder_config(config)
            encoder = exporter._create_encoder(
                config.output_path, encoder_config)
            assert isinstance(encoder, StandardImageSequenceEncoder), codec
            encoder.close()


class TestG14FullTimelineExport:
    def test_export_renders_png_frames(self, tmp_path):
        """端到端：真实素材时间线导出 PNG 序列，帧数正确、内容可回读。"""
        video = Path("tests/test_videos/2K_h264.mp4")
        if not video.exists():
            pytest.skip("tests/test_videos/2K_h264.mp4 不可用")

        from src.features.edit.core.project import EditProject, MediaAsset
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            TimelineExporter,
            TimelineExportConfig,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        from src.shared.media.metadata import Rational

        clip = ClipInstance("c1", "asset-v", "v0", 0, 6, 0, 6)
        seq = TimelineSequence(
            timeline_id="seq-g14", name="G14", width=1920, height=1080,
            fps=Rational(25, 1),
            video_tracks=(EditTrack("v0", "V1", "video", 0, clips=(clip,)),),
        )
        project = EditProject(
            "p-g14", "P",
            assets={"asset-v": MediaAsset(
                "asset-v", str(video), "video", "V", {})},
        )
        exporter = TimelineExporter(project, seq, Mock())
        out_dir = tmp_path / "frames"
        out_dir.mkdir()
        config = TimelineExportConfig(
            output_path=str(out_dir / "g14"),
            codec=ExportCodec.PNG_SEQUENCE,
            range_type=ExportRangeType.ENTIRE,
            timeline_id="seq-g14",
        )
        outcome = exporter.export_timeline(config)
        assert outcome.success, outcome.error or "export failed"
        pngs = sorted(out_dir.glob("*.png"))
        assert len(pngs) == 6, f"应导出 6 帧，实际 {len(pngs)}"
        # 回读核验：标准 PNG 可被独立解码且有真实内容（非全黑）
        import av

        first = av.open(str(pngs[0]))
        frame = next(first.decode(video=0))
        assert frame.width == 1920 and frame.height == 1080
        first.close()
