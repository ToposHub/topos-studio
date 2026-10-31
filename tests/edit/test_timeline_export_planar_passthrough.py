"""Strict no-op TOPOS planar timeline export coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from src.features.edit.core.timeline_export import (
    TimelineExporter,
    _topos_timeline_cpu_threads,
)
from src.shared.media.ffmpeg_source import _normalize_chroma_siting
from src.shared.media.frame import DecodedFrame, PlaneInfo
from src.shared.media.metadata import Rational, VideoMetadata


def _identity_layer(source_frame: int = 12) -> SimpleNamespace:
    clip = SimpleNamespace(
        clip_id="clip-1",
        asset_id="asset-1",
        is_enabled=True,
        effects=(),
        composite_id=None,
        render_cache_id=None,
        raw=None,
        freeze_source_frame=None,
        animated_params={},
        opacity=1.0,
        fade_in_duration=0,
        fade_out_duration=0,
        blend_mode=SimpleNamespace(value="normal"),
        speed=SimpleNamespace(
            speed=1.0, reverse=False, interpolation="nearest",
            speed_points=()),
        _source_pts_index=None,
        source_start=10,
        source_duration=20,
        timeline_start=10,
        timeline_duration=20,
        timeline_end=30,
        color_stack_id=None,
    )
    return SimpleNamespace(
        clip=clip,
        clip_id="clip-1",
        asset_id="asset-1",
        source_frame=source_frame,
        opacity=1.0,
        crop=(0.0, 0.0, 0.0, 0.0, 0.0),
        transform=(0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
    )


def _video_frame(
    width: int = 8,
    height: int = 4,
    *,
    codec_name: str = "prores",
    chroma_siting: int | None = None,
    pixel_format: str = "yuv422p10le",
    bit_depth: int = 10,
    color_range: str = "limited",
) -> DecodedFrame:
    subsampling = "420" if "420" in pixel_format else "422"
    dtype = np.uint8 if bit_depth <= 8 else np.uint16
    chroma_h = height // 2 if subsampling == "420" else height
    y = np.full((height, width), 64, dtype=dtype)
    u = np.full((chroma_h, width // 2), 512 if dtype == np.uint16 else 128,
                dtype=dtype)
    v = np.full((chroma_h, width // 2), 512 if dtype == np.uint16 else 128,
                dtype=dtype)
    planes = (y, u, v)
    infos = (
        PlaneInfo.create(0, width, height, "Y", dtype, bit_depth),
        PlaneInfo.create(1, width // 2, chroma_h, "U", dtype, bit_depth),
        PlaneInfo.create(2, width // 2, chroma_h, "V", dtype, bit_depth),
    )
    metadata = VideoMetadata(
        width=width,
        height=height,
        fps=Rational(25, 1),
        total_frames=30,
        codec_name=codec_name,
        codec_profile="422hq",
        pixel_format=pixel_format,
        bit_depth=bit_depth,
        color_primaries="bt709",
        color_transfer="bt709",
        color_matrix="bt709",
        color_range=color_range,
        sample_aspect_ratio=Rational(1, 1),
        has_alpha=False,
        field_order=None,
        rotation=0,
        chroma_siting=chroma_siting,
    )
    return DecodedFrame(
        frame_index=12,
        pts=12,
        time_seconds=12 / 25,
        planes=planes,
        pixel_format=pixel_format,
        width=width,
        height=height,
        metadata=metadata,
        plane_infos=infos,
    )


def _make_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    codec: str = "topos",
    pix_fmt: str = "yuv422p10le",
    high_precision: bool = True,
    frame: Any = None,
) -> tuple[Any, ...]:
    import src.features.edit.core.timeline_export as export_module

    width, height = 8, 4
    source = frame if frame is not None else _video_frame(width, height)
    asset = SimpleNamespace(media_type="video", path="/media/source.mov")
    project = SimpleNamespace(
        color_stacks={},
        get_asset_by_id=lambda _asset_id: asset,
    )
    sequence = SimpleNamespace(
        width=width,
        height=height,
        fps=Rational(25, 1),
        format_settings={},
    )
    color = SimpleNamespace(
        primaries="bt709", transfer="bt709", matrix="bt709", range="limited")
    encoder_config = SimpleNamespace(
        codec=codec,
        pix_fmt=pix_fmt,
        width=width,
        height=height,
        fps=Rational(25, 1),
        has_alpha=False,
        color_metadata=color,
    )
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = project
    exporter._sequence = sequence
    exporter._encoder = SimpleNamespace(_config=encoder_config)
    exporter._use_high_precision_export = high_precision
    exporter._export_has_alpha = False
    exporter._export_start_frame = 10
    requested: list[tuple[str, int, str]] = []

    def _request(path: str, frame: int, asset_id: str) -> DecodedFrame:
        requested.append((path, frame, asset_id))
        return source

    exporter._request_high_depth_decoded_frame = _request
    monkeypatch.setattr(
        export_module,
        "export_source_path",
        lambda _project, requested_asset: requested_asset.path,
    )
    config = SimpleNamespace(resolution=(width, height), burn_subtitles=False)
    plan = SimpleNamespace(
        has_content=True,
        layers=(_identity_layer(),),
        transitions=(),
    )
    return exporter, plan, config, source, requested


def test_noop_topos_export_returns_rebased_planar_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, source, requested = _make_env(monkeypatch)

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv422p10le"
    assert output.frame_index == 2
    assert output.pts == 2
    assert output.time_seconds == pytest.approx(2 / 25)
    assert all(out is src for out, src in zip(output.planes, source.planes))
    assert requested == [("/media/source.mov", 12, "asset-1")]


@pytest.mark.parametrize(
    "reject",
    ("effect", "opacity", "speed", "crop", "transform", "transition", "subtitle"),
)
def test_non_identity_timeline_state_falls_back_before_decode(
    monkeypatch: pytest.MonkeyPatch,
    reject: str,
) -> None:
    exporter, plan, config, _source, requested = _make_env(monkeypatch)
    layer = plan.layers[0]
    if reject == "effect":
        layer.clip.effects = (SimpleNamespace(),)
    elif reject == "opacity":
        layer.opacity = 0.5
    elif reject == "speed":
        layer.clip.speed = SimpleNamespace(
            speed=2.0, reverse=False, interpolation="nearest", speed_points=())
    elif reject == "crop":
        layer.crop = (0.1, 0.0, 0.0, 0.0, 0.0)
    elif reject == "transform":
        layer.transform = (1.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
    elif reject == "transition":
        plan.transitions = (SimpleNamespace(),)
    elif reject == "subtitle":
        config.burn_subtitles = True

    assert exporter._try_render_planar_passthrough(plan, 12, config) is None
    assert requested == []


def test_planar_frame_color_tag_mismatch_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, source, requested = _make_env(monkeypatch)
    mismatched = _video_frame()
    mismatched.metadata = VideoMetadata(
        **{
            **mismatched.metadata.__dict__,
            "color_matrix": "bt2020nc",
        }
    )
    exporter._request_high_depth_decoded_frame = (
        lambda _path, _frame, _asset_id: mismatched)

    assert exporter._try_render_planar_passthrough(plan, 12, config) is None
    assert len(requested) == 0
    assert source.pixel_format == "yuv422p10le"


def test_planar_frame_chroma_siting_mismatch_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, requested = _make_env(monkeypatch)
    mismatched = _video_frame(chroma_siting=1)
    exporter._request_high_depth_decoded_frame = (
        lambda _path, _frame, _asset_id: mismatched)

    assert exporter._try_render_planar_passthrough(plan, 12, config) is None
    assert requested == []


def test_untagged_non_prores_planar_frame_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, requested = _make_env(monkeypatch)
    ambiguous = _video_frame(codec_name="dnxhd")
    exporter._request_high_depth_decoded_frame = (
        lambda _path, _frame, _asset_id: ambiguous)

    assert exporter._try_render_planar_passthrough(plan, 12, config) is None
    assert requested == []


def test_chroma_siting_normalizes_ffmpeg_labels_and_enum_values() -> None:
    assert _normalize_chroma_siting("left") == 0
    assert _normalize_chroma_siting("AVCHROMA_LOC_CENTER") == 1
    assert _normalize_chroma_siting("top_left") == 2
    assert _normalize_chroma_siting(1) == 0
    assert _normalize_chroma_siting(2) == 1
    assert _normalize_chroma_siting(3) == 2
    assert _normalize_chroma_siting(4) == 1
    assert _normalize_chroma_siting(5) == 2
    assert _normalize_chroma_siting(6) == 1
    assert _normalize_chroma_siting(0) is None
    assert _normalize_chroma_siting("bottomleft") == 2


@pytest.mark.parametrize(
    ("logical_cpus", "expected"),
    (
        (24, (16, 8)),
        (16, (11, 5)),
        (12, (8, 4)),
        (8, (8, None)),
        (4, (4, None)),
    ),
)
def test_topos_timeline_cpu_budget_reserves_decoder_workers(
    logical_cpus: int,
    expected: tuple[int, int | None],
) -> None:
    assert _topos_timeline_cpu_threads(logical_cpus) == expected


@pytest.mark.parametrize(
    ("encoder_codec", "source_codec", "expected_threads", "reject_setter"),
    (
        ("topos", "prores", 8, False),
        ("ffmpeg", "prores", 0, False),
        ("topos", "h264", 0, False),
        ("topos", "prores", 0, True),
    ),
)
def test_decoder_thread_budget_is_limited_to_topos_intra_sources(
    monkeypatch: pytest.MonkeyPatch,
    encoder_codec: str,
    source_codec: str,
    expected_threads: int,
    reject_setter: bool,
) -> None:
    """Limit TOPOS/intra decoders and retain auto mode if setting fails."""
    import src.features.edit.core.timeline_export as export_module
    import src.shared.media.ffmpeg_source as ffmpeg_module

    seen: dict[str, Any] = {}

    class _CodecContext:
        def __init__(self) -> None:
            self.name = source_codec
            self._thread_count = 0
            self.is_open = False

        @property
        def thread_count(self) -> int:
            return self._thread_count

        @thread_count.setter
        def thread_count(self, count: int) -> None:
            if reject_setter:
                raise RuntimeError("thread count cannot be changed")
            self._thread_count = count

    class _FakeSource:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._video_stream = SimpleNamespace(codec_context=_CodecContext())

        def open(self) -> None:
            seen["threads_when_opened"] = (
                self._video_stream.codec_context.thread_count)

        def read_frame(self, _frame_index: int) -> SimpleNamespace:
            context = self._video_stream.codec_context
            seen["threads_when_read"] = context.thread_count
            seen["context_was_open"] = context.is_open
            context.is_open = True
            return SimpleNamespace(planes=(object(), object(), object()))

    monkeypatch.setattr(ffmpeg_module, "FFmpegMediaSource", _FakeSource)
    monkeypatch.setattr(
        export_module, "_topos_timeline_cpu_threads", lambda: (16, 8))

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._export_high_depth_sources = {}
    exporter._encoder = SimpleNamespace(
        _config=SimpleNamespace(codec=encoder_codec))

    frame = exporter._request_high_depth_decoded_frame(
        "/media/source.mov", 12, "asset-1")

    assert frame is not None
    assert seen["threads_when_read"] == expected_threads
    assert seen["context_was_open"] is False
    # open() ran before the targeted override, while the first read observes
    # the configured count. This is the point before PyAV opens its decoder.
    assert seen["threads_when_opened"] == 0


def test_422_co_sited_output_location_is_equivalent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, _requested = _make_env(monkeypatch)
    exporter._encoder._movie_cfg = SimpleNamespace(chroma_siting=2)

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)


def test_planar_siting_must_match_output_horizontal_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, requested = _make_env(monkeypatch)
    exporter._encoder._movie_cfg = SimpleNamespace(chroma_siting=1)

    assert exporter._try_render_planar_passthrough(plan, 12, config) is None
    assert requested == []


# ======================================================================
# h264 yuv420p 直通（2026-09-15 导出提速计划：解码平面 → 硬编直填）
# ======================================================================

def _h264_frame(chroma_siting: int | None = None, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {"bit_depth": 8}
    kwargs.update(overrides)
    return _video_frame(
        chroma_siting=chroma_siting,
        codec_name="h264",
        pixel_format="yuv420p",
        **kwargs,
    )


def test_h264_yuv420p_passthrough_returns_planar_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未标注 siting 的 h264 按 left(0) 缺省豁免，8bit 无需高精度导出。"""
    exporter, plan, config, source, requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=_h264_frame(),
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"
    assert output.frame_index == 2
    assert all(out is src for out, src in zip(output.planes, source.planes))
    assert requested == [("/media/source.mov", 12, "asset-1")]


def test_h264_yuv420p_passthrough_resizes_planar_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output scaling stays in YUV and returns encoder-sized planes."""
    source = _h264_frame(width=16, height=8)
    source.planes = (
        np.arange(16 * 8, dtype=np.uint8).reshape(8, 16),
        np.arange(4 * 8, dtype=np.uint8).reshape(4, 8) + 120,
        np.arange(4 * 8, dtype=np.uint8).reshape(4, 8) + 130,
    )
    exporter, plan, config, _source, requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=source,
    )
    exporter._sequence.width = 16
    exporter._sequence.height = 8
    exporter._encoder._config.width = 8
    exporter._encoder._config.height = 4
    config.resolution = (8, 4)

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert [plane.shape for plane in output.planes] == [
        (4, 8), (2, 4), (2, 4)]
    assert all(plane is not src for plane, src in zip(
        output.planes, source.planes))
    assert all(info.width == plane.shape[1]
               and info.height == plane.shape[0]
               and info.stride == plane.strides[0]
               for info, plane in zip(output.plane_infos, output.planes))
    import cv2
    assert np.array_equal(
        output.planes[0], cv2.resize(
            source.planes[0], (8, 4), interpolation=cv2.INTER_LINEAR))
    assert np.array_equal(
        output.planes[1], cv2.resize(
            source.planes[1], (4, 2), interpolation=cv2.INTER_LINEAR))
    assert np.array_equal(
        output.planes[2], cv2.resize(
            source.planes[2], (4, 2), interpolation=cv2.INTER_LINEAR))
    assert output.metadata.width == 8
    assert output.metadata.height == 4
    assert requested == [("/media/source.mov", 12, "asset-1")]


def test_h264_yuv420p_passthrough_applies_opacity_and_fade_in_yuv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uniform opacity/fade keeps the delivery in limited-range YUV."""
    source = _h264_frame()
    source.planes = (
        np.arange(8 * 4, dtype=np.uint8).reshape(4, 8),
        np.arange(2 * 4, dtype=np.uint8).reshape(2, 4) + 120,
        np.arange(2 * 4, dtype=np.uint8).reshape(2, 4) + 130,
    )
    exporter, plan, config, _source, requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=source,
    )
    plan.layers[0].opacity = 0.5
    plan.layers[0].clip.fade_in_duration = 4

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    expected = (
        np.rint(np.clip(16.0 + (source.planes[0] - 16.0) * 0.5,
                        16.0, 235.0)).astype(np.uint8),
        np.rint(np.clip(128.0 + (source.planes[1] - 128.0) * 0.5,
                        16.0, 240.0)).astype(np.uint8),
        np.rint(np.clip(128.0 + (source.planes[2] - 128.0) * 0.5,
                        16.0, 240.0)).astype(np.uint8),
    )
    assert all(np.array_equal(actual, expected_plane)
               for actual, expected_plane in zip(output.planes, expected))
    assert requested == [("/media/source.mov", 12, "asset-1")]


def test_h264_passthrough_explicit_left_siting_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=_h264_frame(chroma_siting=0),
    )
    output = exporter._try_render_planar_passthrough(plan, 12, config)
    assert isinstance(output, DecodedFrame)


def test_h264_passthrough_explicit_center_siting_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式标注 siting=1（center）与编码器隐式 0 不一致 → 拒绝。"""
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=_h264_frame(chroma_siting=1),
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


def test_h264_passthrough_rejects_16bit_planes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平面 dtype 与位深不符（uint16 帧配 8bit 目标）→ 拒绝。"""
    frame = _h264_frame(bit_depth=10)
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=frame,
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


def test_h264_passthrough_rejects_full_range_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全范围源（yuvj420p 语义）与 limited 输出标签不等 → 拒绝。"""
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=_h264_frame(color_range="full"),
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


def test_hevc_output_10bit_target_still_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hevc+yuv420p10le 不在直通白名单（10-bit 档未验证）→ fail-closed。"""
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p10le", high_precision=False,
        frame=_hevc_frame(),
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


def test_cross_codec_untagged_h264_source_passes_hevc_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """untagged 豁免跨编码器互认（2026-09-17）：h264/hevc untagged 4:2:0
    约定同为 left(0)，采样网格与目标 codec 无关。"""
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=_h264_frame(),
    )
    output = exporter._try_render_planar_passthrough(plan, 12, config)
    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"


def test_cross_codec_untagged_hevc_source_passes_h264_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=_hevc_frame(),
    )
    output = exporter._try_render_planar_passthrough(plan, 12, config)
    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"


def test_h264_accepts_10bit_420_source_with_downconvert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """h264 10-bit 420 源降位通道（2026-09-17 扩展，与 hevc 档同构）。

    4K_H265/手机 HDR 等 yuv420p10le 源是常见素材；此前 h264 目标
    fail-closed 拒绝、全档困在合成慢路。现在 >>2 降位直交付。
    """
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=_video_frame(
            codec_name="hevc", pixel_format="yuv420p10le", bit_depth=10),
    )
    output = exporter._try_render_planar_passthrough(plan, 12, config)
    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"
    assert all(plane.dtype == np.uint8 for plane in output.planes)


def test_h264_passthrough_rejects_422_source_planes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4:2:2 源平面形状（色度全高）不满足 4:2:0 目标契约 → 拒绝。"""
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=_video_frame(codec_name="h264"),  # 默认 yuv422p10le uint16
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


# ======================================================================
# hevc yuv420p 8-bit 直通（2026-09-16 扩展；与 h264 同构）
# ======================================================================

def _hevc_frame(chroma_siting: int | None = None, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {"bit_depth": 8}
    kwargs.update(overrides)
    return _video_frame(
        chroma_siting=chroma_siting,
        codec_name="hevc",
        pixel_format="yuv420p",
        **kwargs,
    )


def test_hevc_yuv420p_passthrough_returns_planar_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未标注 siting 的 hevc 按 left(0) 缺省豁免（x265/AMF 同 h264 约定）。"""
    exporter, plan, config, source, requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=_hevc_frame(),
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"
    assert output.frame_index == 2
    assert all(out is src for out, src in zip(output.planes, source.planes))
    assert requested == [("/media/source.mov", 12, "asset-1")]


def test_hevc_yuv420p_passthrough_resizes_planar_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scalar_delivery 泛化后 HEVC 同享 YUV 域 resize（无 RGB 往返）。"""
    source = _hevc_frame(width=16, height=8)
    source.planes = (
        np.arange(16 * 8, dtype=np.uint8).reshape(8, 16),
        np.arange(4 * 8, dtype=np.uint8).reshape(4, 8) + 120,
        np.arange(4 * 8, dtype=np.uint8).reshape(4, 8) + 130,
    )
    exporter, plan, config, _source, requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=source,
    )
    exporter._sequence.width = 16
    exporter._sequence.height = 8
    exporter._encoder._config.width = 8
    exporter._encoder._config.height = 4
    config.resolution = (8, 4)

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert [plane.shape for plane in output.planes] == [
        (4, 8), (2, 4), (2, 4)]
    import cv2
    assert np.array_equal(
        output.planes[0], cv2.resize(
            source.planes[0], (8, 4), interpolation=cv2.INTER_LINEAR))
    assert output.metadata.width == 8
    assert output.metadata.height == 4
    assert requested == [("/media/source.mov", 12, "asset-1")]


def test_hevc_yuv420p_passthrough_applies_opacity_and_fade_in_yuv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEVC 8-bit 档的 opacity/fade 围绕 limited-range 中性点在 YUV 域保持。"""
    source = _hevc_frame()
    source.planes = (
        np.arange(8 * 4, dtype=np.uint8).reshape(4, 8),
        np.arange(2 * 4, dtype=np.uint8).reshape(2, 4) + 120,
        np.arange(2 * 4, dtype=np.uint8).reshape(2, 4) + 130,
    )
    exporter, plan, config, _source, requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=source,
    )
    plan.layers[0].opacity = 0.5
    plan.layers[0].clip.fade_in_duration = 4

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    expected = (
        np.rint(np.clip(16.0 + (source.planes[0] - 16.0) * 0.5,
                        16.0, 235.0)).astype(np.uint8),
        np.rint(np.clip(128.0 + (source.planes[1] - 128.0) * 0.5,
                        16.0, 240.0)).astype(np.uint8),
        np.rint(np.clip(128.0 + (source.planes[2] - 128.0) * 0.5,
                        16.0, 240.0)).astype(np.uint8),
    )
    assert all(np.array_equal(actual, expected_plane)
               for actual, expected_plane in zip(output.planes, expected))
    assert requested == [("/media/source.mov", 12, "asset-1")]


def test_hevc_passthrough_explicit_center_siting_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式标注 siting=1（center）与编码器隐式 0 不一致 → 拒绝。"""
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=_hevc_frame(chroma_siting=1),
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


def test_hevc_passthrough_rejects_full_range_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全范围源（yuvj420p 语义）与 limited 输出标签不等 → 拒绝。"""
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=_hevc_frame(color_range="full"),
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


# ======================================================================
# hevc 10-bit 源 → 8-bit 档 YUV 域降位直通（2026-09-17 扩展）
# ======================================================================

def _hevc10_frame(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {"bit_depth": 10}
    kwargs.update(overrides)
    return _video_frame(
        codec_name="hevc", pixel_format="yuv420p10le", **kwargs)


def test_hevc_downconvert_10bit_source_shifts_planes_to_8bit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10-bit 平面按 (x+2)>>2 降位，交付声明改写为 yuv420p/8-bit。

    limited-range 锚点精确整除：Y 64→16、940→235；UV 64→16、960→240。
    """
    source = _hevc10_frame()
    source.planes = (
        np.array(
            [[64, 65, 940, 1023, 64, 65, 940, 1023]] * 4, dtype=np.uint16),
        np.array([[64, 512, 960, 300]] * 2, dtype=np.uint16),
        np.array([[940, 700, 100, 64]] * 2, dtype=np.uint16),
    )
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=source,
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"
    assert all(plane.dtype == np.uint8 for plane in output.planes)
    for out_plane, src_plane in zip(output.planes, source.planes):
        assert np.array_equal(
            out_plane, ((src_plane.astype(np.uint16) + 2) >> 2).astype(np.uint8))
    # limited-range 锚点
    assert output.planes[0][0, 0] == 16
    assert output.planes[0][0, 2] == 235
    assert output.planes[1][0, 2] == 240
    assert all(
        int(getattr(info, "bit_depth", 0)) == 8
        and info.stride == plane.strides[0]
        for info, plane in zip(output.plane_infos, output.planes))
    # 元数据随交付域改写（不残留 10-bit 判读）
    assert output.metadata.bit_depth == 8
    assert output.metadata.pixel_format == "yuv420p"


def test_hevc_downconvert_10bit_resize_stays_in_yuv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10-bit 源 + 输出缩放：降位后按 8-bit 标量通道 resize。"""
    source = _hevc10_frame(width=16, height=8)
    source.planes = (
        (np.arange(16 * 8, dtype=np.uint16).reshape(8, 16) * 16 + 64),
        np.full((4, 8), 512, dtype=np.uint16),
        np.full((4, 8), 512, dtype=np.uint16),
    )
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=source,
    )
    exporter._sequence.width = 16
    exporter._sequence.height = 8
    exporter._encoder._config.width = 8
    exporter._encoder._config.height = 4
    config.resolution = (8, 4)

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"
    assert [plane.shape for plane in output.planes] == [
        (4, 8), (2, 4), (2, 4)]
    assert all(plane.dtype == np.uint8 for plane in output.planes)
    downshifted = (
        ((source.planes[0].astype(np.uint16) + 2) >> 2).astype(np.uint8),
        ((source.planes[1].astype(np.uint16) + 2) >> 2).astype(np.uint8),
        ((source.planes[2].astype(np.uint16) + 2) >> 2).astype(np.uint8),
    )
    import cv2
    for out_plane, src_plane in zip(output.planes, downshifted):
        assert np.array_equal(
            out_plane,
            cv2.resize(src_plane, (out_plane.shape[1], out_plane.shape[0]),
                       interpolation=cv2.INTER_LINEAR))


def test_hevc_downconvert_10bit_full_range_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全范围 10-bit 源与 limited 输出标签不等 → 拒绝（fail-closed）。"""
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=_hevc10_frame(color_range="full"),
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


def test_hevc_downconvert_10bit_explicit_center_siting_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="hevc", pix_fmt="yuv420p", high_precision=False,
        frame=_hevc10_frame(chroma_siting=1),
    )
    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


# ---------------------------------------------------------------------------
# Topos TPIC 中间片源（provider 原生解码平面）的导出直通（2026-09-17）。
# 与 FFmpeg 源的关键差异：解码走 provider 既有会话（FFmpeg 打开 TPIC 必败），
# 源 siting 从帧 extra 的 topos_chroma_siting 读取。
# ---------------------------------------------------------------------------


def _topos_provider_frame(
    width: int = 8,
    height: int = 4,
    *,
    chroma_siting: int = 0,
    pixel_format: str = "yuv422p10le",
) -> DecodedFrame:
    frame = _video_frame(width, height, codec_name="topos",
                         pixel_format=pixel_format)
    frame.extra = {"topos_chroma_siting": chroma_siting}
    return frame


def _make_topos_source_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    codec: str,
    pix_fmt: str,
    high_precision: bool,
    frame: Any = None,
    asset_metadata: Any = None,
):
    """_make_env 的 Topos 源变体：asset 声明 codec + provider 解码桩。"""
    import src.features.edit.core.timeline_export as export_module

    width, height = 8, 4
    source = frame if frame is not None else _topos_provider_frame(width, height)
    asset = SimpleNamespace(
        media_type="video",
        path="/media/source.mov",
        metadata=asset_metadata if asset_metadata is not None
        else {"codec": "topos"},
    )
    project = SimpleNamespace(
        color_stacks={},
        get_asset_by_id=lambda _asset_id: asset,
    )
    sequence = SimpleNamespace(
        width=width,
        height=height,
        fps=Rational(25, 1),
        format_settings={},
    )
    color = SimpleNamespace(
        primaries="bt709", transfer="bt709", matrix="bt709", range="limited")
    encoder_config = SimpleNamespace(
        codec=codec,
        pix_fmt=pix_fmt,
        width=width,
        height=height,
        fps=Rational(25, 1),
        has_alpha=False,
        color_metadata=color,
    )
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = project
    exporter._sequence = sequence
    exporter._encoder = SimpleNamespace(_config=encoder_config)
    exporter._use_high_precision_export = high_precision
    exporter._export_has_alpha = False
    exporter._export_start_frame = 10

    provider_requests: list[tuple[str, int, str]] = []
    high_depth_requests: list[tuple[str, int, str]] = []

    def _provider_request(path: str, frame_no: int, asset_id: str,
                          session_key: str = "") -> DecodedFrame:
        provider_requests.append((path, frame_no, asset_id))
        return source

    def _high_depth_request(path: str, frame_no: int, asset_id: str,
                            session_key: str = "") -> DecodedFrame:
        high_depth_requests.append((path, frame_no, asset_id))
        raise AssertionError(
            "Topos asset must not go through the FFmpeg high-depth source")

    exporter._request_video_decoded_frame = _provider_request
    exporter._request_high_depth_decoded_frame = _high_depth_request
    monkeypatch.setattr(
        export_module,
        "export_source_path",
        lambda _project, requested_asset: requested_asset.path,
    )
    config = SimpleNamespace(resolution=(width, height), burn_subtitles=False)
    plan = SimpleNamespace(
        has_content=True,
        layers=(_identity_layer(),),
        transitions=(),
    )
    return exporter, plan, config, source, provider_requests


def test_topos_source_topos_target_delivers_provider_planes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topos 中间片 → Topos 导出：同格式平面直填，解码走 provider 会话。"""
    source = _topos_provider_frame()
    exporter, plan, config, _source, provider_requests = _make_topos_source_env(
        monkeypatch,
        codec="topos", pix_fmt="yuv422p10le", high_precision=True,
        frame=source,
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv422p10le"
    assert output.planes == source.planes
    assert provider_requests, "provider decode path must be used"


def test_topos_source_asset_codec_name_key_also_recognized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, _requests = _make_topos_source_env(
        monkeypatch,
        codec="topos", pix_fmt="yuv422p10le", high_precision=True,
        asset_metadata={"codec_name": "topos"},
    )
    output = exporter._try_render_planar_passthrough(plan, 12, config)
    assert isinstance(output, DecodedFrame)


def test_topos_source_h264_delivery_downconverts_to_420_8bit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topos 422 10-bit 源 → h264 8-bit 420：YUV 域降位 + 色度半高。"""
    source = _topos_provider_frame()
    exporter, plan, config, _source, _requests = _make_topos_source_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=source,
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"
    assert [plane.dtype for plane in output.planes] == [np.uint8] * 3
    # 420 目标：色度半高（源 422 全高）
    assert [plane.shape for plane in output.planes] == [(4, 8), (2, 4), (2, 4)]
    # limited-range 锚点在降位 + 垂直子采样后保持
    assert int(output.planes[0][0, 0]) == 16
    assert all(
        int(getattr(info, "bit_depth", 0)) == 8
        for info in output.plane_infos)


def test_topos_source_dnxhr_8bit_delivery_keeps_full_height_chroma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topos 源 → DNxHR 8-bit 422：色度保持全高（422 目标几何）。

    回归：交付段曾硬编码 420 半高色度目标，422 目标平面字节数对不上
    （编码器 "got N bytes; need M" 转换失败）。
    """
    source = _topos_provider_frame()
    exporter, plan, config, _source, _requests = _make_topos_source_env(
        monkeypatch,
        codec="dnxhr", pix_fmt="yuv422p", high_precision=False,
        frame=source,
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv422p"
    assert [plane.dtype for plane in output.planes] == [np.uint8] * 3
    # 422 目标：色度全高
    assert [plane.shape for plane in output.planes] == [(4, 8), (4, 4), (4, 4)]


def test_topos_source_prores_10bit_same_format_direct_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topos 源 → ProRes 422 10-bit：同格式 uint16 平面零转换交付。"""
    source = _topos_provider_frame()
    exporter, plan, config, _source, _requests = _make_topos_source_env(
        monkeypatch,
        codec="prores", pix_fmt="yuv422p10le", high_precision=True,
        frame=source,
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv422p10le"
    assert all(plane.dtype == np.uint16 for plane in output.planes)
    assert output.planes == source.planes


def test_topos_source_non_identity_edit_still_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带效果的 Topos 源仍走合成路径（fail-closed 门不被源类型绕过）。"""
    source = _topos_provider_frame()
    exporter, plan, config, _source, _requests = _make_topos_source_env(
        monkeypatch,
        codec="topos", pix_fmt="yuv422p10le", high_precision=True,
        frame=source,
    )
    layer = plan.layers[0]
    layer.clip = SimpleNamespace(
        **{**vars(layer.clip), "effects": (SimpleNamespace(effect_type="blur"),)})

    assert exporter._try_render_planar_passthrough(plan, 12, config) is None


def test_encode_frame_planar_whitelist_allows_same_format_prores_dnxhr() -> None:
    """`_encode_frame` 平面白名单：prores/dnxhr 同格式平面放行，其余拒绝。"""
    frame = _topos_provider_frame()

    def _exporter(codec: str, pix_fmt: str) -> tuple[TimelineExporter, list]:
        exporter = TimelineExporter.__new__(TimelineExporter)
        encoded: list[Any] = []
        exporter._encoder = SimpleNamespace(
            _config=SimpleNamespace(codec=codec, pix_fmt=pix_fmt),
            encode_frame=encoded.append,
        )
        exporter._current_frame = 0
        exporter._sequence = SimpleNamespace(fps=Rational(25, 1))
        exporter._record_gpu_stage = lambda *a, **k: None
        exporter._record_gpu_fallback = lambda *a, **k: None
        return exporter, encoded

    def _planar(pixel_format: str) -> DecodedFrame:
        return _topos_provider_frame(pixel_format=pixel_format)

    for codec, pix_fmt, frame_pix in (
            ("prores", "yuv422p10le", "yuv422p10le"),
            ("dnxhr", "yuv422p", "yuv422p"),
            ("topos", "yuv422p10le", "yuv422p10le"),
            ("h264", "yuv420p", "yuv420p")):
        exporter, encoded = _exporter(codec, pix_fmt)
        exporter._encode_frame(_planar(frame_pix))
        assert len(encoded) == 1

    for codec, pix_fmt, frame_pix in (
            ("gif", "rgb24", "yuv422p10le"),
            ("prores", "yuv422p10le", "yuv444p10le"),
            ("h264", "yuv420p", "yuv422p10le")):
        exporter, _encoded = _exporter(codec, pix_fmt)
        with pytest.raises(TypeError):
            exporter._encode_frame(_planar(frame_pix))


# ---------------------------------------------------------------------------
# 10-bit 4:2:0 源（4K_H265/手机 HDR 常见）的交付扩展（2026-09-17 第二批）：
# h264 降位直交付；prores/dnxhr/topos 走 420→422 色度域上采样。
# ---------------------------------------------------------------------------


def _hevc10_420_frame() -> DecodedFrame:
    return _video_frame(
        width=8, height=4, codec_name="hevc",
        pixel_format="yuv420p10le", bit_depth=10)


def test_topos_target_10bit_420_source_upsamples_chroma_to_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """420→422 色度域上采样：Y 不动、色度全高、位深保持 10-bit。"""
    source = _hevc10_420_frame()
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="topos", pix_fmt="yuv422p10le", high_precision=True,
        frame=source,
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv422p10le"
    assert all(plane.dtype == np.uint16 for plane in output.planes)
    # Y 平面零转换；色度从 (2, 4) 上采样到 (4, 4)
    assert output.planes[0] is source.planes[0]
    assert [plane.shape for plane in output.planes] == [(4, 8), (4, 4), (4, 4)]
    assert all(
        int(getattr(info, "bit_depth", 0)) == 10
        for info in output.plane_infos)


def test_prores_target_10bit_420_source_upsamples_chroma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="prores", pix_fmt="yuv422p10le", high_precision=True,
        frame=_hevc10_420_frame(),
    )
    output = exporter._try_render_planar_passthrough(plan, 12, config)
    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv422p10le"
    assert [plane.shape for plane in output.planes] == [(4, 8), (4, 4), (4, 4)]


def test_dnxhr_8bit_420_source_downconverts_and_upsamples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """420 10-bit 源 → DNxHR 8-bit 422：>>2 降位 + 色度上采样一步完成。"""
    source = _hevc10_420_frame()
    source.planes = (
        np.array([[64, 64, 940, 940, 64, 64, 940, 940]] * 4, dtype=np.uint16),
        np.array([[512, 512, 512, 512]] * 2, dtype=np.uint16),
        np.array([[512, 512, 512, 512]] * 2, dtype=np.uint16),
    )
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="dnxhr", pix_fmt="yuv422p", high_precision=False,
        frame=source,
    )

    output = exporter._try_render_planar_passthrough(plan, 12, config)

    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv422p"
    assert all(plane.dtype == np.uint8 for plane in output.planes)
    # 422 目标：色度全高 (4, 4)；limited 锚点在降位后保持
    assert [plane.shape for plane in output.planes] == [(4, 8), (4, 4), (4, 4)]
    assert int(output.planes[0][0, 0]) == 16
    assert int(output.planes[0][0, 2]) == 235
    assert all(
        int(getattr(info, "bit_depth", 0)) == 8
        for info in output.plane_infos)


def test_h264_10bit_420_source_downconvert_keeps_420_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """420→420 无色度重采样：仅降位，几何不变。"""
    source = _hevc10_420_frame()
    exporter, plan, config, _source, _requested = _make_env(
        monkeypatch,
        codec="h264", pix_fmt="yuv420p", high_precision=False,
        frame=source,
    )
    output = exporter._try_render_planar_passthrough(plan, 12, config)
    assert isinstance(output, DecodedFrame)
    assert output.pixel_format == "yuv420p"
    assert [plane.shape for plane in output.planes] == [(4, 8), (2, 4), (2, 4)]
    assert all(plane.dtype == np.uint8 for plane in output.planes)
