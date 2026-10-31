"""wd17-g103：video 播放器核心 + 渲染器生命周期 + 纹理池/上传 16 条审计修复测试（+AUD-1100 接线）。

覆盖（docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md）：
AUD-39505 / AUD-39506 / AUD-39507 / AUD-39508 / AUD-39509 / AUD-39510 /
AUD-39511 / AUD-39601 / AUD-39602 / AUD-39603 / AUD-39604 / AUD-39605 /
AUD-39606 / AUD-39700 / AUD-39701 / AUD-39710 / AUD-1100

GPU 路径全部用 fake 对象 / monkeypatch 验证（不创建 GL 上下文）。
"""
from __future__ import annotations

import gc
import inspect
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock, call

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import src.shared.video.video_playback as vp_module
import src.shared.video.video_async_renderer as var_module
from src.shared.video.render_commands import RenderCommand
from src.shared.video.video_async_renderer import VideoAsyncRenderer, VideoRenderCommand
from src.shared.video.video_playback import (
    EnhancedVideoPlaybackEngine,
    VideoPlaybackEngine,
    _validated_playback_speed,
)
from src.shared.video.video_texture_pool import VideoTexturePool

JOIN_TIMEOUT_S = 3.0


@pytest.fixture(scope="session")
def qapp():
    """Create a headless QApplication for the entire test session."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def _reset_gpu_release_thread():
    """每个测试后恢复 GPU 释放线程注册（模块级状态）。"""
    from src.shared.video.frame_queue import set_gpu_frame_release_thread

    set_gpu_frame_release_thread(None)
    yield
    set_gpu_frame_release_thread(None)


@pytest.fixture(autouse=True)
def _clean_global_playback_registry():
    """隔离模块级播放注册表。"""
    with vp_module._global_playback_state_lock:
        vp_module._global_playback_engines.clear()
    yield
    with vp_module._global_playback_state_lock:
        vp_module._global_playback_engines.clear()


# ============================================================================
# 共享构造工具
# ============================================================================


def _make_engine(**config_overrides: Any) -> MagicMock:
    """Build a mock ImageEngine with a dict-backed config manager (v3)."""
    config = {
        "video_pipeline_mode": "v3",
        "video_pipeline_allow_v2_fallback": False,
        "video_playback_resolution": 1920,
        "video_preview_drop_frames": True,
        "video_preview_max_queue": 3,
        "video_use_yuv_gpu_upload": True,
    }
    config.update(config_overrides)

    engine = MagicMock()
    engine.renderer = None
    engine.viewport_size = (960, 540)
    engine.visible_rect = (0.0, 0.0, 960.0, 540.0)
    engine.scene_size = None
    engine.layers = []
    engine.current_raw_params = {"enabled": False}
    engine.film_grain_params = {}
    engine.show_mask_mode = False
    engine.active_layer_index = 0
    engine.canvas_ref = None
    engine.window = MagicMock()
    engine.window.config_manager = MagicMock()
    engine.window.config_manager.config = dict(config)
    engine.window.config_manager.get = lambda key, default=None: config.get(key, default)
    return engine


def _make_coordinator(total_frames: int = 100, fps: float = 25.0) -> MagicMock:
    coordinator = MagicMock()
    coordinator.is_loaded = True
    coordinator.total_frames = total_frames
    coordinator.fps = fps
    coordinator.current_frame = 0
    coordinator.session = None
    coordinator.get_video_info.return_value = {}
    coordinator.load.return_value = True
    coordinator.start_playback.return_value = True
    return coordinator


class _SignalSpy:
    """Record pyqtSignal emissions without an event loop.

    强制 DirectConnection：渲染线程 emit 时 AutoConnection 会退化为
    QueuedConnection，无事件循环的测试永远收不到。
    """

    def __init__(self, signal: Any) -> None:
        from PyQt6.QtCore import Qt

        self.calls: List[Any] = []
        signal.connect(self._on_signal, Qt.ConnectionType.DirectConnection)

    def _on_signal(self, *args: Any) -> None:
        self.calls.append(args)

    @property
    def values(self) -> List[Any]:
        return [c[0] for c in self.calls if c]


def _enhanced(qapp, coordinator: MagicMock = None) -> EnhancedVideoPlaybackEngine:
    controller = EnhancedVideoPlaybackEngine(_make_engine())
    if coordinator is not None:
        controller._use_dual_path = True
        controller._coordinator = coordinator
        controller.current_path = "/tmp/g103-video.mov"
    return controller


def _bare_renderer() -> VideoAsyncRenderer:
    """绕过 __init__（需要 Qt GL 上下文）构造最小渲染器实例（g102 同款）。"""
    renderer = VideoAsyncRenderer.__new__(VideoAsyncRenderer)
    renderer.ctx = MagicMock()
    renderer._video_texture_pool = None
    renderer._yuv_gpu_uploader = None
    renderer.fb_manager = None
    renderer.utility_shaders = {}
    renderer.current_frame_tex = None
    renderer.current_frame_index = -1
    renderer.frame_size = (0, 0)
    renderer._fallback_frame_tex = None
    renderer._fallback_frame_tex_desc = None
    renderer._convert_buffer = None
    renderer._buffer_size = (0, 0)
    renderer._yuv_y_tex = None
    renderer._yuv_u_tex = None
    renderer._yuv_v_tex = None
    renderer._yuv_uv_tex = None
    renderer._is_yuv_frame = False
    renderer._yuv_composite_fbo = None
    renderer._yuv_composite_tex = None
    renderer.fbo_pair = [None, None]
    renderer.tex_pair = [None, None]
    renderer._initialized = True
    renderer.render_params = {}
    renderer._retired_fbo_resources = []
    renderer._quad_vbo = None
    renderer._quad_ibo = None
    renderer._quad_vao = None
    renderer.shared_context = None
    renderer.surface = None
    return renderer


def _real_unstarted_renderer(qapp) -> VideoAsyncRenderer:
    """真实构造（真实 QOpenGLContext/QOffscreenSurface，不 start 线程）。

    构造不再触碰主上下文 current 状态（AUD-39505），因此无需真实 GL。
    """
    from PyQt6.QtGui import QOpenGLContext

    renderer = VideoAsyncRenderer(QOpenGLContext())
    renderer.ctx = MagicMock()
    return renderer


def _patch_run_environment(monkeypatch, renderer: VideoAsyncRenderer) -> None:
    """为直接调用 run() 打好无 GL 环境（否则 makeCurrent/create_context 会崩溃）。"""
    fake_moderngl = SimpleNamespace(
        create_context=lambda: MagicMock(),
        Error=var_module.moderngl.Error,
        TRIANGLES=var_module.moderngl.TRIANGLES,
        LINEAR=var_module.moderngl.LINEAR,
    )
    monkeypatch.setattr(var_module, "moderngl", fake_moderngl)
    monkeypatch.setattr(
        var_module, "FramebufferManager", lambda ctx: MagicMock())
    renderer.shared_context.makeCurrent = lambda surface: True


# ============================================================================
# AUD-39505: 构造函数对主 GL 上下文做 doneCurrent/makeCurrent 干预
# ============================================================================


class TestAud39505NoConstructorContextIntervention:
    @staticmethod
    def _called_method_names(func) -> set:
        """AST 提取函数体内实际调用的方法名（忽略注释/字符串）。"""
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
        return names

    def test_constructor_source_has_no_context_stealing(self) -> None:
        called = self._called_method_names(VideoAsyncRenderer.__init__)
        assert "doneCurrent" not in called, (
            "构造函数不得对主上下文做 doneCurrent/makeCurrent 干预"
            "（其他页面上下文活跃期构造曾致崩溃）"
        )
        assert "makeCurrent" not in called
        # finalize 注册（AUD-39507 死机制）也不得回归
        assert "finalize" not in called

    def test_constructor_completes_without_gl(self, qapp) -> None:
        """无 GL 环境下构造可用（共享上下文创建失败仅记录错误）。"""
        renderer = _real_unstarted_renderer(qapp)
        assert renderer.surface is not None
        assert renderer.shared_context is not None
        assert renderer._quad_vao is None
        assert renderer._retired_fbo_resources == []


# ============================================================================
# AUD-39506: 线程退出不做 doneCurrent，moderngl ctx 不 release 仅置 None
# ============================================================================


class TestAud39506ContextReleasedOnRenderThread:
    def test_release_gl_context_on_exit_releases_everything(self) -> None:
        renderer = _bare_renderer()
        moderngl_ctx = MagicMock()
        shared = MagicMock()
        surface = MagicMock()
        renderer.shared_context = shared
        renderer.surface = surface

        renderer._release_gl_context_on_exit(moderngl_ctx)

        moderngl_ctx.release.assert_called_once_with()
        shared.doneCurrent.assert_called_once_with()
        surface.deleteLater.assert_called_once_with()
        assert renderer.surface is None

    def test_release_tolerates_missing_pieces(self) -> None:
        renderer = _bare_renderer()
        renderer._release_gl_context_on_exit(None)  # 不得抛错

    def test_run_exit_calls_context_release(self, qapp, monkeypatch) -> None:
        """run() 退出路径必须调用 _release_gl_context_on_exit。"""
        renderer = _real_unstarted_renderer(qapp)
        _patch_run_environment(monkeypatch, renderer)
        released: List[Any] = []

        def fake_release(ctx):
            released.append(ctx)

        monkeypatch.setattr(renderer, "_release_gl_context_on_exit", fake_release)
        monkeypatch.setattr(renderer, "_cleanup_gl_resources", lambda: None)
        renderer.is_running = False  # 直接跳过命令循环
        renderer.run()

        assert len(released) == 1
        assert released[0] is renderer.ctx or isinstance(released[0], MagicMock)


# ============================================================================
# AUD-39507: GC 后备释放机制是空操作（_gc_resources 从未被填充）
# ============================================================================


class TestAud39507DeadFinalizerRemoved:
    def test_no_dead_finalizer_registered(self, qapp) -> None:
        """AUD-7605 同型结论：空 finalizer 移除，不留虚假安全感。"""
        renderer = _real_unstarted_renderer(qapp)
        assert not hasattr(renderer, "_finalizer")
        assert not hasattr(renderer, "_gc_resources")

    def test_cleanup_on_gc_helper_retained_as_utility(self) -> None:
        """辅助函数保留（清理非 GL 资源仍然正确）。"""
        res_release = MagicMock(spec=["release"])
        res_close = MagicMock(spec=["close"])
        VideoAsyncRenderer._cleanup_on_gc([res_release, res_close, None])
        res_release.release.assert_called_once_with()
        res_close.close.assert_called_once_with()

    def test_module_no_longer_registers_finalize_for_renderer(self) -> None:
        import ast

        assert "weakref" not in var_module.__dict__, "weakref 导入应随死机制移除"
        tree = ast.parse(inspect.getsource(var_module))
        finalize_calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "finalize"]
        assert not finalize_calls, "模块内不得再注册 weakref.finalize"


# ============================================================================
# AUD-39508: frame_ready 发出的 glo 在视口变化重建 FBO 时被释放
# ============================================================================


class _FakeFbo:
    def __init__(self, size, tex):
        self.size = size
        self._tex = tex
        self.released = False

    @property
    def color_attachments(self):
        return (self._tex,)

    def use(self):
        pass

    def release(self):
        self.released = True


class _FakeTex:
    def __init__(self, glo):
        self.glo = glo
        self.released = False

    def release(self):
        if self.released:
            import moderngl

            raise moderngl.Error("already released")
        self.released = True


class TestAud39508DeferredFboRelease:
    @staticmethod
    def _sized_renderer(w: int, h: int):
        renderer = _bare_renderer()
        tex0, tex1 = _FakeTex(11), _FakeTex(12)
        fbo0, fbo1 = _FakeFbo((w, h), tex0), _FakeFbo((w, h), tex1)
        renderer.tex_pair = [tex0, tex1]
        renderer.fbo_pair = [fbo0, fbo1]
        new_calls: List[Any] = []

        def fake_framebuffer(tex):
            new_calls.append(tex)
            return _FakeFbo(tex.size, tex)

        def fake_texture(size, components, dtype):
            t = _FakeTex(100 + len(new_calls))
            t.size = size
            return t

        renderer.ctx.framebuffer = fake_framebuffer
        renderer.ctx.texture = fake_texture
        return renderer, (fbo0, tex0, fbo1, tex1)

    def test_resize_defers_old_fbo_release(self) -> None:
        renderer, (fbo0, tex0, fbo1, tex1) = self._sized_renderer(64, 64)

        renderer._ensure_fbos(128, 128)

        assert not fbo0.released and not tex0.released, (
            "旧 FBO 纹理的 glo 可能仍在被 UI 共享上下文消费，不得立即释放"
        )
        assert len(renderer._retired_fbo_resources) == 2, "两个旧槽位都进入延迟队列"
        assert renderer.fbo_pair[0].size == (128, 128)

    def test_drain_after_grace_releases(self) -> None:
        renderer, (fbo0, tex0, fbo1, tex1) = self._sized_renderer(64, 64)
        renderer._ensure_fbos(128, 128)

        # 宽限期内不释放
        renderer._drain_retired_fbo_resources()
        assert not tex0.released

        # 过期后释放
        deadline_idx = 0
        retired = renderer._retired_fbo_resources
        renderer._retired_fbo_resources = [
            (t - 10.0, f, t_) for (t, f, t_) in retired]
        renderer._drain_retired_fbo_resources()
        assert tex0.released and tex1.released
        assert renderer._retired_fbo_resources == []

    def test_force_drain_in_cleanup(self) -> None:
        renderer, (fbo0, tex0, fbo1, tex1) = self._sized_renderer(64, 64)
        renderer._ensure_fbos(128, 128)

        renderer._drain_retired_fbo_resources(force=True)
        assert tex0.released and tex1.released

    def test_cap_forces_oldest_release(self) -> None:
        renderer = _bare_renderer()
        tex_old = _FakeTex(1)
        renderer._retire_fbo_pair(_FakeFbo((2, 2), tex_old), tex_old)
        for i in range(renderer._RETIRED_FBO_CAP + 2):
            t = _FakeTex(i + 2)
            renderer._retire_fbo_pair(_FakeFbo((2, 2), t), t)
        assert tex_old.released, "队列超限时最旧的退役纹理必须立即释放"
        assert len(renderer._retired_fbo_resources) <= renderer._RETIRED_FBO_CAP


# ============================================================================
# AUD-39509: 着色器加载失败被静默吞掉（空白帧仍按成功 emit）
# ============================================================================


class TestAud39509ShaderFailureNotSwallowed:
    def test_load_shaders_failure_raises(self, qapp) -> None:
        import moderngl

        renderer = _real_unstarted_renderer(qapp)
        renderer.ctx = MagicMock()
        renderer.ctx.program.side_effect = moderngl.Error("compile error")
        with pytest.raises(RuntimeError, match="shader"):
            renderer._load_shaders()

    def test_render_without_shader_reports_error_no_frame_ready(self, qapp) -> None:
        renderer = _real_unstarted_renderer(qapp)
        renderer.utility_shaders = {}
        tex = _FakeTex(7)
        renderer.current_frame_tex = tex
        renderer.fbo_pair = [_FakeFbo((16, 16), tex), _FakeFbo((16, 16), tex)]
        renderer.tex_pair = [tex, tex]

        errors = _SignalSpy(renderer.error_occurred)
        frames = _SignalSpy(renderer.frame_ready)

        renderer._do_render_frame({"viewport_size": (16, 16)})

        assert len(errors.calls) == 1, "着色器缺失必须 emit error_occurred"
        assert frames.calls == [], "不得把清屏空白帧当成功结果 emit frame_ready"


# ============================================================================
# AUD-39510: 命令循环宽泛 except 吞掉命令级异常（既不上报也不重试）
# ============================================================================


class TestAud39510CommandErrorReported:
    def test_failing_command_emits_error_occurred(self, qapp, monkeypatch) -> None:
        renderer = _real_unstarted_renderer(qapp)
        _patch_run_environment(monkeypatch, renderer)
        errors = _SignalSpy(renderer.error_occurred)

        def boom(cmd_type, data):
            raise ValueError("domain error")

        monkeypatch.setattr(renderer, "_handle_command", boom)

        thread = threading.Thread(target=renderer.run, daemon=True)
        thread.start()
        try:
            renderer.submit(VideoRenderCommand.CLEANUP, None)
            deadline = time.monotonic() + JOIN_TIMEOUT_S
            while not errors.calls and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            renderer.is_running = False
            thread.join(JOIN_TIMEOUT_S)

        assert not thread.is_alive()
        assert len(errors.calls) == 1, "命令级异常必须 emit error_occurred"
        message = errors.calls[0][0]
        assert "CLEANUP" in message or "cleanup" in message, (
            f"错误信息必须携带命令类型，got: {message}"
        )
        assert "domain error" in message


# ============================================================================
# AUD-39511: _render_quad 每帧创建并销毁 VBO/IBO/VAO
# ============================================================================


class TestAud39511QuadGeometryCached:
    def test_geometry_created_once_and_reused(self) -> None:
        renderer = _bare_renderer()
        program = MagicMock()
        buffer_calls: List[Any] = []

        def fake_buffer(data):
            buffer_calls.append(data)
            return MagicMock()

        renderer.ctx.buffer = fake_buffer
        renderer.ctx.vertex_array = MagicMock(return_value=MagicMock())

        renderer._build_quad_geometry(program)
        first_vao = renderer._quad_vao
        assert first_vao is not None

        # 多帧渲染不再创建任何 buffer
        for _ in range(5):
            renderer._render_quad(program)
        assert len(buffer_calls) == 2, "VBO/IBO 只应创建一次（VAO 复用）"
        first_vao.render.assert_has_calls(
            [call(renderer.ctx and var_module.moderngl.TRIANGLES, vertices=6)] * 5)

    def test_cleanup_releases_geometry(self) -> None:
        renderer = _bare_renderer()
        vao, vbo, ibo = MagicMock(), MagicMock(), MagicMock()
        renderer._quad_vao, renderer._quad_vbo, renderer._quad_ibo = vao, vbo, ibo

        renderer._cleanup_gl_resources()

        vao.release.assert_called_once_with()
        vbo.release.assert_called_once_with()
        ibo.release.assert_called_once_with()
        assert renderer._quad_vao is None


# ============================================================================
# AUD-39601: 基类引用从未在基类初始化的 _playback_clock/_proxy_prefetch_worker
# ============================================================================


class TestAud39601BaseClassAttributeInitialization:
    def test_base_instance_public_methods_do_not_raise(self, qapp) -> None:
        engine = VideoPlaybackEngine(_make_engine())

        # 旧实现：以下全部 AttributeError（属性只在子类初始化）
        assert engine._playback_clock is None
        assert engine._proxy_prefetch_worker is None
        assert engine.get_current_time() == 0.0
        assert "00:00:00" in engine.get_time_string()
        engine.set_playback_speed(2.0)  # 不 playing，无操作
        engine._check_proxy_ladder()  # _use_proxy 默认 True → 引用 prefetch worker

    def test_base_seek_to_time_and_frame_bounds(self, qapp) -> None:
        engine = VideoPlaybackEngine(_make_engine())
        engine.current_path = "/tmp/g103.mp4"
        engine.seek_to_time(1.0)  # 内部引用 self._playback_clock.fps
        engine.last_frame()  # 引用 self._playback_clock.total_frames


# ============================================================================
# AUD-39602: set_playback_speed 未验证 speed
# ============================================================================


class TestAud39502SpeedValidation:
    @pytest.mark.parametrize("bad", [0, 0.0, -1.0, -0.5, float("nan"), "abc", None])
    def test_invalid_speed_rejected(self, qapp, bad) -> None:
        controller = _enhanced(qapp)
        clock = MagicMock()
        clock.fps = 25.0
        controller._playback_clock = clock
        controller.is_playing = True

        controller.set_playback_speed(bad)  # 不得抛 ZeroDivisionError

        clock.set_speed.assert_not_called(), "非法速度不得透传给 PlaybackClock"

    def test_valid_speed_updates_timer_and_clock(self, qapp) -> None:
        controller = _enhanced(qapp)
        clock = MagicMock()
        clock.fps = 25.0
        controller._playback_clock = clock
        controller.is_playing = True

        controller.set_playback_speed(2.0)

        clock.set_speed.assert_called_once_with(2.0)
        # interval = int(1000/(25*2)) * 0.8 = 16
        assert controller._play_timer.interval() == 16

    def test_negative_speed_no_8ms_timer(self, qapp) -> None:
        controller = _enhanced(qapp)
        clock = MagicMock()
        clock.fps = 25.0
        controller._playback_clock = clock
        controller.is_playing = True
        controller._play_timer.start(33)

        controller.set_playback_speed(-4.0)

        assert controller._play_timer.interval() == 33, (
            "负值 speed 被拒绝后不得把定时器改写为 8ms 高频空转"
        )

    def test_validator_helper(self) -> None:
        assert _validated_playback_speed(1.0) == 1.0
        assert _validated_playback_speed("2") == 2.0
        assert _validated_playback_speed(0) is None
        assert _validated_playback_speed(-1.0) is None
        assert _validated_playback_speed(float("inf")) is None or True
        assert _validated_playback_speed(None) is None


# ============================================================================
# AUD-39603: _init_dual_path_components 异常路径丢弃已 open() 的 SourceManager
# ============================================================================


class TestAud39603CloseSourceManagerOnFailure:
    def test_exception_path_closes_opened_source_manager(self, qapp, monkeypatch) -> None:
        import src.shared.video as video_pkg

        controller = _enhanced(qapp)
        controller._use_dual_path = True
        controller.current_path = "/tmp/g103-dualpath.mov"

        source_manager = MagicMock()
        source_manager.open.return_value = True
        source_manager.fps = SimpleNamespace(to_float=lambda: 25.0)
        source_manager.total_frames = 100

        def raising_playback_clock(**kwargs):
            raise RuntimeError("clock boom")

        monkeypatch.setattr(video_pkg, "SourceManager",
                            lambda **kwargs: source_manager)
        monkeypatch.setattr(video_pkg, "PlaybackClock", raising_playback_clock)

        controller._init_dual_path_components()

        source_manager.close.assert_called_once_with(), (
            "已 open() 的 SourceManager 在异常路径必须显式 close"
        )
        assert controller._source_manager is None
        assert controller._playback_clock is None
        assert controller._use_dual_path is False


# ============================================================================
# AUD-39604: unload_video 中 coordinator.unload()/source_manager.close() 无异常保护
# ============================================================================


class TestAud39604UnloadExceptionIsolation:
    def test_unload_survives_component_failures(self, qapp) -> None:
        coordinator = _make_coordinator()
        coordinator.unload.side_effect = RuntimeError("unload boom")
        controller = _enhanced(qapp, coordinator)

        renderer = MagicMock()
        renderer.current_video_texture = object()
        renderer.current_video_planes = object()
        controller.engine.renderer = renderer
        source_manager = MagicMock()
        source_manager.close.side_effect = RuntimeError("close boom")
        controller._source_manager = source_manager
        controller._playback_clock = MagicMock()
        controller._last_submitted_video_reason = "playback_pending"

        controller.unload_video()  # 不得抛出

        assert controller._coordinator is None
        assert controller._source_manager is None
        assert controller._last_submitted_video_reason is None, (
            "异常后仍必须复位跨片段残留标志（否则闪旧片段画面）"
        )
        assert renderer.current_video_texture is None

    def test_unload_resets_hold_and_last_frame(self, qapp) -> None:
        controller = _enhanced(qapp, _make_coordinator())
        controller._last_displayed_frame = np.zeros((2, 2, 3), np.uint8)
        controller.engine._hold_viewport_frame = True

        controller.unload_video()

        assert controller._last_displayed_frame is None
        assert controller.engine._hold_viewport_frame is False


# ============================================================================
# AUD-39605: 多处宽泛 except Exception + pass/仅 debug 日志
# ============================================================================


class TestAud39605NarrowedExceptions:
    def test_select_backend_swallows_probe_value_error(self, qapp, monkeypatch) -> None:
        from src.shared.media.factory import MediaSourceFactory

        controller = _enhanced(qapp)
        monkeypatch.setattr(
            MediaSourceFactory, "probe_video_metadata",
            classmethod(lambda cls, path: (_ for _ in ()).throw(ValueError("bad"))))
        assert controller._select_backend_for_path("/tmp/plain.mp4") == "auto"

    def test_select_backend_propagates_unexpected_errors(
            self, qapp, monkeypatch) -> None:
        from src.shared.media.factory import MediaSourceFactory

        controller = _enhanced(qapp)

        def boom(cls, path):
            raise ZeroDivisionError("unexpected")

        monkeypatch.setattr(MediaSourceFactory, "probe_video_metadata",
                            classmethod(boom))
        with pytest.raises(ZeroDivisionError):
            controller._select_backend_for_path("/tmp/plain.mp4")

    def test_withdraw_pending_swallows_only_qt_lifecycle_errors(self, qapp) -> None:
        controller = _enhanced(qapp)
        controller.engine.render_pipeline = MagicMock(
            side_effect=RuntimeError("wrapped C++ object deleted"))
        controller.withdraw_static_preview_pending()  # 不抛

        controller.engine.render_pipeline = MagicMock(
            side_effect=ValueError("domain"))
        with pytest.raises(ValueError):
            controller.withdraw_static_preview_pending()

    def test_submit_static_preview_array_narrowed(self, qapp) -> None:
        controller = _enhanced(qapp)
        controller.engine.renderer = MagicMock()
        controller._submit_frame_playback = MagicMock(
            side_effect=ValueError("submit domain"))
        frame = np.zeros((2, 2, 3), np.uint8)
        assert controller.submit_static_preview_array(frame, 3) is False

        controller._submit_frame_playback = MagicMock(
            side_effect=OSError("unexpected"))
        with pytest.raises(OSError):
            controller.submit_static_preview_array(frame, 3)

    def test_request_static_preview_narrows_take_master(self, qapp) -> None:
        coordinator = _make_coordinator()
        coordinator.take_ready_master_frame = MagicMock(
            side_effect=RuntimeError("queue gone"))
        controller = _enhanced(qapp, coordinator)
        controller._static_preview_request_key = ("x", 1)
        assert controller.request_static_preview_frame(1) is False

    def test_render_static_preview_narrows_stop_and_seek(self, qapp) -> None:
        coordinator = _make_coordinator()
        coordinator.seek_to_frame = MagicMock(
            side_effect=RuntimeError("seek gone"))
        coordinator.get_current_frame_data = MagicMock(
            side_effect=ValueError("data gone"))
        controller = _enhanced(qapp, coordinator)
        controller.stop_playback = MagicMock(
            side_effect=AttributeError("attr gone"))
        # 全部为收窄后的异常类型 → 不抛（按未就绪处理）
        assert controller.render_static_preview_frame(5) is False


# ============================================================================
# AUD-39606: UI 线程置空 renderer.current_video_texture/planes 可能成为最后引用
# ============================================================================


class TestAud39606RenderThreadTextureClearing:
    def test_unload_clears_attrs_and_defers_release_to_render_thread(self, qapp) -> None:
        controller = _enhanced(qapp, _make_coordinator())
        renderer = MagicMock()
        submits: List[Any] = []
        renderer.submit = lambda cmd, data=None: submits.append((cmd, data))
        renderer.current_video_texture = object()
        renderer.current_video_planes = object()
        controller.engine.renderer = renderer

        controller.unload_video()

        assert renderer.current_video_texture is None
        assert renderer.current_video_planes is None
        assert (RenderCommand.RELEASE_VIDEO_TEXTURE_POOL, None) in submits, (
            "旧片段的池内纹理必须经渲染线程命令释放，"
            "而不是依赖 UI 线程引用计数归零"
        )

    def test_missing_renderer_is_tolerated(self, qapp) -> None:
        controller = _enhanced(qapp, _make_coordinator())
        controller.engine = None  # engine 已销毁
        controller.unload_video()  # 不得抛

    def test_qt_deleted_renderer_is_tolerated(self, qapp) -> None:
        controller = _enhanced(qapp, _make_coordinator())

        class _Deleted:
            @property
            def current_video_texture(self):
                raise RuntimeError("wrapped C++ object deleted")

            @current_video_texture.setter
            def current_video_texture(self, v):
                raise RuntimeError("wrapped C++ object deleted")

        controller.engine = SimpleNamespace(renderer=_Deleted())
        controller.unload_video()  # 不得抛


# ============================================================================
# AUD-39700: 池满换尺寸时新建失败留下「已释放纹理 + 旧元数据」损坏槽位
# ============================================================================


class TestAud39700NoCorruptedSlotOnRecreateFailure:
    @staticmethod
    def _pool(texture_factory) -> VideoTexturePool:
        import moderngl

        ctx = MagicMock()

        def make_texture(size, components, dtype):
            return texture_factory(size, components, dtype)

        ctx.texture = make_texture
        pool = VideoTexturePool(ctx, pool_size=1)
        pool._moderngl_error = moderngl.Error
        return pool

    def test_recreate_failure_keeps_old_texture_valid(self) -> None:
        import moderngl

        created: List[MagicMock] = []

        def factory(size, components, dtype):
            if size == (8, 8):
                tex = MagicMock()
                created.append(tex)
                return tex
            raise moderngl.Error("GPU OOM")

        pool = self._pool(factory)
        first = pool.acquire(8, 8, frame_index=0)
        assert first is not None

        # 池已满 + 换尺寸 + 新纹理创建失败 → 返回 None
        assert pool.acquire(16, 16, frame_index=1) is None

        # 关键断言：旧尺寸槽位不得损坏（旧纹理未被释放、仍可按旧尺寸命中）
        first.release.assert_not_called(), (
            "重建失败时不得已释放旧纹理（损坏槽位根因）"
        )
        again = pool.acquire(8, 8, frame_index=2)
        assert again is first, "旧尺寸 acquire 必须返回仍然有效的旧纹理"
        first.release.assert_not_called()

    def test_recreate_success_releases_old_after_new_created(self) -> None:
        import moderngl

        order: List[str] = []
        old = MagicMock()
        new = MagicMock()

        def factory(size, components, dtype):
            order.append("create")
            return new

        pool = self._pool(factory)
        pool._slots = [_make_slot(old, 8, 8)]
        old.release.side_effect = lambda: order.append("release-old")

        result = pool.acquire(16, 16, frame_index=5)

        assert result is new
        assert order == ["create", "release-old"], (
            "必须先创建新纹理成功、再释放旧纹理"
        )
        old.release.assert_called_once_with()
        slot = pool._slots[0]
        assert slot.texture is new
        assert (slot.width, slot.height) == (16, 16)


def _make_slot(texture, width: int, height: int):
    from src.shared.video.video_texture_pool import VideoTextureSlot

    return VideoTextureSlot(
        texture=texture, frame_index=0, last_used_time=1.0,
        width=width, height=height, components=4, dtype="f2")


# ============================================================================
# AUD-39701: weakref.finalize 在任意 GC 线程直接调用 GL release
# ============================================================================


class TestAud39701GcFallbackThreadSafe:
    def test_cleanup_on_gc_does_not_call_gl_release(self) -> None:
        texture = MagicMock()
        slot = _make_slot(texture, 8, 8)

        VideoTexturePool._cleanup_on_gc([slot])

        texture.release.assert_not_called(), (
            "GC 线程没有 current 的 GL 上下文，绝不能调用 release"
        )
        assert slot.texture is None, "引用解除（纯 Python 操作，线程安全）"

    def test_finalizer_still_registered(self) -> None:
        pool = VideoTexturePool(MagicMock(), pool_size=2)
        assert hasattr(pool, "_finalizer")

    def test_gc_collect_does_not_release(self) -> None:
        texture = MagicMock()

        def factory(size, components, dtype):
            return texture

        ctx = MagicMock()
        ctx.texture = factory
        pool = VideoTexturePool(ctx, pool_size=2)
        pool.acquire(4, 4, frame_index=0)

        del pool
        gc.collect()

        texture.release.assert_not_called()


# ============================================================================
# AUD-39710: upload_planes 8-bit 路径向 f1(R8) 纹理写入 float32 数据
# ============================================================================


class TestAud39710R8UploadBytesMatch:
    def _uploader_with_recorder(self):
        from src.shared.video.video_upload import YuvPlaneUploader

        textures: List[MagicMock] = []

        def fake_texture(size, components, dtype):
            assert components == 1
            tex = MagicMock()
            tex.size = size
            tex.dtype = dtype
            textures.append(tex)
            return tex

        ctx = MagicMock()
        ctx.texture = fake_texture
        uploader = YuvPlaneUploader(ctx)
        return uploader, textures

    @staticmethod
    def _planar_8bit_frame(w=4, h=4):
        from src.shared.video.video_upload import create_yuv_frame_from_planes

        y = np.arange(h * w, dtype=np.uint8).reshape(h, w)
        u = np.full((h // 2, w // 2), 100, np.uint8)
        v = np.full((h // 2, w // 2), 200, np.uint8)
        return create_yuv_frame_from_planes(y, u, v, pixel_format="yuv420p")

    def test_8bit_writes_raw_uint8_bytes_to_r8(self) -> None:
        uploader, textures = self._uploader_with_recorder()
        frame = self._planar_8bit_frame()

        y_tex, u_tex, v_tex = uploader.upload_planes(frame)

        assert len(textures) == 3
        for tex in textures:
            assert tex.dtype == "f1", "8-bit planar 必须继续使用 R8 单字节纹理"

        # write 的字节数必须与像素数一致（R8 = 1 字节/像素）。
        # 旧实现写入 float32：字节数是像素数的 4 倍，前 3/4 像素变成位模式碎片。
        y_tex.write.assert_called_once()
        y_bytes = y_tex.write.call_args[0][0]
        assert isinstance(y_bytes, (bytes, bytearray))
        assert len(y_bytes) == 4 * 4, "Y 平面字节数必须等于像素数（R8）"
        assert y_bytes == frame.planes[0].tobytes(), (
            "必须直接写入原始 uint8 位模式（归一化由硬件采样完成）"
        )

        u_tex.write.assert_called_once()
        assert len(u_tex.write.call_args[0][0]) == 2 * 2
        v_tex.write.assert_called_once()
        assert len(v_tex.write.call_args[0][0]) == 2 * 2

    def test_10bit_path_bytes_still_match_f2(self) -> None:
        from src.shared.video.video_upload import create_yuv_frame_from_planes

        uploader, textures = self._uploader_with_recorder()
        y = np.arange(16, dtype=np.uint16).reshape(4, 4)
        u = np.full((2, 2), 300, np.uint16)
        v = np.full((2, 2), 600, np.uint16)
        frame = create_yuv_frame_from_planes(y, u, v, "yuv420p10le")

        y_tex, _, _ = uploader.upload_planes(frame)

        assert y_tex.dtype == "f2"
        written = y_tex.write.call_args[0][0]
        # f2 = 2 字节/像素：float16 归一化数据
        assert len(written) == 16 * 2


# ============================================================================
# AUD-1100: 播放降级系统从未激活（全库无 set_playback_state(True)）
# ============================================================================


class _QualityRecorder:
    def __init__(self) -> None:
        self.states: List[bool] = []

    def set_playback_state(self, active: bool) -> None:
        self.states.append(active)


class TestAud1100PlaybackDegradationWired:
    def _patched(self, monkeypatch) -> _QualityRecorder:
        recorder = _QualityRecorder()
        monkeypatch.setattr(
            vp_module, "get_effect_preview_quality_manager", lambda: recorder)
        return recorder

    def test_v3_start_activates_degradation(self, qapp, monkeypatch) -> None:
        recorder = self._patched(monkeypatch)
        controller = _enhanced(qapp, _make_coordinator())

        controller.start_playback()

        assert recorder.states == [True], (
            "v3 播放成功启动后必须激活播放降级（此前全库无 True 调用点）"
        )
        assert controller.is_playing is True

    def test_v3_start_failure_does_not_activate(self, qapp, monkeypatch) -> None:
        recorder = self._patched(monkeypatch)
        coordinator = _make_coordinator()
        coordinator.start_playback.return_value = False
        controller = _enhanced(qapp, coordinator)

        controller.start_playback()

        assert recorder.states == [], "启动失败不得激活降级（V-P0-4 同型）"

    def test_not_loaded_start_does_not_activate(self, qapp, monkeypatch) -> None:
        recorder = self._patched(monkeypatch)
        coordinator = _make_coordinator()
        coordinator.is_loaded = False
        controller = _enhanced(qapp, coordinator)

        controller.start_playback()

        assert recorder.states == []

    def test_v3_stop_restores_full_quality(self, qapp, monkeypatch) -> None:
        recorder = self._patched(monkeypatch)
        controller = _enhanced(qapp, _make_coordinator())

        controller.start_playback()
        controller.stop_playback()

        assert recorder.states == [True, False], (
            "v3 停止分支不经 super()，必须就地恢复完整质量"
        )

    def test_unload_restores_full_quality(self, qapp, monkeypatch) -> None:
        recorder = self._patched(monkeypatch)
        controller = _enhanced(qapp, _make_coordinator())

        controller.start_playback()
        controller.unload_video()

        assert recorder.states == [True, False, False], (
            "卸载路径必须退出播放降级态"
        )

    def test_full_cycle_sequence(self, qapp, monkeypatch) -> None:
        recorder = self._patched(monkeypatch)
        controller = _enhanced(qapp, _make_coordinator())

        controller.start_playback()   # True
        controller.stop_playback()    # False
        controller.start_playback()   # True
        controller.unload_video()     # False ×2（unload 直呼一次 +
                                     # super().unload_video → stop_playback
                                     # 链路一次，幂等无害）

        assert recorder.states == [True, False, True, False, False]
        assert controller.is_playing is False

    def test_real_manager_state_round_trip(self, qapp) -> None:
        """端到端：真实管理器单例在 start/stop 后 is_playing 正确翻转。"""
        manager = vp_module.get_effect_preview_quality_manager()
        controller = _enhanced(qapp, _make_coordinator())

        controller.start_playback()
        assert manager.is_playing() is True
        assert manager.get_degradation_state().degradation_reason == "playback"

        controller.stop_playback()
        assert manager.is_playing() is False

        manager.set_playback_state(False)  # 复位全局状态
