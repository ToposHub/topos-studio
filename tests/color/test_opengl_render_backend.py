"""OpenGLRenderBackend 与 AsyncRenderer 接线测试（计划 P3-02 / P3-03）。

沿用 lane-merge 测试的 ``AsyncRenderer.__new__`` 无 GL 手法，验证：
- run() 初始化序列的资源登记覆盖计划点名的五个资源组
- 懒创建点（scope engine / present 双缓冲）的登记
- _cleanup 后台账清空、backend 标记 RELEASED
- 渲染线程上 facade 的 GL 操作委托（ctx / fb_manager）
"""

from __future__ import annotations

from unittest.mock import MagicMock

from PyQt6.QtCore import QThread
import pytest

from src.features.color.core.async_renderer import AsyncRenderer
from src.features.color.core.opengl_render_backend import OpenGLRenderBackend
from src.shared.gpu.gl_resource_registry import (
    GROUP_DENOISE,
    GROUP_FRAMEBUFFERS,
    GROUP_GRADING,
    GROUP_PRESENT,
    GROUP_SHADERS,
    GROUP_UPLOAD,
)


def _make_renderer() -> AsyncRenderer:
    """无 GL 构造：__new__ + 手工属性（与 test_async_renderer_lane_merge 一致）。"""
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    renderer._gpu_backend = OpenGLRenderBackend(
        renderer, owner_thread=QThread.currentThread()
    )
    return renderer


class TestCoreResourceRegistration:
    def test_run_init_resources_cover_the_five_plan_groups(self):
        renderer = _make_renderer()
        renderer.fb_manager = MagicMock()
        renderer.utility_shaders = {"blur": MagicMock()}
        renderer._video_texture_pool = MagicMock()
        renderer._yuv_gpu_uploader = MagicMock()
        renderer.video_denoise_pipeline = MagicMock()
        for name in (
            "identity_lut", "raw_lut_tex", "default_curves_tex",
            "default_hsl_curve_tex", "default_warper_tex", "white_tex",
        ):
            setattr(renderer, name, MagicMock())

        AsyncRenderer._register_core_gpu_resources(renderer)

        groups = renderer.get_gpu_backend_report()["groups"]
        # 计划 P3-03 点名：上传、FBO、shader、denoise（scope 在懒创建点）
        assert GROUP_UPLOAD in groups
        assert groups[GROUP_UPLOAD] == ["video_texture_pool", "yuv_gpu_uploader"]
        assert GROUP_FRAMEBUFFERS in groups
        assert GROUP_SHADERS in groups
        assert GROUP_DENOISE in groups
        assert GROUP_GRADING in groups

    def test_missing_optional_resources_do_not_break_registration(self):
        renderer = _make_renderer()
        renderer.fb_manager = MagicMock()
        renderer.utility_shaders = {}
        renderer._video_texture_pool = MagicMock()
        renderer._yuv_gpu_uploader = MagicMock()
        renderer.video_denoise_pipeline = None  # 初始化失败被禁用的情形
        # 默认调色纹理全部缺失
        for name in (
            "identity_lut", "raw_lut_tex", "default_curves_tex",
            "default_hsl_curve_tex", "default_warper_tex", "white_tex",
        ):
            setattr(renderer, name, None)

        AsyncRenderer._register_core_gpu_resources(renderer)

        groups = renderer.get_gpu_backend_report()["groups"]
        assert GROUP_DENOISE not in groups
        assert GROUP_GRADING not in groups

    def test_lazy_scope_engine_registration(self):
        renderer = _make_renderer()
        renderer._gpu_scope_engine = None
        renderer.ctx = object()  # GpuScopeEngine 只存引用，可无头构造

        engine = AsyncRenderer._ensure_scope_engine(renderer)

        groups = renderer.get_gpu_backend_report()["groups"]
        assert groups.get("scope") == ["scope_engine"]
        assert engine is renderer._gpu_scope_engine

    def test_lazy_present_pair_registration(self):
        renderer = _make_renderer()
        renderer.fbo_pair = [None, None]
        renderer.tex_pair = [None, None]
        renderer.garbage = []
        renderer.ctx = MagicMock()  # texture()/framebuffer() 返回 mock
        renderer._process_garbage = MagicMock()

        AsyncRenderer._ensure_fbos(renderer, 640, 480)

        groups = renderer.get_gpu_backend_report()["groups"]
        assert groups.get(GROUP_PRESENT) == ["fbo_pair"]

    def test_cleanup_clears_ledger_and_marks_released(self):
        renderer = _make_renderer()
        renderer.fb_manager = MagicMock()
        renderer.utility_shaders = {}
        renderer._video_texture_pool = MagicMock()
        renderer._yuv_gpu_uploader = MagicMock()
        renderer.video_denoise_pipeline = None

        AsyncRenderer._register_core_gpu_resources(renderer)
        # _cleanup 的台账收尾是纯 Python，可单独验证（真实 GL 释放仍走原路径）
        backend = renderer._gpu_backend
        backend.release()

        assert backend.state.value == "released"
        assert renderer.get_gpu_backend_report()["groups"] == {}
        assert renderer.get_gpu_backend_report()["state"] == "released"

    def test_get_gpu_backend_report_without_backend(self):
        renderer = AsyncRenderer.__new__(AsyncRenderer)  # 无 _gpu_backend
        report = renderer.get_gpu_backend_report()
        assert report["state"] == "uninitialized"
        assert report["groups"] == {}


class TestFacadeOnRendererThread:
    """渲染线程身份下 facade 的真实委托路径。"""

    def test_facade_attached_in_init_defaults_to_renderer_thread(self):
        # __init__ 默认 owner_thread=orchestrator：测试进程当前线程不是
        # 渲染线程对象，因此真实 GL 调用必须被拒绝（.clinerules 守卫生效）。
        renderer = AsyncRenderer.__new__(AsyncRenderer)
        backend = OpenGLRenderBackend(renderer)
        from src.shared.gpu.graphics_backend import (
            BackendThreadViolationError,
            TextureDescriptor,
        )
        assert backend._owner_thread is renderer
        with pytest.raises(BackendThreadViolationError):
            backend.create_texture(TextureDescriptor(width=2, height=2))

    def test_orchestrator_attribute_exposed(self):
        renderer = _make_renderer()
        assert renderer._gpu_backend.orchestrator is renderer
