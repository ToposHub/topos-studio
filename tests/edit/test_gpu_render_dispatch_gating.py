"""T-P1-21：GPU 导出分派门控与 shader kernel 性能契约回归。

钉死两类已修复缺陷：
1. ``output_yuv422p10`` 默认只传给 Metal，OpenGL 仅单层平面路径显式启用。
   此前不分后端传入——
   macOS auto→opengl 下每帧 TypeError→整帧回落 CPU float 管线（grade
   实测 1.4fps，forced_gpu 与 cpu_render 同速）。
2. 效果 GLSL 的静态 kernel 循环（``for (int i = -MAX_KERNEL; ...)`` +
   逐迭代 continue 守卫）在 AMD 上不因 continue 跳过成本——blur radius
   12 时 129 次迭代仅 25 次有效采样、glow/defocus 257 次。已改动态边界；
   扫描 catalog shader 源防止回潮。
"""

from __future__ import annotations

import inspect
from concurrent.futures import Future
from types import SimpleNamespace

from src.features.edit.core.timeline_export import TimelineExporter
from src.features.shared.effects import get_effect_catalog
from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFrameRelease


class TestGpuOutputKwargsGating:
    def test_opengl_never_receives_yuv422p10(self) -> None:
        kwargs = TimelineExporter._gpu_render_output_kwargs(
            "opengl", output_u16=True, output_yuv420p=False,
            output_yuv422p10=True)
        assert "output_yuv422p10" not in kwargs
        assert kwargs["output_u16"] is True

    def test_metal_receives_yuv422p10(self) -> None:
        kwargs = TimelineExporter._gpu_render_output_kwargs(
            "metal", output_u16=False, output_yuv420p=False,
            output_yuv422p10=True)
        assert kwargs["output_yuv422p10"] is True

    def test_yuv420p_passes_for_any_backend(self) -> None:
        for backend in ("opengl", "metal"):
            kwargs = TimelineExporter._gpu_render_output_kwargs(
                backend, output_u16=False, output_yuv420p=True,
                output_yuv422p10=False)
            assert kwargs["output_yuv420p"] is True

    def test_dip_kwargs_only_when_requested(self) -> None:
        plain = TimelineExporter._gpu_render_output_kwargs(
            "opengl", output_u16=False, output_yuv420p=False,
            output_yuv422p10=False)
        assert "dip_color" not in plain
        dipped = TimelineExporter._gpu_render_output_kwargs(
            "opengl", output_u16=False, output_yuv420p=False,
            output_yuv422p10=False, include_dip=True,
            dip_color=(0.0, 0.0, 0.0), dip_factor=0.5)
        assert dipped["dip_color"] == (0.0, 0.0, 0.0)
        assert dipped["dip_factor"] == 0.5


class TestGlRendererSignatureContract:
    def test_only_the_gl_planar_entry_accepts_yuv422p10(self) -> None:
        from src.features.edit.core.timeline_gpu_export_renderer import (
            TimelineGpuExportRenderer,
        )

        for method in ("render_planar", "render_packed_bgr",
                       "render_layer_stack"):
            params = inspect.signature(
                getattr(TimelineGpuExportRenderer, method)).parameters
            assert ("output_yuv422p10" in params) == (method == "render_planar")


def test_topos_gpu_planes_require_no_program_matte() -> None:
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._timeline_graphics_backend = "metal"
    exporter._encoder = SimpleNamespace(
        _config=SimpleNamespace(codec="topos", pix_fmt="yuv422p10le"))
    exporter._export_color_metadata = SimpleNamespace(
        primaries="bt709", transfer="bt709", matrix="bt709", range="limited")
    params = {"fill_black": True, "burn_in": False}
    exporter._sequence = SimpleNamespace(format_settings={
        "program_guide": {"type": "aspect_ratio", "params": params},
    })
    config = SimpleNamespace(burn_subtitles=False)

    assert exporter._gpu_yuv422p10_output_allowed(config, 3840, 2160)
    params["burn_in"] = True
    assert not exporter._gpu_yuv422p10_output_allowed(config, 3840, 2160)


def test_unused_lut_prefetch_releases_its_frame_once() -> None:
    exporter = TimelineExporter.__new__(TimelineExporter)
    released: list[int] = []
    frame = GpuDecodedFrame(
        frame_index=1, pts=1, time_seconds=0.04, width=8, height=4,
        visible_rect=None, pixel_format="yuv422p10le", bit_depth=10,
        planes=(), zero_copy=False,
        release=GpuFrameRelease(release_callback=lambda: released.append(1)),
    )
    ready: Future[GpuDecodedFrame] = Future()
    ready.set_result(frame)
    exporter._topos_lut_prefetch = (1, ready)

    assert exporter._take_topos_lut_prefetch(2) is None
    exporter._discard_topos_lut_prefetch()
    exporter._discard_topos_lut_prefetch()

    assert exporter._topos_lut_prefetch is None
    assert released == [1]


class TestEffectShaderKernelContract:
    """效果 shader 禁止静态 kernel 循环 + continue 守卫（AMD 全代价）。"""

    def test_no_static_kernel_loops_in_catalog_shaders(self) -> None:
        bad_marker = "for (int i = -MAX_KERNEL"
        offenders = []
        for spec in get_effect_catalog().get_all_specs():
            sources = list(spec.shader_sources or {})
            for variant in (spec.shader_variants or {}).values():
                if isinstance(variant, str):
                    sources.append(variant)
            for source in sources:
                if bad_marker in source:
                    offenders.append(spec.effect_type)
                    break
        assert not offenders, (
            f"以下效果 shader 仍用静态 MAX_KERNEL 循环（AMD 上 continue "
            f"守卫不省迭代成本，radius 越小浪费越大）: {offenders}"
        )

    def test_separable_blur_uses_dynamic_kernel_bound(self) -> None:
        spec = get_effect_catalog().get_spec("blur")
        assert spec is not None
        h_pass = list(spec.shader_sources)[0]
        assert "for (int i = -kernel_size; i <= kernel_size; i++)" in h_pass
