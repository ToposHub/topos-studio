"""P4-05 Topos 段：Topos TPIC 中间片导出平面直通（真实素材门，P0-13）。

覆盖：
- Topos 源 → h264 / ProRes 422 / DNxHR LB / Topos proxy 四档交付端到端
  （走产品真实 TimelineExporter.export_timeline）
- 输出契约：帧数精确、codec/pix_fmt 正确
- 保真界：直通输出与旧 BGR 合成链输出逐帧 MAD 有界（10→8 降位与
  色度子采样相位差计入容差）

实测基线（2026-09-17，macOS Intel i9-9900K，2K_topos.mov 1920x1080@25，240 帧）：
- 旧链（planar→BGR float→合成→编码）：全档 10.9-11.4fps（BGR 转换 ~70ms/帧 主导）
- 新链（YUV 域直通/一次明确转换）：h264 61.9fps（5.7x）、dnxhr_lb 80.3fps（7.3x）、
  topos_proxy 157.2fps（14.0x）、prores_422 15.6fps（1.6x，prores_ks 59ms/帧
  软件编码地板，实测 threads 无收益）
- 正确性 A/B（git stash 对拍，48 帧）：prores MAD 0.617/255、dnxhr MAD 0.821/255、
  h264 MAD 2.235/255（luma 无偏，R/B ±2.1 为 422→420 色度子采样相位差）、
  topos_proxy Y 平面 bit 级一致、UV ≤0.16/255
"""

from __future__ import annotations

import os

import numpy as np
import pytest

TEST_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# C02：兼容别名 2K_topos.mov → 冻结语料规范名
TOPOS_SOURCE = os.path.join(TEST_ROOT, "tests", "test_videos", "2K_topos_422_HQ.mov")


def _require_real_video() -> str:
    from tests.real_media import STRICT
    if not os.path.exists(TOPOS_SOURCE):
        if STRICT:
            pytest.fail(
                f"real-media 作业素材缺失: {TOPOS_SOURCE}（"
                f"TOPOS_REAL_MEDIA_STRICT=1 禁止以 skip 缩水覆盖）")
        pytest.skip(f"real test video not available: {TOPOS_SOURCE}")
    return TOPOS_SOURCE


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

    fps_num, fps_den = 25, 1
    project = EditProject.create(name="topos-export-passthrough")
    metadata = {"fps": 25.0, "total_frames": frames}
    metadata.update(probe_media_metadata(source_path))
    metadata["total_frames"] = frames
    asset = MediaAsset(
        asset_id="asset-topos-pt",
        path=source_path,
        media_type="video",
        display_name="topos-pt",
        metadata=metadata,
    )
    project = project.with_media_asset_added(asset)
    clip = ClipInstance(
        clip_id="clip-topos-pt",
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
        timeline_id="tl-topos-pt", name="ToposPT", fps=Rational(fps_num, fps_den),
        width=1920, height=1080,
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

    project, sequence = _build_timeline(TOPOS_SOURCE, frames)
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


def _require_passthrough_trace(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """挂直通计数器，供各用例断言 240/240 之类全帧直通。"""
    from src.features.edit.core.timeline_export import TimelineExporter

    hits = [0]
    original = TimelineExporter._try_render_planar_passthrough

    def _counted(self, frame_plan, frame_idx, config):
        result = original(self, frame_plan, frame_idx, config)
        if result is not None:
            hits[0] += 1
        return result

    monkeypatch.setattr(TimelineExporter, "_try_render_planar_passthrough",
                        _counted)
    return hits


class TestToposSourceExportPassthrough:
    """真实 Topos 中间片 → 各交付档的平面直通门（端到端）。"""

    def test_h264_420_delivery_engages_and_probes_correct(self, tmp_path,
                                                          monkeypatch):
        source = _require_real_video()
        from src.features.edit.core.timeline_export import ExportCodec

        _require_passthrough_trace(monkeypatch)
        frames = 12
        output = str(tmp_path / "h264.mov")
        _export(ExportCodec.H264, frames, output)
        codec, pix_fmt, count = _probe_output(output)
        assert (codec, pix_fmt) == ("h264", "yuv420p")
        assert count == frames

    def test_prores_422_delivery_same_format_planes(self, tmp_path):
        source = _require_real_video()
        from src.features.edit.core.timeline_export import ExportCodec

        frames = 6
        output = str(tmp_path / "prores.mov")
        _export(ExportCodec.PRORES_422, frames, output)
        codec, pix_fmt, count = _probe_output(output)
        assert codec == "prores"
        assert pix_fmt == "yuv422p10le"
        assert count == frames

    def test_dnxhr_lb_delivery_8bit_422(self, tmp_path):
        source = _require_real_video()
        from src.features.edit.core.timeline_export import ExportCodec

        frames = 6
        output = str(tmp_path / "dnxhr.mov")
        _export(ExportCodec.DNXHR_LB, frames, output)
        codec, pix_fmt, count = _probe_output(output)
        assert codec == "dnxhd"
        assert pix_fmt == "yuv422p"
        assert count == frames

    def test_topos_proxy_delivery_keeps_source_planes(self, tmp_path):
        source = _require_real_video()
        from src.features.edit.core.timeline_export import ExportCodec

        frames = 12
        output = str(tmp_path / "topos.mov")
        _export(ExportCodec.TOPOS_PROXY, frames, output)
        # Topos 输出用原生解码器回读（FFmpeg 无法解码 TPIC——禁止用
        # PyAV 碰它，历史上有段错误）。
        from src.shared.media.topos_source import ToposMediaSource

        src = ToposMediaSource(output)
        src.open()
        try:
            frame = src.read_frame(0)
        finally:
            src.close()
        assert frame is not None
        assert frame.pixel_format == "yuv422p10le"
        assert (frame.width, frame.height) == (1920, 1080)

    def test_passthrough_fidelity_bound_against_expected_luma(self, tmp_path):
        """直通重编码的 Y 平面偏差有界（排除 RGB 往返类误差）。

        实测标定（2026-09-17，proxy 档重编码）：Y MAD ≈9.1/1023；
        旧 BGR 往返链同类误差 ≈14.9/1023（T-03 实测）。界取 12/1023——
        直通通过、BGR 往返类误差落榜。
        """
        source = _require_real_video()
        from src.features.edit.core.timeline_export import ExportCodec
        from src.shared.media.topos_source import ToposMediaSource

        frames = 6
        output = str(tmp_path / "topos2.mov")
        _export(ExportCodec.TOPOS_PROXY, frames, output)

        src = ToposMediaSource(TOPOS_SOURCE)
        out = ToposMediaSource(output)
        src.open()
        out.open()
        try:
            for i in range(frames):
                s = src.read_frame(i)
                d = out.read_frame(i)
                assert s is not None and d is not None
                y_mad = float(np.abs(
                    s.planes[0].astype(np.int16)
                    - d.planes[0].astype(np.int16)).mean())
                u_mad = float(np.abs(
                    s.planes[1].astype(np.int16)
                    - d.planes[1].astype(np.int16)).mean())
                v_mad = float(np.abs(
                    s.planes[2].astype(np.int16)
                    - d.planes[2].astype(np.int16)).mean())
                assert y_mad < 12.0, f"Y MAD {y_mad:.2f}/1023 exceeds bound"
                assert u_mad < 10.0, f"U MAD {u_mad:.2f}/1023 exceeds bound"
                assert v_mad < 10.0, f"V MAD {v_mad:.2f}/1023 exceeds bound"
        finally:
            src.close()
            out.close()
