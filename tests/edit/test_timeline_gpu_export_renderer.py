"""P0-GPU-1 导出 GPU 合成渲染器 focused tests。

分组：
- eligibility 门控（纯 CPU，不触碰 GL）：单层/无转场/无 crop/denoise/
  effects/非 alpha/非高精度，不满足一律返回 None 回落 CPU。
- 格式不适用的 clip 级记忆（避免逐帧二次取帧）。
- GL 可用时的像素一致性（无 GL 环境自动 skip）：恒等链 ≤2 LSB、
  典型调色与 CPU GradingMath 逐位一致（本机实测口径）。

生产默认在 pytest 下回退 CPU 口径（TimelineExporter.__init__ 的
PYTEST_CURRENT_TEST 检测），保证既有导出测试确定性不受 GL 影响。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.features.edit.core.timeline_export import TimelineExporter  # noqa: E402
from src.shared.media.frame import DecodedFrame  # noqa: E402
from src.shared.export.gpu_encode_surface import (  # noqa: E402
    GpuEncodeSurface,
    GpuEncodeSurfaceDescriptor,
)
from src.shared.export.gpu_trail import ExportGpuTrail  # noqa: E402
from src.shared.media.gpu_frame import (  # noqa: E402
    GpuFramePlane,
    GpuFrameRelease,
    GpuPlaneResource,
)

GRADE_TYPICAL = {
    "exposure": 8.0, "temp": 4.0, "tint": -2.0, "contrast": 6.0,
    "lift": (1.5, 0.0, -1.5), "gamma": (0.0, 2.0, 0.0),
    "gain": (2.0, 0.0, -2.0), "shadow": -8.0, "highlights": 6.0,
    "vibrance": 10.0, "saturation": 5.0,
}


def _make_planes(fmt: str, w: int = 96, h: int = 64):
    rng = np.random.default_rng(11)
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)
    xg, yg = np.meshgrid(xs, ys)
    base = (0.5 + 0.25 * np.sin(9 * xg * 3.14159) * np.cos(7 * yg * 3.14159)
            + 0.06 * rng.standard_normal((h, w)).astype(np.float32))
    u_base = 0.5 + 0.2 * np.sin(5 * xg * 3.14159 + 1.0)
    v_base = 0.5 + 0.2 * np.cos(3 * yg * 3.14159 + 0.5)
    bits = 10 if "10le" in fmt else 8
    dtype = np.uint16 if bits == 10 else np.uint8
    scale8 = 1 << max(0, bits - 8)
    y = np.rint(np.clip(base, 0.0, 1.0) * 219.0 * scale8
                + 16.0 * scale8).astype(dtype)
    u_small = u_base[::2, ::2] if "420" in fmt else (
        u_base[:, ::2] if "422" in fmt else u_base)
    v_small = v_base[::2, ::2] if "420" in fmt else (
        v_base[:, ::2] if "422" in fmt else v_base)
    u = np.rint(np.clip(u_small, 0.0, 1.0) * 224.0 * scale8
                + 128.0 * scale8).astype(dtype)
    v = np.rint(np.clip(v_small, 0.0, 1.0) * 224.0 * scale8
                + 128.0 * scale8).astype(dtype)
    return (np.ascontiguousarray(y), np.ascontiguousarray(u),
            np.ascontiguousarray(v))


def _make_exporter(project=None, provider=None):
    ex = TimelineExporter.__new__(TimelineExporter)
    ex._project = project
    ex._sequence = None
    ex._frame_provider = provider
    ex._open_clips = {}
    ex._gpu_export_renderer = None
    ex._gpu_ineligible_clip_ids = set()
    ex._gpu_render_disabled = False
    ex._use_high_precision_export = False
    ex._export_has_alpha = False
    ex._export_color_metadata = None
    return ex


def test_gpu_yuv420_delivery_gate_requires_supported_export_contract():
    ex = _make_exporter()
    ex._encoder = SimpleNamespace(_config=SimpleNamespace(
        codec="h264", pix_fmt="yuv420p"))
    ex._export_color_metadata = SimpleNamespace(
        primaries="bt709", transfer="bt709", matrix="bt709", range="limited")
    ex._sequence = SimpleNamespace(
        format_settings={},
        get_active_subtitle_clips=lambda frame_idx: ())
    config = SimpleNamespace(burn_subtitles=False)

    assert ex._gpu_yuv420_output_allowed(config, 192, 108)
    assert not ex._gpu_yuv420_output_allowed(config, 191, 108)
    # 2026-09-17：burn_subtitles 惰性语义逐帧化——无活跃字幕的帧仍走
    # GPU YUV 直出；有活跃字幕或查询异常才 fail-closed 回落 BGR。
    config.burn_subtitles = True
    assert ex._gpu_yuv420_output_allowed(config, 192, 108, frame_idx=0)
    ex._sequence.get_active_subtitle_clips = (
        lambda frame_idx: (SimpleNamespace(),))
    assert not ex._gpu_yuv420_output_allowed(config, 192, 108, frame_idx=3)
    ex._sequence.get_active_subtitle_clips = None
    assert not ex._gpu_yuv420_output_allowed(config, 192, 108, frame_idx=3)
    ex._sequence.get_active_subtitle_clips = (
        lambda frame_idx: ())
    config.burn_subtitles = False
    ex._export_color_metadata.range = "full"
    assert not ex._gpu_yuv420_output_allowed(config, 192, 108)
    # 2026-09-16：hevc 8-bit 档与 h264 同构进入 GPU 直出门
    ex._export_color_metadata.range = "limited"
    ex._encoder._config.codec = "hevc"
    assert ex._gpu_yuv420_output_allowed(config, 192, 108)
    # 10-bit hevc 是既有独立档位；vp9 等其他 codec 仍 fail-closed
    ex._encoder._config.pix_fmt = "yuv420p10le"
    assert ex._gpu_yuv420_output_allowed(config, 192, 108)
    ex._encoder._config.codec = "vp9"
    ex._encoder._config.pix_fmt = "yuv420p"
    assert not ex._gpu_yuv420_output_allowed(config, 192, 108)


def test_gpu_yuv422p10_delivery_gate_requires_topos_bt709_contract():
    ex = _make_exporter()
    ex._encoder = SimpleNamespace(_config=SimpleNamespace(
        codec="topos", pix_fmt="yuv422p10le"))
    ex._export_color_metadata = SimpleNamespace(
        primaries="bt709", transfer="bt709", matrix="bt709", range="limited")
    config = SimpleNamespace(burn_subtitles=False)

    assert ex._gpu_yuv422p10_output_allowed(config, 192, 108)
    assert not ex._gpu_yuv422p10_output_allowed(config, 191, 108)
    config.burn_subtitles = True
    assert not ex._gpu_yuv422p10_output_allowed(config, 192, 108)
    config.burn_subtitles = False
    ex._encoder._config.pix_fmt = "yuv444p10le"
    assert not ex._gpu_yuv422p10_output_allowed(config, 192, 108)


def test_encode_frame_accepts_topos_yuv422p10_gpu_planar_frame():
    ex = _make_exporter()
    encoded = []
    ex._encoder = SimpleNamespace(
        _config=SimpleNamespace(codec="topos", pix_fmt="yuv422p10le"),
        encode_frame=encoded.append,
    )
    frame = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        planes=(np.full((8, 8), 512, np.uint16),
                np.full((8, 4), 512, np.uint16),
                np.full((8, 4), 512, np.uint16)),
        pixel_format="yuv422p10le",
    )
    ex._encode_frame(frame)
    assert encoded == [frame]


def test_encode_frame_accepts_only_h264_yuv420_gpu_planar_frame():
    ex = _make_exporter()
    encoded = []
    ex._encoder = SimpleNamespace(
        _config=SimpleNamespace(codec="h264", pix_fmt="yuv420p"),
        encode_frame=encoded.append,
    )
    ex._current_frame = 5
    ex._get_sequence_fps = lambda: 25.0
    frame = DecodedFrame(
        frame_index=5, pts=5, time_seconds=0.2,
        planes=(np.full((8, 8), 16, np.uint8),
                np.full((4, 4), 128, np.uint8),
                np.full((4, 4), 128, np.uint8)),
        pixel_format="yuv420p",
    )
    ex._encode_frame(frame)
    assert encoded == [frame]
    # 2026-09-16：hevc 8-bit 平面交付与 h264 同构接受
    ex._encoder._config.codec = "hevc"
    ex._encode_frame(frame)
    assert encoded == [frame, frame]
    # 未在白名单内的 codec（如 vp9）仍显式拒绝
    ex._encoder._config.codec = "vp9"
    with pytest.raises(TypeError):
        ex._encode_frame(frame)


def test_encode_frame_submits_gpu_surface_without_host_frame_conversion():
    ex = _make_exporter()
    ex._gpu_trail = ExportGpuTrail("gpu-surface-submit")
    submitted = []
    plane = GpuFramePlane(
        "Y", "metal", 0, "2d", 8, 8,
        resource=GpuPlaneResource(
            api="metal", native_handle=object(), resource_format="r8unorm",
            width=8, height=8, device_identity="gpu-0",
        ),
    )
    surface = GpuEncodeSurface(
        GpuEncodeSurfaceDescriptor(
            backend="videotoolbox", pixel_format="nv12", width=8, height=8,
            bit_depth=8, device_identity="gpu-0", planes=(plane,),
        ),
        GpuFrameRelease(),
        lambda item: submitted.append(item) or {"gpu_submit_ms": 0.5},
    )

    ex._encode_frame(surface)

    assert submitted == [surface]
    assert ex.gpu_trail_snapshot.stage("encode_submit").backend == "videotoolbox"
    assert ex.gpu_trail_snapshot.stage("encode_submit").gpu_submit_ms == 0.5
    surface.release()


def _make_video_env(fmt: str = "yuv422p10le", w: int = 96, h: int = 64,
                    grade_params=None, with_stack=False):
    planes = _make_planes(fmt, w, h)
    meta = SimpleNamespace(color_range="limited", color_matrix="bt709")
    decoded = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        planes=planes, pixel_format=fmt, metadata=meta)
    requests = []

    def _request(clip_id, frame, quality="master"):
        requests.append((clip_id, frame, quality))
        return decoded

    stack_attrs = dict(
        grading_params=dict(grade_params or {}), layers=[],
        active_layer_index=None)
    stack = SimpleNamespace(**stack_attrs)
    asset = SimpleNamespace(
        media_type="video", asset_id="a1", path="fake.mov",
        effective_path=None)
    project = SimpleNamespace(
        color_stacks={"sid": stack} if with_stack else {},
        get_asset_by_id=lambda _aid: asset)
    provider = SimpleNamespace(
        open_clip=lambda p, share_by_path=False: "clip-1",
        request_frame=_request)
    layer = SimpleNamespace(
        clip_id="c1", asset_id="a1", source_frame=0, opacity=1.0,
        crop=(), track_index=0,
        clip=SimpleNamespace(effects=(), color_stack_id="sid" if with_stack else None))
    return planes, meta, layer, project, provider, requests


# ---------------------------------------------------------------------------
# eligibility 门控（无 GL 依赖：所有用例在不满足条件时须于触碰
# provider/GPU 之前返回 None）
# ---------------------------------------------------------------------------

def test_gpu_disabled_env_short_circuits_before_provider():
    _planes, _meta, layer, project, provider, requests = _make_video_env()
    ex = _make_exporter(project, provider)
    ex._gpu_render_disabled = True
    assert ex._gpu_render_composite([layer], (), 128, 72) is None
    assert requests == []  # 短路必须发生在取帧之前


def test_failed_gpu_renderer_short_circuits_before_provider():
    _planes, _meta, layer, project, provider, requests = _make_video_env()
    ex = _make_exporter(project, provider)
    ex._gpu_export_renderer = SimpleNamespace(failed=True)

    assert ex._gpu_render_composite([layer], (), 128, 72) is None
    assert requests == []


def test_gpu_ineligible_multi_layer_with_effects():
    """P0-GPU-3：多层本身已支持；任一层挂特效 → 整帧回落 CPU，
    且短路发生在触碰 provider 之前。"""
    _planes, _meta, l1, project, provider, requests = _make_video_env()
    l2 = SimpleNamespace(**{**l1.__dict__, "clip_id": "c2", "track_index": 1})
    l1.clip.effects = (SimpleNamespace(order=0),)
    ex = _make_exporter(project, provider)
    assert ex._gpu_render_composite([l1, l2], (), 128, 72) is None
    assert requests == []


def test_gpu_ineligible_with_transitions():
    _planes, _meta, layer, project, provider, requests = _make_video_env()
    ex = _make_exporter(project, provider)
    assert ex._gpu_render_composite(
        [layer], (SimpleNamespace(transition_id="t"),), 128, 72) is None
    assert requests == []


def test_gpu_ineligible_with_effects():
    _planes, _meta, layer, project, provider, requests = _make_video_env()
    layer.clip.effects = (SimpleNamespace(order=0),)
    ex = _make_exporter(project, provider)
    assert ex._gpu_render_composite([layer], (), 128, 72) is None
    assert requests == []


def test_gpu_ineligible_alpha_excludes_before_provider():
    _planes, _meta, layer, project, provider, requests = _make_video_env()
    ex = _make_exporter(project, provider)
    ex._export_has_alpha = True
    assert ex._gpu_render_composite([layer], (), 128, 72) is None
    assert requests == []  # 短路必须发生在取帧之前
    # P0-GPU-2：高精度（u16）交付不再排除——由 RGB16UI 管线承接，
    # 见 test_gpu_u16_delivery_parity_and_dtype


def test_gpu_unsupported_format_is_cached_per_clip():
    # 不支持的格式（yuv444p16le 不在表内）→ 返回 None 并记住 clip，
    # 第二帧不再发起 GPU 取帧
    _planes, _meta, layer, project, provider, requests = _make_video_env(
        "yuv444p16le")
    ex = _make_exporter(project, provider)
    assert ex._gpu_render_composite([layer], (), 128, 72) is None
    assert len(requests) == 1
    assert ex._gpu_render_composite([layer], (), 128, 72) is None
    assert len(requests) == 1  # 已按 clip 记忆，不再二次取帧
    assert layer.clip_id in ex._gpu_ineligible_clip_ids


def test_release_gpu_export_renderer_idempotent_without_gl():
    ex = _make_exporter()
    ex._release_gpu_export_renderer()  # 从未创建：应为无操作
    assert ex._gpu_export_renderer is None


def test_gpu_single_layer_zero_blend_uses_planar_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通单视频层必须路由到 renderer 的 planar 快速路径。"""
    import src.features.edit.core.timeline_gpu_export_renderer as renderer_module

    _planes, _meta, layer, project, provider, requests = _make_video_env()
    calls: list[tuple[Any, ...]] = []

    class _FakeRenderer:
        failed = False
        gl_errors = 0

        def render_planar(
            self,
            source: Any,
            grade_uniforms: Any,
            width: int,
            height: int,
            opacity: float,
            *,
            output_u16: bool = False,
        ) -> np.ndarray:
            calls.append((source, grade_uniforms, width, height, opacity,
                          output_u16))
            return np.full((height, width, 3), 17, dtype=np.uint8)

        def render_packed_bgr(self, *args: Any, **kwargs: Any) -> np.ndarray:
            raise AssertionError("planar source must use render_planar")

        def render_layer_stack(self, *args: Any, **kwargs: Any) -> np.ndarray:
            raise AssertionError("normal blend should use single-layer fast path")

    monkeypatch.setattr(
        renderer_module, "TimelineGpuExportRenderer", _FakeRenderer)
    ex = _make_exporter(project, provider)

    result = ex._gpu_render_composite([layer], (), 128, 72)

    assert result is not None
    assert result.shape == (72, 128, 3)
    assert result.dtype == np.uint8
    assert len(calls) == 1
    assert len(requests) == 1


def test_gpu_packed_rgb24_uses_bgr_renderer_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FFmpeg master RGB24 frames must reach GPU packed rendering as BGR."""
    import src.features.edit.core.timeline_gpu_export_renderer as renderer_module

    rgb = np.empty((64, 96, 3), dtype=np.uint8)
    rgb[...] = (240, 80, 20)
    decoded = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        data=rgb, pixel_format="rgb24",
    )
    requests: list[tuple[Any, ...]] = []

    def _request(clip_id, frame, quality="master"):
        requests.append((clip_id, frame, quality))
        return decoded

    asset = SimpleNamespace(
        media_type="video", asset_id="a1", path="fake.mov",
        effective_path=None,
    )
    project = SimpleNamespace(
        color_stacks={}, get_asset_by_id=lambda _aid: asset,
    )
    provider = SimpleNamespace(
        open_clip=lambda _path, share_by_path=False: "clip-1",
        request_frame=_request,
    )
    layer = SimpleNamespace(
        clip_id="c1", asset_id="a1", source_frame=0, opacity=1.0,
        crop=(), track_index=0,
        clip=SimpleNamespace(effects=(), color_stack_id=None),
    )
    received: list[np.ndarray] = []

    class _FakeRenderer:
        failed = False
        gl_errors = 0

        def render_planar(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("packed input must use render_packed_bgr")

        def render_packed_bgr(
            self, frame_bgr: np.ndarray, *args: Any, **kwargs: Any,
        ) -> np.ndarray:
            received.append(frame_bgr)
            return np.zeros((64, 96, 3), dtype=np.uint8)

        def render_layer_stack(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("single layer must use the packed fast path")

    monkeypatch.setattr(
        renderer_module, "TimelineGpuExportRenderer", _FakeRenderer,
    )
    exporter = _make_exporter(project, provider)

    result = exporter._gpu_render_composite([layer], (), 96, 64)

    assert result is not None
    assert len(requests) == 1
    assert len(received) == 1
    assert received[0].flags.c_contiguous
    np.testing.assert_array_equal(received[0][0, 0], (20, 80, 240))
    assert "c1" not in exporter._gpu_ineligible_clip_ids


def test_metal_multi_layer_route_preserves_zero_opacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.features.edit.core.timeline_metal_export_renderer as metal_module

    _planes, _meta, layer, project, provider, requests = _make_video_env(
        "yuv422p10le")
    top = SimpleNamespace(
        clip_id="c2", asset_id="a1", source_frame=0, opacity=0.0,
        crop=(), track_index=1,
        clip=SimpleNamespace(effects=(), color_stack_id=None, blend_mode=None),
    )
    calls = []

    class _FakeMetalRenderer:
        failed = False
        metal_errors = 0

        def render_layer_stack(self, layers, width, height, **kwargs):
            calls.append((layers, width, height, kwargs))
            return np.full((height, width, 3), 23, dtype=np.uint8)

        def release(self):
            return None

    monkeypatch.setattr(
        metal_module, "TimelineMetalExportRenderer", _FakeMetalRenderer,
        raising=False,
    )
    exporter = _make_exporter(project, provider)
    exporter._timeline_graphics_backend = "metal"
    exporter._metal_fallback_warnings = set()
    exporter._sequence = SimpleNamespace()
    exporter._get_sequence_fps = lambda: 25.0

    result = exporter._gpu_render_composite([layer, top], (), 64, 48)

    assert result is not None and result.shape == (48, 64, 3)
    assert len(requests) == 1
    assert len(calls) == 1
    assert len(calls[0][0]) == 2
    assert calls[0][0][1][2] == 0.0
    assert calls[0][0][0][0] is calls[0][0][1][0]


def test_metal_spatial_transition_is_routed_as_gpu_wipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.features.edit.core.timeline_metal_export_renderer as metal_module
    from src.features.edit.core.timeline_gpu_export_renderer import GpuWipeElement

    _planes, _meta, left, project, provider, requests = _make_video_env(
        "yuv422p10le")
    left.transform = (1.5, -2.0, 1.05, 0.95, 8.0,
                      "center", 32.0, 24.0)
    left.crop = (0.05, 0.05, 0.05, 0.05, 0.0)
    left.clip.effects = (SimpleNamespace(
        effect_type="blur", params={
            "blur_type": "gaussian", "radius_x": 1.0,
            "radius_y": 1.0, "mix": 0.75,
        }, order=0, is_enabled=True,
    ),)
    right = SimpleNamespace(
        **{**left.__dict__, "clip_id": "c2", "asset_id": "a2",
        "track_index": 1,
        "clip": SimpleNamespace(effects=(), color_stack_id=None,
                                 blend_mode=None)},
    )
    right.transform = (-0.75, 1.0, 0.9, 1.1, -5.0,
                       "center", 32.0, 24.0)
    right.crop = (0.02, 0.02, 0.03, 0.03, 1.5)
    transition = SimpleNamespace(
        transition_type="wipe", transition_id="wipe-test",
        left_clip_id="c1", right_clip_id="c2", blend_factor=0.5,
        left_opacity=1.0, right_opacity=1.0,
        transition=SimpleNamespace(params={"direction": "left"}),
    )
    calls = []

    class _FakeMetalRenderer:
        failed = False
        metal_errors = 0

        def render_layer_stack(self, layers, width, height, **kwargs):
            calls.append((layers, width, height, kwargs))
            return np.full((height, width, 3), 23, dtype=np.uint8)

    monkeypatch.setattr(
        metal_module, "TimelineMetalExportRenderer", _FakeMetalRenderer,
        raising=False,
    )
    exporter = _make_exporter(project, provider)
    exporter._timeline_graphics_backend = "metal"
    exporter._metal_fallback_warnings = set()
    exporter._sequence = SimpleNamespace(width=64, height=48)
    exporter._get_sequence_fps = lambda: 25.0

    result = exporter._gpu_render_composite(
        [left, right], (transition,), 64, 48, frame_idx=12)

    assert result is not None and result.shape == (48, 64, 3)
    assert len(calls) == 1
    assert len(requests) == 2
    wipe = calls[0][0][0]
    assert isinstance(wipe, GpuWipeElement)
    assert wipe.left_opacity == 1.0 and wipe.right_opacity == 1.0
    # Metal generates the spatial mask procedurally; a CPU pixel mask would
    # reintroduce the per-frame upload that M-B06 removes.
    assert wipe.mask is None
    assert wipe.mask_spec is not None
    assert wipe.left_effects is not None and len(wipe.left_effects) == 1
    assert wipe.left_transform == left.transform
    assert wipe.right_transform == right.transform
    assert wipe.left_crop == left.crop and wipe.right_crop == right.crop
    assert exporter._metal_fallback_warnings == set()


def test_metal_frame_blend_retime_falls_back_before_decode():
    _planes, _meta, layer, project, provider, requests = _make_video_env()
    layer.clip.timeline_start = 0
    layer.clip.source_duration = 10
    layer.clip.speed = SimpleNamespace(
        interpolation="frame_blend",
        timeline_to_source_frame_pair=lambda _local, _duration: (0, 1, 0.5),
    )
    exporter = _make_exporter(project, provider)
    exporter._timeline_graphics_backend = "metal"
    exporter._metal_fallback_warnings = set()

    assert exporter._gpu_render_composite([layer], (), 64, 48, frame_idx=2) is None
    assert requests == []


def test_gpu_single_layer_non_normal_blend_uses_planar_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU 首层语义忽略 blend mode，GPU 单层路径也必须如此。"""
    import src.features.edit.core.timeline_gpu_export_renderer as renderer_module

    _planes, _meta, layer, project, provider, requests = _make_video_env()
    layer.clip.blend_mode = SimpleNamespace(code=3)
    calls: list[tuple[Any, ...]] = []

    class _FakeRenderer:
        failed = False
        gl_errors = 0

        def render_planar(self, *args: Any, **kwargs: Any) -> np.ndarray:
            calls.append(args)
            return np.full((72, 128, 3), 23, dtype=np.uint8)

        def render_packed_bgr(self, *args: Any, **kwargs: Any) -> np.ndarray:
            raise AssertionError("planar source must use render_planar")

        def render_layer_stack(
            self,
            layers: list[tuple[Any, ...]],
            width: int,
            height: int,
            **kwargs: Any,
        ) -> np.ndarray:
            raise AssertionError("single layer must ignore blend mode")

    monkeypatch.setattr(
        renderer_module, "TimelineGpuExportRenderer", _FakeRenderer)
    ex = _make_exporter(project, provider)

    result = ex._gpu_render_composite([layer], (), 128, 72)

    assert result is not None
    assert result.shape == (72, 128, 3)
    assert len(calls) == 1
    assert len(requests) == 1


def test_gpu_single_layer_active_dip_uses_layer_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单层有效 dip_to_color 必须进入能消费 dip 参数的 stack renderer。"""
    import src.features.edit.core.timeline_gpu_export_renderer as renderer_module

    _planes, _meta, layer, project, provider, _requests = _make_video_env()
    # A first/only layer's stored blend mode is ignored by CPU composition.
    # The stack path used for dip must preserve that behavior explicitly.
    layer.clip.blend_mode = SimpleNamespace(code=3)
    received: dict[str, Any] = {}

    class _FakeRenderer:
        failed = False
        gl_errors = 0

        def render_planar(self, *args: Any, **kwargs: Any) -> np.ndarray:
            raise AssertionError("active dip must use layer-stack path")

        def render_packed_bgr(self, *args: Any, **kwargs: Any) -> np.ndarray:
            raise AssertionError("active dip must use layer-stack path")

        def render_layer_stack(
            self,
            layers: list[tuple[Any, ...]],
            width: int,
            height: int,
            **kwargs: Any,
        ) -> np.ndarray:
            received["layers"] = layers
            received.update(kwargs)
            return np.full((height, width, 3), 31, dtype=np.uint8)

    monkeypatch.setattr(
        renderer_module, "TimelineGpuExportRenderer", _FakeRenderer)
    ex = _make_exporter(project, provider)
    dip = SimpleNamespace(
        transition_type="dip_to_color",
        transition_id="dip-test",
        left_clip_id=None,
        right_clip_id=None,
        get_color_factor=lambda: 0.4,
        blend_color=(1.0, 0.5, 0.25),
    )

    result = ex._gpu_render_composite([layer], (dip,), 128, 72)

    assert result is not None
    assert received["layers"][0][3] == 0
    assert received["dip_color"] == (1.0, 0.5, 0.25)
    assert received["dip_factor"] == 0.4


def test_pipeline_releases_gpu_renderer_on_producer_thread():
    """P0-GPU-1 线程亲和契约：流水线模式下 GPU context 必须由 render
    生产者线程（创建线程）释放，而非导出/消费线程。"""
    import threading

    released_threads = []

    class _FakeRenderer:
        def release(self) -> None:
            released_threads.append(threading.get_ident())

    ex = _make_exporter()
    ex._gpu_export_renderer = _FakeRenderer()
    ex._render_plan = SimpleNamespace()
    ex._cancel_requested = False
    # 生产 __init__ 初始化的暂停协议状态（_wait_if_paused 读取）
    ex._pause_requested = False
    ex._resume_event = threading.Event()
    ex._current_frame = 0
    ex._total_frames = 2
    ex._progress_callback = None
    ex._render_frame = lambda fi, cfg: None  # noqa: E731
    ex._encode_frame = lambda frame: None  # noqa: E731
    ex._render_encode_pipeline(0, 2, SimpleNamespace())

    assert ex._gpu_export_renderer is None  # 释放后被清空
    assert len(released_threads) == 1, "GPU 渲染器应恰好释放一次"
    assert released_threads[0] != threading.get_ident(), (
        "GPU 渲染器不得在导出（消费）线程释放"
    )


# ---------------------------------------------------------------------------
# GL 可用时的像素一致性（CI/headless 自动 skip）
# ---------------------------------------------------------------------------

def _gl_available() -> bool:
    try:
        import moderngl

        ctx = moderngl.create_standalone_context(require=330)
        ctx.release()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_planar_parity_vs_cpu_kernel():
    import cv2

    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuvSource, TimelineGpuExportRenderer, grade_params_to_uniforms,
    )

    for fmt, cx, cy, bits in (("yuv422p10le", 2, 1, 10),
                              ("yuv420p", 2, 2, 8)):
        planes = _make_planes(fmt)
        meta = SimpleNamespace(color_range="limited", color_matrix="bt709")
        ex = _make_exporter()
        cpu = ex._planes_to_bgr_float(planes, fmt, meta, None, False)
        cpu = np.rint(np.clip(cpu, 0.0, 1.0) * 255.0).astype(np.uint8)
        cpu = cv2.resize(cpu, (192, 128), interpolation=cv2.INTER_LINEAR)
        cpu = np.rint(np.clip(
            cpu.astype(np.float32) / 255.0, 0.0, 1.0) * 255.0).astype(np.uint8)

        renderer = TimelineGpuExportRenderer()
        try:
            gpu = renderer.render_planar(
                GpuYuvSource(planes=planes, luma_bits=bits,
                             chroma_x_div=cx, chroma_y_div=cy,
                             full_range=False, kr=0.2126, kb=0.0722),
                None, 192, 128, 1.0)
            assert gpu is not None, f"{fmt}: GPU 渲染失败"
            diff = np.abs(cpu.astype(np.int16) - gpu.astype(np.int16))
            # 420 两轴插值的 cv2 定点残差包络 ±3；422 ≤2
            bar = 3 if "420" in fmt else 2
            assert diff.max() <= bar, (
                f"{fmt}: maxdiff {diff.max()} 超出 {bar} LSB 约定")
        finally:
            renderer.release()


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_grade_bit_exact_vs_cpu_grading_math():
    import cv2

    from src.features.color.core.the_decompiler import GradingMath
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuvSource, TimelineGpuExportRenderer, grade_params_to_uniforms,
    )

    planes = _make_planes("yuv422p10le")
    meta = SimpleNamespace(color_range="limited", color_matrix="bt709")
    ex = _make_exporter()
    frame = np.rint(np.clip(
        ex._planes_to_bgr_float(planes, "yuv422p10le", meta, None, False),
        0.0, 1.0) * 255.0).astype(np.uint8)
    rgb = frame[..., ::-1].astype(np.float32) / 255.0
    graded = GradingMath.apply_grading(
        rgb.reshape(-1, 3), GRADE_TYPICAL).reshape(rgb.shape)
    cpu = np.rint(np.clip(graded[..., ::-1], 0.0, 1.0)
                  * 255.0).astype(np.uint8)
    cpu = cv2.resize(cpu, (192, 128), interpolation=cv2.INTER_LINEAR)

    renderer = TimelineGpuExportRenderer()
    try:
        gpu = renderer.render_planar(
            GpuYuvSource(planes=planes, luma_bits=10, chroma_x_div=2,
                         chroma_y_div=1, full_range=False,
                         kr=0.2126, kb=0.0722),
            grade_params_to_uniforms(GRADE_TYPICAL), 192, 128, 1.0)
        assert gpu is not None
        diff = np.abs(cpu.astype(np.int16) - gpu.astype(np.int16))
        # 本机实测口径：典型调色下与 CPU 链逐位一致
        assert diff.max() <= 2, f"maxdiff {diff.max()}"
    finally:
        renderer.release()


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_yuv420_output_matches_pyav_bt709_conversion():
    """GPU SDR output planes stay within 2 codes of the established swscale path."""
    av = pytest.importorskip("av")
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuv420Output, TimelineGpuExportRenderer,
    )

    rng = np.random.default_rng(20260914)
    height, width = 108, 192
    source = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    renderer = TimelineGpuExportRenderer()
    try:
        bgr = renderer.render_packed_bgr(
            source, None, width, height, 0.87)
        assert bgr is not None
        gpu = renderer.render_packed_bgr(
            source, None, width, height, 0.87, output_yuv420p=True)
        assert isinstance(gpu, GpuYuv420Output)
        assert [p.shape for p in gpu.planes] == [
            (height, width), (height // 2, width // 2),
            (height // 2, width // 2),
        ]

        cpu_rgb = av.VideoFrame.from_ndarray(bgr, format="bgr24")
        cpu_yuv = cpu_rgb.reformat(
            format="yuv420p", dst_colorspace="ITU709",
            src_color_range="JPEG", dst_color_range="MPEG")
        shapes = ((height, width),
                  (height // 2, width // 2),
                  (height // 2, width // 2))
        for index, (actual, shape) in enumerate(zip(gpu.planes, shapes)):
            plane = cpu_yuv.planes[index]
            expected = np.frombuffer(
                plane, dtype=np.uint8).reshape(shape[0], plane.line_size)
            expected = expected[:, :shape[1]]
            delta = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
            assert delta.max() <= 2, (
                f"plane {index}: max difference {delta.max()} codes")
    finally:
        renderer.release()


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_direct_yuv420_resize_matches_code_domain_reference():
    """The SDR delivery fast path matches code-domain bilinear resizing."""
    import cv2

    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuv420Output, GpuYuvSource, TimelineGpuExportRenderer,
    )

    planes = _make_planes("yuv420p")
    source = GpuYuvSource(
        planes=planes, luma_bits=8, chroma_x_div=2, chroma_y_div=2,
        full_range=False, kr=0.2126, kb=0.0722)
    direct_renderer = TimelineGpuExportRenderer()
    try:
        direct = direct_renderer.render_planar(
            source, None, 64, 48, 0.87, output_yuv420p=True)
        assert isinstance(direct, GpuYuv420Output)
        assert direct_renderer.direct_yuv420_frames == 1

        def _resize_plane(plane: np.ndarray, neutral: float,
                          low: float, high: float,
                          out_size: tuple[int, int]) -> np.ndarray:
            resized = cv2.resize(
                plane.astype(np.float32), out_size,
                interpolation=cv2.INTER_LINEAR)
            codes = neutral + (resized - neutral) * 0.87
            return np.rint(np.clip(codes, low, high)).astype(np.uint8)

        expected = (
            _resize_plane(planes[0], 16.0, 16.0, 235.0, (64, 48)),
            _resize_plane(planes[1], 128.0, 16.0, 240.0, (32, 24)),
            _resize_plane(planes[2], 128.0, 16.0, 240.0, (32, 24)),
        )
        for index, (actual, expected_plane) in enumerate(
                zip(direct.planes, expected)):
            delta = np.abs(actual.astype(np.int16)
                           - expected_plane.astype(np.int16))
            assert delta.max() <= 2, (
                f"plane {index}: max difference {delta.max()} codes")
    finally:
        direct_renderer.release()


# ---------------------------------------------------------------------------
# P0-GPU-2：高精度（u16）交付
# ---------------------------------------------------------------------------

def test_decoded_frame_is_high_depth_detection():
    ex = _make_exporter()
    planar10 = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        planes=_make_planes("yuv422p10le"), pixel_format="yuv422p10le")
    packed48 = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        data=np.zeros((8, 8, 3), dtype=np.uint16), pixel_format="rgb48le")
    packed8 = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        data=np.zeros((8, 8, 3), dtype=np.uint8), pixel_format="bgr24")
    assert ex._decoded_frame_is_high_depth(planar10)
    assert ex._decoded_frame_is_high_depth(packed48)
    assert not ex._decoded_frame_is_high_depth(packed8)


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_u16_delivery_parity_and_dtype():
    """高精度交付：10-bit 源走 RGB16UI 全链，u16 恒等 ≤8 code16、
    典型调色与 CPU 逐位一致（本机实测口径）。"""
    import cv2

    from src.features.color.core.the_decompiler import GradingMath
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuvSource, TimelineGpuExportRenderer, grade_params_to_uniforms,
    )

    planes = _make_planes("yuv422p10le")
    meta = SimpleNamespace(color_range="limited", color_matrix="bt709")
    ex = _make_exporter()

    def _cpu_ref(grade):
        frame = ex._convert_yuv_planes_to_bgr16(planes, "yuv422p10le", meta)
        scale = 65535.0
        if grade is not None:
            rgb = frame[..., ::-1].astype(np.float32) / scale
            graded = GradingMath.apply_grading(
                rgb.reshape(-1, 3), grade).reshape(rgb.shape)
            frame = np.rint(np.clip(graded[..., ::-1], 0.0, 1.0)
                            * scale).astype(np.uint16)
        resized = cv2.resize(frame, (192, 128),
                             interpolation=cv2.INTER_LINEAR)
        return np.rint(np.clip(
            resized.astype(np.float32) / scale, 0.0, 1.0)
            * scale).astype(np.uint16)

    renderer = TimelineGpuExportRenderer()
    try:
        src = GpuYuvSource(planes=planes, luma_bits=10, chroma_x_div=2,
                           chroma_y_div=1, full_range=False,
                           kr=0.2126, kb=0.0722)
        gpu = renderer.render_planar(src, None, 192, 128, 1.0,
                                     output_u16=True)
        assert gpu is not None
        assert gpu.dtype == np.uint16 and gpu.shape == (128, 192, 3)
        diff = np.abs(_cpu_ref(None).astype(np.int32) - gpu.astype(np.int32))
        assert diff.max() <= 8, f"identity maxdiff {diff.max()} code16"

        gpu_g = renderer.render_planar(
            src, grade_params_to_uniforms(GRADE_TYPICAL), 192, 128, 1.0,
            output_u16=True)
        assert gpu_g is not None
        diff_g = np.abs(
            _cpu_ref(GRADE_TYPICAL).astype(np.int32)
            - gpu_g.astype(np.int32))
        assert diff_g.max() <= 8, f"graded maxdiff {diff_g.max()} code16"
    finally:
        renderer.release()


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_high_precision_routes_packed_provider_to_hd_planar_source():
    """provider 帧为 rgb48le packed（GPU 不可用形态）时，高精度导出镜像
    CPU 解码偏好改走原生平面源，输出 u16 GPU 帧。"""
    planes, meta, layer, project, provider, requests = _make_video_env(
        "yuv422p10le")
    hd_decoded = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        planes=planes, pixel_format="yuv422p10le", metadata=meta)
    packed48 = DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0,
        data=np.zeros((64, 96, 3), dtype=np.uint16),
        pixel_format="rgb48le", metadata=meta)

    def _request(clip_id, frame, quality="master"):
        requests.append((clip_id, frame, quality))
        return packed48

    provider.request_frame = _request
    ex = _make_exporter(project, provider)
    ex._use_high_precision_export = True
    ex._request_high_depth_decoded_frame = (
        lambda path, frame, asset_id: hd_decoded)

    try:
        result = ex._gpu_render_composite([layer], (), 128, 64)
        assert result is not None
        assert result.dtype == np.uint16
        assert result.shape == (64, 128, 3)
        assert len(requests) == 1  # hd 路由只消费一次 provider 帧
    finally:
        ex._release_gpu_export_renderer()


# ---------------------------------------------------------------------------
# P0-GPU-3：多层 GPU 合成（RGBA32F 累积）
# ---------------------------------------------------------------------------

def _cpu_stack_reference(ex, layer_inputs, meta, out_w, out_h,
                         output_u16=False):
    """多层 CPU 参照（镜像 _composite_layers 层循环，产品函数口径）。"""
    import cv2

    composite = None
    for src, fmt, grade, opacity in layer_inputs:
        if isinstance(src, tuple):
            if output_u16:
                frame = ex._convert_yuv_planes_to_bgr16(src, fmt, meta)
            else:
                f = ex._planes_to_bgr_float(src, fmt, meta, None, False)
                frame = np.rint(np.clip(f, 0.0, 1.0) * 255.0).astype(np.uint8)
        else:
            frame = src
        scale = float(np.iinfo(frame.dtype).max)
        if grade is not None:
            from src.features.color.core.the_decompiler import GradingMath

            rgb = frame[..., ::-1].astype(np.float32) / scale
            graded = GradingMath.apply_grading(
                rgb.reshape(-1, 3), grade).reshape(rgb.shape)
            frame = np.rint(np.clip(graded[..., ::-1], 0.0, 1.0)
                            * scale).astype(frame.dtype)
        resized = cv2.resize(frame, (out_w, out_h),
                             interpolation=cv2.INTER_LINEAR)
        layer_float = resized.astype(np.float32) / scale
        if composite is None:
            composite = layer_float * opacity
        else:
            composite = composite * (1.0 - opacity) + layer_float * opacity
    out_max = 65535.0 if output_u16 else 255.0
    return np.rint(np.clip(composite, 0.0, 1.0) * out_max).astype(
        np.uint16 if output_u16 else np.uint8)


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_multi_layer_stack_parity():
    """2 层（422 调色 + 图片 0.65）与 CPU 层循环口径对齐：u8 ≤4 LSB。"""
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuvSource, TimelineGpuExportRenderer, grade_params_to_uniforms,
    )

    meta = SimpleNamespace(color_range="limited", color_matrix="bt709")
    ex = _make_exporter()
    planes = _make_planes("yuv422p10le", 96, 64)
    img = np.rint(np.clip(
        ex._planes_to_bgr_float(_make_planes("yuv420p", 96, 64),
                                "yuv420p", meta, None, False),
        0.0, 1.0) * 255.0).astype(np.uint8)
    src = GpuYuvSource(planes=planes, luma_bits=10, chroma_x_div=2,
                       chroma_y_div=1, full_range=False, kr=0.2126,
                       kb=0.0722)

    renderer = TimelineGpuExportRenderer()
    try:
        gpu = renderer.render_layer_stack(
            [(src, grade_params_to_uniforms(GRADE_TYPICAL), 1.0),
             (img, None, 0.65)], 192, 128)
        assert gpu is not None
        assert gpu.dtype == np.uint8 and gpu.shape == (128, 192, 3)
        cpu = _cpu_stack_reference(
            ex, [(planes, "yuv422p10le", GRADE_TYPICAL, 1.0),
                 (img, None, None, 0.65)], meta, 192, 128)
        diff = np.abs(cpu.astype(np.int16) - gpu.astype(np.int16))
        assert diff.max() <= 4, f"maxdiff {diff.max()}"
    finally:
        renderer.release()


def test_multi_layer_integration_video_plus_image():
    """视频层 + 图片层走 _composite_layers GPU 钩子（无 GL 自动回落）。"""
    planes, meta, layer, project, provider, requests = _make_video_env(
        "yuv422p10le")
    ex = _make_exporter()
    img = np.rint(np.clip(
        ex._planes_to_bgr_float(_make_planes("yuv420p", 96, 64),
                                "yuv420p",
                                SimpleNamespace(color_range="limited",
                                                color_matrix="bt709"),
                                None, False),
        0.0, 1.0) * 255.0).astype(np.uint8)
    asset2 = SimpleNamespace(
        media_type="image", asset_id="a2", path="fake_img.png",
        effective_path=None)
    project2 = SimpleNamespace(
        color_stacks=project.color_stacks,
        get_asset_by_id=lambda aid: (
            project.get_asset_by_id("a1") if aid == "a1" else asset2))
    layer2 = SimpleNamespace(
        clip_id="c2", asset_id="a2", source_frame=0, opacity=0.65,
        crop=(), track_index=1,
        clip=SimpleNamespace(effects=(), color_stack_id=None))
    integ = _make_exporter(project2, provider)
    integ._get_image_frame = lambda p: img
    try:
        result = integ._gpu_render_composite([layer, layer2], (), 192, 128)
        if result is not None:
            assert result.dtype == np.uint8
            assert result.shape == (128, 192, 3)
            assert len(requests) == 1  # 仅视频层取帧一次
    finally:
        integ._release_gpu_export_renderer()


# ---------------------------------------------------------------------------
# P0-GPU-4：转场（dissolve/wipe/dip）
# ---------------------------------------------------------------------------

def _make_transition_env():
    planes, meta, layer, project, provider, requests = _make_video_env(
        "yuv422p10le")
    l2 = SimpleNamespace(
        clip_id="c2", asset_id="a1", source_frame=0, opacity=1.0,
        crop=(), track_index=1,
        clip=SimpleNamespace(effects=(), color_stack_id=None))
    return planes, meta, layer, l2, project, provider, requests


def _fake_trans(ttype, left_id, right_id, lo=1.0, ro=1.0,
                factor=0.3, color=(1.0, 0.5, 0.0)):
    return SimpleNamespace(
        transition_type=ttype,
        transition_id=f"t-{ttype}",
        left_clip_id=left_id, right_clip_id=right_id,
        blend_factor=0.4,
        left_opacity=lo, right_opacity=ro,
        transition=SimpleNamespace(params={"direction": "right"}),
        get_color_factor=lambda: factor,
        blend_color=color)


def test_gpu_transition_routing_reaches_provider():
    """转场不再被提前短路：wipe 帧的两侧都完成取帧后才进入渲染。"""
    _planes, _meta, l1, l2, project, provider, requests = (
        _make_transition_env())
    ex = _make_exporter(project, provider)
    try:
        # 无 GL 环境在渲染阶段返回 None，但路由必须已消费两侧帧
        ex._gpu_render_composite(
            [l1, l2], (_fake_trans("wipe", "c1", "c2"),), 128, 64)
        assert len(requests) == 2
    finally:
        ex._release_gpu_export_renderer()


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_transitions_parity_vs_real_cpu_path():
    """GPU 转场 vs 真实 _composite_layers CPU 路径（开/关对拍）：
    dissolve/wipe/dip 均 ≤4 LSB。"""
    _planes, _meta, l1, l2, project, provider, _requests = (
        _make_transition_env())
    cases = [
        ("dissolve", _fake_trans("dissolve", "c1", "c2", 0.35, 0.75)),
        ("wipe", _fake_trans("wipe", "c1", "c2")),
        ("dip_to_color", _fake_trans("dip_to_color", "c1", "c2")),
    ]
    config = SimpleNamespace(resolution=(192, 128))
    for label, trans in cases:
        ex_g = _make_exporter(project, provider)
        gpu = ex_g._composite_layers([l1, l2], (trans,), config, 0)
        ex_g._release_gpu_export_renderer()
        ex_c = _make_exporter(project, provider)
        ex_c._gpu_render_disabled = True
        cpu = ex_c._composite_layers([l1, l2], (trans,), config, 0)
        assert gpu is not None, f"{label}: GPU 渲染失败"
        assert cpu is not None
        diff = np.abs(cpu.astype(np.int16) - gpu.astype(np.int16))
        assert diff.max() <= 4, f"{label}: maxdiff {diff.max()}"


# ======================================================================
# CPU/GPU 自适应选路（2026-09-16 提速计划：主机可见帧按任务复杂度选路）
# ======================================================================

def _policy_layer(**clip_overrides: Any) -> SimpleNamespace:
    """直通形状的单层 ClipLayerInfo double（与直通测试同构）。"""
    clip_fields: dict[str, Any] = dict(
        clip_id="clip-1",
        effects=(),
        blend_mode=SimpleNamespace(value="normal"),
        speed=SimpleNamespace(
            speed=1.0, reverse=False, interpolation="nearest",
            speed_points=()),
        freeze_source_frame=None,
        color_stack_id=None,
    )
    clip_fields.update(clip_overrides)
    clip = SimpleNamespace(**clip_fields)
    return SimpleNamespace(
        clip=clip,
        clip_id="clip-1",
        crop=(0.0, 0.0, 0.0, 0.0, 0.0),
        transform=(0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
    )


def _policy_exporter(**overrides: Any) -> Any:
    ex = _make_exporter(SimpleNamespace(color_stacks={}))
    ex._timeline_graphics_backend = "opengl"
    ex._gpu_render_forced = False
    reasons: list[str] = []
    ex._gpu_trail = SimpleNamespace(set_fallback_reason=reasons.append)
    for name, value in overrides.items():
        setattr(ex, name, value)
    return ex, reasons


def test_adaptive_policy_skips_gpu_for_trivial_single_layer_on_opengl():
    """passthrough 形状的单层帧（主机可见源）直接走 CPU 合成。"""
    ex, reasons = _policy_exporter()

    reason = ex._adaptive_gpu_composite_skip_reason(
        [_policy_layer()], (), "opengl")

    assert reason == (
        "adaptive_policy:cpu_preferred_single_layer_trivial_composite")
    # 集成口径：_gpu_render_composite 在取帧/拷贝前提前返回并记录原因
    assert ex._gpu_render_composite([_policy_layer()], (), 8, 4) is None
    assert reasons == [
        "adaptive_policy:cpu_preferred_single_layer_trivial_composite"]


@pytest.mark.parametrize(
    ("layers", "transitions"),
    (
        # 多层混合：GPU 值得（合成即逐像素工作）
        (None, ()),
        # 转场：双源合成必须走 GPU 栈
        ([_policy_layer()], object()),
    ),
)
def test_adaptive_policy_keeps_gpu_for_multi_layer_or_transitions(
    layers, transitions,
):
    ex, _reasons = _policy_exporter()
    if layers is None:
        layers = [_policy_layer(clip_id="c1"), _policy_layer(clip_id="c2")]

    assert ex._adaptive_gpu_composite_skip_reason(
        layers, transitions, "opengl") is None


@pytest.mark.parametrize(
    "clip_overrides",
    (
        # 特效链：GLSL/GPU 栈渲染
        {"effects": (SimpleNamespace(is_enabled=True),)},
        # 非常规混合模式
        {"blend_mode": SimpleNamespace(value="screen")},
        # 帧混合变速：双源插值
        {"speed": SimpleNamespace(
            speed=1.0, reverse=False, interpolation="frame_blend",
            speed_points=())},
    ),
)
def test_adaptive_policy_keeps_gpu_for_pixel_work(clip_overrides):
    ex, _reasons = _policy_exporter()

    assert ex._adaptive_gpu_composite_skip_reason(
        [_policy_layer(**clip_overrides)], (), "opengl") is None


def test_adaptive_policy_keeps_gpu_for_geometry_or_grade():
    ex, reasons = _policy_exporter()
    layer = _policy_layer()
    layer.transform = (4.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
    assert ex._adaptive_gpu_composite_skip_reason(
        [layer], (), "opengl") is None

    # 调色栈激活（非默认 grading_params）→ GPU
    graded = _policy_layer(color_stack_id="stack-1")
    ex._project.color_stacks["stack-1"] = SimpleNamespace(
        grading_params={"exposure": 1.0},
        curves_data=None, hsl_curve_data=None, mixer_params=None,
        lut_path=None, layers=(), active_layer_index=None)
    assert ex._adaptive_gpu_composite_skip_reason(
        [graded], (), "opengl") is None
    assert reasons == []


def test_adaptive_policy_defers_to_forced_gpu():
    """TOPOS_EXPORT_GPU_RENDER=1（强制 GPU）时自适应让位。"""
    ex, reasons = _policy_exporter(_gpu_render_forced=True)

    assert ex._adaptive_gpu_composite_skip_reason(
        [_policy_layer()], (), "opengl") is None


def test_adaptive_policy_keeps_gpu_on_native_surface_backend():
    """Metal 源帧 native GPU 驻留（无上传/读回），不适用主机可见判据。"""
    ex, reasons = _policy_exporter()

    assert ex._adaptive_gpu_composite_skip_reason(
        [_policy_layer()], (), "metal") is None
    assert reasons == []


# ======================================================================
# OpenGL 导出效果链门控（2026-09-17：blur GPU 化，pass2.5）
# ======================================================================

def _blur_effect(**param_overrides):
    params = {
        "blur_type": "gaussian", "radius": 12.0, "mix": 1.0,
        "edge_policy": "clamp",
        "channel_r": True, "channel_g": True, "channel_b": True,
        "channel_a": False,
    }
    params.update(param_overrides)
    return SimpleNamespace(
        effect_type="blur", order=0, is_enabled=True, params=params)


def _gate_exporter():
    _planes, _meta, layer, project, provider, _requests = _make_video_env()
    ex = _make_exporter(project, provider)
    return ex, layer


def test_opengl_effect_chain_accepts_gaussian_blur():
    """高斯模糊（separable，半径≤16）→ 放行，链元素为 (spec, params)。"""
    ex, layer = _gate_exporter()
    layer.clip.effects = (_blur_effect(),)
    supported, chain = ex._gpu_opengl_effect_chain(layer, 0)
    assert supported
    assert chain is not None and len(chain) == 1
    spec, params = chain[0]
    assert spec.dispatch_key == "blur"
    assert float(params["radius"]) == 12.0


def test_opengl_effect_chain_accepts_sharpen_and_glow():
    """2026-09-17 扩展：7 种内置效果全部放行（Metal 校验器同集）。"""
    ex, layer = _gate_exporter()
    layer.clip.effects = (SimpleNamespace(
        effect_type="sharpen", order=0, is_enabled=True,
        params={"amount": 1.0, "radius": 2.0}),)
    supported, chain = ex._gpu_opengl_effect_chain(layer, 0)
    assert supported and chain is not None and len(chain) == 1
    assert chain[0][0].dispatch_key == "sharpen"

    layer.clip.effects = (SimpleNamespace(
        effect_type="glow", order=0, is_enabled=True,
        params={"radius": 15.0, "intensity": 1.5}),)
    supported, chain = ex._gpu_opengl_effect_chain(layer, 0)
    assert supported and chain is not None and chain[0][0].dispatch_key == "glow"


def test_opengl_effect_chain_rejects_unknown_effect():
    """目录外/无 runner 执行路径的效果类型整帧回落 CPU（fail-closed）。"""
    ex, layer = _gate_exporter()
    layer.clip.effects = (SimpleNamespace(
        effect_type="definitely_not_an_effect", order=0, is_enabled=True,
        params={}),)
    supported, chain = ex._gpu_opengl_effect_chain(layer, 0)
    assert not supported
    assert chain is None


def test_opengl_effect_chain_skips_noop_radius():
    """半径≈0 的 blur 是逐像素 no-op → 跳过，链为空。"""
    ex, layer = _gate_exporter()
    layer.clip.effects = (_blur_effect(radius=0.0),)
    supported, chain = ex._gpu_opengl_effect_chain(layer, 0)
    assert supported
    assert chain is None


def test_opengl_effect_chain_rejects_alpha_channel_mask():
    """alpha 通道掩码在 RGB 导出链上无对应语义 → 拒绝。"""
    ex, layer = _gate_exporter()
    layer.clip.effects = (_blur_effect(channel_a=True),)
    supported, chain = ex._gpu_opengl_effect_chain(layer, 0)
    assert not supported
    assert chain is None


def test_opengl_effect_chain_rejects_partial_rgb_mask():
    """部分 RGB 通道掩码与 Metal 门语义一致（拒绝，走 CPU）。"""
    ex, layer = _gate_exporter()
    layer.clip.effects = (_blur_effect(channel_g=False),)
    supported, chain = ex._gpu_opengl_effect_chain(layer, 0)
    assert not supported
    assert chain is None


def test_opengl_effect_chain_rejects_oversized_radius():
    """半径 >16（MSL 静态 tap 上限门）→ 两后端一致拒绝。"""
    ex, layer = _gate_exporter()
    layer.clip.effects = (_blur_effect(radius=64.0),)
    supported, chain = ex._gpu_opengl_effect_chain(layer, 0)
    assert not supported
    assert chain is None


# ======================================================================
# OpenGL 几何契约门（2026-09-17 复查修复：transform/crop 静默丢失）
# ======================================================================

def test_opengl_rejects_non_identity_transform_before_provider():
    """OpenGL 渲染器不实现 clip 几何：非恒等 transform fail-closed 回落
    CPU（修复前被静默丢弃，2x scale 输出 MAD 13.7/255）。"""
    _planes, _meta, layer, project, provider, requests = _make_video_env()
    layer.transform = (0.0, 0.0, 2.0, 2.0, 0.0, "center", 0.0, 0.0)
    ex = _make_exporter(project, provider)
    assert ex._gpu_render_composite([layer], (), 128, 72) is None
    assert requests == []


def test_opengl_rejects_non_identity_crop_before_provider():
    _planes, _meta, layer, project, provider, requests = _make_video_env()
    layer.crop = (0.1, 0.1, 0.1, 0.1, 0.0)
    ex = _make_exporter(project, provider)
    assert ex._gpu_render_composite([layer], (), 128, 72) is None
    assert requests == []


def test_opengl_wipe_side_clip_effects_fall_back_before_provider():
    """wipe 双源 pass 的 side effects 槽位仅 Metal 填充：OpenGL 侧层挂
    效果时整帧 fail-closed（修复前效果被静默丢弃）。"""
    _planes, _meta, left, project, provider, requests = _make_video_env()
    right = SimpleNamespace(
        **{**left.__dict__, "clip_id": "c2", "asset_id": "a2",
        "track_index": 1})
    left.clip.effects = (SimpleNamespace(
        effect_type="blur", order=0, is_enabled=True, params={}),)
    transition = SimpleNamespace(
        transition_type="wipe", transition_id="wipe-fx",
        left_clip_id="c1", right_clip_id="c2", blend_factor=0.5,
        left_opacity=1.0, right_opacity=1.0,
        transition=SimpleNamespace(params={"direction": "left"}),
    )
    ex = _make_exporter(project, provider)
    assert ex._gpu_render_composite(
        [left, right], (transition,), 128, 72) is None
    assert requests == []
