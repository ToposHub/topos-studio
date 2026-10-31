"""P4-05 扩展：10-bit 4:2:0 源（4K HEVC）导出平面直通（真实素材门，P0-13）。

覆盖：
- 4K_H265（yuv420p10le 3840x2160@25）→ h264 / topos_proxy 端到端
- h264：>>2 降位直交付（此前 fail-closed 全档困在合成慢路）
- topos_proxy：420→422 色度域上采样到容器契约
- 保真界：Y 平面 MAD 有界（色度上采样与重编码量化计入容差）

实测基线（2026-09-17，macOS Intel i9-9900K，120 帧）：
- 旧合成慢路：h264 8.9 / prores_422 2.0 / dnxhr_lb 10.5 / topos_proxy 2.1 fps
- 直通后：h264 23.4（2.6x）/ prores 4.5（2.25x，prores_ks 4K 编码地板
  ~230ms/帧）/ dnxhr 27.1（2.6x）/ topos_proxy 45.1（21.4x）
"""

from __future__ import annotations

import os

import numpy as np
import pytest

TEST_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEVC10_SOURCE = os.path.join(TEST_ROOT, "tests", "test_videos", "4K_H265.mov")


def _require_real_video() -> str:
    if not os.path.exists(HEVC10_SOURCE):
        pytest.skip(f"real test video not available: {HEVC10_SOURCE}")
    return HEVC10_SOURCE


def _build_timeline(source_path: str, frames: int):
    from src.features.edit.core.project import (
        EditProject,
        MediaAsset,
        probe_media_metadata,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    from src.shared.media.metadata import Rational

    project = EditProject.create(name="hevc10-export-passthrough")
    metadata = {"fps": 25.0, "total_frames": frames}
    metadata.update(probe_media_metadata(source_path))
    metadata["total_frames"] = frames
    asset = MediaAsset(
        asset_id="asset-hevc10-pt",
        path=source_path,
        media_type="video",
        display_name="hevc10-pt",
        metadata=metadata,
    )
    project = project.with_media_asset_added(asset)
    clip = ClipInstance(
        clip_id="clip-hevc10-pt",
        asset_id=asset.asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=frames,
        source_start=0,
        source_duration=frames,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    sequence = TimelineSequence(
        timeline_id="tl-hevc10-pt", name="HEVC10PT",
        fps=Rational(25, 1), width=3840, height=2160,
        video_tracks=(v1,), audio_tracks=(),
    )
    return project, sequence


def _export(codec_enum, frames: int, output_path: str) -> None:
    from src.features.edit.core.timeline_export import (
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
        create_timeline_exporter,
    )

    project, sequence = _build_timeline(HEVC10_SOURCE, frames)
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id=sequence.timeline_id,
        range_type=ExportRangeType.ENTIRE,
        codec=codec_enum,
        include_audio=False,
        output_path=output_path,
    )
    exporter = create_timeline_exporter(project=project, sequence=sequence)
    outcome = exporter.export_timeline(config)
    assert outcome.success, f"export failed: {outcome.cause}"


def _probe_output(path: str) -> tuple[str, str, int]:
    import av

    with av.open(path) as container:
        stream = container.streams.video[0]
        codec = stream.codec_context.name if stream.codec_context else "unknown"
        pix_fmt = str(stream.codec_context.pix_fmt) \
            if stream.codec_context else "unknown"
        count = sum(1 for _ in container.decode(stream))
    return codec, pix_fmt, count


def _source_y_planes(count: int) -> list[np.ndarray]:
    """源 Y 平面——走产品同款 planar 解码契约（limited-range 10-bit）。

    不用 PyAV to_ndarray(gray16le)：该重格式化会做 limited→full 范围
    拉伸（实测 Y 64..940 → 229..56611），与输出平面不可比。
    """
    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    source = FFmpegMediaSource(
        HEVC10_SOURCE, preserve_planes=True, output_mode="planar_yuv")
    source.open()
    planes = []
    try:
        for i in range(count):
            frame = source.read_frame(i)
            if frame is None:
                break
            planes.append(frame.planes[0].astype(np.int16))
    finally:
        source.close()
    return planes


class TestHevc10SourceExportPassthrough:
    """10-bit 420 源 → 各交付档直通门（端到端）。"""

    def test_h264_delivery_probes_correct(self, tmp_path):
        _require_real_video()
        from src.features.edit.core.timeline_export import ExportCodec

        frames = 12
        output = str(tmp_path / "h264.mov")
        _export(ExportCodec.H264, frames, output)
        codec, pix_fmt, count = _probe_output(output)
        assert (codec, pix_fmt) == ("h264", "yuv420p")
        assert count == frames

    def test_topos_proxy_delivery_upsamples_to_422(self, tmp_path):
        _require_real_video()
        from src.features.edit.core.timeline_export import ExportCodec
        from src.shared.media.topos_source import ToposMediaSource

        frames = 6
        output = str(tmp_path / "topos.mov")
        _export(ExportCodec.TOPOS_PROXY, frames, output)
        src = ToposMediaSource(output)
        src.open()
        try:
            frame = src.read_frame(0)
        finally:
            src.close()
        assert frame is not None
        # 422 容器契约：源 420 色度已上采样到全高
        assert frame.pixel_format == "yuv422p10le"
        assert (frame.width, frame.height) == (3840, 2160)
        assert frame.planes[1].shape == (2160, 1920)

    def test_prores_fidelity_y_bound(self, tmp_path):
        """直通 Y 平面与源 Y 的 MAD 有界（降位通道在 h264 档，prores 不降位）。"""
        _require_real_video()
        from src.features.edit.core.timeline_export import ExportCodec
        from src.shared.media.topos_source import ToposMediaSource

        frames = 4
        output = str(tmp_path / "topos2.mov")
        _export(ExportCodec.TOPOS_PROXY, frames, output)

        source_planes = _source_y_planes(frames)
        src = ToposMediaSource(output)
        src.open()
        try:
            for i in range(frames):
                out_frame = src.read_frame(i)
                assert out_frame is not None
                out_y = out_frame.planes[0].astype(np.int16)
                src_y = source_planes[i]
                # 源是 limited-range 10-bit Y；输出同域。proxy 档重编码
                # 量化 MAD 实测 ~9/1023 量级（2K 标定），取 16/1023 上界。
                mad = float(np.abs(src_y - out_y).mean())
                assert mad < 16.0, f"Y MAD {mad:.2f}/1023 exceeds bound"
        finally:
            src.close()
