"""EF-17 批次审计修复验证（src/features/color/ui 10 条 + src/shared/gpu 10 条）。

每条 AUD ID 至少一个正例 + 一个回退敏感断言：把对应修复还原后测试必须失败。

- AUD-1209 delete_fence 吞错        → 失败留痕（fake OpenGL 注入）
- AUD-1217 gl_pass_timer docstring  → last_ms("composite")
- AUD-1218 query __enter__ 抛出泄漏 → 探测/测量两条路径都释放
- AUD-1219 query 无池化             → 第 4 帧复用，不再新建
- AUD-1403 _last_gpu_time_ms 竞态   → submit/读侧全部持 _lock
- AUD-1416 smeta 布局 docstring 矛盾 → 12×u32 → 13×u32
- AUD-1418 histogram_cdf 双重扫描   → 合并为单次循环（语义对拍）
- AUD-1523 _completed/_errors 无界  → 有界 + 溢出告警
- AUD-1606 _reuse_violations 空转   → register_slot 顶替/上报真实递增
- AUD-1610 acquire 失败日志急切求值 → debug 关闭时不求值
- AUD-25100 包 docstring 缺 ColorMappingPanel
- AUD-25213 set_brush_params 信号回声/无防递归锁
- AUD-25301 轮询定时器无条件常驻
- AUD-25316 set_params 静默丢非法值/子控件未锁
- AUD-25403 set_params 防递归锁挂在面板自身
- AUD-25413 paintGL 死代码父级遍历
- AUD-25418 _on_hsl_curve_changed 死槽
- AUD-25510 右键菜单每次重扫插件目录
- AUD-25517 扫描冗余缩略图解码 / copy2 无保护
- AUD-25809 set_tracking_binding 重复排序

GPU 测试不创建真实 GL/Metal 上下文（fake/stub 注入）；UI 控件用 qapp，
不弹模态对话框（QFileDialog/QMessageBox 均被替身）。
"""
from __future__ import annotations

import importlib
import inspect
import logging
import sys
import threading
import types
from types import SimpleNamespace

import numpy as np
import pytest

pytestmark = pytest.mark.usefixtures("qapp")

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QHideEvent, QShowEvent


# ===========================================================================
# src/shared/gpu
# ===========================================================================


class _FakeOpenGLHarness:
    """往 sys.modules 注入假 OpenGL 绑定（不依赖真实 PyOpenGL/GL 上下文）。"""

    def __init__(self) -> None:
        class Error(Exception):
            pass

        self.Error = Error
        self.delete_calls: list = []
        self.delete_error: Exception | None = None

        opengl = types.ModuleType("OpenGL")
        gl = types.ModuleType("OpenGL.GL")
        err = types.ModuleType("OpenGL.error")
        err.Error = Error

        def _glDeleteSync(sync) -> None:
            self.delete_calls.append(sync)
            if self.delete_error is not None:
                raise self.delete_error

        gl.glDeleteSync = _glDeleteSync
        opengl.GL = gl
        self.modules = {
            "OpenGL": opengl,
            "OpenGL.GL": gl,
            "OpenGL.error": err,
        }

    def install(self, monkeypatch) -> None:
        for name, module in self.modules.items():
            monkeypatch.setitem(sys.modules, name, module)


class TestAud1209DeleteFenceDiagnostics:
    """AUD-1209: delete_fence 吞掉 glDeleteSync 失败，GLsync 泄漏无诊断。"""

    def test_failure_is_logged_not_swallowed(self, monkeypatch, caplog) -> None:
        harness = _FakeOpenGLHarness()
        harness.install(monkeypatch)
        harness.delete_error = harness.Error("glDeleteSync failed")
        from src.shared.gpu.composite_gpu_frame_handle import delete_fence

        with caplog.at_level(logging.WARNING,
                             logger="src.shared.gpu.composite_gpu_frame_handle"):
            delete_fence(0xDEADBEEF)  # 不得向调用方抛出

        assert len(harness.delete_calls) == 1  # 确实尝试了删除
        # 回退（except: pass）时无任何日志 → 失败
        assert any(
            "delete_fence" in r.getMessage() and "deadbeef" in r.getMessage()
            for r in caplog.records
        ), "glDeleteSync 失败未留诊断日志"

    def test_success_does_not_log_warning(self, monkeypatch, caplog) -> None:
        harness = _FakeOpenGLHarness()
        harness.install(monkeypatch)
        from src.shared.gpu.composite_gpu_frame_handle import delete_fence

        with caplog.at_level(logging.WARNING,
                             logger="src.shared.gpu.composite_gpu_frame_handle"):
            delete_fence(1234)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_zero_fence_is_noop(self) -> None:
        from src.shared.gpu.composite_gpu_frame_handle import delete_fence
        delete_fence(0)  # 无绑定注入也必须安全返回


class TestAud1217PassTimerDocstring:
    """AUD-1217: 模块 docstring 引用不存在的 last_composite_ms API。"""

    def test_docstring_matches_real_api(self) -> None:
        mod = importlib.import_module("src.shared.gpu.gl_pass_timer")
        doc = mod.__doc__ or ""
        # 回退（仍写 last_composite_ms）→ 失败
        assert "last_composite_ms" not in doc
        assert 'last_ms("composite")' in doc


class _FakeQuery:
    def __init__(self, fail_enter: bool = False) -> None:
        self.fail_enter = fail_enter
        self.exited = 0
        self.released = False

    def __enter__(self) -> "_FakeQuery":
        if self.fail_enter:
            raise RuntimeError("enter failed")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.exited += 1
        return False

    def release(self) -> None:
        self.released = True

    @property
    def elapsed(self):
        return 1_000_000 if self.exited else None  # 1ms in ns


class _FakeCtx:
    def __init__(self) -> None:
        self.created: list = []
        self.fail_next_enter = False

    def query(self, time: bool = False) -> _FakeQuery:
        fail = self.fail_next_enter
        self.fail_next_enter = False
        q = _FakeQuery(fail_enter=fail)
        self.created.append(q)
        return q


class TestAud1218QueryReleaseOnEnterFailure:
    """AUD-1218: query __enter__ 抛出时对象不释放（探测 + 测量两条路径）。"""

    def test_probe_enter_failure_releases_query(self) -> None:
        from src.shared.gpu.gl_pass_timer import GlPassTimer

        ctx = _FakeCtx()
        ctx.fail_next_enter = True
        timer = GlPassTimer(ctx)
        assert timer.unsupported
        # 回退（探测路径无 finally）时 created[0] 不会被释放 → 失败
        assert ctx.created[0].released, "探测 query 在 __enter__ 失败后未释放"

    def test_measure_enter_failure_releases_query(self) -> None:
        from src.shared.gpu.gl_pass_timer import GlPassTimer

        ctx = _FakeCtx()
        timer = GlPassTimer(ctx)  # 探测成功
        assert not timer.unsupported
        ctx.fail_next_enter = True
        measurement = timer.measure("composite")
        measurement.__enter__()
        assert measurement._query is None
        # 回退（测量路径直接 except 吞掉）时最后一个 query 未释放 → 失败
        assert ctx.created[-1].released, "measure query 在 __enter__ 失败后未释放"


class TestAud1219QueryPooling:
    """AUD-1219: 每个 pass 每帧新建 query，无池化。"""

    def test_queries_reused_across_frames(self) -> None:
        from src.shared.gpu.gl_pass_timer import GlPassTimer

        ctx = _FakeCtx()
        timer = GlPassTimer(ctx)  # probe → created[0]
        for _ in range(3):
            timer.begin_frame()
            with timer.measure("composite"):
                pass
            timer.collect()
        # probe + 3 帧（RING_DEPTH=2 帧延迟，第 3 帧 collect 才回收 q1）
        assert len(ctx.created) == 4
        # 第 4 帧必须复用池中 query，不再新建（回退无池化时 == 5 → 失败）
        timer.begin_frame()
        with timer.measure("composite"):
            pass
        timer.collect()
        assert len(ctx.created) == 4, "collect 读回后的 query 未回池复用"
        # 池中的 query 未被销毁
        assert not ctx.created[1].released
        # release() 必须连带释放池中缓存的 query
        timer.release()
        assert all(q.released for q in ctx.created)


class TestAud1403GpuTimeDiagLocking:
    """AUD-1403: _last_gpu_time_ms 在 submit 锁外读写，并发提交数据竞态。"""

    def test_submit_and_readers_hold_lock(self) -> None:
        from src.shared.gpu.metal.context import MetalComputeContext

        submit_src = inspect.getsource(MetalComputeContext.submit)
        # 1 处取 cb + 2 处保护 _last_gpu_time_ms（置 None / 写测量值）
        # 回退（只有取 cb 一处锁）时 count == 1 → 失败
        assert submit_src.count("with self._lock:") >= 3
        reader = inspect.getsource(MetalComputeContext.last_gpu_time_ms.fget)
        assert "with self._lock:" in reader
        stats_src = inspect.getsource(MetalComputeContext.get_stats)
        assert "with self._lock:" in stats_src

    def test_diagnostics_readable_on_bare_instance(self) -> None:
        """不创建 Metal 设备也能安全读诊断（锁路径不崩）。"""
        from src.shared.gpu.metal.context import MetalComputeContext

        ctx = MetalComputeContext.__new__(MetalComputeContext)
        ctx._lock = threading.Lock()
        ctx._last_gpu_time_ms = None
        ctx._kernels = {}
        ctx._device = SimpleNamespace(name=lambda: "Fake GPU")
        assert ctx.last_gpu_time_ms is None
        with ctx._lock:
            ctx._last_gpu_time_ms = 2.5
        assert ctx.last_gpu_time_ms == pytest.approx(2.5)
        stats = ctx.get_stats()
        assert stats["last_gpu_time_ms"] == pytest.approx(2.5)


class TestAud1416SmetaLayoutDoc:
    """AUD-1416: 模块 docstring 内 smeta 布局 12×u32 与 13×u32 自相矛盾。"""

    def test_docstring_agrees_with_kernel_stride(self) -> None:
        mod = importlib.import_module("src.shared.gpu.metal.kernels_dec")
        doc = mod.__doc__ or ""
        # 回退（12×u32 仍在）→ 失败
        assert "12×u32" not in doc
        assert "13×u32" in doc
        # 与 kernel 实际步长一致（防 docstring 改错方向）
        assert "sid * 13u" in mod.METAL_RANS2_DECODE_MSL


class TestAud1418HistogramCdfSinglePass:
    """AUD-1418: histogram_cdf 每线程重复线性扫描全通道 256 桶两次。"""

    def test_kernel_has_single_scan_loop(self) -> None:
        mod = importlib.import_module("src.shared.gpu.metal.kernels_histogram")
        body = mod.METAL_HISTOGRAM_MSL.split("kernel void histogram_cdf")[1]
        # 回退（total 扫描 + cum 扫描两个 for）时 count == 2 → 失败
        assert body.count("for (") == 1, "histogram_cdf 仍是两次线性扫描"

    def test_merged_scan_semantics_match_reference(self) -> None:
        """合并扫描的数学语义与 cdf_reference 逐值一致（HDR 值域不变）。"""
        mod = importlib.import_module("src.shared.gpu.metal.kernels_histogram")
        rng = np.random.default_rng(42)
        hist = rng.integers(0, 5, size=1024).astype(np.uint32)
        reference = mod.cdf_reference(hist, 0xF)
        for ch in range(4):
            for bin_ in (0, 37, 128, 255):
                channel_total = 0
                cum = 0
                for i in range(256):
                    count = int(hist[ch * 256 + i])
                    channel_total += count
                    if i <= bin_:
                        cum += count
                assert cum / channel_total == pytest.approx(
                    float(reference[ch][bin_]), abs=1e-6)


class _FakeCommandBuffer:
    def __init__(self, error=None) -> None:
        self._error = error

    def error(self):
        return self._error

    def waitUntilCompleted(self) -> None:
        pass


class _SyncSubmitContext:
    """submit() 同步触发 completion 的假 Metal 上下文。"""

    def __init__(self, error=None) -> None:
        self._error = error

    def submit(self, completion=None, wait=False):
        cb = _FakeCommandBuffer(self._error)
        if completion is not None:
            completion(cb)
        return cb


class TestAud1523SchedulerBoundedDiagnostics:
    """AUD-1523: _completed/_errors 无界增长，依赖调用方自觉 poll。"""

    def test_completed_buffer_is_bounded_with_warning(self, caplog) -> None:
        from src.shared.gpu.metal.submission_scheduler import (
            MetalSubmissionScheduler,
        )

        sched = MetalSubmissionScheduler(_SyncSubmitContext(), max_in_flight=1)
        sched._completed_limit = 4
        with caplog.at_level(logging.WARNING,
                             logger="src.shared.gpu.metal.submission_scheduler"):
            for _ in range(10):
                assert sched.submit() is not None
        completed = sched.poll()
        # 回退（无界 list）时 len == 10 → 失败
        assert len(completed) == 4, "_completed 未按上限截断"
        assert completed[-1] == 9  # 保留最新
        assert sched.poll() == ()  # poll 消费后清空
        assert any("poll()" in r.getMessage() for r in caplog.records), \
            "溢出未告警"

    def test_error_map_is_bounded(self) -> None:
        from src.shared.gpu.metal.submission_scheduler import (
            MetalSubmissionScheduler,
        )

        sched = MetalSubmissionScheduler(
            _SyncSubmitContext(error="boom"), max_in_flight=1)
        sched._errors_limit = 3
        for _ in range(6):
            assert sched.submit() is not None
        # 回退（无界 dict）时 len == 6 → 失败
        assert len(sched._errors) <= 3
        assert sched.error(5) == "boom"  # 最新错误必须保留


class TestAud1606ReuseViolationDetection:
    """AUD-1606: _reuse_violations 无递增路径，验收断言恒通过。"""

    def test_reregistering_unreleased_slot_counts_violation(self) -> None:
        from src.shared.gpu.present_frame import OutputSlotPool

        pool = OutputSlotPool()
        pool.register_slot(0, texture_glo=101, width=8, height=8)
        lease = pool.acquire_for_writing(generation_id=1, frame_index=0)
        lease.mark_ready(fence_ns=0)  # READY（未 ack）
        pool.register_slot(0, texture_glo=102, width=8, height=8)  # 顶替
        # 回退（计数器无递增路径）时 == 0 → 失败
        assert pool.stats()["reuse_violations"] == 1
        with pytest.raises(AssertionError):
            pool.assert_no_reuse_violations()

    def test_clean_pool_and_report_api(self) -> None:
        from src.shared.gpu.present_frame import OutputSlotPool

        pool = OutputSlotPool()
        pool.register_slot(0, texture_glo=101, width=8, height=8)
        pool.register_slot(0, texture_glo=101, width=8, height=8)  # FREE 顶替非违规
        pool.assert_no_reuse_violations()  # 干净池必须通过（正例）
        assert pool.stats()["reuse_violations"] == 0
        pool.report_reuse_violation("lease begin_writing rejected (READY)")
        assert pool.stats()["reuse_violations"] == 1
        with pytest.raises(AssertionError):
            pool.assert_no_reuse_violations()


class TestAud1610LazyAcquireFailureLog:
    """AUD-1610: acquire 失败热路径日志 f-string 急切求值。"""

    def test_no_state_summary_when_debug_disabled(self, monkeypatch, caplog) -> None:
        from src.shared.gpu.present_frame import OutputSlotPool

        pool = OutputSlotPool()
        pool.register_slot(0, texture_glo=101, width=8, height=8)
        lease = pool.acquire_for_writing(generation_id=1, frame_index=0)
        lease.mark_ready(fence_ns=0)  # 占住唯一 slot

        calls: list = []
        monkeypatch.setattr(pool, "_state_summary",
                            lambda: calls.append(1) or {})
        # debug 关闭（WARNING 级）
        caplog.set_level(logging.WARNING,
                         logger="src.shared.gpu.present_frame")
        assert pool.acquire_for_writing(generation_id=1, frame_index=1) is None
        # 回退（f-string 急切求值 _state_summary）时 calls 非空 → 失败
        assert calls == [], "debug 未启用时仍急切求值 _state_summary()"
        # stats() 自身合法地调用 _state_summary —— 放在回退断言之后
        assert pool.stats()["acquire_failures"] == 1


# ===========================================================================
# src/features/color/ui
# ===========================================================================


class TestAud25100PackageDocstring:
    """AUD-25100: 模块 docstring 未列出导出的 ColorMappingPanel。"""

    def test_docstring_lists_color_mapping_panel(self) -> None:
        color_ui = importlib.import_module("src.features.color.ui")
        doc = color_ui.__doc__ or ""
        # 回退（清单无 ColorMappingPanel）→ 失败
        assert "ColorMappingPanel" in doc
        assert "ColorMappingPanel" in color_ui.__all__


def _class_defining(module_name: str, method_name: str):
    mod = importlib.import_module(module_name)
    owners = [
        obj for obj in vars(mod).values()
        if isinstance(obj, type)
        and getattr(obj, "__module__", None) == module_name
        and method_name in vars(obj)
    ]
    assert owners, f"{module_name} 中没有类定义 {method_name}"
    return owners[0]


def _make_canvas_dummy(syncing: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        _brush_params={"size": 50, "feather": 50, "opacity": 1.0, "flow": 1.0},
        _brush_params_syncing=syncing,
        brush_cursor=SimpleNamespace(set_params=lambda *a: None),
        brush_stroke_preview=SimpleNamespace(set_show_overlay=lambda v: None),
        scene=SimpleNamespace(update=lambda: None),
        brush_params_updated=SimpleNamespace(emit=lambda p: None),
    )


class TestAud25213BrushParamsEcho:
    """AUD-25213: set_brush_params 无条件回发信号回声且自身无状态锁。"""

    def test_external_update_does_not_echo(self) -> None:
        cls = _class_defining("src.features.color.ui.canvas", "set_brush_params")
        emitted: list = []
        dummy = _make_canvas_dummy()
        dummy.brush_params_updated = SimpleNamespace(emit=emitted.append)
        cls.set_brush_params(dummy, {"size": 30, "show_overlay": False})
        assert dummy._brush_params["size"] == 30  # 参数仍然生效（正例）
        # 回退（无条件 emit）时非空 → 失败
        assert emitted == [], "外部驱动的更新仍回发 brush_params_updated"

    def test_recursion_guard_blocks_reentry(self) -> None:
        cls = _class_defining("src.features.color.ui.canvas", "set_brush_params")
        applied: list = []
        dummy = _make_canvas_dummy(syncing=True)

        def _record(*args) -> None:
            applied.append(args)

        dummy.brush_cursor = SimpleNamespace(set_params=_record)
        cls.set_brush_params(dummy, {"size": 10})
        # 回退（无状态锁）时参数被再次应用 → 失败
        assert dummy._brush_params["size"] == 50
        assert applied == []


class TestAud25301PollTimerGating:
    """AUD-25301: 500ms 轮询定时器无条件常驻。"""

    def test_timer_only_runs_with_provider_and_visible(self, qapp, monkeypatch) -> None:
        from src.features.color.ui.color_status_bar import ColorMonitorStatusBar

        bar = ColorMonitorStatusBar()
        try:
            # 回退（构造即 start）时 isActive() 为 True → 失败
            assert not bar._poll_timer.isActive()
            bar.set_denoise_quality_provider(lambda: "preview")
            # 不可见（测试构造、未 show）→ 不轮询（回退无可见性闸门 → 失败）
            assert not bar._poll_timer.isActive()
            monkeypatch.setattr(bar, "isVisible", lambda: True, raising=False)
            bar.set_denoise_quality_provider(lambda: "preview")
            assert bar._poll_timer.isActive()  # 正例：provider + 可见 → 轮询
            bar.set_denoise_quality_provider(None)
            # 回退（provider=None 不停表）时仍 active → 失败
            assert not bar._poll_timer.isActive()
        finally:
            bar._poll_timer.stop()
            bar.deleteLater()

    def test_show_hide_events_toggle_polling(self, qapp, monkeypatch) -> None:
        from src.features.color.ui.color_status_bar import ColorMonitorStatusBar

        bar = ColorMonitorStatusBar()
        try:
            monkeypatch.setattr(bar, "isVisible", lambda: True, raising=False)
            bar.set_denoise_quality_provider(lambda: "preview")
            assert bar._poll_timer.isActive()
            monkeypatch.setattr(bar, "isVisible", lambda: False, raising=False)
            bar.hideEvent(QHideEvent())
            # 回退（无 hideEvent 暂停）时仍 active → 失败
            assert not bar._poll_timer.isActive()
            bar.showEvent(QShowEvent())  # 不可见 → 保持停止
            assert not bar._poll_timer.isActive()
            monkeypatch.setattr(bar, "isVisible", lambda: True, raising=False)
            bar.showEvent(QShowEvent())
            assert bar._poll_timer.isActive()  # 可见恢复 → 重新轮询
        finally:
            bar._poll_timer.stop()
            bar.deleteLater()


class TestAud25316DeblurSetParams:
    """AUD-25316: set_params 静默忽略非法持久化值；子控件未锁。"""

    def test_invalid_value_falls_back_with_log(self, qapp, caplog) -> None:
        from src.features.color.ui.deblur_panel import DeblurPanel

        panel = DeblurPanel()
        try:
            with caplog.at_level(logging.WARNING,
                                 logger="src.features.color.ui.deblur_panel"):
                panel.set_params({
                    "blend": 0.5, "use_tiling": True,
                    "tile_size": 768,  # 不在候选 [256,512,1024,2048] 内
                    "overlap": 64,
                })
            assert panel.blend_control.value() == pytest.approx(0.5)
            assert panel.tile_size_control['combo'].currentText() == "512"
            assert panel.overlap_control['combo'].currentText() == "64"
            # 回退（setCurrentText 静默忽略）时无告警日志 → 失败
            assert any("tile_size" in r.getMessage() for r in caplog.records), \
                "非法持久化值未留告警"
        finally:
            panel.deleteLater()

    def test_valid_values_apply(self, qapp) -> None:
        from src.features.color.ui.deblur_panel import DeblurPanel

        panel = DeblurPanel()
        try:
            panel.set_params({"tile_size": 1024, "overlap": 32})
            assert panel.tile_size_control['combo'].currentText() == "1024"
            assert panel.overlap_control['combo'].currentText() == "32"
        finally:
            panel.deleteLater()

    def test_child_combo_signals_blocked_during_restore(self, qapp) -> None:
        from src.features.color.ui.deblur_panel import DeblurPanel

        panel = DeblurPanel()
        try:
            hits: list = []
            combo = panel.auto_tiling_combo['combo']
            combo.currentIndexChanged.connect(lambda i: hits.append(i))
            panel.set_params({"use_tiling": False})
            assert combo.currentIndex() == 1
            # 回退（只 blockSignals(self)，子控件照常发射）时 hits == [1] → 失败
            assert hits == [], "恢复期间子控件信号未被抑制"
        finally:
            panel.deleteLater()


class TestAud25403DenoiseSetParamsGuard:
    """AUD-25403: 恢复窗口的程序化回填不得触发参数发射（防递归/风暴）。

    2026-11 重设计后降噪面板仅剩视频降噪，恢复入口为
    ``set_video_params_dict``；防递归锁即恢复期的 ``_video_updating``
    （旧 ``set_params``/``mode_combo`` 随经典/AI UI 移除）。
    """

    def test_restore_suppresses_video_param_emission(self, qapp) -> None:
        from src.features.color.ui.denoise_panel import DenoisePanel
        from src.shared.video.denoise.params import (
            TemporalDenoiseParams,
            VideoDenoiseParams,
        )

        panel = DenoisePanel()
        try:
            emitted: list = []
            panel.video_denoise_changed.connect(lambda d: emitted.append(d))
            panel.set_video_params_dict(VideoDenoiseParams(
                temporal=TemporalDenoiseParams(enabled=True, frames=3,
                                               luma=60.0),
            ).to_dict())
            # 回退（恢复期不设 _video_updating）时 Frames/Luma 回填会触发
            # _emit_video_params → emitted 非空 → 失败
            assert emitted == [], "恢复期间视频参数被发射"
            assert panel._video_updating is False  # 状态锁必须复位
            # 恢复值真实落位 + 灰联动刷新（Frames=3 → 时域行可用）
            assert panel.temporal_frames['combo'].currentIndex() == 3
            assert panel.temporal_luma.value() == pytest.approx(60.0)
            assert panel.temporal_luma.isEnabled()
        finally:
            panel._video_param_timer.stop()
            panel.deleteLater()


class TestAud25413PaintGLDeadTraversal:
    """AUD-25413: paintGL 中父级遍历结果从未使用（每帧热路径死代码）。"""

    def test_paintgl_has_no_parent_traversal(self) -> None:
        cls = _class_defining("src.features.color.ui.gl_viewport", "paintGL")
        src = inspect.getsource(cls.paintGL)
        # 回退（遍历仍在）时包含 parent_canvas → 失败
        assert "parent_canvas" not in src
        assert ".clear(" in src  # 背景清除逻辑仍在


class TestAud25418DeadForwardSlot:
    """AUD-25418: _on_hsl_curve_changed 死代码 + 虚构转发信号。"""

    def test_dead_slot_removed(self, qapp) -> None:
        from src.features.color.ui.hsl_curves_panel import HSLCurvesPanel

        # 回退（空转发槽仍在）时 hasattr 为 True → 失败
        assert not hasattr(HSLCurvesPanel, "_on_hsl_curve_changed")
        assert "_on_hsl_curve_changed" not in inspect.getsource(
            HSLCurvesPanel.init_ui)
        panel = HSLCurvesPanel()
        try:
            # 正例：picker 转发链路保持完好
            assert hasattr(panel, "hsl_picker_toggled")
            assert panel.hsl_curves_widget is not None
        finally:
            panel.deleteLater()


class _FakeSignal:
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, *args) -> None:
        for slot in self._slots:
            slot(*args)


class _FakeMenu:
    instances: list = []

    def __init__(self, *args, **kwargs) -> None:
        self.items: list = []
        _FakeMenu.instances.append(self)

    def addMenu(self, *args, **kwargs) -> "_FakeMenu":
        sub = _FakeMenu()
        self.items.append(sub)
        return sub

    def addAction(self, *args, **kwargs):
        action = args[0]
        self.items.append(action)
        return action

    def addSeparator(self) -> None:
        return None

    def exec(self, *args, **kwargs) -> None:
        return None


class _FakeAction:
    def __init__(self, *args, **kwargs) -> None:
        self.text = args[0] if args else ""
        self.triggered = _FakeSignal()

    def setEnabled(self, enabled: bool) -> None:
        pass


class TestAud25510ContextMenuRescan:
    """AUD-25510: 右键菜单每次打开都在 UI 线程同步重扫插件目录。"""

    def _install_fake_menu(self, monkeypatch) -> None:
        monkeypatch.setattr(
            importlib.import_module("src.features.color.ui.plugin_panel"),
            "QMenu", _FakeMenu)
        monkeypatch.setattr(
            importlib.import_module("src.features.color.ui.plugin_panel"),
            "QAction", _FakeAction)
        _FakeMenu.instances.clear()

    def _make_panel_dummy(self) -> SimpleNamespace:
        return SimpleNamespace(
            mapToGlobal=lambda pos: pos,
            _reload_plugins=lambda: None,
        )

    def test_no_rescan_when_registry_populated(self, monkeypatch) -> None:
        pp = importlib.import_module("src.features.color.ui.plugin_panel")
        self._install_fake_menu(monkeypatch)
        calls: list = []
        fake = SimpleNamespace(
            get_all_plugins=lambda: {"p1": {"type": "dctl", "name": "P1"}},
            scan_plugins=lambda: calls.append("scan"),
            reload_plugins=lambda: calls.append("reload"),
        )
        monkeypatch.setattr(pp, "get_plugin_manager", lambda: fake)
        dummy = self._make_panel_dummy()
        pp.PluginPanel._show_context_menu(dummy, QPoint(1, 1))
        # 回退（无条件 scan_plugins）时 calls == ["scan"] → 失败
        assert calls == [], "注册表非空时右键仍触发重扫"
        # Reload 动作存在（显式重扫入口）
        reload_actions = [
            a for menu in _FakeMenu.instances for a in menu.items
            if isinstance(a, _FakeAction) and a.text == "Reload Effects"
        ]
        assert reload_actions, "菜单缺少显式 Reload Effects 动作"

    def test_lazy_scan_when_registry_empty(self, monkeypatch) -> None:
        pp = importlib.import_module("src.features.color.ui.plugin_panel")
        self._install_fake_menu(monkeypatch)
        calls: list = []
        fake = SimpleNamespace(
            get_all_plugins=lambda: {},
            scan_plugins=lambda: calls.append("scan"),
            reload_plugins=lambda: calls.append("reload"),
        )
        monkeypatch.setattr(pp, "get_plugin_manager", lambda: fake)
        dummy = self._make_panel_dummy()
        pp.PluginPanel._show_context_menu(dummy, QPoint(1, 1))
        assert calls == ["scan"]  # 仅注册表为空时惰性扫描一次

    def test_reload_slot_invokes_manager(self, monkeypatch) -> None:
        pp = importlib.import_module("src.features.color.ui.plugin_panel")
        calls: list = []
        fake = SimpleNamespace(
            get_all_plugins=lambda: {},
            scan_plugins=lambda: calls.append("scan"),
            reload_plugins=lambda: calls.append("reload"),
        )
        monkeypatch.setattr(pp, "get_plugin_manager", lambda: fake)
        pp.PluginPanel._reload_plugins(SimpleNamespace())
        assert calls == ["reload"]


class TestAud25517PresetScanAndExport:
    """AUD-25517: 扫描路径冗余缩略图解码；_export_thumbnail copy2 无保护。"""

    def test_scan_presets_has_no_eager_decode(self) -> None:
        from src.features.color.ui.presets_panel import PresetsPanel

        src = inspect.getsource(PresetsPanel.scan_presets)
        # 回退（扫描内 QPixmap(thumb_path) + item.setIcon 仍在）→ 失败
        assert "QPixmap(thumb_path)" not in src
        assert "item.setIcon" not in src

    def test_scan_still_populates_items(self, qapp, monkeypatch, tmp_path) -> None:
        pp = importlib.import_module("src.features.color.ui.presets_panel")
        from src.features.color.ui.presets_panel import PresetsPanel
        (tmp_path / "a.topos").write_bytes(b"x")
        (tmp_path / "a.thumb.jpg").write_bytes(b"jpg")
        decoded: list = []

        class _FakePixmap:
            def __init__(self, *args, **kwargs) -> None:
                decoded.append(args)

            def isNull(self) -> bool:
                return False

        monkeypatch.setattr(pp, "QPixmap", _FakePixmap)
        panel = PresetsPanel()
        try:
            panel._current_folder = str(tmp_path)
            panel.scan_presets()
            assert panel.list_widget.count() == 1  # 正例：条目照常入列
            # 回退（扫描路径解码 QPixmap）时非空 → 失败
            assert decoded == [], "scan_presets 仍在解码缩略图"
        finally:
            panel.deleteLater()

    @staticmethod
    def _patch_file_dialog(monkeypatch, result: tuple) -> None:
        """_export_thumbnail 内 `from PyQt6.QtWidgets import QFileDialog`
        按名读取模块属性——替换 QtWidgets 模块的 QFileDialog 即可，
        不弹真实模态对话框。"""

        class _FakeFileDialog:
            @staticmethod
            def getSaveFileName(*args, **kwargs) -> tuple:
                return result

        import PyQt6.QtWidgets as QtWidgets
        monkeypatch.setattr(QtWidgets, "QFileDialog", _FakeFileDialog)

    def test_export_thumbnail_survives_copy_error(self, qapp, monkeypatch,
                                                  tmp_path, caplog) -> None:
        pp = importlib.import_module("src.features.color.ui.presets_panel")
        from src.features.color.ui.presets_panel import PresetsPanel
        (tmp_path / "p.topos").write_bytes(b"x")
        (tmp_path / "p.thumb.jpg").write_bytes(b"jpg")
        self._patch_file_dialog(
            monkeypatch, (str(tmp_path / "out.jpg"), "JPEG"))

        def _boom(src, dst) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(pp.shutil, "copy2", _boom)
        warned: list = []

        class _FakeMessageBox:
            @staticmethod
            def warning(*args, **kwargs) -> None:
                warned.append(args)

        # QMessageBox 是 presets_panel 的模块级 import——须补丁面板模块
        # 命名空间（QtWidgets 包属性替换对已导入的引用无效）。
        monkeypatch.setattr(pp, "QMessageBox", _FakeMessageBox)
        with caplog.at_level(logging.WARNING,
                             logger="src.features.color.ui.presets_panel"):
            # 回退（copy2 无保护）时抛 OSError → 失败
            PresetsPanel._export_thumbnail(SimpleNamespace(),
                                           str(tmp_path / "p.topos"))
        assert warned, "copy2 失败未提示用户"
        assert any("缩略图" in r.getMessage() for r in caplog.records)

    def test_export_thumbnail_success_copies(self, qapp, monkeypatch,
                                             tmp_path) -> None:
        pp = importlib.import_module("src.features.color.ui.presets_panel")
        from src.features.color.ui.presets_panel import PresetsPanel
        (tmp_path / "p.topos").write_bytes(b"x")
        (tmp_path / "p.thumb.jpg").write_bytes(b"jpg")
        copied: list = []
        monkeypatch.setattr(pp.shutil, "copy2",
                            lambda src, dst: copied.append((src, dst)))
        self._patch_file_dialog(
            monkeypatch, (str(tmp_path / "out.jpg"), ""))
        PresetsPanel._export_thumbnail(SimpleNamespace(),
                                       str(tmp_path / "p.topos"))
        assert copied == [(str(tmp_path / "p.thumb.jpg"),
                           str(tmp_path / "out.jpg"))]


class TestAud25809TrackingBindingSort:
    """AUD-25809: set_tracking_binding 全量重建 + 三重排序。"""

    def test_single_sort_of_all_transforms(self) -> None:
        from src.features.color.ui.tracker_panel import TrackerPanel

        src = inspect.getsource(TrackerPanel.set_tracking_binding)
        # 回退（series 循环 + keyframes 各排一次）时 count == 2 → 失败
        assert src.count("sorted(all_transforms)") == 1

    def test_binding_output_unchanged(self, qapp) -> None:
        from src.features.color.ui.tracker_panel import TrackerPanel

        panel = TrackerPanel()
        try:
            def _tr(tx, ty, sx=1.0, sy=1.0, rot=0.0, status="ok", conf=1.0,
                    matrix=()) -> SimpleNamespace:
                return SimpleNamespace(
                    translation=(tx, ty), scale=(sx, sy), rotation=rot,
                    matrix=matrix, status=SimpleNamespace(value=status),
                    confidence=conf)

            binding = SimpleNamespace(
                shape_name="W1",
                transforms={10: _tr(1, 2, status="failed"),
                            5: _tr(0, 0)},
                manual_corrections={
                    7: _tr(3, 4, rot=90.0,
                           matrix=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 0.6, 1.0)),
                },
                reference_frame=5)
            series_rec: dict = {}
            markers_rec: dict = {}
            panel.graph.set_series = series_rec.update
            panel.graph.set_markers = (
                lambda k, m, f: markers_rec.update(
                    keyframes=k, manual=m, failed=f))
            panel.set_tracking_binding(binding)
            # 输出语义与修复前一致（正例）
            assert [f for f, _ in series_rec["tx"]] == [5, 7, 10]
            assert markers_rec["keyframes"] == [5, 7, 10]
            assert markers_rec["manual"] == [7]
            assert markers_rec["failed"] == [10]
        finally:
            panel.deleteLater()
