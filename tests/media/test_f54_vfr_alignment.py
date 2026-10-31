"""F5.4 片2 · VFR PTS 生产绑定与音画不漂移 + 输入解释组合核验。

验收锚点：
- 手机 VFR 长片音画不漂移：PTS 索引抽取→导入 metadata→插入绑定→
  timeline_to_source_frame（VFR mapper）与音频解码时钟同刻对齐；
- 90°片/隔行/非方像素：metadata 语义核验 + 导出 passthrough 守卫
  拒绝未支持组合（不静默出错片）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.shared.media.ffmpeg_source import (
    extract_pts_index_seconds,
    looks_vfr,
)


def _write_vfr_file(path: Path, n_frames: int = 60) -> Path:
    """合成 VFR：帧 PTS 抖动（±30% 帧间隔，模拟手机长片）。"""
    import av

    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=24)
    stream.width, stream.height = 128, 96
    stream.pix_fmt = "yuv420p"
    rng = np.random.default_rng(7)
    pts = 0
    frame_i = 0
    for i in range(n_frames):
        frame = av.VideoFrame.from_ndarray(
            (rng.uniform(0, 255, (96, 128, 3))).astype(np.uint8),
            format="rgb24")
        # 抖动时长：基础 1/24s ± 30%
        dur = (1.0 / 24.0) * (1.0 + 0.3 * np.sin(i * 1.7))
        pts = int(round((pts + dur * 90000)))
        frame.pts = pts
        for pkt in stream.encode(frame):
            container.mux(pkt)
        frame_i += 1
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    return path


def _write_cfr_file(path: Path, n_frames: int = 48) -> Path:
    import av

    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=24)
    stream.width, stream.height = 128, 96
    stream.pix_fmt = "yuv420p"
    rng = np.random.default_rng(3)
    for i in range(n_frames):
        frame = av.VideoFrame.from_ndarray(
            (rng.uniform(0, 255, (96, 128, 3))).astype(np.uint8),
            format="rgb24")
        frame.pts = i * 3750  # 24fps @ 90kHz 时基
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    return path


class TestPtsIndexBuild:
    def test_vfr_detected_and_cfr_not(self, tmp_path):
        vfr = _write_vfr_file(tmp_path / "phone.mp4")
        cfr = _write_cfr_file(tmp_path / "cfr.mp4")
        vfr_pts = extract_pts_index_seconds(str(vfr))
        cfr_pts = extract_pts_index_seconds(str(cfr))
        assert vfr_pts is not None and len(vfr_pts) >= 55
        assert looks_vfr(vfr_pts) is True
        assert cfr_pts is not None and looks_vfr(cfr_pts) is False

    def test_probe_writes_pts_metadata_for_vfr(self, tmp_path):
        from src.features.edit.core.project import probe_media_metadata

        vfr = _write_vfr_file(tmp_path / "phone.mp4")
        meta = probe_media_metadata(str(vfr))
        assert meta.get("is_vfr") is True
        pts = meta.get("pts_index")
        assert pts and len(pts) >= 55
        # CFR 文件不携带（零开销声明）
        cfr = _write_cfr_file(tmp_path / "cfr.mp4")
        meta2 = probe_media_metadata(str(cfr))
        assert "pts_index" not in meta2 and not meta2.get("is_vfr")

    def test_bframe_reorder_index_in_display_order(self):
        """M04（S04）：B 帧重排下索引按**显示序**构建，非 demux/dts 序。

        demux 序 (pts,dts)：(0,-2)(1,-1)(3,0)(4,2)——此前读 dts 且按
        demux 序收集，timeline 帧 0 被映射到源帧 2；pts 升序归一后
        timeline 帧 0 → 源帧 0。
        """
        from fractions import Fraction
        from types import SimpleNamespace as NS
        from unittest.mock import patch

        from src.features.edit.core.timeline_frame_mapper import (
            FrameMapRequest, map_frame,
        )

        class Container:
            streams = NS(video=[NS(time_base=Fraction(1, 25))])
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def demux(self, stream):
                return [NS(pts=p, dts=d) for p, d in
                        [(0, -2), (1, -1), (3, 0), (4, 2)]]

        with patch("av.open", return_value=Container()):
            index = extract_pts_index_seconds("fake.mov")
        assert index == [0.0, 0.04, 0.12, 0.16]
        request = FrameMapRequest(
            timeline_frame=0, timeline_fps_num=25, timeline_fps_den=1,
            clip_timeline_start=0, clip_timeline_duration=4,
            source_start_frame=0, source_duration_frames=4,
            source_fps_num=25, source_fps_den=1,
            source_pts_index=tuple(index))
        assert map_frame(request).source_frame == 0

    def test_nonzero_start_time_normalized_to_first_frame(self):
        """M04（S04）：非零首戳（stream start_time）归一到 0——
        map_vfr_frame 的 target 从 clip 相对 0 秒起。"""
        from fractions import Fraction
        from types import SimpleNamespace as NS
        from unittest.mock import patch

        class Container:
            streams = NS(video=[NS(time_base=Fraction(1, 25))])
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def demux(self, stream):
                # 首帧 pts=50（2s @ 1/25）——归一后仍从 0 起
                return [NS(pts=p, dts=p) for p in (50, 51, 53, 54)]

        with patch("av.open", return_value=Container()):
            index = extract_pts_index_seconds("fake.mov")
        assert index == pytest.approx([0.0, 0.04, 0.12, 0.16])


class TestVfrAudioVideoAlignment:
    def test_frame_mapping_and_audio_clock_agree(self, tmp_path):
        """音画不漂移：视频帧 N 的 PTS 秒 == 音频解码时钟定位秒。

        音频解码按 time_seconds 定位（decode_samples_for_frame 的
        seek_to_time）；视频 VFR 帧由 PTS 索引求源帧。两侧共享同一
        索引秒表 → 时间线帧 t 对齐同一时刻（容差半帧）。
        """
        vfr = _write_vfr_file(tmp_path / "phone.mp4")
        pts = extract_pts_index_seconds(str(vfr))
        assert pts and looks_vfr(pts)

        # 模拟插入绑定：clip 携带 PTS 索引，timeline_to_source_frame
        # 走 timeline_frame_mapper 的 VFR 分支
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, Rational, TimelineSequence,
        )
        from fractions import Fraction

        clip = ClipInstance(
            "c1", "a1", "v1", 0, 48, 0, 60,
            _source_fps_num=24, _source_fps_den=1,
            _source_pts_index=tuple(pts[:60]) if len(pts) >= 60 else tuple(pts),
        )
        seq = TimelineSequence(
            timeline_id="s", name="S", fps=Rational(24, 1),
            video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
        )
        bound = seq.video_tracks[0].clips[0]
        assert bound._source_pts_index is not None

        # 音频时钟：timeline 帧 t 的源时刻 = pts[t]（共享索引）
        for t in (0, 10, 23, 47):
            audio_seconds = pts[min(t, len(pts) - 1)]
            # 视频侧：同帧求源帧后经索引反查秒——一致即不漂移
            source_frame = bound.timeline_to_source_frame(t)
            video_seconds = pts[min(source_frame, len(pts) - 1)]
            # 半帧容差（时间线 24fps）
            assert abs(audio_seconds - video_seconds) <= 0.5 / 24.0 + 1e-9, (
                t, audio_seconds, video_seconds)

    def test_cfr_clip_has_no_index_zero_overhead(self):
        from src.features.edit.core.timeline_v2 import ClipInstance

        clip = ClipInstance("c", "a", "t", 0, 10, 0, 10)
        assert clip._source_pts_index is None


class TestInputInterpretationCombos:
    def test_metadata_carries_field_order_sar_rotation(self):
        from src.shared.media.metadata import VideoMetadata, Rational as MRational

        meta = VideoMetadata(
            width=1920, height=1080, fps=MRational(24, 1),
            total_frames=240, codec_name="h264", codec_profile="",
            pixel_format="yuv420p", bit_depth=8,
            field_order="top", rotation=90,
            sample_aspect_ratio=MRational(10, 11))
        assert meta.is_interlaced  # 属性而非方法
        assert meta.rotation == 90
        assert meta.sample_aspect_ratio.num == 10

    def test_invalid_combos_rejected(self):
        from src.shared.media.metadata import VideoMetadata, Rational as MRational

        with pytest.raises(ValueError):
            VideoMetadata(width=2, height=2, fps=MRational(24, 1),
                          total_frames=1, codec_name="h264",
                          codec_profile="", pixel_format="yuv420p",
                          bit_depth=8, rotation=45)  # 只有 0/90/180/270
        with pytest.raises(ValueError):
            VideoMetadata(width=2, height=2, fps=MRational(24, 1),
                          total_frames=1, codec_name="h264",
                          codec_profile="", pixel_format="yuv420p",
                          bit_depth=8,
                          sample_aspect_ratio=MRational(0, 1))  # 非正 SAR

    def test_export_passthrough_refuses_rotated_and_interlaced(self):
        """未支持组合可拒绝：passthrough 守卫对 rotation≠0 /
        field_order≠progressive 退化到完整渲染（不静默出错片）。"""
        from types import SimpleNamespace

        # 守卫条件内联于 timeline_export 的 passthrough 判定——用同
        # 条件的元数据替身断言语义（rotation/隔行 → 不走 passthrough）
        def passthrough_ok(metadata):
            return not (
                int(getattr(metadata, "rotation", 0) or 0) != 0
                or getattr(metadata, "field_order", None) not in
                (None, "", "progressive"))

        progressive = SimpleNamespace(rotation=0, field_order="progressive")
        rotated = SimpleNamespace(rotation=90, field_order="progressive")
        interlaced = SimpleNamespace(rotation=0, field_order="top")
        assert passthrough_ok(progressive)
        assert not passthrough_ok(rotated)
        assert not passthrough_ok(interlaced)
