"""S01 示波器分段诊断测试 —— fake demand 验证各拒绝原因可识别。

计划 S01 验收：E09 提交/回传、E03 GPU_SCOPE 队列与执行、ScopePanel 接收、
GpuScopeWidget 呈现的丢弃不再静默——每类拒绝有稳定 (stage, reason) 计数
与上下文（revision/mode/tex_id）；诊断可关闭；不逐帧刷屏（计数器 +
每键一份最后上下文，verbose 日志按键去重）。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import src.features.color.core.scope_diagnostics as scope_diag_mod
from src.features.color.core.async_renderer import AsyncRenderer, RenderLane
from src.features.color.core.scope_diagnostics import ScopeDiagnostics
from src.features.color.core.scope_types import (
    DEFAULT_ANALYSIS_SIZE,
    ScopeDemand,
)
from tests.color.test_gpu_scope_pipeline import (
    _FakeRenderer,
    _GpuScopeGuardRenderer,
)


@pytest.fixture(autouse=True)
def _fresh_diag():
    """每个用例独立实例（接线点惰性取模块属性 → 替换即生效）。"""
    diag = ScopeDiagnostics(enabled=True)
    original = scope_diag_mod.scope_diagnostics
    scope_diag_mod.scope_diagnostics = diag
    try:
        yield diag
    finally:
        scope_diag_mod.scope_diagnostics = original


@pytest.fixture()
def _qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _demand(revision: int = 5, mode: str = "waveform") -> ScopeDemand:
    return ScopeDemand(revision=revision, source_revision=10, mode=mode,
                       display_size=(100, 100))


def _guard_renderer(**overrides):
    """带池/参数最小集的 guard fake（参数检查之前的依赖补齐）。"""
    r = _GpuScopeGuardRenderer()
    r._ensure_gpu_scope_pool = lambda *_a, **_k: {}
    for key, value in overrides.items():
        setattr(r, key, value)
    return r


# ── 执行段（E03 guard） ───────────────────────────────────────────────────

class TestExecuteStageReasons:
    def test_gpu_lane_disabled(self, _fresh_diag):
        r = _GpuScopeGuardRenderer()
        r.gpu_scope_enabled = False
        AsyncRenderer._do_render_gpu_scope(r, _demand())
        assert _fresh_diag.count("execute", "gpu_lane_disabled") == 1

    def test_demand_shape_invalid(self, _fresh_diag):
        r = _GpuScopeGuardRenderer()
        AsyncRenderer._do_render_gpu_scope(r, object())
        assert _fresh_diag.count("execute", "demand_shape_invalid") == 1

    def test_no_front_texture(self, _fresh_diag):
        r = _GpuScopeGuardRenderer()
        r.current_front_tex_id = None
        AsyncRenderer._do_render_gpu_scope(r, _demand(mode="parade"))
        assert _fresh_diag.count("execute", "no_front_texture") == 1
        snap = _fresh_diag.snapshot()["events"]["execute/no_front_texture"]
        assert snap["last"]["mode"] == "parade"

    def test_shaders_not_loaded_disables_and_records(self, _fresh_diag):
        r = _GpuScopeGuardRenderer()
        r._gpu_scope_shaders_loaded = False
        AsyncRenderer._do_render_gpu_scope(r, _demand())
        assert r.gpu_scope_enabled is False, "编译失败永久关闭（既有语义）"
        assert _fresh_diag.count("execute", "shaders_not_loaded") == 1
        assert r._pending_slots[RenderLane.GPU_SCOPE] is None

    def test_stale_revision(self, _fresh_diag):
        r = _GpuScopeGuardRenderer()
        r._scope_demand_revision = 5
        AsyncRenderer._do_render_gpu_scope(r, _demand(revision=3))
        assert _fresh_diag.count("execute", "stale_revision") == 1
        snap = _fresh_diag.snapshot()["events"]["execute/stale_revision"]
        assert snap["last"]["revision"] == 3
        assert snap["last"]["demand_revision"] == 5

    def test_no_render_params(self, _fresh_diag):
        r = _guard_renderer(_last_scope_render_params=None)
        AsyncRenderer._do_render_gpu_scope(r, _demand())
        assert _fresh_diag.count("execute", "no_render_params") == 1

    def test_color_output_failed(self, _fresh_diag):
        r = _guard_renderer(
            _last_scope_render_params={"layers": []},
            _scope_content_revision=10,
            _scope_color_output_revision=-1,   # 强制重建路径
        )
        r._do_render_color_output = lambda _p: False
        AsyncRenderer._do_render_gpu_scope(r, _demand())
        assert _fresh_diag.count("execute", "color_output_failed") == 1

    def test_output_texture_invalid(self, _fresh_diag):
        r = _guard_renderer(
            _last_scope_render_params={"layers": []},
            _scope_content_revision=10,
            _scope_color_output_revision=10,
            _scope_color_output_max_dim=max(DEFAULT_ANALYSIS_SIZE),
            _scope_color_output_tex=Mock(size=(0, 0)),   # 候选尺寸失效
        )
        AsyncRenderer._do_render_gpu_scope(r, _demand())
        assert _fresh_diag.count("execute", "output_texture_invalid") == 1
        assert r._scope_color_output_revision == -1


# ── 队列段（E02/E03 lane） ────────────────────────────────────────────────

class TestQueueStageReasons:
    def test_coalesced_replaced_latest_wins(self, _fresh_diag):
        from src.features.color.core.async_renderer import RenderCommand
        r = _FakeRenderer()
        r.submit(RenderCommand.RENDER_GPU_SCOPE, _demand(revision=1))
        r.submit(RenderCommand.RENDER_GPU_SCOPE, _demand(revision=2))
        assert _fresh_diag.count("queue", "coalesced_replaced") == 1

    def test_no_renderer(self, _fresh_diag):
        from src.features.color.core.image_engine import ImageEngine
        engine = ImageEngine.__new__(ImageEngine)
        engine.renderer = None
        engine.request_gpu_scope(_demand())
        assert _fresh_diag.count("queue", "no_renderer") == 1


# ── 提交段（E09 controller） ──────────────────────────────────────────────

def _make_host(*, scope_visible, renderer=None):
    """最小 LayerOpsMixin 宿主（不拉起 Qt 主窗口；构造即提交一次）。"""
    from src.app.controllers.layer_controller import LayerOpsMixin
    if renderer is None:
        renderer = Mock(gpu_scope_enabled=True)
    host = SimpleNamespace(
        engine=SimpleNamespace(
            renderer=renderer,
            scope_source_revision=10,
            is_interacting=False,
            get_active_layer=lambda: None,
            show_mask_mode=False,
            request_gpu_scope=lambda _d: None,
        ),
        scope_panel=Mock(
            gpu_demand_revision=3,
            demand_state=lambda: {
                "mode": "waveform", "display_size": (300, 200),
                "vectorscope_zoom": 1.0, "scope_transfer": "srgb",
                "scope_primaries": "bt709", "falsecolor_mode": "sdr",
                "zebra_threshold": 1.0},
        ),
        _is_scope_panel_visible=lambda: scope_visible,
        _is_curves_panel_visible=lambda: False,
        _scope_playback_is_active=lambda: False,
        _scope_scrub_is_active=lambda: False,
        _current_curve_mode=lambda: "Custom",
        _active_curve_canvas=lambda: Mock(
            histogram_display_metrics=lambda: ((1024, 512), 1.0)),
        _gpu_scope_last_submit=0.0,
        _gpu_scope_last_activity=0.0,
        _curves_scope_completed_key=None,
        _curves_scope_pending_key=None,
    )
    LayerOpsMixin._submit_gpu_scope_demand.__get__(host)(is_playing=False)
    return host


class TestSubmitStageReasons:
    def test_panel_hidden(self, _fresh_diag):
        _make_host(scope_visible=False)
        assert _fresh_diag.count("submit", "panel_hidden") == 1

    def test_gpu_lane_disabled(self, _fresh_diag):
        _make_host(scope_visible=True,
                   renderer=Mock(gpu_scope_enabled=False))
        assert _fresh_diag.count("submit", "gpu_lane_disabled") == 1

    def test_rate_limited(self, _fresh_diag, monkeypatch):
        import src.app.controllers.layer_controller as lc
        now = [1000.0]
        monkeypatch.setattr(lc.time, "monotonic", lambda: now[0])
        from src.app.controllers.layer_controller import LayerOpsMixin
        host = _make_host(scope_visible=True)
        assert _fresh_diag.count("submit", "rate_limited") == 0
        host._gpu_scope_last_submit = now[0]   # 刚提交过 → 节流命中
        LayerOpsMixin._submit_gpu_scope_demand.__get__(host)(
            is_playing=False)
        assert _fresh_diag.count("submit", "rate_limited") == 1


# ── 面板段（ScopePanel 接收） ─────────────────────────────────────────────

class TestPanelStageReasons:
    def test_stale_revision(self, _fresh_diag, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel()
        panel.update_gpu_scope(SimpleNamespace(revision=41,
                                               source_revision=10))
        assert _fresh_diag.count("panel", "stale_revision") == 1

    def test_source_not_graded(self, _fresh_diag, _qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel()
        panel._scope_source = "source"
        panel.update_gpu_scope(SimpleNamespace(
            revision=panel.gpu_demand_revision, source_revision=10))
        assert _fresh_diag.count("panel", "source_not_graded") == 1


# ── 呈现段（GpuScopeWidget） ──────────────────────────────────────────────

def _make_widget(_qapp):
    from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget
    widget = GpuScopeWidget()
    widget._gl_ready = True
    widget._frame_tex_id = 77
    widget._read_fbo = 5
    return widget


class TestWidgetStageReasons:
    def test_mutex_busy(self, _fresh_diag, _qapp):
        widget = _make_widget(_qapp)
        widget._mutex = SimpleNamespace(tryLock=lambda: False,
                                        unlock=lambda: None)
        widget.paintGL()   # tryLock 失败在 GL 调用之前返回
        assert _fresh_diag.count("widget", "mutex_busy") == 1

    def test_texture_not_shared(self, _fresh_diag, _qapp):
        widget = _make_widget(_qapp)
        import src.shared.ui.widgets.gpu_scope_widget as wmod
        with patch.object(wmod, "glIsTexture", return_value=0):
            widget._blit_shared_texture()
        assert _fresh_diag.count("widget", "texture_not_shared") == 1

    def test_fbo_incomplete(self, _fresh_diag, _qapp):
        widget = _make_widget(_qapp)
        import src.shared.ui.widgets.gpu_scope_widget as wmod
        with patch.object(wmod, "glIsTexture", return_value=1), \
             patch.object(wmod, "glCheckFramebufferStatus",
                          return_value=0x8CDD), \
             patch.object(wmod, "glBindFramebuffer"), \
             patch.object(wmod, "glFramebufferTexture2D"):
            widget._blit_shared_texture()
        assert _fresh_diag.count("widget", "fbo_incomplete") == 1
        snap = _fresh_diag.snapshot()["events"]["widget/fbo_incomplete"]
        assert snap["last"]["tex_id"] == 77
        assert "8CDD" in snap["last"]["detail"]


# ── 诊断自身契约 ─────────────────────────────────────────────────────────

class TestDiagnosticsContract:
    def test_disable_stops_recording(self):
        diag = ScopeDiagnostics(enabled=True)
        diag.record("execute", "no_front_texture", mode="waveform")
        diag.set_enabled(False)
        assert diag.enabled is False
        diag.record("execute", "no_front_texture")
        assert diag.count("execute", "no_front_texture") == 1, (
            "关闭后不新增；旧数据保留（可继续观察历史）")

    def test_snapshot_keeps_only_whitelisted_context(self):
        diag = ScopeDiagnostics(enabled=True)
        diag.record("widget", "mutex_busy", tex_id=9, mode="waveform",
                    texture_object=object(), callback=lambda: None)
        snap = diag.snapshot()["events"]["widget/mutex_busy"]
        assert snap["last"] == {"tex_id": 9, "mode": "waveform"}, (
            "快照只留标量白名单字段（不持有纹理/回调对象）")

    def test_counters_are_thread_safe(self):
        diag = ScopeDiagnostics(enabled=True)

        def hammer():
            for _ in range(500):
                diag.record("execute", "stale_revision", revision=1)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert diag.count("execute", "stale_revision") == 2000

    def test_stats_embedding(self, _fresh_diag):
        r = _GpuScopeGuardRenderer()
        r.current_front_tex_id = None
        r._temp_texture_alloc_count = 0
        r._finish_call_count = 0
        r._persistent_tex_size = 0
        r._yuv_rgb_output_cache = {}
        r.video_render_sync_mode = "auto"
        r._scope_diag_snapshot = AsyncRenderer._scope_diag_snapshot.__get__(r)
        AsyncRenderer._do_render_gpu_scope(r, _demand())
        stats = AsyncRenderer.get_render_stats(r)
        scope = stats["gpu_scope_diagnostics"]
        assert scope["events"]["execute/no_front_texture"]["count"] == 1
