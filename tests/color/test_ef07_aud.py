"""EF-07 批次审计修复回归测试（AUD-0206 ~ AUD-1003，19 条 Suggestion 级）。

每个测试对应 REMEDIATION_PLAN.md 的一条 ID；修复被回退时对应测试必须
失败。全部使用 mock / ``__new__`` 构造的对象，不创建真实 GL 上下文，
不弹出任何模态对话框（无 QMessageBox 路径）。
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.async_renderer import AsyncRenderer
from src.features.color.core.cache_manager import MemoryPressureMonitor
from src.features.color.core.color_output import ColorOutput
from src.features.color.core.color_session import (
    ColorSession,
    ColorSessionManager,
)
from src.features.color.core.grade_keyframes import GradeKeyframeTrack
from src.features.color.core.layer import Layer, _DEFAULT_QUALIFIER_PARAMS
from src.features.color.core.opengl_render_backend import OpenGLRenderBackend
from src.features.color.core.project_dependencies import (
    DependencyEntry,
    DependencyReport,
)
from src.features.color.core.video_denoise_pipeline import VideoDenoisePipeline
from src.shared.video.denoise.params import VideoDenoiseParams


# ---------------------------------------------------------------------------
# AUD-0206: load_prog 吞错 → _get_utility_shader 显式降级
# ---------------------------------------------------------------------------

def _bare_renderer() -> AsyncRenderer:
    """经 __new__ 构造最小 renderer（跳过 __init__ / 不触碰 GL）。"""
    return AsyncRenderer.__new__(AsyncRenderer)


class TestAud0206:
    def test_missing_shader_returns_none_and_logs_once(self, caplog):
        r = _bare_renderer()
        r.utility_shaders = {}
        with caplog.at_level(logging.ERROR, logger="src.features.color.core.async_renderer"):
            prog1 = r._get_utility_shader("keyer")
            prog2 = r._get_utility_shader("keyer")
        assert prog1 is None and prog2 is None
        errors = [rec for rec in caplog.records
                  if "keyer" in rec.getMessage() and rec.levelno == logging.ERROR]
        assert len(errors) == 1  # 每个名字只降级记录一次，不每帧刷屏

    def test_present_shader_returned_without_error(self, caplog):
        r = _bare_renderer()
        sentinel = object()
        r.utility_shaders = {"keyer": sentinel}
        with caplog.at_level(logging.ERROR, logger="src.features.color.core.async_renderer"):
            assert r._get_utility_shader("keyer") is sentinel
        assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]

    def test_keyer_missing_degrades_to_white_matte(self):
        """keyer 编译失败时 _generate_source_matte 走全白 matte 降级而非 KeyError。"""
        r = _bare_renderer()
        r.utility_shaders = {}
        r.ctx = MagicMock()
        surface = object()
        r.fb_manager = MagicMock()
        r.fb_manager.get_temp_texture.return_value = surface
        r.fb_manager.get_fbo.return_value = MagicMock()
        r._get_source_tex_for_qualifier = MagicMock()
        r._render_quad = MagicMock()
        layer = {"id": "L1",
                 "qualifier_params": {"enabled": True, "hue_center": 0.1},
                 "mask_shapes": []}
        out = r._generate_source_matte(layer, MagicMock(), 32, 32)
        assert out is surface
        r.ctx.clear.assert_called_with(1.0, 1.0, 1.0, 1.0)  # 降级为全白
        r._render_quad.assert_not_called()
        r._get_source_tex_for_qualifier.assert_not_called()

    def test_morphology_and_blur_missing_skip_pass(self):
        r = _bare_renderer()
        r.utility_shaders = {}
        r.ctx = MagicMock()
        r.fb_manager = MagicMock()
        src = object()
        assert r._morphology_pass(src, 32, 32, 2.0, 0) is src
        assert r._blur_matte_separable(src, 32, 32, 2.0) is src
        # blur shader 缺失 → _apply_matte_postprocess 跳过模糊（不分配中间纹理）
        qp = {"enabled": True, "blur": 5.0}
        matte = object()
        out = r._apply_matte_postprocess(matte, 32, 32, qp, [])
        assert out is matte
        r.fb_manager.get_temp_texture.assert_not_called()


# ---------------------------------------------------------------------------
# AUD-0208: baked / AI 纹理上传形状校验 + write 失败不泄漏
# ---------------------------------------------------------------------------

class _FakeTex:
    def __init__(self, fail_write: bool = False):
        self.released = False
        self.fail_write = fail_write
        self.wrote = False

    def filter(self, f):
        pass

    def write(self, data):
        self.wrote = True
        if self.fail_write:
            raise RuntimeError("GL_INVALID_VALUE: write size mismatch")

    def release(self):
        self.released = True


def _upload_renderer(baked=None, ai=None):
    r = _bare_renderer()
    r.ctx = SimpleNamespace(texture=lambda size, comps, dtype="f4": _fresh_tex[0])
    r.baked_textures = {} if baked is None else baked
    r.ai_plugin_textures = {} if ai is None else ai
    r.pyramid_textures = []
    return r


_fresh_tex = [_FakeTex()]


class TestAud0208:
    def setup_method(self):
        _fresh_tex[0] = _FakeTex()

    def test_baked_valid_3ch_uploaded(self):
        r = _upload_renderer()
        arr = np.zeros((4, 5, 3), dtype=np.float32)
        r._do_upload_baked_texture({"layer_id": "L1", "image_array": arr})
        assert "L1" in r.baked_textures
        assert r.baked_textures["L1"].wrote

    def test_baked_invalid_shape_rejected_old_texture_preserved(self):
        old = _FakeTex()
        baked = {"L1": old}
        r = _upload_renderer(baked=baked)
        bad = np.zeros((4, 5), dtype=np.float32)  # 2 维
        r._do_upload_baked_texture({"layer_id": "L1", "image_array": bad})
        assert not old.released          # 校验在释放旧纹理之前
        assert r.baked_textures["L1"] is old
        assert not _fresh_tex[0].wrote   # 纹理根本没被创建

    def test_baked_write_failure_releases_texture_no_leak(self):
        _fresh_tex[0] = _FakeTex(fail_write=True)
        r = _upload_renderer()
        arr = np.zeros((4, 5, 3), dtype=np.float32)
        r._do_upload_baked_texture({"layer_id": "L1", "image_array": arr})
        assert _fresh_tex[0].released    # 失败纹理被释放，不泄漏
        assert "L1" not in r.baked_textures

    def test_ai_grayscale_normalized_and_uploaded(self):
        r = _upload_renderer()
        arr = np.zeros((4, 5), dtype=np.float32)  # 灰度 → 归一化为 3 通道
        r._do_upload_ai_plugin_result(
            {"layer_id": "L1", "instance_id": "p1", "image_array": arr})
        key = ("L1", "p1")
        assert key in r.ai_plugin_textures
        assert r.ai_plugin_textures[key].wrote

    def test_ai_unnormalizable_shape_rejected_old_preserved(self):
        old = _FakeTex()
        ai = {("L1", "p1"): old}
        r = _upload_renderer(ai=ai)
        bad = np.zeros((4, 5, 5), dtype=np.float32)  # 5 通道，归一化无法处理
        r._do_upload_ai_plugin_result(
            {"layer_id": "L1", "instance_id": "p1", "image_array": bad})
        assert not old.released
        assert r.ai_plugin_textures[("L1", "p1")] is old
        assert not _fresh_tex[0].wrote

    def test_ai_write_failure_releases_texture_no_leak(self):
        _fresh_tex[0] = _FakeTex(fail_write=True)
        r = _upload_renderer()
        arr = np.zeros((4, 5, 3), dtype=np.float32)
        r._do_upload_ai_plugin_result(
            {"layer_id": "L1", "instance_id": "p1", "image_array": arr})
        assert _fresh_tex[0].released
        assert ("L1", "p1") not in r.ai_plugin_textures


# ---------------------------------------------------------------------------
# AUD-0211: run() 初始化失败路径必须执行 _cleanup
# ---------------------------------------------------------------------------

class TestAud0211:
    def test_makecurrent_failure_runs_cleanup(self, qapp):
        r = _bare_renderer()
        r.shared_pyqt_context = MagicMock()
        r.shared_pyqt_context.makeCurrent.return_value = False
        r.surface = MagicMock()
        r.is_running = True
        calls = []
        r._cleanup = lambda: calls.append(1)
        r.run()
        assert calls, "makeCurrent 失败路径必须执行 _cleanup"
        assert r.is_running is False

    def test_init_exception_runs_cleanup(self, qapp, monkeypatch):
        from src.features.color.core import async_renderer as ar_mod

        r = _bare_renderer()
        r.shared_pyqt_context = MagicMock()
        r.shared_pyqt_context.makeCurrent.return_value = True
        r.surface = MagicMock()
        r.is_running = True
        calls = []
        r._cleanup = lambda: calls.append(1)

        def boom():
            raise RuntimeError("no GL on CI")
        monkeypatch.setattr(ar_mod.moderngl, "create_context", boom)
        r.run()
        assert calls, "初始化异常路径必须执行 _cleanup"
        assert r.is_running is False


# ---------------------------------------------------------------------------
# AUD-0212: _do_render_video_frame 不得改写调用方 render_params
# ---------------------------------------------------------------------------

class TestAud0212:
    def test_caller_render_params_not_mutated(self):
        r = _bare_renderer()
        r.ctx = MagicMock()
        r._video_generation_id = 0
        captured = {}
        r._do_upload_video_frame = lambda payload: object()  # 上传成功
        r._do_render = lambda params, allow_during_playback: captured.update(
            params=dict(params))
        caller_params = {"viewport_size": (0, 0), "layers": []}
        data = {
            "frame_data": np.zeros((4, 4, 4), dtype=np.float16),
            "width": 4, "height": 4, "frame_index": 0,
            "render_params": caller_params,
        }
        r._do_render_video_frame(data)
        # 修正只发生在渲染线程私有副本上，调用方 dict 不被跨线程改写
        assert caller_params["viewport_size"] == (0, 0)
        assert captured["params"]["viewport_size"] == (4, 4)


# ---------------------------------------------------------------------------
# AUD-0301: ColorOutput 元组快照
# ---------------------------------------------------------------------------

class TestAud0301:
    def test_update_snapshot_consistent(self):
        co = ColorOutput()
        co.update(7, 1920, 1080)
        snap = co.snapshot()
        assert isinstance(snap, tuple)
        assert snap == (7, 1920, 1080, 1)
        assert co.texture_id == 7 and co.width == 1920 and co.height == 1080
        assert not co.is_dirty and co.is_valid

    def test_revision_monotonic_and_properties_follow(self):
        co = ColorOutput()
        co.update(1, 10, 20)
        co.update(2, 30, 40)
        snap = co.snapshot()
        assert snap[0] == 2 and snap[1] == 30 and snap[2] == 40 and snap[3] == 2
        co.mark_dirty()
        assert co.is_dirty

    def test_snapshot_absent_means_revert(self):
        assert hasattr(ColorOutput(), "snapshot"), \
            "AUD-0301 修复要求提供 snapshot() 一致快照接口"


# ---------------------------------------------------------------------------
# AUD-0319: MemoryPressureMonitor start/stop 加锁
# ---------------------------------------------------------------------------

class TestAud0319:
    def test_start_blocked_while_state_lock_held(self, monkeypatch):
        """持有 _state_lock 时 start() 必须等待——证明 check-then-act 在锁内。"""
        monkeypatch.setattr(MemoryPressureMonitor, "_monitor_loop",
                            lambda self: None)
        mon = MemoryPressureMonitor(MagicMockCacheManager())
        assert hasattr(mon, "_state_lock")
        mon._state_lock.acquire()
        try:
            t = threading.Thread(target=mon.start, daemon=True)
            t.start()
            t.join(0.3)
            assert t.is_alive(), "start() 未在 _state_lock 上等待（修复被回退）"
            assert mon._monitor_thread is None
        finally:
            mon._state_lock.release()
        t.join(2.0)
        assert not t.is_alive()
        assert mon.is_running
        assert mon._monitor_thread is not None
        mon.stop()

    def test_double_start_creates_single_thread(self, monkeypatch):
        unblock = threading.Event()
        monkeypatch.setattr(
            MemoryPressureMonitor, "_monitor_loop",
            lambda self: unblock.wait(5.0))
        mon = MemoryPressureMonitor(MagicMockCacheManager())
        try:
            mon.start()
            first = mon._monitor_thread
            mon.start()  # 第二次 start 必须是 no-op
            assert mon._monitor_thread is first
        finally:
            unblock.set()
            mon.stop()
        assert not mon.is_running


class MagicMockCacheManager:
    """CacheManager 替身（monitor 只在 pressure 回调时用到）。"""


# ---------------------------------------------------------------------------
# AUD-0401: 会话历史有界
# ---------------------------------------------------------------------------

class TestAud0401:
    def test_history_bounded_at_max(self):
        mgr = ColorSessionManager()
        limit = mgr._max_history
        ids = []
        for i in range(limit + 20):
            s = ColorSession.create_single_asset(
                project_id="p", color_stack_id=f"s{i}",
                source_path=f"/tmp/{i}.jpg")
            ids.append(s.session_id)
            mgr.set_current_session(s)
        assert len(mgr._session_history) == limit
        assert mgr.get_session_by_id(ids[0]) is None      # 最旧被淘汰
        assert mgr.get_session_by_id(ids[-1]) is not None  # 最新保留

    def test_resetting_same_session_does_not_grow(self):
        mgr = ColorSessionManager()
        s = ColorSession.create_single_asset(
            project_id="p", color_stack_id="s", source_path="/tmp/a.jpg")
        for _ in range(5):
            mgr.set_current_session(s)
        assert len(mgr._session_history) == 1


# ---------------------------------------------------------------------------
# AUD-0403: get_save_target_region 收敛为单行返回
# ---------------------------------------------------------------------------

class TestAud0403:
    def test_all_kinds_return_resolved_stack_region(self):
        sessions = [
            ColorSession.create_single_asset(
                project_id="p1", color_stack_id="st1", source_path="/a.jpg"),
            ColorSession.create_project_asset(
                project_id="p2", asset_id="a2", color_stack_id="st2",
                source_path="/b.jpg"),
            ColorSession.create_timeline_clip(
                project_id="p3", asset_id="a3", clip_id="c3",
                timeline_id="t3", color_stack_id="st3",
                source_path="/c.mp4"),
        ]
        for session, sid in zip(sessions, ("st1", "st2", "st3")):
            assert session.get_save_target_region() == f"color_stacks[{sid}]"

    def test_branchy_implementation_removed(self):
        src = inspect.getsource(
            ColorSession.get_save_target_region)
        assert 'return "unknown"' not in src
        assert "elif" not in src


# ---------------------------------------------------------------------------
# AUD-0415: 悬挂加载线程台账与回收
# ---------------------------------------------------------------------------

class TestAud0415:
    def setup_method(self):
        from src.features.color.core.denoise_engine import DenoiseWorker
        DenoiseWorker._hung_loader_threads.clear()

    def teardown_method(self):
        from src.features.color.core.denoise_engine import DenoiseWorker
        for thread, _ in DenoiseWorker._hung_loader_threads:
            thread.join(timeout=0.1)
        DenoiseWorker._hung_loader_threads.clear()

    def test_hung_loader_thread_tracked(self, monkeypatch):
        from src.features.color.core.denoise_engine import DenoiseWorker

        release = threading.Event()
        hung_started = threading.Event()

        class _FakeOrt:
            class InferenceSession:
                def __init__(self, *args, **kwargs):
                    hung_started.set()
                    release.wait(timeout=5.0)

        monkeypatch.setattr(
            "src.features.color.core.denoise_engine.ort", _FakeOrt)
        sess = DenoiseWorker._load_with_timeout(
            "model.onnx", object(), ["X"], timeout=0.2)
        assert sess is None
        assert hung_started.wait(1.0)
        ledger = DenoiseWorker._hung_loader_threads
        assert len(ledger) == 1, "超时线程必须进台账（修复被回退）"
        thread, path = ledger[0]
        assert path == "model.onnx"
        assert thread.is_alive()
        release.set()
        thread.join(2.0)

    def test_completed_hung_thread_reaped_on_next_call(self, monkeypatch):
        from src.features.color.core.denoise_engine import DenoiseWorker

        release = threading.Event()

        class _HungOrt:
            class InferenceSession:
                def __init__(self, *args, **kwargs):
                    release.wait(timeout=5.0)

        monkeypatch.setattr(
            "src.features.color.core.denoise_engine.ort", _HungOrt)
        assert DenoiseWorker._load_with_timeout(
            "m.onnx", object(), ["X"], timeout=0.2) is None
        release.set()
        for thread, _ in DenoiseWorker._hung_loader_threads:
            thread.join(2.0)

        class _FastOrt:
            @staticmethod
            def InferenceSession(*args, **kwargs):
                return "SESSION"

        monkeypatch.setattr(
            "src.features.color.core.denoise_engine.ort", _FastOrt)
        sess = DenoiseWorker._load_with_timeout(
            "other.onnx", object(), ["X"], timeout=2.0)
        assert sess == "SESSION"
        assert DenoiseWorker._hung_loader_threads == [], \
            "已结束的悬挂线程应在下次调用时回收"


# ---------------------------------------------------------------------------
# AUD-0418: 导出降噪桥输出 FBO 缓存
# ---------------------------------------------------------------------------

class _FakeFbo:
    def __init__(self):
        self.released = False
        self.reads = 0

    def read(self, components=3, dtype="f4"):
        self.reads += 1
        return b"\x00" * (4 * 4 * 3 * 4)

    def release(self):
        self.released = True


class TestAud0418:
    def _bridge(self, created):
        from src.features.color.core.export_denoise_bridge import (
            ExportVideoDenoiseBridge,
        )
        b = ExportVideoDenoiseBridge.__new__(ExportVideoDenoiseBridge)
        b._ctx = SimpleNamespace(
            framebuffer=lambda tex: created.append(_FakeFbo()) or created[-1])
        b._pipeline = object()
        b._input_tex = None
        b._out_fbo = None
        b._out_fbo_tex = None
        b._failed = False
        b._warned_process_error = False
        return b

    def test_fbo_cached_per_texture_identity(self):
        created = []
        b = self._bridge(created)
        tex = object()
        fbo1 = b._get_or_create_out_fbo(tex)
        fbo2 = b._get_or_create_out_fbo(tex)
        assert fbo1 is fbo2, "同一输出纹理必须复用缓存 FBO（修复被回退）"
        assert len(created) == 1

    def test_fbo_rebuilt_when_texture_changes(self):
        created = []
        b = self._bridge(created)
        tex_a, tex_b = object(), object()
        fbo_a = b._get_or_create_out_fbo(tex_a)
        fbo_b = b._get_or_create_out_fbo(tex_b)
        assert fbo_a is not fbo_b
        assert fbo_a.released, "旧 FBO 必须随纹理更换被释放"
        assert len(created) == 2

    def test_release_drops_cached_fbo_first(self):
        created = []
        b = self._bridge(created)
        tex = object()
        fbo = b._get_or_create_out_fbo(tex)
        b.release()
        assert fbo.released
        assert b._out_fbo is None and b._out_fbo_tex is None


# ---------------------------------------------------------------------------
# AUD-0522: 关键帧轨道编辑/求值互斥
# ---------------------------------------------------------------------------

class TestAud0522:
    def test_set_keyframe_blocked_while_lock_held(self):
        track = GradeKeyframeTrack("exposure")
        track.set_keyframe(0, 0.0)
        track.set_keyframe(10, 1.0)
        assert hasattr(track, "_lock")
        track._lock.acquire()
        try:
            t = threading.Thread(target=track.set_keyframe, args=(5, 0.5),
                                 daemon=True)
            t.start()
            t.join(0.3)
            assert t.is_alive(), "编辑未在轨道锁上等待（修复被回退）"
        finally:
            track._lock.release()
        t.join(2.0)
        assert not t.is_alive()
        assert [kf.frame for kf in track.keyframes] == [0, 5, 10]

    def test_value_at_blocked_while_lock_held(self):
        track = GradeKeyframeTrack("exposure")
        track.set_keyframe(0, 0.0)
        track.set_keyframe(10, 1.0)
        track._lock.acquire()
        try:
            t = threading.Thread(target=track.value_at, args=(5, 0.0),
                                 daemon=True)
            t.start()
            t.join(0.3)
            assert t.is_alive(), "求值未在轨道锁上等待（修复被回退）"
        finally:
            track._lock.release()
        t.join(2.0)
        assert not t.is_alive()

    def test_value_at_semantics_preserved(self):
        track = GradeKeyframeTrack("exposure")
        track.set_keyframe(0, 0.0)
        track.set_keyframe(10, 1.0)
        assert track.value_at(-5, 9.9) == 0.0   # 首帧前外推
        assert track.value_at(99, 9.9) == 1.0   # 末帧后外推
        assert track.value_at(5, 9.9) == 0.5    # 线性中点


# ---------------------------------------------------------------------------
# AUD-0612: on_raw_params_changed 隔离调用方 dict
# ---------------------------------------------------------------------------

class TestAud0612:
    def _engine(self):
        from src.features.color.core.image_engine import ImageEngine
        engine = ImageEngine.__new__(ImageEngine)
        engine.current_file_path = "/tmp/photo.jpg"  # 非 RAW → 不进 worker
        engine.current_raw_metadata = {"camera_wb": [2.0, 1.0, 1.5, 1.0]}
        engine._last_applied_raw_params = {"enabled": False, "exposure": 99.0}
        engine.current_raw_params = None
        engine.renderer = None
        engine.render_pipeline = lambda **kwargs: None
        engine.is_interacting = False
        return engine

    def test_caller_dict_not_mutated_and_no_aliasing(self):
        engine = self._engine()
        caller = {"wb_type": "camera", "exposure": 0.5}
        engine.on_raw_params_changed(caller)
        assert "user_wb" not in caller, \
            "调用方 dict 被就地写入 user_wb（修复被回退）"
        assert engine.current_raw_params is not caller
        assert engine.current_raw_params["exposure"] == 0.5
        # user_wb 是 camera_wb 的副本，不是同一引用（防引用污染）
        wb = engine.current_raw_params["user_wb"]
        assert wb == engine.current_raw_metadata["camera_wb"]
        assert wb is not engine.current_raw_metadata["camera_wb"]


# ---------------------------------------------------------------------------
# AUD-0704: reset_params 复位完整 + film_grain 死代码移除
# ---------------------------------------------------------------------------

class TestAud0704:
    def test_reset_params_restores_full_qualifier_defaults(self):
        layer = Layer()
        layer.qualifier_params.update({
            "enabled": True, "hue_center": 0.2, "hue_width": 0.3,
            "sat_low": 0.4, "lum_high": 0.7, "invert": True,
        })
        layer.reset_params()
        assert layer.qualifier_params == dict(_DEFAULT_QUALIFIER_PARAMS)
        assert layer.qualifier_params["hue_center"] == 0.5
        assert layer.qualifier_params["enabled"] is False

    def test_copy_params_from_ignores_film_grain(self):
        source = Layer()
        source.grading_params["exposure"] = 1.5
        source.film_grain_params = {"enabled": True}  # engine 级属性被误挂
        target = Layer()
        target.copy_params_from(source)
        assert target.grading_params["exposure"] == 1.5
        assert not hasattr(target, "film_grain_params"), \
            "film_grain_params 死代码分支被恢复（修复被回退）"

    def test_docstring_no_longer_promises_film_grain_copy(self):
        assert "胶片颗粒参数" not in inspect.getsource(
            Layer.copy_params_from).split('"""')[1]


# ---------------------------------------------------------------------------
# AUD-0714: export_lut 回调连接幂等
# ---------------------------------------------------------------------------

class TestAud0714:
    def test_duplicate_export_does_not_duplicate_callback(self, qapp, tmp_path):
        from src.features.color.core.lut_exporter import LutExporter

        exporter = LutExporter()
        try:
            finished_calls = []
            layer = SimpleNamespace(
                visible=True,
                grading_params={"exposure": 0.25},
                curves_lut=None, hsl_curve_lut=None,
                mixer_params={}, soft_clip_params={},
                lut_intensity=1.0, active_lut_path=None,
            )
            exporter.export_lut([layer], str(tmp_path / "a.cube"),
                                on_finished=finished_calls.append)
            exporter.export_lut([layer], str(tmp_path / "b.cube"),
                                on_finished=finished_calls.append)
            exporter.worker.finished.emit(str(tmp_path / "a.cube"))
            qapp.processEvents()
            assert len(finished_calls) == 1, \
                "UniqueConnection 缺失，回调重复触发（修复被回退）"
        finally:
            exporter.worker_thread.quit()
            exporter.worker_thread.wait(2000)


# ---------------------------------------------------------------------------
# AUD-0731: release() 非渲染线程释放带 release_fn 台账时告警
# ---------------------------------------------------------------------------

class TestAud0731:
    def test_release_off_thread_with_release_fn_logs_error(self, caplog):
        backend = OpenGLRenderBackend(object())
        backend.registry.register(
            "upload", "tex", release_fn=lambda: None)
        with caplog.at_level(logging.ERROR,
                             logger="src.features.color.core.opengl_render_backend"):
            backend.release()
        assert any("release" in rec.getMessage().lower()
                   and "render thread" in rec.getMessage().lower()
                   for rec in caplog.records), \
            "非渲染线程释放带 release_fn 台账必须留下 error 日志（修复被回退）"
        assert backend.state.value == "released"

    def test_release_on_owner_thread_quiet(self, qapp, caplog):
        from PyQt6.QtCore import QThread
        backend = OpenGLRenderBackend(object())
        backend._owner_thread = QThread.currentThread()
        backend.registry.register(
            "upload", "tex", release_fn=lambda: None)
        with caplog.at_level(logging.ERROR,
                             logger="src.features.color.core.opengl_render_backend"):
            backend.release()
        assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
        assert backend.registry.record_count() == 0

    def test_release_pure_ledger_off_thread_no_error(self, caplog):
        """release_fn=None（当前生产唯一用法）在任何线程清台账都安全无告警。"""
        backend = OpenGLRenderBackend(object())
        backend.registry.register("shaders", "utility")
        with caplog.at_level(logging.ERROR,
                             logger="src.features.color.core.opengl_render_backend"):
            backend.release()
        assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


# ---------------------------------------------------------------------------
# AUD-0820: DependencyReport.from_dict 容忍损坏的外部 JSON
# ---------------------------------------------------------------------------

class TestAud0820:
    def test_non_dict_entries_skipped_not_crash(self):
        report = DependencyReport.from_dict({"entries": [
            {"kind": "lut", "key": "k1", "status": "ok", "size_bytes": 5},
            "junk-string",
            42,
            None,
            ["also", "junk"],
        ]})
        assert len(report.entries) == 1
        assert report.entries[0].key == "k1"
        assert report.entries[0].size_bytes == 5

    def test_non_numeric_size_bytes_coerced_to_zero(self):
        entry = DependencyEntry.from_dict(
            {"kind": "lut", "key": "k2", "size_bytes": "not-a-number"})
        assert entry.size_bytes == 0

    def test_clean_roundtrip_unchanged(self):
        data = {"entries": [
            {"kind": "plugin", "key": "p", "status": "missing",
             "size_bytes": 0, "detail": "d"},
        ]}
        report = DependencyReport.from_dict(data)
        assert report.missing[0].key == "p"
        assert report.to_dict()["entries"][0]["detail"] == "d"


# ---------------------------------------------------------------------------
# AUD-0905: pipeline.params 返回只读快照
# ---------------------------------------------------------------------------

class TestAud0905:
    def test_params_property_returns_copy(self):
        p = VideoDenoisePipeline.__new__(VideoDenoisePipeline)
        p._params = VideoDenoiseParams()
        got = p.params
        assert got is not p._params, \
            "params 必须返回副本而非内部对象（修复被回退）"
        assert got == p._params

    def test_params_reflects_set_params(self):
        p = VideoDenoisePipeline.__new__(VideoDenoisePipeline)
        p._params = VideoDenoiseParams()
        p._params = VideoDenoiseParams().sanitized()
        assert p.params == p._params


# ---------------------------------------------------------------------------
# AUD-0914: decompile 输入校验
# ---------------------------------------------------------------------------

class TestAud0914:
    def setup_method(self):
        from src.features.color.core import the_decompiler
        if not the_decompiler.HAS_SCIPY:
            pytest.skip("scipy 不可用，decompile 校验路径不适用")

    def test_missing_path_raises_file_not_found(self):
        from src.features.color.core.the_decompiler import Decompiler
        d = Decompiler()
        with pytest.raises(FileNotFoundError):
            d.decompile("/nonexistent/definitely-missing.cube")

    def test_empty_path_raises_value_error(self):
        from src.features.color.core.the_decompiler import Decompiler
        d = Decompiler()
        with pytest.raises(ValueError):
            d.decompile("")

    def test_invalid_lut_size_raises_value_error(self, tmp_path):
        from src.features.color.core.the_decompiler import Decompiler
        lut_file = tmp_path / "tiny.cube"
        lut_file.write_text("# dummy")
        d = Decompiler()

        def fake_load_cube(path):
            d.lut_size = 1
            d.lut_data = None
        d.load_cube = fake_load_cube
        with pytest.raises(ValueError, match="invalid size"):
            d.decompile(str(lut_file))


# ---------------------------------------------------------------------------
# AUD-1003: 因果模式历史推进拷贝的决策记录（保留 + 记录理由）
# ---------------------------------------------------------------------------

class TestAud1003:
    def test_causal_history_copy_decision_documented(self):
        """AUD-1003 采用审计的替代分支：保留拷贝并记录决策理由。

        评估结论：删除拷贝需同时移除因果路径的 ``temporal_out`` 表面键，
        而该键属于 RenderSurfaceCache 表面键契约（现有 adjacent 测试断言
        其存在），单方面移除超出本批最小改动边界——按审计原文
        "若下游确有写入冲突风险，则保留但记录该决策" 处理。
        """
        from src.features.color.core.video_temporal_denoise import (
            VideoTemporalDenoiser,
        )
        src = inspect.getsource(VideoTemporalDenoiser._process_causal)
        assert "AUD-1003" in src, "决策记录被移除（修复被回退）"
        # 行为契约不变：历史推进后仍返回 accum 输出纹理
        assert "next_hist.use()" in src
        assert "return out_tex" in src
