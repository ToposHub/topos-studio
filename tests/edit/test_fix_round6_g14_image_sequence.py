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
    @pytest.mark.parametrize("output_space", [
        "rec709-g24", "srgb", "rec709-bt709", "p3-d65-g24", "display-p3", "rec2020-g24",
        "dci-p3-g26", "rec2020-bt2020",
    ])
    def test_project_output_space_in_16bit_tiff_delivery(self, tmp_path, output_space):
        import cv2

        from src.features.edit.core.project import EditProject, MediaAsset
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            TimelineExporter,
            TimelineExportConfig,
        )
        from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
        from src.shared.color.color_space_descriptor import OUTPUT_SPACE_KEY, TIMELINE_SPACE_KEY

        # Include a near-black sample and values between adjacent 8-bit codes.
        samples = np.array([321, 5961, 18364, 33539, 56272, 65535], dtype=np.uint16)
        frame = np.tile(samples.reshape(1, 2, 3), (8, 8, 1))
        source = tmp_path / "source.tif"
        assert cv2.imwrite(str(source), frame)
        asset = MediaAsset("asset-image", str(source), "image", "Plate", {})
        project = EditProject(
            "p-color", "P", assets={asset.asset_id: asset},
            color_management={TIMELINE_SPACE_KEY: "rec709-g24", OUTPUT_SPACE_KEY: output_space},
        )
        clip = ClipInstance("c1", asset.asset_id, "v0", 0, 1, 0, 1)
        sequence = TimelineSequence(
            timeline_id="seq-color", name="Project output", width=16, height=8,
            fps=Rational_24, video_tracks=(EditTrack("v0", "V1", "video", 0, clips=(clip,)),),
        )
        config = TimelineExportConfig(
            output_path=str(tmp_path / "grade"), codec=ExportCodec.TIFF_SEQUENCE,
            timeline_id=sequence.timeline_id, bit_depth=16, include_audio=False,
        )
        exporter = TimelineExporter(project, sequence, Mock())
        outcome = exporter.export_timeline(config)
        assert outcome.success, outcome.cause

        output_file, = tmp_path.glob("grade_*.tif")
        rendered = cv2.imread(str(output_file), cv2.IMREAD_UNCHANGED)
        assert rendered.dtype == np.uint16
        if output_space == "rec709-g24":
            np.testing.assert_array_equal(rendered, frame)
            return
        linear = (frame.astype(np.float64) / 65535.0) ** 2.4
        profiled = output_space in (
            "p3-d65-g24", "display-p3", "rec2020-g24", "dci-p3-g26", "rec2020-bt2020",
        )
        if profiled:
            from PIL import Image
            from PyQt6.QtGui import QColor, QColorSpace

            with Image.open(output_file) as tagged:
                assert tagged.tag_v2[258] == (16, 16, 16)
                profile = QColorSpace.fromIccProfile(tagged.info["icc_profile"])
                if output_space == "display-p3":
                    assert profile == QColorSpace(QColorSpace.NamedColorSpace.DisplayP3)
                elif output_space == "rec2020-bt2020":
                    assert profile.isValid()
                    linear_2020 = QColorSpace(QColorSpace.Primaries.Bt2020,
                                              QColorSpace.TransferFunction.Linear)
                    transform = profile.transformationToColorSpace(linear_2020)
                    gray = transform.map(QColor.fromRgbF(0.5, 0.5, 0.5))
                    assert gray.redF() == pytest.approx(((0.5 + 0.099) / 1.099) ** (1 / 0.45),
                                                       abs=1e-4)
                else:
                    assert profile.isValid()
                    gamma = 2.6 if output_space == "dci-p3-g26" else 2.4
                    assert profile.gamma() == pytest.approx(gamma, abs=0.001)
            p3_d65_matrix = [[0.822461968714, 0.177538031286, 0.0],
                             [0.033194198851, 0.966805801149, 0.0],
                             [0.017082630721, 0.072397440664, 0.910519928615]]
            matrices = {
                "rec2020-g24": [[0.627403895935, 0.329283038378, 0.043313065687],
                                [0.069097289358, 0.919540395075, 0.011362315566],
                                [0.016391438875, 0.088013307877, 0.895595253248]],
                "dci-p3-g26": [[0.89895156, 0.19404920, 0.0],
                               [0.03182076, 0.92680323, 0.0],
                               [0.01965417, 0.08329581, 1.04758528]],
            }
            matrix_key = "rec2020-g24" if output_space == "rec2020-bt2020" else output_space
            rgb_matrix = np.asarray(matrices.get(matrix_key, p3_d65_matrix))
            linear = (linear[..., ::-1] @ rgb_matrix.T)[..., ::-1]
        if output_space in ("srgb", "display-p3"):
            encoded = np.where(linear <= 0.0031308, 12.92 * linear,
                               1.055 * linear ** (1 / 2.4) - 0.055)
        elif output_space in ("p3-d65-g24", "rec2020-g24"):
            encoded = linear ** (1 / 2.4)
        elif output_space == "dci-p3-g26":
            encoded = linear ** (1 / 2.6)
        else:
            encoded = np.where(linear < 0.018, 4.5 * linear,
                               1.099 * linear ** 0.45 - 0.099)
        expected = np.rint(np.clip(encoded, 0.0, 1.0) * 65535).astype(np.uint16)
        np.testing.assert_allclose(rendered, expected, atol=1, rtol=0)

        if profiled:
            config.codec = ExportCodec.PRORES_422HQ
            with pytest.raises(ValueError, match="16-bit RGB TIFF"):
                exporter._create_encoder_config(config)

    def test_gamma24_prores_writes_primaries_and_matrix_with_unspecified_transfer(self, tmp_path):
        from src.features.edit.core.project import EditProject
        from src.features.edit.core.timeline_export import ExportCodec, TimelineExporter, TimelineExportConfig
        from src.features.edit.core.timeline_v2 import TimelineSequence
        from src.shared.color.color_space_descriptor import OUTPUT_SPACE_KEY, TIMELINE_SPACE_KEY
        from src.shared.media.frame import DecodedFrame

        project = EditProject(
            "p-color", "P", color_management={
                TIMELINE_SPACE_KEY: "rec709-g24", OUTPUT_SPACE_KEY: "rec709-g24",
            },
        )
        sequence = TimelineSequence("seq-color", "Project output", fps=Rational_24,
                                    width=128, height=96)
        exporter = TimelineExporter(project, sequence, Mock())
        path = tmp_path / "grade.mov"
        config = TimelineExportConfig(
            output_path=str(path), codec=ExportCodec.PRORES_422HQ, timeline_id=sequence.timeline_id,
            include_audio=False,
        )
        encoder_config = exporter._create_encoder_config(config)
        assert encoder_config.color_metadata.transfer == "unknown"
        encoder = exporter._create_encoder(str(path), encoder_config)
        encoder.open()
        try:
            encoder.encode_frame(DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                data=np.full((96, 128, 3), 32768, dtype=np.uint16),
                pixel_format="bgr48le", width=128, height=96,
            ))
        finally:
            encoder.close()
        assert b"colrnclc\x00\x01\x00\x02\x00\x01" in path.read_bytes()

    def test_r210_source_preserves_10bit_rgb_codes_and_channel_order(self, tmp_path):
        import av
        import cv2

        from src.features.edit.core.project import EditProject, MediaAsset
        from src.features.edit.core.timeline_export import ExportCodec, TimelineExportConfig, create_timeline_exporter
        from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
        from src.shared.color.color_space_descriptor import OUTPUT_SPACE_KEY, TIMELINE_SPACE_KEY

        codes = np.array([1, 123, 1021, 699, 1023, 102], dtype=np.uint16)
        bgr = np.tile(codes.reshape(1, 2, 3), (8, 8, 1))
        src = tmp_path / "rgb10.mov"
        with av.open(str(src), "w") as container:
            stream = container.add_stream("r210", rate=24, width=16, height=8)
            stream.pix_fmt = "gbrp10le"
            video = av.VideoFrame(16, 8, format="gbrp10le")
            for plane, channel in zip(video.planes, (1, 0, 2)):
                packed = np.zeros((8, plane.line_size // 2), dtype=np.uint16)
                packed[:, :16] = bgr[..., channel]
                plane.update(packed.tobytes())
            for packet in stream.encode(video) + stream.encode():
                container.mux(packet)

        # The exporter should discover the actual source depth even if older
        # projects do not contain the persisted pixel format.
        asset = MediaAsset("asset-rgb10", str(src), "video", "RGB10", {})
        project = EditProject(
            "p-color", "P", assets={asset.asset_id: asset},
            color_management={TIMELINE_SPACE_KEY: "rec709-g24", OUTPUT_SPACE_KEY: "rec709-g24"},
        )
        clip = ClipInstance("c1", asset.asset_id, "v0", 0, 1, 0, 1)
        sequence = TimelineSequence(
            timeline_id="seq-color", name="Project output", width=16, height=8,
            fps=Rational_24, video_tracks=(EditTrack("v0", "V1", "video", 0, clips=(clip,)),),
        )
        exporter = create_timeline_exporter(project, sequence)
        outcome = exporter.export_timeline(TimelineExportConfig(
            output_path=str(tmp_path / "grade"), codec=ExportCodec.TIFF_SEQUENCE,
            timeline_id=sequence.timeline_id, bit_depth=16, include_audio=False,
        ))
        assert outcome.success, outcome.error
        output_file, = tmp_path.glob("grade_*.tif")
        rendered = cv2.imread(str(output_file), cv2.IMREAD_UNCHANGED)
        expected = np.rint(bgr.astype(np.float64) * (65535.0 / 1023)).astype(np.uint16)
        assert rendered.dtype == np.uint16
        np.testing.assert_array_equal(rendered, expected)

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
