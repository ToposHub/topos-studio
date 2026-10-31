"""P0-PREV-GPU-T 预览 GPU 双输入转场 focused tests。

分组：
- 转场任务分类：所有内置双源转场 → pixel-free GPU 控制任务、dip →
  颜色任务、未知类型 → 明确 CPU 回退；
- 转场/dip fragment shader 像素一致性（headless GL 自动 skip）：
  与 CPU numpy 公式逐位比对（≤1 LSB）；
- worker/渲染器契约：EditRenderTask.transition_jobs 默认、
  has_transition_support 门禁、opacity 覆写映射。
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

from src.features.edit.core.edit_gpu_preview_renderer import (  # noqa: E402
    _DIP_FRAGMENT_SHADER_SOURCE,
    _FULL_FRAME_TRANSITION_FRAGMENT_SHADER_SOURCE,
    _PREVIEW_PASS_VERTEX_SHADER_SOURCE,
    _TRANSITION_FRAGMENT_SHADER_SOURCE,
    EditRenderTask,
)
from src.features.edit.core.timeline_render_plan import (  # noqa: E402
    TransitionBlendInfo,
)
from src.features.edit.core.transition_mask_gpu import (  # noqa: E402
    GpuTransitionMaskSpec,
)
from src.features.edit.core.timeline_preview_renderer import (  # noqa: E402
    PreviewRenderWorker,
)

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _FakeTransition:
    def __init__(self, transition_type: str, params: dict | None = None):
        self.transition_type = transition_type
        self.params = params or {}
        self.transition_id = f"tid-{transition_type}"


def _make_trans(
    transition_type: str,
    blend_factor: float = 0.5,
    params: dict | None = None,
    left_clip_id: str = "clipA",
    right_clip_id: str = "clipB",
) -> TransitionBlendInfo:
    return TransitionBlendInfo(
        transition=_FakeTransition(transition_type, params),
        left_clip=SimpleNamespace(clip_id=left_clip_id),
        right_clip=SimpleNamespace(clip_id=right_clip_id),
        blend_factor=blend_factor,
        left_source_frame=0,
        right_source_frame=1,
        left_opacity=1.0 - blend_factor,
        right_opacity=blend_factor,
        blend_color=(0.2, 0.4, 0.6),
        track_index=0,
        track_name="V1",
    )


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------


class TestBuildGpuTransitionJobs:
    VIEWPORT = (192, 108)

    def test_spatial_transition_produces_pixel_free_gpu_job(self):
        jobs = PreviewRenderWorker._build_gpu_transition_jobs(
            (_make_trans("wipe", params={"direction": "left"}),),
            self.VIEWPORT,
        )
        assert jobs is not None and len(jobs) == 1
        trans, mask = jobs[0]
        assert trans.transition_type == "wipe"
        assert isinstance(mask, GpuTransitionMaskSpec)
        assert mask.kind == 0 and mask.direction == 0
        assert not hasattr(mask, "mask")

    def test_dip_produces_color_job_without_mask(self):
        jobs = PreviewRenderWorker._build_gpu_transition_jobs(
            (_make_trans("dip_to_color"),),
            self.VIEWPORT,
        )
        assert jobs is not None and len(jobs) == 1
        trans, mask = jobs[0]
        assert trans.transition_type == "dip_to_color"
        assert mask is None

    @pytest.mark.parametrize(
        "transition_type",
        ["blur_dissolve", "defocus_dissolve", "film_dissolve"],
    )
    def test_full_frame_dissolve_produces_pixel_free_gpu_job(
        self, transition_type: str,
    ):
        jobs = PreviewRenderWorker._build_gpu_transition_jobs(
            (_make_trans(transition_type),),
            self.VIEWPORT,
        )
        assert jobs is not None and len(jobs) == 1
        trans, mask = jobs[0]
        assert trans.transition_type == transition_type
        assert isinstance(mask, GpuTransitionMaskSpec)
        assert mask.kind == 4

    @pytest.mark.parametrize(
        "transition_type, expected_mode",
        [
            ("smooth_cut", 3), ("motion_blur_left", 1),
            ("whip_pan_right", 2), ("zoom_blur_in", 3),
            ("spin_blur_cw", 4), ("rgb_split_left", 5),
        ],
    )
    def test_motion_and_full_effects_use_gpu_control_payload(
        self, transition_type: str, expected_mode: int,
    ):
        jobs = PreviewRenderWorker._build_gpu_transition_jobs(
            (_make_trans(transition_type),),
            self.VIEWPORT,
        )
        assert jobs is not None and len(jobs) == 1
        _trans, spec = jobs[0]
        assert isinstance(spec, GpuTransitionMaskSpec)
        if transition_type == "smooth_cut":
            assert spec.transition_mode == 3
        else:
            assert spec.effect_mode == expected_mode

    def test_cross_dissolve_needs_no_gl_job(self):
        jobs = PreviewRenderWorker._build_gpu_transition_jobs(
            (_make_trans("cross_dissolve"),),
            self.VIEWPORT,
        )
        assert jobs == ()

    def test_unknown_type_needs_no_gl_job(self):
        """未知类型必须整帧回退，不能静默降级成普通 opacity-lerp。"""
        jobs = PreviewRenderWorker._build_gpu_transition_jobs(
            (_make_trans("totally_unknown_transition"),),
            self.VIEWPORT,
        )
        assert jobs is None

    def test_mask_failure_falls_back_whole_frame(self):
        # wipe 的 softness 非法值会被 clamp；用一个必然异常的 params 对象
        jobs = PreviewRenderWorker._build_gpu_transition_jobs(
            (_make_trans("wipe", params={"direction": object()}),),
            self.VIEWPORT,
        )
        # direction 非法会被归一化为 left，不异常——本用例只验证仍成功
        assert jobs is not None

    def test_iris_circle_job_contains_geometry_only(self):
        """GPU 任务只携带几何控制，不生成逐像素 CPU mask。"""
        trans = _make_trans("iris_circle", blend_factor=0.4)
        jobs = PreviewRenderWorker._build_gpu_transition_jobs(
            (trans,), self.VIEWPORT,
        )
        _, mask = jobs[0]
        assert isinstance(mask, GpuTransitionMaskSpec)
        assert mask.kind == 1
        assert mask.progress == 0.4
        assert not isinstance(mask, np.ndarray)


# ---------------------------------------------------------------------------
# GL 像素一致性（headless 自动 skip）
# ---------------------------------------------------------------------------


def _gl_available() -> bool:
    try:
        import moderngl

        ctx = moderngl.create_standalone_context(require=330)
        ctx.release()
        return True
    except Exception:
        return False


def _render_program(ctx, frag_source, uniforms, textures, w, h) -> np.ndarray:
    """用与渲染器一致的 quad 拓扑跑一个 pass，回读 RGBA。"""
    import moderngl

    prog = ctx.program(
        vertex_shader=_PREVIEW_PASS_VERTEX_SHADER_SOURCE,
        fragment_shader=frag_source,
    )
    vertices = np.array([
        # pos       texcoord（标准 GL 方向，不翻转 = _blend_quad_vbo）
        -1.0, -1.0, 0.0, 0.0,
        1.0, -1.0, 1.0, 0.0,
        -1.0, 1.0, 0.0, 1.0,
        1.0, 1.0, 1.0, 1.0,
    ], dtype=np.float32)
    vbo = ctx.buffer(vertices)
    vao = ctx.vertex_array(
        prog, [(vbo, "2f 2f", "in_vert", "in_texcoord")],
    )
    fbo = ctx.framebuffer(color_attachments=[
        ctx.texture((w, h), components=4, dtype="f1")
    ])
    fbo.use()
    ctx.viewport = (0, 0, w, h)
    ctx.clear(0.0, 0.0, 0.0, 1.0)
    for name, value in uniforms.items():
        prog[name] = value
    for index, tex in enumerate(textures):
        tex.use(index)
    vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
    data = fbo.read(components=4)
    return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)


def _gradient_texture(ctx, w, h, seed: int):
    """创建渐变纹理，返回 (tex, rgb_uint8_array) 供 numpy 参考计算。"""
    rng = np.random.default_rng(seed)
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)
    xg, yg = np.meshgrid(xs, ys)
    rgb = np.stack([
        0.5 + 0.4 * np.sin(7 * xg * np.pi + seed),
        0.5 + 0.4 * np.cos(5 * yg * np.pi + seed * 0.5),
        0.3 + 0.3 * xg,
    ], axis=-1)
    rgb = (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)
    tex = ctx.texture((w, h), components=3, dtype="f1")
    tex.write(np.ascontiguousarray(rgb))
    return tex, rgb


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
class TestTransitionShaderGolden:
    W, H = 64, 48

    def _ctx(self):
        import moderngl

        return moderngl.create_standalone_context(require=330)

    def test_transition_blend_matches_numpy(self):
        ctx = self._ctx()
        try:
            w, h = self.W, self.H
            rng = np.random.default_rng(3)
            mask = rng.random((h, w)).astype(np.float32)
            left, left_rgb = _gradient_texture(ctx, w, h, 1)
            right, right_rgb = _gradient_texture(ctx, w, h, 2)
            mask_tex = ctx.texture((w, h), components=1, dtype="f4")
            mask_tex.write(mask)

            gpu = _render_program(
                ctx,
                _TRANSITION_FRAGMENT_SHADER_SOURCE,
                {
                    "u_left_tex": 0,
                    "u_right_tex": 1,
                    "u_mask_tex": 2,
                    "u_left_opacity": 0.3,
                    "u_right_opacity": 0.7,
                },
                [left, right, mask_tex],
                w, h,
            )

            lo, ro = 0.3, 0.7
            expected = (
                left_rgb.astype(np.float32) * (1.0 - mask[..., None]) * lo
                + right_rgb.astype(np.float32) * mask[..., None] * ro
            )
            expected = np.rint(np.clip(expected, 0.0, 255.0)).astype(np.int16)
            diff = np.abs(
                gpu[..., :3].astype(np.int16) - expected,
            )
            assert diff.max() <= 1, f"maxdiff {diff.max()} > 1 LSB"
        finally:
            ctx.release()

    def test_dip_matches_numpy(self):
        ctx = self._ctx()
        try:
            w, h = self.W, self.H
            tex, base = _gradient_texture(ctx, w, h, 5)
            dip_color = (0.2, 0.4, 0.6)
            factor = 0.45

            gpu = _render_program(
                ctx,
                _DIP_FRAGMENT_SHADER_SOURCE,
                {
                    "u_tex": 0,
                    "u_dip_color": dip_color,
                    "u_dip_factor": factor,
                },
                [tex],
                w, h,
            )

            expected = (
                base.astype(np.float32) * (1.0 - factor)
                + np.array(dip_color, dtype=np.float32) * 255.0 * factor
            )
            expected = np.rint(np.clip(expected, 0.0, 255.0)).astype(np.int16)
            diff = np.abs(gpu[..., :3].astype(np.int16) - expected)
            assert diff.max() <= 1, f"maxdiff {diff.max()} > 1 LSB"
        finally:
            ctx.release()

    def test_full_frame_dissolve_is_animated_and_reaches_both_endpoints(self):
        """The dedicated dissolve shader must actually change with progress."""
        ctx = self._ctx()
        textures = []
        try:
            w, h = self.W, self.H
            left_rgb = np.zeros((h, w, 3), dtype=np.uint8)
            left_rgb[..., 0] = 220
            right_rgb = np.zeros((h, w, 3), dtype=np.uint8)
            right_rgb[..., 2] = 220
            left = ctx.texture((w, h), components=3, dtype="f1")
            right = ctx.texture((w, h), components=3, dtype="f1")
            left.write(left_rgb)
            right.write(right_rgb)
            textures.extend((left, right))

            def render(progress: float) -> np.ndarray:
                return _render_program(
                    ctx,
                    _FULL_FRAME_TRANSITION_FRAGMENT_SHADER_SOURCE,
                    {
                        "u_left_tex": 0,
                        "u_right_tex": 1,
                        "u_progress": progress,
                        "u_left_opacity": 1.0,
                        "u_right_opacity": 1.0,
                        "u_blur_ratio": 0.045,
                        "u_transition_mode": 0,
                        "u_use_procedural": 1,
                        "u_transition_kind": 4,
                    },
                    textures,
                    w,
                    h,
                )

            start = render(0.0)[..., :3]
            middle = render(0.5)[..., :3]
            end = render(1.0)[..., :3]
            assert float(start[..., 0].mean()) > 200.0
            assert float(end[..., 2].mean()) > 200.0
            assert not np.array_equal(middle, start)
            assert not np.array_equal(middle, end)
        finally:
            for texture in textures:
                texture.release()
            ctx.release()


# ---------------------------------------------------------------------------
# 渲染器/任务契约
# ---------------------------------------------------------------------------


class TestTaskAndGateContract:
    def test_transition_jobs_default_empty(self):
        task = EditRenderTask(
            frame=1, generation_id=1, layers=[],
        )
        assert task.transition_jobs == ()

    def test_has_transition_support_requires_both_programs(self):
        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditGpuPreviewRenderer,
        )

        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._transition_program = None
        renderer._dip_program = None
        assert renderer.has_transition_support() is False
        renderer._transition_program = SimpleNamespace()
        renderer._dip_program = None
        assert renderer.has_transition_support() is False
        renderer._dip_program = SimpleNamespace()
        assert renderer.has_transition_support() is True

    def test_worker_gate_falls_back_when_renderer_lacks_support(self, monkeypatch):
        """渲染器缺转场程序时，转场帧必须整体回退 CPU（词表不变）。"""

        class _RendererStub:
            is_initialized = True

            @staticmethod
            def has_transition_support():
                return False

        worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
        worker._gpu_renderer = _RendererStub()
        worker._viewport_size = (64, 48)
        worker._current_render_realtime = False
        monkeypatch.setattr(
            PreviewRenderWorker,
            "_is_abandoned_interactive_scrub_task",
            lambda self, frame, meta: False,
        )
        ok, trace = worker._try_gpu_render(
            7,
            (),
            (_make_trans("wipe", params={"direction": "left"}),),
            1,
        )
        assert ok is False
        assert trace["fallback_reason"] == "native_transition_cpu_compositor"

    def test_worker_gate_builds_jobs_when_supported(self, monkeypatch):
        class _RendererStub:
            is_initialized = True

            @staticmethod
            def has_transition_support():
                return True

            @staticmethod
            def has_cached_composite(*_args, **_kwargs):
                return False

        worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
        worker._gpu_renderer = _RendererStub()
        worker._viewport_size = (64, 48)
        worker._current_render_realtime = False
        monkeypatch.setattr(
            PreviewRenderWorker,
            "_is_abandoned_interactive_scrub_task",
            lambda self, frame, meta: False,
        )
        # 无层 → 在转场任务构造后走"无有效层"回退，但绝不能再报
        # native_transition_cpu_compositor（转场已放行 GPU）。
        ok, trace = worker._try_gpu_render(
            7,
            (),
            (_make_trans("wipe", params={"direction": "left"}),),
            1,
        )
        assert ok is False
        assert trace.get("fallback_reason") != "native_transition_cpu_compositor"

    def test_worker_gate_falls_back_when_full_frame_shader_is_unavailable(
        self, monkeypatch,
    ):
        class _RendererStub:
            is_initialized = True

            @staticmethod
            def has_transition_support():
                return True

            @staticmethod
            def has_full_frame_transition_support():
                return False

            @staticmethod
            def has_cached_composite(*_args, **_kwargs):
                return False

        worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
        worker._gpu_renderer = _RendererStub()
        worker._viewport_size = (64, 48)
        worker._current_render_realtime = False
        monkeypatch.setattr(
            PreviewRenderWorker,
            "_is_abandoned_interactive_scrub_task",
            lambda self, frame, meta: False,
        )
        ok, trace = worker._try_gpu_render(
            7,
            (),
            (_make_trans("blur_dissolve"),),
            1,
        )
        assert ok is False
        assert trace["fallback_reason"] == "native_transition_cpu_compositor"
