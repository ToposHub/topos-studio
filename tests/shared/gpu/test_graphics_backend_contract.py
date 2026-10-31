"""GraphicsBackend 契约测试（计划 P3-02 / §3.2）。

覆盖：OpenGLRenderBackend 满足 GraphicsBackend 协议、状态机转换、
线程守卫（UI 线程 GL 调用必须显式抛错）、render_pass/present 委托语义。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from PyQt6.QtCore import QThread
import pytest

from src.shared.gpu.graphics_backend import (
    BackendNotReadyError,
    BackendThreadViolationError,
    GraphicsBackend,
    GraphicsBackendError,
    GraphicsBackendState,
    TextureDescriptor,
)
from src.features.color.core.opengl_render_backend import OpenGLRenderBackend


def _make_backend(with_ctx: bool = True, owner_thread=None) -> tuple[OpenGLRenderBackend, MagicMock]:
    orchestrator = MagicMock()
    orchestrator.ctx = MagicMock() if with_ctx else None
    orchestrator.fb_manager = MagicMock()
    orchestrator.current_front_tex_id = 42
    orchestrator.current_front_size = (1920, 1080)
    orchestrator.current_front_fence = object()
    backend = OpenGLRenderBackend(
        orchestrator, owner_thread=owner_thread, owner_name="test"
    )
    return backend, orchestrator


class TestProtocolConformance:
    def test_opengl_backend_satisfies_graphics_backend_protocol(self):
        backend, _ = _make_backend()
        assert isinstance(backend, GraphicsBackend)

    def test_backend_id_and_api(self):
        backend, _ = _make_backend()
        assert backend.backend_id == "opengl"
        assert backend.graphics_api == "opengl"
        assert backend.state == GraphicsBackendState.UNINITIALIZED


class TestStateMachine:
    def test_initialize_without_ctx_fails_with_diagnostic(self):
        backend, _ = _make_backend(with_ctx=False)
        with pytest.raises(BackendNotReadyError, match="context"):
            backend.initialize()
        assert backend.state == GraphicsBackendState.FAILED

    def test_initialize_with_ctx_becomes_ready(self):
        backend, _ = _make_backend()
        backend.initialize()
        assert backend.state == GraphicsBackendState.READY

    def test_initialize_is_idempotent_when_ready(self):
        backend, _ = _make_backend()
        backend.initialize()
        backend.initialize()
        assert backend.state == GraphicsBackendState.READY

    def test_release_marks_released_and_reinit_requires_switch_flow(self):
        backend, _ = _make_backend()
        backend.initialize()
        backend.release()
        assert backend.state == GraphicsBackendState.RELEASED
        with pytest.raises(GraphicsBackendError, match="切换"):
            backend.initialize()

    def test_release_is_idempotent(self):
        backend, _ = _make_backend()
        backend.release()
        backend.release()
        assert backend.state == GraphicsBackendState.RELEASED


class TestThreadGuard:
    """真实 GL 操作必须拒绝非渲染线程调用（.clinerules 硬约束）。"""

    def test_create_texture_from_wrong_thread_raises(self):
        # owner_thread 设为一个哨兵对象（≠ 当前线程）模拟「非渲染线程调用」
        backend, _ = _make_backend(owner_thread=object())
        with pytest.raises(BackendThreadViolationError, match="create_texture"):
            backend.create_texture(TextureDescriptor(width=4, height=4))

    def test_create_target_from_wrong_thread_raises(self):
        backend, _ = _make_backend(owner_thread=object())
        with pytest.raises(BackendThreadViolationError, match="create_target"):
            backend.create_target(MagicMock())

    def test_create_texture_from_owner_thread_delegates_to_ctx(self):
        backend, orchestrator = _make_backend(owner_thread=QThread.currentThread())
        tex = backend.create_texture(TextureDescriptor(width=64, height=32, components=3))

        orchestrator.ctx.texture.assert_called_once_with((64, 32), 3, data=None, dtype="f2")
        # LINEAR + clamp 与 GLContextManager.create_texture 语义一致
        import moderngl
        assert tex.filter == (moderngl.LINEAR, moderngl.LINEAR)
        assert tex.repeat_x is False
        assert tex.repeat_y is False

    def test_create_target_delegates_to_fb_manager(self):
        backend, orchestrator = _make_backend(owner_thread=QThread.currentThread())
        texture = MagicMock()
        fbo = backend.create_target(texture)
        orchestrator.fb_manager.get_fbo.assert_called_once_with(texture)
        assert fbo is orchestrator.fb_manager.get_fbo.return_value

    def test_create_target_without_fb_manager_raises(self):
        backend, orchestrator = _make_backend(owner_thread=QThread.currentThread())
        del orchestrator.fb_manager
        with pytest.raises(BackendNotReadyError, match="FramebufferManager"):
            backend.create_target(MagicMock())


class TestCrossThreadEntries:
    """render_pass / present 是设计上的跨线程安全入口。"""

    def test_render_pass_delegates_to_submit(self):
        backend, orchestrator = _make_backend(owner_thread=object())
        backend.render_pass(7, {"size": (10, 10)})
        orchestrator.submit.assert_called_once_with(7, {"size": (10, 10)})

    def test_render_pass_without_data_submits_empty_dict(self):
        backend, orchestrator = _make_backend(owner_thread=object())
        backend.render_pass(7)
        orchestrator.submit.assert_called_once_with(7, {})

    def test_present_reads_front_buffer_descriptor(self):
        backend, orchestrator = _make_backend(owner_thread=object())
        descriptor = backend.present()
        assert descriptor.texture_id == 42
        assert descriptor.width == 1920
        assert descriptor.height == 1080
        assert descriptor.fence is orchestrator.current_front_fence

    def test_present_with_missing_attrs_returns_zeroed_descriptor(self):
        orchestrator = MagicMock(spec=["ctx"])
        orchestrator.ctx = None
        backend = OpenGLRenderBackend(orchestrator, owner_thread=object())
        descriptor = backend.present()
        assert descriptor.texture_id is None
        assert descriptor.width == 0
        assert descriptor.height == 0


class TestResourceReport:
    def test_resource_report_shape(self):
        backend, _ = _make_backend()
        backend.registry.register("upload", "pool")
        report = backend.resource_report()
        assert report["backend_id"] == "opengl"
        assert report["graphics_api"] == "opengl"
        assert report["state"] == "uninitialized"
        assert report["groups"] == {"upload": ["pool"]}

    def test_release_flushes_ledger_via_release_all(self):
        calls = []
        backend, _ = _make_backend()
        backend.registry.register("upload", "pool", release_fn=lambda: calls.append(1))
        backend.release()
        assert calls == [1]
        assert backend.resource_report()["groups"] == {}
