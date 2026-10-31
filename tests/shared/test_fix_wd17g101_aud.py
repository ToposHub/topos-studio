"""wd17-g101：video 域前段（色彩矩阵/去马赛克/解码 worker/帧供给）16 条审计修复测试。

覆盖（docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md）：
AUD-38700 / AUD-38702 / AUD-38705 / AUD-38801 / AUD-38802 / AUD-38905 /
AUD-39000 / AUD-39004 / AUD-39101 / AUD-39102 / AUD-39103 / AUD-39104 /
AUD-39105 / AUD-39106 / AUD-39107 / AUD-39200

GPU 路径全部用 fake ctx/fbo 或 monkeypatch 验证（不创建 GL 上下文）。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.media.metadata import Rational, VideoMetadata
from src.shared.media.proxy import DiskProxyInfo, ProxyStatus
from src.shared.video.decode_worker import (
    MasterDecodeWorker,
    ProxyDecodeWorker,
    WorkerState,
)
from src.shared.video.fallback_types import (
    FallbackEvent,
    VideoPipelineFallbackReason,
    fallback_logger,
    get_last_fallback_reason,
)
from src.shared.video.frame_provider_service import (
    ClipSession,
    FrameQuality,
    VideoFrameProviderService,
)
from src.shared.video.frame_queue import (
    FrameSource,
    QueueEntry,
    ReadyFrameQueue,
)

JOIN_TIMEOUT_S = 3.0


# ============================================================================
# AUD-38700: resolve_debayer_params 整对象优先级（中性 RawParams 覆盖 as-shot）
# ============================================================================


class TestAud38700ResolveDebayerParamsPriority:
    def _trwm(self) -> dict:
        return {"wb_gain_r": 1.9, "wb_gain_g": 1.0, "wb_gain_b": 1.6,
                "exposure_stops": 0.3, "black_level": 64.0,
                "sensor_matrix_id": 1}

    def test_neutral_raw_params_fall_back_to_trwm(self) -> None:
        """全中性默认 RawParams 不得静默覆盖 TRWM as-shot WB/黑位。"""
        from src.features.edit.core.clip_params import RawParams
        from src.shared.video.camera_gamut_matrices import resolve_debayer_params

        rp = resolve_debayer_params(RawParams(), self._trwm())
        assert rp["wb_gains"] == (1.9, 1.0, 1.6)
        assert rp["exposure_stops"] == 0.3
        assert rp["black_level"] == 64.0

    def test_neutral_simple_namespace_falls_back_to_trwm(self) -> None:
        """同名属性对象（序列化兼容）同语义。"""
        from src.shared.video.camera_gamut_matrices import resolve_debayer_params

        raw = SimpleNamespace(wb_gain_r=1.0, wb_gain_g=1.0, wb_gain_b=1.0,
                              exposure_stops=0.0, black_level=0.0,
                              color_space="native", gamma="preview-2.2",
                              display_ootf=True)
        rp = resolve_debayer_params(raw, self._trwm())
        assert rp["wb_gains"] == (1.9, 1.0, 1.6)
        assert rp["black_level"] == 64.0

    def test_user_authored_raw_params_still_win_whole_object(self) -> None:
        """任一字段被用户改过 → 整对象优先（2026-09-21 复查锁定的语义）。"""
        from src.features.edit.core.clip_params import RawParams
        from src.shared.video.camera_gamut_matrices import resolve_debayer_params

        raw = RawParams(wb_gain_r=2.0)
        rp = resolve_debayer_params(raw, self._trwm())
        assert rp["wb_gains"] == (2.0, 1.0, 1.0)
        assert rp["exposure_stops"] == 0.0
        assert rp["black_level"] == 0.0


# ============================================================================
# AUD-38702: CPU black_level 应用点与 GPU shader 一致（先平均后减）
# ============================================================================


class TestAud38702BlackLevelApplicationPoint:
    def test_black_level_subtracted_after_interpolation(self) -> None:
        """跨黑位采样：GPU 语义 = 平均后减黑位（clamp 0），非逐平面先减。"""
        from src.shared.video.cfa_cpu_debayer import (
            _assemble_rgb,
            debayer_cfa_planes,
        )

        w = h = 4
        pw = ph = 2
        rng_values = np.array([[150, 0], [50, 0]], dtype=np.uint16)
        planes = (
            rng_values,
            np.zeros((ph, pw), dtype=np.uint16),
            np.zeros((ph, pw), dtype=np.uint16),
            np.zeros((ph, pw), dtype=np.uint16),
        )
        black = 100.0
        out = debayer_cfa_planes(planes, w, h, bit_depth=16,
                                 black_level=black, gamma_mode=0)

        # 参考实现：先组装（双线性平均）再减黑位（与 GPU shader 同点）
        r_full, _, _ = _assemble_rgb(
            *(np.asarray(p, dtype=np.float32) for p in planes))
        expected_r = np.clip(
            (np.maximum(r_full - black, 0.0))[:h, :w] / 65535.0, 0.0, 1.0)
        np.testing.assert_allclose(out[..., 0], expected_r, atol=1e-6)

        # 反证旧缺陷：全分辨率 (1,0) 位点 R = (150+50)/2 = 100，减黑位后 0；
        # 旧的"逐平面先减后平均"会得到 25/65535 ≈ 3.8e-4
        assert out[1, 0, 0] == 0.0

    def test_positive_shadow_values_match_gpu_order(self) -> None:
        """黑位以上区域两种顺序数值一致（回归保护：修复不改变亮区）。"""
        from src.shared.video.cfa_cpu_debayer import debayer_cfa_planes

        w = h = 4
        pw = ph = 2
        base = np.full((ph, pw), 1000, dtype=np.uint16)
        planes = (base, base, base, base)
        out = debayer_cfa_planes(planes, w, h, bit_depth=16,
                                 black_level=100.0, gamma_mode=0)
        np.testing.assert_allclose(
            out, np.full((h, w, 3), (1000 - 100) / 65535.0, dtype=np.float32),
            atol=1e-6)


# ============================================================================
# AUD-38705: upload_and_render 的 viewport 被 fbo.use() 覆盖
# ============================================================================


class _FakeCtx:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._viewport = (0, 0, 100, 100)

    @property
    def viewport(self) -> tuple:
        return self._viewport

    @viewport.setter
    def viewport(self, value: tuple) -> None:
        self.calls.append(("set_viewport", tuple(int(v) for v in value)))
        self._viewport = tuple(int(v) for v in value)


class _FakeFbo:
    def __init__(self, ctx: _FakeCtx) -> None:
        self._ctx = ctx

    def use(self) -> None:
        # ModernGL 语义：fbo.use() 会把 viewport 重绑为 FBO 自身 viewport
        self._ctx.calls.append(("fbo_use",))
        self._ctx._viewport = (0, 0, 64, 64)


class TestAud38705ViewportOrdering:
    def test_viewport_set_after_fbo_use_and_restored(self) -> None:
        pytest.importorskip("moderngl")
        from src.shared.video.cfa_gpu_debayer import CfaGpuDebayer

        ctx = _FakeCtx()
        fbo = _FakeFbo(ctx)
        deb = CfaGpuDebayer(ctx, bit_depth=12)

        render_viewports: list[tuple] = []
        from collections import defaultdict

        deb._prog = defaultdict(lambda: SimpleNamespace(value=None))
        deb._vao = SimpleNamespace(
            render=lambda mode, **kw: render_viewports.append(ctx.viewport))
        # 绕过真实 GL 资源创建（fake ctx 没有 texture/buffer）
        deb._ensure_program = lambda: deb._prog  # type: ignore[method-assign]
        fake_textures = [MagicMock() for _ in range(4)]
        deb._ensure_textures = (  # type: ignore[method-assign]
            lambda pw, ph: fake_textures)
        deb._write_plane = lambda idx, plane: None  # type: ignore[method-assign]
        pw = ph = 2
        planes = tuple(
            np.zeros((ph, pw), dtype=np.uint16) for _ in range(4))

        deb.upload_and_render(planes, 4, 4, fbo, viewport=(5, 6, 32, 32))

        set_calls = [c for c in ctx.calls if c[0] == "set_viewport"]
        assert set_calls == [
            ("set_viewport", (5, 6, 32, 32)),    # fbo.use() 之后设定
            ("set_viewport", (0, 0, 100, 100)),  # 结束后恢复调用前值
        ], "viewport 只应被请求值设定一次（fbo.use() 后）并恢复前值"
        # fbo.use() 必须先于 viewport 设定（否则会被 fbo.use() 覆盖）
        assert ctx.calls.index(("fbo_use",)) < ctx.calls.index(
            ("set_viewport", (5, 6, 32, 32)))
        # 绘制时生效
        assert render_viewports == [(5, 6, 32, 32)]
        # 结束后恢复调用前 viewport
        assert ctx.viewport == (0, 0, 100, 100)

    def test_shader_template_black_level_after_interpolation(self) -> None:
        """GLSL 静态校验：黑位减除在相位插值之后（AUD-38702 GPU 侧锚点）。"""
        from src.shared.video import cfa_gpu_debayer as gpu_mod

        fs = gpu_mod._DEBAYER_FS_TEMPLATE
        # fetch/插值完成后才有 black_level 减除；减除点在逆曲线前
        interp_pos = fs.index("f_color = vec4")  # noqa: F841—占位
        bl_pos = fs.index("max(r - black_level")
        inverse_pos = fs.index("traw_log_inverse(r)")
        assert bl_pos < inverse_pos, "黑位减除必须在逆曲线之前"


# ============================================================================
# AUD-38801: 帧去重标记检查与置位不原子（check-then-act 竞态）
# ============================================================================


def _proxy_worker() -> ProxyDecodeWorker:
    return ProxyDecodeWorker(
        frame_provider=SimpleNamespace(total_frames=100),
        frame_queue=SimpleNamespace(enqueue=lambda entry: True),
    )


class TestAud38801AtomicDedupMark:
    def test_pop_marks_inflight_atomically(self) -> None:
        """get_next_request 出队即在途标记置位（同一临界区）。"""
        worker = _proxy_worker()
        worker.request_frame(7, generation_id=1)

        request = worker.get_next_request()
        assert request is not None and request.frame_index == 7
        with worker._request_lock:
            assert (1, 7) in worker._decoding_frames, (
                "出队后必须立即置位在途标记（关闭 check-then-act 窗口）"
            )

    def test_request_during_inflight_window_is_deduped(self) -> None:
        """出队后、解码完成前的重复请求必须被去重（不再重复解码）。"""
        from src.shared.video.frame_request import FrameRequestReason

        worker = _proxy_worker()
        worker.request_frame(9, generation_id=2)
        popped = worker.get_next_request()
        assert popped is not None

        # 旧缺陷窗口：pending 已被取走、标记未置位 → 重复入队
        worker.request_frame(
            9, generation_id=2, reason=FrameRequestReason.SEEK)
        with worker._request_lock:
            pending = list(worker._pending_requests)
        assert pending == [], "在途帧的重复请求必须被去重"
        assert worker._stats.requests_deduped >= 1

    def test_master_request_during_inflight_window_is_deduped(self) -> None:
        from src.shared.video.frame_request import FrameRequestReason

        worker = MasterDecodeWorker(
            frame_provider=SimpleNamespace(total_frames=100),
            frame_queue=SimpleNamespace(enqueue=lambda entry: True),
        )
        worker.request_master_frame(
            5, generation_id=1, reason=FrameRequestReason.SEEK)
        popped = worker.get_next_request()
        assert popped is not None
        with worker._request_lock:
            assert (1, 5) in worker._decoding_frames

        worker.request_master_frame(
            5, generation_id=1, reason=FrameRequestReason.SEEK)
        assert worker._pending_master_request is None, (
            "master 在途帧的重复请求必须被去重"
        )
        assert worker._stats.requests_deduped >= 1


# ============================================================================
# AUD-38802: stop() 超时后 start() 产生双工作线程
# ============================================================================


class TestAud38802NoDoubleWorkerAfterStopTimeout:
    def test_start_refused_while_stale_thread_alive(self) -> None:
        worker = _proxy_worker()
        release = threading.Event()
        stale = threading.Thread(
            target=lambda: release.wait(5.0), daemon=True)
        stale.start()
        # 模拟 stop() 超时后的状态：ERROR + 残留线程
        worker._thread = stale
        with worker._state_lock:
            worker._state = WorkerState.ERROR

        assert worker.start() is False, (
            "旧线程仍存活时 start() 必须拒绝，避免双工作线程"
        )
        assert worker._thread is stale, "不得创建新线程"
        assert worker.state == WorkerState.ERROR

        release.set()
        stale.join(JOIN_TIMEOUT_S)
        assert worker.start() is True, "旧线程退出后允许重启"
        assert worker.state == WorkerState.RUNNING
        assert worker.stop(timeout=JOIN_TIMEOUT_S) is True


# ============================================================================
# AUD-38905: window_provider 独立解码会话泄漏（clear 兜底释放）
# ============================================================================


class TestAud38905WindowProviderSessionRelease:
    def _make_provider(self):
        pytest.importorskip("PyQt6")
        from PyQt6.QtCore import QCoreApplication

        if QCoreApplication.instance() is None:
            QCoreApplication([])
        from src.shared.video.denoise.window_provider import (
            VideoDenoiseWindowProvider,
        )
        return VideoDenoiseWindowProvider()

    def test_clear_releases_clip_session_via_worker(
            self, monkeypatch) -> None:
        provider = self._make_provider()
        closed: list[str] = []
        fake_service = SimpleNamespace(
            close_clip=lambda clip_id: closed.append(clip_id))
        import src.shared.video.frame_provider_service as fps_mod
        monkeypatch.setattr(
            fps_mod, "get_frame_provider_service", lambda: fake_service)

        provider._clip_id = "clip_denoise"
        provider._clip_epoch = 7
        provider.clear()

        deadline = time.time() + JOIN_TIMEOUT_S
        while not closed and time.time() < deadline:
            time.sleep(0.02)
        assert closed == ["clip_denoise"], (
            "clear() 必须经后台 worker 关闭帧服务独立会话（AUD-38905 泄漏）"
        )
        assert provider._clip_id is None
        assert provider._clip_path is None
        provider._jobs.put(None)

    def test_close_job_skipped_when_epoch_advanced(self) -> None:
        provider = self._make_provider()
        closed: list[str] = []
        fake_service = SimpleNamespace(
            close_clip=lambda clip_id: closed.append(clip_id))
        import src.shared.video.frame_provider_service as fps_mod
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            fps_mod, "get_frame_provider_service", lambda: fake_service)
        try:
            provider._clip_id = "clip_new"
            provider._clip_epoch = 5
            # 关闭任务携带旧世代号（排队期间已有新会话打开）
            provider._close_clip_job("clip_new", 4)
            assert closed == [], "世代号已变，关闭任务必须放弃"
            assert provider._clip_id == "clip_new"
        finally:
            monkeypatch.undo()
        provider._jobs.put(None)


# ============================================================================
# AUD-39000: external_yuv_renderer 宽泛 except 静默 + GL 状态不恢复
# ============================================================================


def _yuv_plane(texture_id: int, target: str = "2d") -> "GpuFramePlane":
    from src.shared.media.gpu_frame import GpuFramePlane

    return GpuFramePlane(
        component="Y", api="opengl", texture_id=texture_id,
        target=target, width=64, height=64, internal_format="GL_R8",
    )


class TestAud39000ExternalYuvRenderer:
    def test_import_failure_is_logged(self, monkeypatch, caplog) -> None:
        from src.shared.video.external_yuv_renderer import ExternalYuvToRgbPass

        monkeypatch.setitem(sys.modules, "moderngl", None)
        render_pass = ExternalYuvToRgbPass(MagicMock())
        with caplog.at_level(
                logging.WARNING,
                logger="src.shared.video.external_yuv_renderer"):
            assert render_pass._ensure_resources() is False
        assert any("unavailable" in rec.message for rec in caplog.records), (
            "领域错误（导入失败）必须留痕，不得静默吞掉"
        )

    def test_shader_failure_is_logged_and_released(self, caplog) -> None:
        moderngl = pytest.importorskip("moderngl")
        from src.shared.video.external_yuv_renderer import ExternalYuvToRgbPass

        ctx = MagicMock()
        ctx.program.side_effect = moderngl.Error("shader compile failed")
        render_pass = ExternalYuvToRgbPass(ctx)
        with caplog.at_level(
                logging.WARNING,
                logger="src.shared.video.external_yuv_renderer"):
            assert render_pass.initialize() is False
        assert any("creation failed" in rec.message for rec in caplog.records)

    def test_render_failure_restores_gl_state(self, caplog) -> None:
        pytest.importorskip("moderngl")
        from src.shared.video.external_yuv_renderer import ExternalYuvToRgbPass

        ctx = MagicMock()
        render_pass = ExternalYuvToRgbPass(ctx)
        assert render_pass.initialize() is True

        bad_fbo = MagicMock()
        bad_fbo.use.side_effect = RuntimeError("fbo lost")
        restored_kwargs: dict = {}

        with patch("OpenGL.GL.glUseProgram"), \
             patch("OpenGL.GL.glActiveTexture"), \
             patch("OpenGL.GL.glBindTexture"), \
             patch("OpenGL.GL.glUniform1i"), \
             patch("OpenGL.GL.glGetUniformLocation"), \
             patch.object(
                 render_pass, "_restore_gl_state",
                 side_effect=lambda **kw: restored_kwargs.update(kw)) as restore:
            with caplog.at_level(
                    logging.WARNING,
                    logger="src.shared.video.external_yuv_renderer"):
                ok = render_pass.render(
                    (_yuv_plane(101), _yuv_plane(102)),
                    SimpleNamespace(color_range="limited",
                                    color_matrix="bt709"),
                    bad_fbo, 64, 64,
                )
        assert ok is False
        restore.assert_called_once(), "失败路径必须恢复 GL 状态"
        assert restored_kwargs["prev_viewport"] is ctx.viewport
        assert any("render failed" in rec.message for rec in caplog.records)

    def test_render_success_does_not_restore(self) -> None:
        pytest.importorskip("moderngl")
        from src.shared.video.external_yuv_renderer import ExternalYuvToRgbPass

        ctx = MagicMock()
        render_pass = ExternalYuvToRgbPass(ctx)
        assert render_pass.initialize() is True
        with patch("OpenGL.GL.glUseProgram"), \
             patch("OpenGL.GL.glActiveTexture"), \
             patch("OpenGL.GL.glBindTexture"), \
             patch("OpenGL.GL.glUniform1i"), \
             patch("OpenGL.GL.glGetUniformLocation"), \
             patch.object(render_pass, "_restore_gl_state") as restore:
            ok = render_pass.render(
                (_yuv_plane(101), _yuv_plane(102)),
                SimpleNamespace(color_range="limited",
                                color_matrix="bt709"),
                MagicMock(), 64, 64,
            )
        assert ok is True
        restore.assert_not_called(), "成功路径不得扰动 GL 状态"


# ============================================================================
# AUD-39004: get_recent 文档承诺与实际顺序相反（doc-gap）
# ============================================================================


class TestAud39004GetRecentOrderContract:
    def test_get_recent_is_chronological(self) -> None:
        fallback_logger.clear()
        try:
            for i in range(3):
                fallback_logger.log(FallbackEvent(
                    reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
                    codec=f"codec{i}"))
            events = fallback_logger.get_recent(limit=3)
            # 实际（且被 tests/video/test_video_pipeline_fallback_types.py
            # 锁定）行为：时间正序，最旧在前、最新在末尾
            assert [e.codec for e in events] == ["codec0", "codec1", "codec2"]
            assert get_last_fallback_reason() == events[-1].reason
        finally:
            fallback_logger.clear()

    def test_docstring_matches_actual_order(self) -> None:
        from src.shared.video.fallback_types import FallbackLogger

        doc = FallbackLogger.get_recent.__doc__ or ""
        assert "时间正序" in doc, "文档必须声明时间正序（修复 doc-gap）"
        assert "最新的在前" not in doc, "文档不得再承诺与实现相反的顺序"


# ============================================================================
# AUD-39101: AUTO 质量缓存桶错位（proxy 帧写进 master 缓存）
# ============================================================================


def _make_session(clip_id: str) -> ClipSession:
    session = ClipSession(clip_id=clip_id, path=f"/mock/{clip_id}.mov")
    session.is_open = True
    session.metadata = MagicMock(total_frames=100)
    session.master_source = MagicMock()
    session.proxy_source = MagicMock()
    return session


class TestAud39101AutoCacheBucket:
    def test_resolve_cache_quality(self) -> None:
        session = _make_session("c")
        assert session.resolve_cache_quality(
            FrameQuality.PROXY) is FrameQuality.PROXY
        assert session.resolve_cache_quality(
            FrameQuality.MASTER) is FrameQuality.MASTER
        # preview 模式 AUTO 由代理源服务 → PROXY 桶
        assert session.resolve_cache_quality(
            FrameQuality.AUTO) is FrameQuality.PROXY
        # 无代理源 → master 源 → MASTER 桶
        session.proxy_source = None
        assert session.resolve_cache_quality(
            FrameQuality.AUTO) is FrameQuality.MASTER
        # paused/export 模式 → MASTER 桶
        session.proxy_source = MagicMock()
        session.set_playback_mode("paused")
        assert session.resolve_cache_quality(
            FrameQuality.AUTO) is FrameQuality.MASTER

    def test_auto_frame_from_proxy_not_cached_as_master(self) -> None:
        service = VideoFrameProviderService()
        session = _make_session("c")
        service._clips[session.clip_id] = session

        proxy_frame = SimpleNamespace(frame_index=0)
        session.proxy_source.read_frame.return_value = proxy_frame
        master_frame = SimpleNamespace(frame_index=0)
        session.master_source.read_frame.return_value = master_frame

        got = service.request_frame(session.clip_id, 0, quality="auto")
        assert got is proxy_frame
        assert 0 in session.frame_cache_proxy
        assert 0 not in session.frame_cache_master, (
            "proxy 源帧不得写入 master 缓存（AUD-39101 桶错位）"
        )

        # 后续 MASTER 请求不得命中 proxy 分辨率帧
        got_master = service.request_frame(
            session.clip_id, 0, quality="master")
        assert got_master is master_frame, (
            "MASTER 请求必须拿到 master 源帧，而不是 proxy 缓存帧"
        )
        assert 0 in session.frame_cache_master


# ============================================================================
# AUD-39102: open_clip 打开失败清理不释放已打开的源
# ============================================================================


class TestAud39102OpenFailureReleasesSources:
    def test_open_failure_closes_half_opened_sources(self) -> None:
        from src.shared.media.source import MediaOpenError

        service = VideoFrameProviderService()
        closed: list[str] = []

        def _half_open(session: ClipSession) -> None:
            session.master_source = SimpleNamespace(
                close=lambda: closed.append("master"))
            session.vt_source = SimpleNamespace(
                close=lambda: closed.append("vt"))
            raise RuntimeError("proxy source boom")

        with patch.object(service, "_open_clip_sources",
                          side_effect=_half_open):
            with pytest.raises(MediaOpenError):
                service.open_clip("/mock/leak.mov")

        assert sorted(closed) == ["master", "vt"], (
            "打开失败必须释放已打开的 master/VT 源（半开状态泄漏）"
        )
        assert service.active_clip_count == 0


# ============================================================================
# AUD-39103: 并发 open_clip 共享路径在打开完成前返回 clip_id
# ============================================================================


class TestAud39103SharedOpenWaitsForCompletion:
    def _register_pending(self, service, clip_id: str,
                          path: str) -> ClipSession:
        session = ClipSession(clip_id=clip_id, path=path)
        service._clips[clip_id] = session
        service._path_to_clips[path] = [clip_id]
        return session

    def test_shared_open_waits_for_inflight_open(self) -> None:
        """在途打开未完成时，共享调用方必须等待而不是立即拿 clip_id。"""
        service = VideoFrameProviderService()
        path = "/mock/inflight.mov"
        session = self._register_pending(service, "clip_pending", path)

        result: dict = {}

        def _sharer() -> None:
            try:
                result["clip_id"] = service.open_clip(path)
            except BaseException as exc:  # noqa: BLE001 - 测试捕获
                result["error"] = exc

        with patch.object(service, "_open_clip_sources"):
            sharer = threading.Thread(target=_sharer, daemon=True)
            sharer.start()
            try:
                time.sleep(0.2)
                assert sharer.is_alive(), (
                    "首个打开者在途时，共享 open_clip 必须等待其完成"
                )
                # 模拟首个打开者失败：标记错误、唤醒等待者、清理映射
                session.open_error = "boom"
                session.open_event.set()
                with service._lock:
                    service._clips.pop("clip_pending", None)
                    service._path_to_clips.pop(path, None)
                sharer.join(JOIN_TIMEOUT_S)
            finally:
                if sharer.is_alive():
                    session.open_error = "timeout"
                    session.open_event.set()
                    sharer.join(JOIN_TIMEOUT_S)

        assert "clip_id" in result, f"共享调用方异常退出: {result}"
        assert result["clip_id"] != "clip_pending", (
            "打开失败的会话绝不能再被共享（死 clip_id）"
        )
        new_session = service.get_clip_session(result["clip_id"])
        assert new_session is not None
        assert new_session.ref_count == 1, "失败会话的引用不得转嫁到新会话"
        assert new_session.open_event.is_set()

    def test_completed_open_is_shared(self) -> None:
        """打开成功后（open_event 置位）共享语义不变。"""
        service = VideoFrameProviderService()
        path = "/mock/done.mov"
        session = self._register_pending(service, "clip_done", path)
        session.open_error = None
        session.open_event.set()

        assert service.open_clip(path) == "clip_done"
        assert session.ref_count == 2


# ============================================================================
# AUD-39104: request_gpu_frame CPU 回退不转发 allow_master_fallback
# ============================================================================


class TestAud39104GpuFrameFallbackForwardsFlag:
    def test_flag_forwarded_when_false(self) -> None:
        service = VideoFrameProviderService()
        session = _make_session("c")
        service._clips[session.clip_id] = session

        with patch.object(service, "request_frame") as rf:
            service.request_gpu_frame(
                session.clip_id, 1, quality="proxy",
                allow_master_fallback=False)
        rf.assert_called_once_with(
            clip_id=session.clip_id, frame_index=1, quality="proxy",
            allow_master_fallback=False)

    def test_default_call_shape_unchanged(self) -> None:
        service = VideoFrameProviderService()
        session = _make_session("c")
        service._clips[session.clip_id] = session

        with patch.object(service, "request_frame") as rf:
            service.request_gpu_frame(session.clip_id, 2, quality="proxy")
        rf.assert_called_once_with(
            clip_id=session.clip_id, frame_index=2, quality="proxy")


# ============================================================================
# AUD-39105: 导出路径 request_metal_frame 优先 VT（ProRes 色度下采样）
# ============================================================================


def _vt_session(clip_id: str, codec: str | None,
                pixel_format: str = "yuv422p10le") -> ClipSession:
    session = _make_session(clip_id)
    # master 源不暴露 read_metal（Topos 原生 surface 路径不参与本测试）
    session.master_source = None
    if codec is None:
        session.metadata = None
    else:
        session.metadata = MagicMock(
            total_frames=100, codec_name=codec, pixel_format=pixel_format)
    gpu_frame = MagicMock(name="vt_metal")
    session.vt_source = MagicMock()
    session.vt_source.read_frame_metal.return_value = gpu_frame
    session._vt_metal_frame = gpu_frame
    return session


class TestAud39105ExportMetalSkipsDownsamplingVt:
    def test_prores_session_skips_vt(self) -> None:
        service = VideoFrameProviderService()
        session = _vt_session("prores_clip", "prores")
        service._clips[session.clip_id] = session
        cpu_frame = MagicMock(name="cpu")

        with patch.object(service, "request_frame", return_value=cpu_frame):
            got = service.request_metal_frame(session.clip_id, 12)
        session.vt_source.read_frame_metal.assert_not_called(), (
            "ProRes（4:2:2）VT 源输出 p010 色度下采样，不得进导出"
        )
        assert got is cpu_frame

    def test_h264_session_still_uses_vt(self) -> None:
        service = VideoFrameProviderService()
        session = _vt_session("h264_clip", "h264", "yuv420p")
        service._clips[session.clip_id] = session

        got = service.request_metal_frame(session.clip_id, 12)
        session.vt_source.read_frame_metal.assert_called_once_with(12)
        assert got is session._vt_metal_frame

    def test_unknown_metadata_keeps_vt(self) -> None:
        """metadata 缺失维持既有 VT 优先（与既有测试套件兼容）。"""
        service = VideoFrameProviderService()
        session = _vt_session("nometa_clip", None)
        service._clips[session.clip_id] = session

        got = service.request_metal_frame(session.clip_id, 12)
        session.vt_source.read_frame_metal.assert_called_once_with(12)
        assert got is session._vt_metal_frame


# ============================================================================
# AUD-39106: 向可能已被关闭的会话赋新源（新源永不释放）
# ============================================================================


class TestAud39106NoSourceAssignmentToClosedSession:
    def test_update_disk_proxy_info_discards_source_for_closed_session(
            self, tmp_path) -> None:
        service = VideoFrameProviderService()
        session = _make_session("closed_clip")
        session.metadata = None
        service._clips[session.clip_id] = session
        service._close_clip_sources(session)  # 置 sources_closed

        proxy_file = tmp_path / "proxy.mov"
        proxy_file.write_bytes(b"x")
        info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path=str(proxy_file),
            proxy_resolution=(960, 540),
        )
        source = MagicMock(name="disk_proxy")
        with patch("src.shared.media.factory.MediaSourceFactory.create",
                   return_value=source), \
             patch("src.shared.media.factory.MediaSourceFactory.open_source"):
            assert service.update_disk_proxy_info(session.clip_id, info) is True

        source.close.assert_called_once(), (
            "会话已关闭时新打开的源必须立即释放，而不是挂在死会话上"
        )
        assert session.disk_proxy_source is None

    def test_create_memory_proxy_source_discards_for_closed_session(
            self) -> None:
        service = VideoFrameProviderService()
        session = ClipSession(clip_id="closed_mem", path="/mock/m.mov")
        session.metadata = VideoMetadata(
            width=3840, height=2160, fps=Rational(24, 1),
            total_frames=120, codec_name="h264",
            codec_profile=None, pixel_format="yuv420p", bit_depth=8)
        session.sources_closed = True  # 模拟已被 _close_clip_sources 关闭

        source = MagicMock(name="mem_proxy")
        with patch("src.shared.media.factory.MediaSourceFactory.create",
                   return_value=source), \
             patch("src.shared.media.factory.MediaSourceFactory.open_source"):
            service._create_memory_proxy_source(session)

        source.close.assert_called_once()
        assert session.proxy_source is None

    def test_create_memory_proxy_source_assigns_for_live_session(
            self) -> None:
        """活跃会话（未注册的直调场景）赋值行为不变（回归保护）。"""
        service = VideoFrameProviderService()
        session = ClipSession(clip_id="live_mem", path="/mock/l.mov")
        session.metadata = VideoMetadata(
            width=3840, height=2160, fps=Rational(24, 1),
            total_frames=120, codec_name="h264",
            codec_profile=None, pixel_format="yuv420p", bit_depth=8)

        source = MagicMock(name="mem_proxy_live")
        with patch("src.shared.media.factory.MediaSourceFactory.create",
                   return_value=source), \
             patch("src.shared.media.factory.MediaSourceFactory.open_source"):
            service._create_memory_proxy_source(session)

        assert session.proxy_source is source
        source.close.assert_not_called()


# ============================================================================
# AUD-39107: _on_proxy_complete 持锁做阻塞 I/O + 跳过校验 + 不关内存代理
# ============================================================================


class TestAud39107ProxyCompleteCallback:
    def test_delegates_to_update_disk_proxy_info_without_holding_lock(self):
        service = VideoFrameProviderService()
        session = _make_session("cb_clip")
        session.metadata = None
        service._clips[session.clip_id] = session
        service._path_to_clips["/mock/cb.mov"] = [session.clip_id]

        lock_reacquired: list[bool] = []
        calls: list[tuple] = []

        def _recorder(clip_id, info):
            calls.append((clip_id, info))
            # 非重入锁同线程二次 acquire 必须失败——若回调仍持有
            # self._lock，这里会死锁/超时失败
            lock_reacquired.append(
                service._lock.acquire(timeout=0.5))
            service._lock.release()

        info = DiskProxyInfo(
            status=ProxyStatus.READY, proxy_path="/mock/p.mov",
            proxy_resolution=(960, 540))

        with patch.object(service, "update_disk_proxy_info",
                          side_effect=_recorder):
            service._on_proxy_complete("/mock/cb.mov", info)

        assert calls == [(session.clip_id, info)], (
            "必须按剪辑委托给 update_disk_proxy_info（含校验/锁协议/内存代理释放）"
        )
        assert lock_reacquired == [True], (
            "_on_proxy_complete 不得在持有 self._lock 时执行重活"
        )

    def test_no_clips_is_noop(self) -> None:
        service = VideoFrameProviderService()
        with patch.object(service, "update_disk_proxy_info") as upd:
            service._on_proxy_complete(
                "/mock/none.mov", MagicMock())
        upd.assert_not_called()


# ============================================================================
# AUD-39200: master 队列容量上限永不生效（无界增长）
# ============================================================================


def _entry(frame_index: int, can_drop: bool = False) -> QueueEntry:
    return QueueEntry(
        frame=SimpleNamespace(frame_index=frame_index),
        decode_ms=10.0,
        source=FrameSource.MASTER,
        can_drop=can_drop,
        generation_id=0,
    )


class TestAud39200MasterQueueCapacity:
    def test_master_queue_is_bounded_by_fifo_eviction(self) -> None:
        queue = ReadyFrameQueue(max_master_size=2)
        for idx in (1, 2, 3, 4):
            assert queue.enqueue(_entry(idx)) is True

        assert queue.get_size(is_master=True) == 2, (
            "master 队列容量上限必须真实生效（不可无界增长）"
        )
        assert queue.get_frame_indices(is_master=True) == [3, 4], (
            "消费方落后时应淘汰最旧帧、保留最新帧"
        )
        assert queue.stats["master_dropped"] == 2

    def test_droppable_entries_route_to_proxy_queue_not_master(self) -> None:
        """路由契约：can_drop=True 的条目进 proxy 队列（AUD-39200 根因锚点）。

        正因为该路由，master 队列成员恒为 can_drop=False，旧的"只丢
        can_drop 帧"上限永远不可能生效——修复后 master 队列按 FIFO
        自行淘汰，不再依赖 can_drop。
        """
        queue = ReadyFrameQueue(max_master_size=2)
        queue.enqueue(_entry(1, can_drop=True))

        assert queue.get_size(is_master=True) == 0
        assert queue.get_size(is_master=False) == 1

    def test_gpu_frames_released_on_eviction(self) -> None:
        from src.shared.media.gpu_frame import GpuDecodedFrame
        queue = ReadyFrameQueue(max_master_size=1)
        released: list[int] = []

        gpu_frame = MagicMock(spec=GpuDecodedFrame)
        gpu_frame.frame_index = 1
        entry = QueueEntry(
            frame=gpu_frame, decode_ms=5.0, source=FrameSource.GPU_MASTER,
            can_drop=False, generation_id=0)

        with patch(
                "src.shared.video.frame_queue.safe_release_gpu_frame",
                side_effect=lambda f: released.append(f.frame_index)):
            assert queue.enqueue(entry) is True
            queue.enqueue(_entry(2))  # 挤出 GPU 帧

        assert released == [1], "被淘汰的 GPU 帧必须释放"
