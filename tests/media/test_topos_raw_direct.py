"""M4-R2/R7/R9：视频 RAW（TRAW CFA）直出聚焦测试。

覆盖：
1. ``build_direct_topos_delivery_plan`` 的 RAW→RAW fail-closed 判定域
   （同位深同尺寸直通；位深/尺寸/格式/Alpha/效果逐项拒绝）；
2. TRAW MOV 端到端直出：自建 TRAW 源 → ``Topos RAW`` 导出 →
   delivery_path=topos_direct_raw_cpu_encode、tier 8 容器、相位平面对拍。

不依赖 tests/test_videos 素材（源由编码器自建）；仅 native 库路径需要
libtopos_codec（编解码 roundtrip）。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")

from src.features.edit.core.clip_params import CropParams, TransformParams
from src.shared.codec import topos_binding


def _lib_available() -> bool:
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


# ---------------------------------------------------------------------------
# stub 构造（与 test_topos_direct_delivery 同形，本地独立避免素材门控）
# ---------------------------------------------------------------------------


class _StubClip:
    def __init__(self, clip_id="c1", timeline_start=0, duration=48,
                 source_start=0, effects=(), opacity=1.0):
        self.clip_id = clip_id
        self.asset_id = "a1"
        self.is_enabled = True
        self.timeline_start = timeline_start
        self._duration = duration
        self.source_start = source_start
        self.effects = effects
        self.color_stack_id = None
        self.composite_id = None
        self.transform = TransformParams()
        self.crop = CropParams()
        self.opacity = opacity
        self.fade_in_duration = 0.0
        self.fade_out_duration = 0.0

    def effective_speed(self) -> float:
        return 1.0

    @property
    def timeline_end(self):
        return self.timeline_start + self._duration


class _StubTrack:
    def __init__(self, clips, transitions=()):
        self.clips = clips
        self.transitions = transitions
        self.is_enabled = True


class _StubSequence:
    def __init__(self, tracks=(), subtitle_tracks=()):
        self.video_tracks = tracks
        self.subtitle_tracks = subtitle_tracks


class _StubAsset:
    def __init__(self, metadata, path=None):
        self.media_type = "video"
        # 判定顺序在格式校验前先查 source_file_missing——指向一个必然
        # 存在的文件（本测试文件），格式域由 metadata 字典驱动
        self.path = path or str(Path(__file__).resolve())
        self.metadata = metadata


class _StubProject:
    def __init__(self, assets):
        self._assets = assets

    def get_asset_by_id(self, asset_id):
        return self._assets.get(asset_id)


class _StubEncoderConfig:
    def __init__(self, codec="topos", pix_fmt="yuv422p10le",
                 width=1920, height=1080, fps=24.0, has_alpha=False):
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.width = width
        self.height = height
        self.fps = fps
        self.has_alpha = has_alpha


class _StubExportConfig:
    def __init__(self, include_audio=False, has_alpha=False):
        self.include_audio = include_audio
        self.has_alpha = has_alpha


def _raw_source_metadata(
    frames: int = 96,
    width: int = 1920,
    height: int = 1080,
    pixel_format: str = "topos_cfa12le",
    bit_depth: int = 12,
):
    return {
        "codec": "topos",
        "pixel_format": pixel_format,
        "bit_depth": bit_depth,
        "width": width,
        "height": height,
        "fps": 24.0,
        "fps_num": 24,
        "fps_den": 1,
        "total_frames": frames,
    }


def _make(sequence=None, project=None, config=None, encoder_config=None,
          start=0, end=48):
    from src.features.edit.core.timeline_render_plan import (
        build_direct_topos_delivery_plan,
    )

    if sequence is None:
        sequence = _StubSequence([_StubTrack([_StubClip(duration=48)])])
    if project is None:
        project = _StubProject({"a1": _StubAsset(_raw_source_metadata())})
    if config is None:
        config = _StubExportConfig()
    if encoder_config is None:
        encoder_config = _StubEncoderConfig(pix_fmt="topos_cfa12le")
    return build_direct_topos_delivery_plan(
        sequence, project, config, encoder_config, start, end)


# ---------------------------------------------------------------------------
# eligibility（不依赖 native 库）
# ---------------------------------------------------------------------------


class TestRawDirectDeliveryEligibility:
    """CFA 直出判定域：同位深同尺寸 RAW→RAW，其余一律拒绝。"""

    def _raw(self, *, src_fmt="topos_cfa12le", src_bd=12,
             out_fmt="topos_cfa12le", width=1920, height=1080,
             out_width=None, out_height=None, has_alpha=False, effects=()):
        md = _raw_source_metadata(pixel_format=src_fmt, bit_depth=src_bd,
                                  width=width, height=height)
        project = _StubProject({"a1": _StubAsset(md)})
        enc = _StubEncoderConfig(pix_fmt=out_fmt,
                                 width=out_width or width,
                                 height=out_height or height)
        seq = _StubSequence([_StubTrack([_StubClip(duration=48,
                                                   effects=effects)])])
        cfg = _StubExportConfig(has_alpha=has_alpha)
        return _make(sequence=seq, project=project, config=cfg,
                     encoder_config=enc)

    def test_raw_identity_eligible(self):
        plan, reason = self._raw()
        assert plan is not None, reason
        assert plan.source_color_layout == "cfa"
        assert plan.target_color_layout == "cfa"
        assert plan.source_bit_depth == 12
        assert plan.target_bit_depth == 12
        assert plan.requires_planar_conversion is False
        assert plan.decoder_resize is False

    def test_raw_bit_depth_mismatch_rejected(self):
        plan, reason = self._raw(out_fmt="topos_cfa16le")
        assert plan is None
        assert reason == "raw_bit_depth_mismatch:12:16"

    def test_raw_size_mismatch_rejected(self):
        plan, reason = self._raw(out_width=1280, out_height=800)
        assert plan is None
        assert reason == "raw_output_size_mismatch"

    def test_raw_source_to_planar_output_rejected(self):
        """RAW 源 → planar 输出必须走渲染车道（debayer 属像素级处理）。"""
        plan, reason = self._raw(out_fmt="yuv422p10le")
        assert plan is None
        assert reason == "raw_source_requires_raw_output:topos_cfa12le"

    def test_planar_source_to_raw_output_rejected(self):
        """planar 源 → RAW 输出不可逆（debayer 不可逆），拒绝。"""
        plan, reason = self._raw(src_fmt="yuv422p10le", src_bd=10)
        assert plan is None
        assert reason == "raw_output_requires_raw_source:yuv422p10le"

    def test_raw_alpha_requested_rejected(self):
        plan, reason = self._raw(has_alpha=True)
        assert plan is None
        assert reason == "alpha_requested"

    def test_raw_effects_rejected(self):
        plan, reason = self._raw(effects=("blur",))
        assert plan is None
        assert reason == "clip_effects_present"


# ---------------------------------------------------------------------------
# 端到端（需 native 库）
# ---------------------------------------------------------------------------


def _cfa_planes(seed: int, w: int, h: int, bd: int = 12):
    rng = np.random.default_rng(seed)
    pw, ph = (w + 1) // 2, (h + 1) // 2
    full = (1 << bd) - 1
    base = rng.integers(0, 1 << (bd - 2), size=(ph, pw), dtype=np.uint16)
    # 相位平面间给出可分辨的直流偏移（full/8 阶梯），平面身份可对拍
    return [np.clip(base.astype(np.int32) + full * k // 8, 0, full)
            .astype("<u2") for k in (0, 1, 2, 6)]


def _write_traw_source(path: Path, frames: int, w: int, h: int,
                       bd: int = 12, crf: int = 59):
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.media.frame import DecodedFrame

    planes_out = []
    enc = ToposVideoEncoder(str(path), SimpleNamespace(
        codec="topos", width=w, height=h, fps=24.0,
        pix_fmt=f"topos_cfa{bd}le", profile="raw", crf=crf))
    enc.open()
    for i in range(frames):
        planes = tuple(_cfa_planes(i, w, h, bd))
        planes_out.append(planes)
        enc.encode_frame(DecodedFrame(
            frame_index=i, pts=i, time_seconds=i / 24.0, data=None,
            planes=planes, pixel_format=f"topos_cfa{bd}le",
            dtype=np.dtype(np.uint16), width=w, height=h))
    enc.close()
    return planes_out


def _build_raw_timeline(raw_src: Path, frames: int, w: int, h: int):
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

    project = EditProject.create(name="raw-direct-delivery")
    metadata = {"fps": 24.0, "total_frames": frames}
    metadata.update(probe_media_metadata(str(raw_src)))
    metadata["total_frames"] = frames
    assert metadata["codec"] == "topos"
    assert metadata["pixel_format"].startswith("topos_cfa")
    asset = MediaAsset(
        asset_id="asset-raw", path=str(raw_src),
        media_type="video", display_name="raw-direct",
        metadata=metadata,
    )
    project = project.with_media_asset_added(asset)
    clip = ClipInstance(
        clip_id="clip-raw", asset_id=asset.asset_id, track_id="v1",
        timeline_start=0, timeline_duration=frames,
        source_start=0, source_duration=frames,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    return project, TimelineSequence(
        timeline_id="tl-raw", name="RawDirect",
        fps=Rational(24, 1), width=w, height=h,
        video_tracks=(v1,), audio_tracks=(),
    )


@pytest.mark.skipif(not _lib_available(), reason="libtopos_codec 未构建")
class TestRawVideoDirectExport:
    """M4-R2 端到端：TRAW MOV 源 → Topos RAW 直出。"""

    def test_raw16_direct_export(self, tmp_path):
        from src.features.edit.core.timeline_export import (
            ExportCodec, ExportRangeType, ExportSource,
            TimelineExportConfig, create_timeline_exporter,
        )
        from src.shared.media.topos_source import ToposMediaSource

        w, h, frames = 640, 360, 4
        raw_src = tmp_path / "raw16_src.mov"
        src_planes = _write_traw_source(raw_src, frames, w, h,
                                       bd=16, crf=72)
        project, sequence = _build_raw_timeline(raw_src, frames, w, h)
        output = tmp_path / "raw16_direct.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.TOPOS_RAW,
            codec_profile="raw16-4",
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_raw_cpu_encode"

        source = ToposMediaSource(str(output))
        try:
            source.open()
            meta = source.probe()
            assert meta.pixel_format == "topos_cfa16le"
            assert meta.total_frames == frames
            for i in range(frames):
                frame = source.read_frame(i)
                assert frame.pixel_format == "topos_cfa16le"
                assert len(frame.planes) == 4
                for phase in range(4):
                    actual = np.asarray(frame.planes[phase],
                                        dtype=np.int32)
                    expected = np.asarray(src_planes[i][phase],
                                          dtype=np.int32)
                    assert actual.shape == expected.shape
                    mae = float(np.abs(actual - expected).mean())
                    assert mae < 0.01 * (1 << 16), (i, phase, mae)
        finally:
            source.close()

    def test_raw_direct_export(self, tmp_path):
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        w, h, frames = 1920, 1080, 8
        raw_src = tmp_path / "raw_src.mov"
        src_planes = _write_traw_source(raw_src, frames, w, h)

        project, sequence = _build_raw_timeline(raw_src, frames, w, h)
        output = tmp_path / "raw_direct.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.TOPOS_RAW,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_raw_cpu_encode"

        from src.shared.media.topos_source import ToposMediaSource

        source = ToposMediaSource(str(output))
        try:
            source.open()
            meta = source.probe()
            assert meta.codec_name == "topos"
            assert meta.pixel_format == "topos_cfa12le"
            assert meta.codec_profile == "Topos RAW"
            assert meta.total_frames == frames
            assert meta.width == w and meta.height == h
            # 相位平面对拍（qp59 有损：均值漂移须有界）
            for i in range(frames):
                frame = source.read_frame(i)
                assert frame.pixel_format == "topos_cfa12le"
                assert len(frame.planes) == 4
                pw, ph = (w + 1) // 2, (h + 1) // 2
                assert frame.planes[0].shape == (ph, pw)
                assert frame.width == w and frame.height == h
                for p in range(4):
                    got = float(np.asarray(frame.planes[p],
                                           dtype=np.float64).mean())
                    want = float(np.asarray(src_planes[i][p],
                                            dtype=np.float64).mean())
                    assert abs(got - want) < 0.01 * (1 << 12), (i, p, got, want)
        finally:
            source.close()

    def test_raw_bit_depth_change_falls_back_or_rejects(self, tmp_path):
        """12-bit RAW 源 → 16-bit RAW 输出：直出判定拒绝（fail-closed），
        普通路径的 packed 兜底同样显式拒绝（不得静默产出）。"""
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        w, h, frames = 640, 360, 4
        raw_src = tmp_path / "raw_src12.mov"
        _write_traw_source(raw_src, frames, w, h, bd=12, crf=59)
        project, sequence = _build_raw_timeline(raw_src, frames, w, h)
        output = tmp_path / "raw16_out.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.TOPOS_RAW,
            codec_profile="raw16-4",
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        # 两条路径都必须显式失败（直出判定拒绝 + 普通路径 CFA 直出拒绝），
        # 不允许静默产出错颜色文件
        assert outcome.success is False
        assert not output.exists()


# ---------------------------------------------------------------------------
# W1：_pixel_format_name 静默错标排查（F1 同款纠偏回归）
# ---------------------------------------------------------------------------


class TestPixelFormatNaming:
    def test_bd16_names_registered(self) -> None:
        """批 4 位深：bd16 组合必须映射到真实注册名（此前静默冒充
        yuv422p10le——16-bit 流被按 10-bit 解释）。"""
        from src.shared.media.topos_source import _pixel_format_name
        from src.shared.media.frame import PIXEL_FORMATS

        for pf, bd, name in ((0, 16, "yuv422p16le"),
                             (1, 16, "yuv444p16le"),
                             (2, 16, "gbrp16le"),
                             (3, 12, "topos_cfa12le"),
                             (3, 16, "topos_cfa16le")):
            got = _pixel_format_name(0, 0, bd, pf)
            assert got == name, (pf, bd, got)
            assert name in PIXEL_FORMATS, name

    def test_unknown_combo_rejected(self) -> None:
        from src.shared.media.topos_source import _pixel_format_name

        with pytest.raises(ValueError, match="错标"):
            _pixel_format_name(0, 0, 14, 0)


@pytest.mark.skipif(not _lib_available(), reason="libtopos_codec 未构建")
class TestRawDirectTrwmPassthrough:
    """RC1：RAW→RAW 直出透传源 TRWM（as-shot 基线随流走）。"""

    def test_trwm_preserved_through_direct_export(self, tmp_path):
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )
        from src.shared.codec.topos_trwm import decode_trwm, encode_trwm

        w, h, frames = 640, 360, 4
        raw_src = tmp_path / "raw_src.mov"
        _write_traw_source(raw_src, frames, w, h)
        # 给源补写 trwm：重新编码不划算——直接在编码器构造时带上
        # （_write_traw_source 不带 trwm，此处单独生成带 trwm 的源）
        from src.shared.export.topos_encoder import ToposVideoEncoder
        from src.shared.media.frame import DecodedFrame

        payload = encode_trwm(1.7, 1.0, 1.5, 0.25, 32.0, "GBRG", 2)
        rng = np.random.default_rng(4)
        pw, ph = (w + 1) // 2, (h + 1) // 2
        src_planes = []
        enc = ToposVideoEncoder(str(raw_src), SimpleNamespace(
            codec="topos", width=w, height=h, fps=24.0,
            pix_fmt="topos_cfa12le", profile="raw", crf=59),
            raw_meta_payload=payload)
        enc.open()
        for i in range(frames):
            planes = tuple(np.full((ph, pw), v, dtype="<u2")
                           for v in (rng.integers(200, 600), 400, 400, 200))
            src_planes.append(planes)
            enc.encode_frame(DecodedFrame(
                frame_index=i, pts=i, time_seconds=i / 24.0, data=None,
                planes=planes, pixel_format="topos_cfa12le",
                dtype=np.dtype(np.uint16), width=w, height=h))
        enc.close()

        project, sequence = _build_raw_timeline(raw_src, frames, w, h)
        output = tmp_path / "raw_direct.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.TOPOS_RAW,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"

        from src.shared.codec.topos_binding import (
            ToposCodec, ToposMovieFile,
        )

        movie = ToposMovieFile(ToposCodec(), str(output))
        try:
            back = decode_trwm(movie.read_raw_meta())
            assert back["cfa_layout"] == "GBRG"
            assert back["sensor_matrix_id"] == 2
            assert back["wb_gain_r"] == pytest.approx(1.7, abs=1e-6)
        finally:
            movie.close()
