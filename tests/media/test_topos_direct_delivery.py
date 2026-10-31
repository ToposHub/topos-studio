"""T-P1-10：TOPOS identity 直通快路径聚焦测试。

覆盖：
1. `build_direct_topos_delivery_plan` 严格 eligibility（正例 + 各拒绝原因）；
2. 真实素材直通导出：2K_topos_422_Proxy.mov → topos_proxy，
   delivery_path=topos_direct_planar_cpu_encode、输出 native probe、
   首中尾帧 Y 平面统计对拍、telemetry 标记；
3. 取消语义：中途取消无输出文件 / 无 .topos-tmp 残留；
4. 不满足条件回落普通路径（single effects clip → normal path）。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.clip_params import CropParams, TransformParams
from src.features.edit.core.timeline_render_plan import (
    build_direct_topos_delivery_plan,
)

# T-P1-11：native 编码阶段计数器需 profiling 开关（M6 dev API）。setdefault
# 在 pytest 收集期（任何测试触发 native 加载之前）生效；生产路径不强制开启。
os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")

TEST_ROOT = Path(__file__).resolve().parents[2]
from tests.real_media import canonical_video, require_video  # noqa: E402

TOPOS_SOURCE = TEST_ROOT / "tests/test_videos" / canonical_video("2K_topos_proxy.mov")
TOPOS_4444_SOURCE = TEST_ROOT / "tests/test_videos/2K_topos_4444.mov"
TOPOS_4K_SOURCE = TEST_ROOT / "tests/test_videos" / canonical_video("4K_topos_hq.mov")
TOPOS_4K_4444_SOURCE = TEST_ROOT / "tests/test_videos/4K_topos_4444.mov"


@pytest.fixture(scope="module", autouse=True)
def _require_real_media():
    """C02：real-media 作业（TOPOS_REAL_MEDIA_STRICT=1）缺素材直接 fail。"""
    require_video(TOPOS_SOURCE.name, TOPOS_4K_SOURCE.name)


# ---------------------------------------------------------------------------
# stub 构造
# ---------------------------------------------------------------------------

def _source_metadata(
    frames: int = 96,
    width: int = 1920,
    height: int = 1080,
    pixel_format: str = "yuv422p10le",
    bit_depth: int = 10,
):
    return {
        "codec": "topos",
        "pixel_format": pixel_format,
        "bit_depth": bit_depth,
        "width": width,
        "height": height,
        "fps": 25.0,
        "fps_num": 25,
        "fps_den": 1,
        "total_frames": frames,
    }


def test_direct_444_to_422_reuses_native_chroma_buffers() -> None:
    """连续直通帧应复用 native 444→422 的 U/V 输出缓冲。"""
    from types import SimpleNamespace

    from src.features.edit.core.timeline_export import TimelineExporter
    from src.shared.media.frame import DecodedFrame

    class _NativePlanarConverter:
        def __init__(self) -> None:
            self.calls = []

        def convert_yuv444_to_422(
            self, u, v, *, bit_depth, max_workers, planes_out=None,
        ):
            del bit_depth, max_workers
            self.calls.append(planes_out)
            if planes_out is None:
                planes_out = [
                    np.empty((u.shape[0], (u.shape[1] + 1) // 2), dtype="<u2"),
                    np.empty((v.shape[0], (v.shape[1] + 1) // 2), dtype="<u2"),
                ]
            for src, dst in zip((u, v), planes_out):
                padded = (np.pad(src, ((0, 0), (0, 1)), mode="edge")
                          if src.shape[1] & 1 else src)
                np.add(padded[:, 0::2], padded[:, 1::2], out=dst)
                np.add(dst, 1, out=dst)
                np.right_shift(dst, 1, out=dst)
            return planes_out

    plan = SimpleNamespace(
        source_bit_depth=10,
        target_bit_depth=10,
        source_color_layout="yuv444",
        target_color_layout="yuv422",
        source_alpha_depth=16,
        target_alpha_depth=None,
    )
    converter = _NativePlanarConverter()
    holder = []

    def make_frame(seed: int) -> DecodedFrame:
        y = np.full((3, 5), seed, dtype="<u2")
        u = np.arange(15, dtype="<u2").reshape(3, 5) + seed
        v = u + 10
        alpha = np.full((3, 5), 65535, dtype="<u2")
        return DecodedFrame(
            frame_index=seed,
            pts=seed,
            time_seconds=seed / 25.0,
            planes=(y, u, v, alpha),
            pixel_format="topos_yuva444p10a16",
            width=5,
            height=3,
        )

    first = TimelineExporter._prepare_direct_topos_frame(
        plan, make_frame(1), converter, conversion_buffers=holder)
    first_u, first_v = first.planes[1:3]
    assert converter.calls == [None]
    assert holder[0] is first_u
    assert holder[1] is first_v

    second = TimelineExporter._prepare_direct_topos_frame(
        plan, make_frame(2), converter, conversion_buffers=holder)
    assert converter.calls[1] is holder
    assert second.planes[1] is first_u
    assert second.planes[2] is first_v


def test_direct_444_to_422_reuses_numpy_fallback_buffers() -> None:
    """旧 native helper 不可用时，NumPy 回退也应复用 U/V 输出缓冲。"""
    from types import SimpleNamespace

    from src.features.edit.core.timeline_export import TimelineExporter
    from src.shared.media.frame import DecodedFrame

    class _UnavailablePlanarConverter:
        def convert_yuv444_to_422(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("old TOPOS DLL")

    plan = SimpleNamespace(
        source_bit_depth=10,
        target_bit_depth=10,
        source_color_layout="yuv444",
        target_color_layout="yuv422",
        source_alpha_depth=16,
        target_alpha_depth=None,
    )
    holder = []

    def make_frame(seed: int) -> DecodedFrame:
        y = np.full((3, 5), seed, dtype="<u2")
        u = np.arange(15, dtype="<u2").reshape(3, 5) + seed
        v = u + 10
        alpha = np.full((3, 5), 65535, dtype="<u2")
        return DecodedFrame(
            frame_index=seed,
            pts=seed,
            time_seconds=seed / 25.0,
            planes=(y, u, v, alpha),
            pixel_format="topos_yuva444p10a16",
            width=5,
            height=3,
        )

    converter = _UnavailablePlanarConverter()
    first = TimelineExporter._prepare_direct_topos_frame(
        plan, make_frame(1), converter, conversion_buffers=holder)
    first_u, first_v = first.planes[1:3]

    second = TimelineExporter._prepare_direct_topos_frame(
        plan, make_frame(2), converter, conversion_buffers=holder)
    assert holder[0] is first_u
    assert holder[1] is first_v
    assert second.planes[1] is first_u
    assert second.planes[2] is first_v


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
    def __init__(self, metadata):
        self.media_type = "video"
        self.path = str(TOPOS_SOURCE)
        self.metadata = metadata


class _StubProject:
    def __init__(self, assets):
        self._assets = assets

    def get_asset_by_id(self, asset_id):
        return self._assets.get(asset_id)


class _StubEncoderConfig:
    def __init__(self, codec="topos", pix_fmt="yuv422p10le",
                 width=1920, height=1080, fps=25.0, has_alpha=False):
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


def _make(sequence=None, project=None, config=None, encoder_config=None,
          start=0, end=48):
    if sequence is None:
        sequence = _StubSequence([_StubTrack([_StubClip(duration=48)])])
    if project is None:
        project = _StubProject({"a1": _StubAsset(_source_metadata())})
    if config is None:
        config = _StubExportConfig()
    if encoder_config is None:
        encoder_config = _StubEncoderConfig()
    return build_direct_topos_delivery_plan(
        sequence, project, config, encoder_config, start, end)


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------


class TestDirectDeliveryEligibility:
    def test_single_identity_clip_eligible(self):
        plan, reason = _make()
        assert plan is not None, reason
        assert plan.asset_path == str(TOPOS_SOURCE)
        assert plan.frames == 48
        assert plan.source_start == 0
        assert plan.source_pixel_format == "yuv422p10le"
        assert plan.requires_planar_conversion is False

    def test_multiple_clips_rejected(self):
        sequence = _StubSequence([_StubTrack([
            _StubClip(clip_id="c1", duration=24),
            _StubClip(clip_id="c2", timeline_start=24, duration=24),
        ])])
        plan, reason = _make(sequence=sequence)
        assert plan is None
        assert reason == "video_clip_count:2"

    def test_effects_rejected(self):
        sequence = _StubSequence([_StubTrack([_StubClip(effects=("blur",))])])
        plan, reason = _make(sequence=sequence)
        assert plan is None
        assert reason == "clip_effects_present"

    def test_output_size_mismatch_rejected(self):
        plan, reason = _make(encoder_config=_StubEncoderConfig(width=1280,
                                                               height=800))
        assert plan is None
        assert reason == "output_size_mismatch"

    def test_non_topos_source_rejected(self):
        project = _StubProject({"a1": _StubAsset(_source_metadata())})
        project._assets["a1"].metadata["codec"] = "prores"
        plan, reason = _make(project=project)
        assert plan is None
        assert reason.startswith("source_not_topos_planar:prores")

    def test_audio_no_longer_rejected(self):
        """v1.1 音频轨（plan M-A6）：include_audio 不再拦截直通——
        导出循环有音频时渲染母带并喂入编码器。"""
        plan, reason = _make(config=_StubExportConfig(include_audio=True))
        assert plan is not None
        assert reason == ""

    def test_alpha_rejected(self):
        plan, reason = _make(config=_StubExportConfig(has_alpha=True))
        assert plan is None
        assert reason == "alpha_requested"

    def test_transitions_rejected(self):
        sequence = _StubSequence([
            _StubTrack([_StubClip(duration=48)], transitions=("dissolve",))])
        plan, reason = _make(sequence=sequence)
        assert plan is None
        assert reason == "transitions_present"

    def test_yuv_to_gbr_format_mismatch_rejected(self):
        plan, reason = _make(
            encoder_config=_StubEncoderConfig(pix_fmt="gbrp10le"))
        assert plan is None
        assert reason == "source_format_mismatch:yuv422p10le:gbrp10le"

    def test_yuv_chroma_layout_conversion_is_eligible(self):
        plan, reason = _make(
            encoder_config=_StubEncoderConfig(pix_fmt="yuv444p10le"))
        assert plan is not None, reason
        assert plan.source_color_layout == "yuv422"
        assert plan.target_color_layout == "yuv444"
        assert plan.requires_planar_conversion is True

    def test_matching_4444_alpha_planar_is_eligible(self):
        source_fmt = "topos_yuva444p10a16"
        project = _StubProject({
            "a1": _StubAsset(_source_metadata(
                pixel_format=source_fmt, bit_depth=10)),
        })
        encoder_config = _StubEncoderConfig(
            pix_fmt=source_fmt, has_alpha=True)
        plan, reason = _make(
            project=project, encoder_config=encoder_config,
            config=_StubExportConfig(has_alpha=True))
        assert plan is not None, reason
        assert plan.source_pixel_format == source_fmt
        assert plan.source_bit_depth == 10

    def test_alpha_depth_only_mismatch_is_eligible(self):
        source_fmt = "topos_yuva444p10a16"
        project = _StubProject({
            "a1": _StubAsset(_source_metadata(
                pixel_format=source_fmt, bit_depth=10)),
        })
        # 裸 yuv444 + has_alpha 使用 Topos 默认 a12；颜色平面仍完全一致，
        # 因此只需在直通循环量化 Alpha，不应回落到普通渲染。
        plan, reason = _make(
            project=project,
            encoder_config=_StubEncoderConfig(
                pix_fmt="yuv444p10le", has_alpha=True),
            config=_StubExportConfig(has_alpha=True),
        )
        assert plan is not None, reason
        assert plan.source_alpha_depth == 16
        assert plan.target_alpha_depth == 12
        assert plan.requires_planar_conversion is True

    def test_matching_12bit_444_planar_is_eligible(self):
        source_fmt = "yuv444p12le"
        project = _StubProject({
            "a1": _StubAsset(_source_metadata(
                pixel_format=source_fmt, bit_depth=12)),
        })
        plan, reason = _make(
            project=project,
            encoder_config=_StubEncoderConfig(pix_fmt=source_fmt),
        )
        assert plan is not None, reason
        assert plan.source_bit_depth == 12

    def test_matching_layout_allows_color_depth_conversion(self):
        project = _StubProject({
            "a1": _StubAsset(_source_metadata(
                pixel_format="yuv444p10le", bit_depth=10)),
        })
        plan, reason = _make(
            project=project,
            encoder_config=_StubEncoderConfig(pix_fmt="yuv444p12le"),
        )
        assert plan is not None, reason
        assert plan.source_bit_depth == 10
        assert plan.target_bit_depth == 12
        assert plan.requires_planar_conversion is True

    def test_non_topos_codec_rejected(self):
        plan, reason = _make(encoder_config=_StubEncoderConfig(codec="h264",
                                                               pix_fmt="yuv420p"))
        assert plan is None
        assert reason == "codec_not_topos:h264"

    def test_opacity_rejected(self):
        sequence = _StubSequence([_StubTrack([_StubClip(opacity=0.5)])])
        plan, reason = _make(sequence=sequence)
        assert plan is None
        assert reason == "non_identity_opacity"


# ---------------------------------------------------------------------------
# 真实素材直通导出
# ---------------------------------------------------------------------------


def _build_real_timeline(
        frames: int,
        source: Path = TOPOS_SOURCE,
        timeline_size: tuple[int, int] | None = None):
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

    project = EditProject.create(name="direct-topos-delivery")
    metadata = {"fps": 25.0, "total_frames": frames}
    metadata.update(probe_media_metadata(str(source)))
    metadata["total_frames"] = frames
    assert metadata["codec"] == "topos"
    asset = MediaAsset(
        asset_id="asset-direct", path=str(source),
        media_type="video", display_name="direct",
        metadata=metadata,
    )
    project = project.with_media_asset_added(asset)
    clip = ClipInstance(
        clip_id="clip-direct", asset_id=asset.asset_id, track_id="v1",
        timeline_start=0, timeline_duration=frames,
        source_start=0, source_duration=frames,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    sequence = TimelineSequence(
        timeline_id="tl-direct", name="Direct",
        fps=Rational(25, 1), width=int(metadata["width"]),
        height=int(metadata["height"]),
        video_tracks=(v1,), audio_tracks=(),
    )
    if timeline_size is not None:
        sequence = TimelineSequence(
            timeline_id=sequence.timeline_id, name=sequence.name,
            fps=sequence.fps, width=int(timeline_size[0]),
            height=int(timeline_size[1]), video_tracks=sequence.video_tracks,
            audio_tracks=sequence.audio_tracks,
        )
    return project, sequence


def _build_alpha_4444_source(tmp_path, frames: int = 24,
                             width: int = 320, height: int = 180) -> Path:
    """编码一个真实的 4444+alpha topos 文件（冻结语料的 4444 为 opaque）。

    2026-09-22 基线清洗后冻结语料的 4444 文件不带 alpha（alpha_mode=0，
    has_alpha=False 直通编码）。alpha 直通交付的正例需要真正带 alpha 的
    源——这里用产品 ToposVideoEncoder 现编一个小文件（yuv444p10 + a16
    无损 alpha，与历史 alpha 语料同构），走真实编码/复用路径。
    """
    import numpy as np
    from dataclasses import replace as _dc_replace
    from src.shared.codec.color_metadata_config import ColorMetadataConfig
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.metadata import Rational

    ys, xs = np.mgrid[0:height, 0:width]

    def _frame(idx: int):
        # yuv444p10 平面为原始 10-bit 码值（uint16 容器，不预移位）；
        # alpha 平面为 16-bit 全量程（a16 无损语义）
        y = ((xs * 971 + ys * 7 + idx * 131) % 1023).astype(np.uint16)
        u = np.full((height, width), 512, np.uint16)
        v = np.full((height, width), 512, np.uint16)
        ramp = (xs * 65535) // max(1, width - 1)
        a = ((ramp + idx * 4096) % 65536).astype(np.uint16)
        return y, u, v, a

    out = tmp_path / "alpha4444_src.mov"
    config = FFmpegEncoderConfig(
        codec="topos", width=width, height=height,
        fps=Rational(25, 1), pix_fmt="topos_yuva444p10a16",
        profile="4444", has_alpha=True,
        color_metadata=ColorMetadataConfig.sdr(),
    )
    enc = ToposVideoEncoder(str(out), config, slice_threads=2)
    enc.open()
    try:
        for i in range(frames):
            y, u, v, a = _frame(i)
            enc.encode_frame(type("P", (), {
                "planes": (y, u, v, a), "pixel_format": "topos_yuva444p10a16",
                "width": width, "height": height, "frame_index": i,
                "is_planar": True,
            })())
    finally:
        enc.close()
    return out


def _probe_topos_output(path: str) -> dict:
    from src.shared.media.topos_source import ToposMediaSource

    source = ToposMediaSource(path)
    try:
        source.open()
        meta = source.probe()
        return {
            "codec": meta.codec_name,
            "pix_fmt": meta.pixel_format,
            "width": meta.width,
            "height": meta.height,
            "frames": meta.total_frames,
            "fps": float(meta.fps),
            "_source": source,
        }
    except Exception:
        source.close()
        raise


def _y_plane_stats(source, frame_index: int) -> tuple:
    frame = source.read_frame(frame_index)
    planes = getattr(frame, "planes", None)
    assert planes and len(planes) >= 3
    y = planes[0]
    arr = np.frombuffer(bytes(y), dtype="<u2") if not isinstance(
        y, np.ndarray) else y
    return float(arr.mean()), float(arr.min()), float(arr.max())


class TestRealDirectDelivery:
    def test_direct_export_probe_and_pixels(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TOPOS_EXPORT_GPU_RENDER", raising=False)
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        frames = 48
        project, sequence = _build_real_timeline(frames)
        output = tmp_path / "direct.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.TOPOS_PROXY,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_planar_cpu_encode"

        # 输出 probe（native）
        probe = _probe_topos_output(str(output))
        source = probe.pop("_source")
        try:
            assert probe["codec"] == "topos"
            assert probe["pix_fmt"] == "yuv422p10le"
            assert (probe["width"], probe["height"]) == (1920, 1080)
            assert probe["fps"] == pytest.approx(25.0)
            assert probe["frames"] == frames

            # 首中尾帧 Y 平面对拍（直通 vs 源直读）：统计量一致（bit 级或近 bit）
            for src_idx in (0, frames // 2, frames - 1):
                out_stats = _y_plane_stats(source, src_idx)
                src_stats_ref = ToposFrameRef.read(TOPOS_SOURCE, src_idx)
                assert out_stats[0] == pytest.approx(src_stats_ref[0],
                                                     abs=0.5)
                assert out_stats[2] <= 1023
        finally:
            source.close()

        # telemetry：delivery_path 标记 + decode backend + 不虚报 GPU
        telemetry = outcome.stage_telemetry
        assert telemetry is not None
        assert telemetry["delivery_path"] == "topos_direct_planar_cpu_encode"
        by_name = {s["name"]: s for s in telemetry["stages"]}
        assert by_name["decode"]["backend"] == "topos_native"
        assert by_name["decode"]["sample_count"] == frames
        assert by_name["encode"]["backend"] == "topos"
        assert telemetry["full_gpu"] is False
        # T-P1-11：直通路径携带 native 编码阶段统计（CPU/墙钟分组）。
        # native 计数器仅在 TOPOS_CODEC_PROFILE=1 时累加（见模块顶部
        # setdefault——收集在全部测试运行前生效）；未开启时导出方保持
        # None（零值 ≠ 测量）。
        native_stats = telemetry.get("native_encode_stage_stats")
        assert isinstance(native_stats, dict)
        assert native_stats["worker_cpu_ns_total"] > 0
        assert set(native_stats["worker_cpu_ns"]) >= {"entropy_ns", "fill_ns"}
        assert "wall_ns" in native_stats

    @pytest.mark.skipif(
        not TOPOS_4444_SOURCE.is_file(), reason="真实 TOPOS 4444 素材缺失")
    def test_opaque_4444_alpha_delivery_fails_closed(
            self, tmp_path, monkeypatch):
        """C02：冻结语料 4444 为 opaque → alpha 交付必须拒绝直通
        （fail-closed），回落普通 render 路径并记录原因。"""
        monkeypatch.delenv("TOPOS_EXPORT_GPU_RENDER", raising=False)
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        frames = 4
        project, sequence = _build_real_timeline(
            frames, source=TOPOS_4444_SOURCE)
        output = tmp_path / "opaque-4444-alpha.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            codec=ExportCodec.TOPOS_4444,
            has_alpha=True,
            include_audio=False,
            output_path=str(output),
        )
        outcome = create_timeline_exporter(
            project=project, sequence=sequence).export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        # 不走直通快路；telemetry 记录拒绝原因
        assert outcome.delivery_path != "topos_direct_planar_cpu_encode"
        telemetry = outcome.stage_telemetry
        assert telemetry is not None
        assert telemetry["delivery_path"].startswith(
            "topos_direct_not_eligible:alpha_format_mismatch")

    def test_default_4444_alpha_uses_direct_planar_path(
            self, tmp_path, monkeypatch):
        """裸 yuv444+alpha 源的默认交付也不得回落到 GPU render。

        C02：历史语料的 alpha 4444 已被冻结语料（opaque）取代；正例改用
        测试内现编的真实 alpha 4444 文件（_build_alpha_4444_source）。"""
        monkeypatch.delenv("TOPOS_EXPORT_GPU_RENDER", raising=False)
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        frames = 8
        alpha_source = _build_alpha_4444_source(tmp_path, frames=frames)
        project, sequence = _build_real_timeline(
            frames, source=alpha_source)
        output = tmp_path / "direct-4444-default.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            codec=ExportCodec.TOPOS_4444,
            has_alpha=True,
            include_audio=False,
            output_path=str(output),
        )
        outcome = create_timeline_exporter(
            project=project, sequence=sequence).export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_planar_cpu_encode"

        probe = _probe_topos_output(str(output))
        source = probe.pop("_source")
        try:
            assert probe["pix_fmt"] == "topos_yuva444p10a12"
            assert probe["frames"] == frames
        finally:
            source.close()

        telemetry = outcome.stage_telemetry
        assert telemetry is not None
        by_name = {s["name"]: s for s in telemetry["stages"]}
        assert by_name["render"]["sample_count"] == 0
        assert by_name["decode"]["sample_count"] == frames
        assert by_name["encode"]["sample_count"] == frames

    def test_4444_to_4444xq_uses_planar_depth_conversion(
            self, tmp_path, monkeypatch):
        """同一 4:4:4 布局的 10→12-bit 交付跳过 RGB/GPU 往返。

        C02：改用测试内现编的真实 alpha 4444 源（冻结语料 4444 为 opaque）。"""
        monkeypatch.delenv("TOPOS_EXPORT_GPU_RENDER", raising=False)
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        frames = 8
        alpha_source = _build_alpha_4444_source(tmp_path, frames=frames)
        project, sequence = _build_real_timeline(
            frames, source=alpha_source)
        output = tmp_path / "direct-4444-to-xq.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            codec=ExportCodec.TOPOS_4444XQ,
            has_alpha=True,
            include_audio=False,
            output_path=str(output),
        )
        outcome = create_timeline_exporter(
            project=project, sequence=sequence).export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_planar_cpu_encode"

        probe = _probe_topos_output(str(output))
        source = probe.pop("_source")
        try:
            assert probe["pix_fmt"] == "topos_yuva444p12a12"
            assert probe["frames"] == frames
        finally:
            source.close()
        telemetry = outcome.stage_telemetry
        assert telemetry is not None
        by_name = {s["name"]: s for s in telemetry["stages"]}
        assert by_name["render"]["sample_count"] == 0

    def test_4444_to_422_uses_planar_chroma_resampling(
            self, tmp_path, monkeypatch):
        """4:4:4→4:2:2 只重采样 U/V 平面，不经过 RGB 合成。

        C02：改用测试内现编的真实 alpha 4444 源（冻结语料 4444 为 opaque）。"""
        monkeypatch.delenv("TOPOS_EXPORT_GPU_RENDER", raising=False)
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        frames = 8
        alpha_source = _build_alpha_4444_source(tmp_path, frames=frames)
        project, sequence = _build_real_timeline(
            frames, source=alpha_source)
        output = tmp_path / "direct-4444-to-422.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            codec=ExportCodec.TOPOS_PROXY,
            has_alpha=True,
            include_audio=False,
            output_path=str(output),
        )
        outcome = create_timeline_exporter(
            project=project, sequence=sequence).export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_planar_cpu_encode"

        probe = _probe_topos_output(str(output))
        source = probe.pop("_source")
        try:
            assert probe["pix_fmt"].startswith("topos_yuva422p10a")
            assert probe["frames"] == frames
        finally:
            source.close()
        telemetry = outcome.stage_telemetry
        assert telemetry is not None
        by_name = {s["name"]: s for s in telemetry["stages"]}
        assert by_name["render"]["sample_count"] == 0

    def test_422_to_444_uses_planar_chroma_upsampling(
            self, tmp_path, monkeypatch):
        """4:2:2→4:4:4 用平面复制扩展色度，不走 RGB 往返。"""
        monkeypatch.delenv("TOPOS_EXPORT_GPU_RENDER", raising=False)
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        frames = 12
        project, sequence = _build_real_timeline(frames)
        output = tmp_path / "direct-422-to-444.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            codec=ExportCodec.TOPOS_4444,
            include_audio=False,
            output_path=str(output),
        )
        outcome = create_timeline_exporter(
            project=project, sequence=sequence).export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_planar_cpu_encode"

        probe = _probe_topos_output(str(output))
        source = probe.pop("_source")
        try:
            assert probe["pix_fmt"] == "yuv444p10le"
            assert probe["frames"] == frames
        finally:
            source.close()
        telemetry = outcome.stage_telemetry
        assert telemetry is not None
        by_name = {s["name"]: s for s in telemetry["stages"]}
        assert by_name["render"]["sample_count"] == 0

    @pytest.mark.skipif(
        not TOPOS_4K_SOURCE.is_file(), reason="真实 TOPOS 4K 素材缺失")
    def test_4k_source_to_2k_timeline_uses_native_resize_decode(
            self, tmp_path, monkeypatch):
        """4K→2K 等比例时间线直接由 native decoder 输出 2K 平面。"""
        monkeypatch.delenv("TOPOS_EXPORT_GPU_RENDER", raising=False)
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        frames = 12
        project, sequence = _build_real_timeline(
            frames, source=TOPOS_4K_SOURCE,
            timeline_size=(1920, 1080))
        output = tmp_path / "direct-4k-to-2k.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            codec=ExportCodec.TOPOS_PROXY,
            include_audio=False,
            output_path=str(output),
        )
        outcome = create_timeline_exporter(
            project=project, sequence=sequence).export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_planar_cpu_encode"

        probe = _probe_topos_output(str(output))
        source = probe.pop("_source")
        try:
            assert (probe["width"], probe["height"]) == (1920, 1080)
            assert probe["pix_fmt"] == "yuv422p10le"
            assert probe["frames"] == frames
        finally:
            source.close()
        telemetry = outcome.stage_telemetry
        assert telemetry is not None
        by_name = {s["name"]: s for s in telemetry["stages"]}
        assert by_name["render"]["sample_count"] == 0

    @pytest.mark.skipif(
        not TOPOS_4K_4444_SOURCE.is_file(),
        reason="真实 TOPOS 4K 4444 素材缺失")
    def test_4k_4444_to_2k_422_uses_scaled_decode_and_planar_delivery(
            self, tmp_path, monkeypatch):
        """4K 4444→2K 422 同时验证 native 缩放解码和 444→422 平面交付。"""
        monkeypatch.delenv("TOPOS_EXPORT_GPU_RENDER", raising=False)
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )

        frames = 8
        project, sequence = _build_real_timeline(
            frames,
            source=TOPOS_4K_4444_SOURCE,
            timeline_size=(1920, 1080),
        )
        output = tmp_path / "direct-4k-4444-to-2k-422.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            codec=ExportCodec.TOPOS_PROXY,
            include_audio=False,
            output_path=str(output),
        )
        outcome = create_timeline_exporter(
            project=project, sequence=sequence).export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == "topos_direct_planar_cpu_encode"

        probe = _probe_topos_output(str(output))
        source = probe.pop("_source")
        try:
            assert (probe["width"], probe["height"]) == (1920, 1080)
            assert probe["pix_fmt"] == "yuv422p10le"
            assert probe["frames"] == frames
        finally:
            source.close()

        telemetry = outcome.stage_telemetry
        assert telemetry is not None
        by_name = {s["name"]: s for s in telemetry["stages"]}
        assert by_name["render"]["sample_count"] == 0
        assert by_name["decode"]["sample_count"] == frames
        assert by_name["encode"]["sample_count"] == frames

    def test_cancel_leaves_no_residue(self, tmp_path, monkeypatch):
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            TimelineExporter,
            create_timeline_exporter,
        )
        from src.shared.export.topos_encoder import ToposVideoEncoder

        frames = 48
        project, sequence = _build_real_timeline(frames)
        output = tmp_path / "cancel.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.TOPOS_PROXY,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)

        real_encode = ToposVideoEncoder.encode_frame
        encode_calls = {"n": 0}

        def cancelling_encode(self, frame):
            encode_calls["n"] += 1
            if encode_calls["n"] >= 4:
                exporter.cancel_export()
            return real_encode(self, frame)

        monkeypatch.setattr(ToposVideoEncoder, "encode_frame",
                            cancelling_encode)
        outcome = exporter.export_timeline(config)
        assert outcome.cancelled is True
        assert outcome.success is False
        assert outcome.delivery_path == "topos_direct_planar_cpu_encode"
        assert not output.exists(), "取消后不得留下目标文件"
        assert not (tmp_path / "cancel.mov.topos-tmp").exists(), (
            "取消后不得留下 .topos-tmp 残留")


class ToposFrameRef:
    """源帧 Y 平面统计缓存（避免逐用例重复解码）。"""

    _cache: dict = {}

    @classmethod
    def read(cls, path: Path, frame_index: int) -> tuple:
        key = (str(path), frame_index)
        if key not in cls._cache:
            from src.shared.media.topos_source import ToposMediaSource

            source = ToposMediaSource(str(path))
            try:
                source.open()
                cls._cache[key] = _y_plane_stats(source, frame_index)
            finally:
                source.close()
        return cls._cache[key]


class TestIneligibleFallsBackToNormalPath:
    def test_effects_clip_uses_normal_path(self, tmp_path):
        """不满足 eligibility（带效果）→ 普通 render path 仍工作。"""
        from src.features.edit.core.timeline_export import (
            ExportCodec,
            ExportRangeType,
            ExportSource,
            TimelineExportConfig,
            create_timeline_exporter,
        )
        from src.features.edit.core.timeline_v2 import ClipInstance

        frames = 8
        project, sequence = _build_real_timeline(frames)
        # 给 clip 加一个效果 → 直通不适用，必须走普通 render path
        from src.features.edit.core.timeline_v2 import ClipInstance as _Clip

        clip = sequence.video_tracks[0].clips[0]
        mutated = _Clip(
            clip_id=clip.clip_id, asset_id=clip.asset_id, track_id="v1",
            timeline_start=0, timeline_duration=frames,
            source_start=0, source_duration=frames,
            effects=("blur",),
        )
        v1 = type(sequence.video_tracks[0])(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(mutated,))
        sequence = type(sequence)(
            timeline_id=sequence.timeline_id, name=sequence.name,
            fps=sequence.fps, width=1920, height=1080,
            video_tracks=(v1,), audio_tracks=(),
        )
        output = tmp_path / "normal.mov"
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.TOPOS_PROXY,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        outcome = exporter.export_timeline(config)
        assert outcome.success, f"export failed: {outcome.cause}"
        assert outcome.delivery_path == ""  # 普通路径不标记直通
        probe = _probe_topos_output(str(output))
        source = probe.pop("_source")
        try:
            assert probe["frames"] == frames
        finally:
            source.close()
