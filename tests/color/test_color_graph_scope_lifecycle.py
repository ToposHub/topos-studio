"""S05/S06：scope 取样新鲜度/失效与颜色/多视图一致性（offscreen 实测）。

S05（``ScopeSampleIdentity``）：在 revision 契约上补充 session/owner/
graph revision/frame/颜色上下文/目标身份；切 clip、图编辑、颜色域切换
后旧结果被拒绝（不闪回）；frame/content revision 是下界（晚到的更新帧
合法）；面板登记身份后 revision 撞号的迟到帧也被拒绝。

S06：同一取样帧一次采样供所有模式（split/quad/all 单次
``_scope_sample_front``）；CIE/假色的颜色 metadata 由 demand 显式携带；
身份（含颜色上下文）变化即拒绝——颜色域切换后缓存未失效可被检出。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.features.color.core.scope_graph_sampling import (
    ScopeSampleIdentity,
    TARGET_NODE_PREFIX,
    TARGET_OUTPUT,
    TARGET_SOURCE,
    explicit_node_target,
)
from src.features.color.core.scope_types import (
    ScopeDemand,
    ScopeGpuFrame,
    is_frame_valid_for_demand,
)

from tests.color.test_gpu_scope_pipeline import _GpuScopeGuardRenderer


def _identity(**overrides) -> ScopeSampleIdentity:
    values = dict(
        session_id="st-1", scope_owner_id=None, graph_revision=3,
        source_content_revision=10, frame=7,
        color_context=("srgb", "bt709"), target=TARGET_OUTPUT)
    values.update(overrides)
    return ScopeSampleIdentity(**values)


# ═══ S05：身份拒绝矩阵 ═══════════════════════════════════════════════════

class TestIdentityRejection:
    def test_same_identity_allowed(self):
        assert _identity().allows(_identity())

    def test_newer_content_and_frame_allowed(self):
        """下界语义：GPU_SCOPE 晚到分析更新 front 是合法的。"""
        assert _identity().allows(
            _identity(source_content_revision=12, frame=9))

    def test_older_content_rejected(self):
        assert not _identity().allows(_identity(source_content_revision=9))

    def test_older_frame_rejected(self):
        assert not _identity().allows(_identity(frame=6))

    def test_clip_switch_rejected(self):
        assert not _identity().allows(_identity(session_id="st-2"))

    def test_graph_edit_rejected(self):
        """参数/连线/结构编辑 → graph revision 变 → 旧帧不闪回。"""
        assert not _identity().allows(_identity(graph_revision=4))

    def test_owner_change_rejected(self):
        assert not _identity(scope_owner_id="grp-1").allows(
            _identity(scope_owner_id="grp-2"))
        assert not _identity(scope_owner_id="grp-1").allows(
            _identity(scope_owner_id=None))

    def test_color_context_change_rejected(self):
        """S06：颜色域切换后旧缓存必须失效（身份可检出）。"""
        assert not _identity().allows(
            _identity(color_context=("pq", "bt2020")))

    def test_target_change_rejected(self):
        assert not _identity().allows(_identity(target=TARGET_SOURCE))
        assert not _identity().allows(_identity(
            target=explicit_node_target("n-1")))

    def test_non_identity_rejected(self):
        assert not _identity().allows(object())


# ═══ S05：与 revision 契约的联合语义 ═════════════════════════════════════

class TestCombinedFreshness:
    def _pair(self, demand_identity=None, frame_identity=None):
        demand = ScopeDemand(
            revision=5, source_revision=10, mode="waveform",
            display_size=(10, 10), sample_identity=demand_identity)
        frame = ScopeGpuFrame(
            revision=5, source_revision=10,
            sample_identity=frame_identity)
        return demand, frame

    def test_both_identities_enforced(self):
        demand, frame = self._pair(_identity(), _identity())
        assert is_frame_valid_for_demand(frame, demand)
        demand, frame = self._pair(_identity(), _identity(session_id="x"))
        assert not is_frame_valid_for_demand(frame, demand)

    def test_single_side_falls_back_to_revision_contract(self):
        """渐进采用：只有一端带身份时保持旧规则（不误杀）。"""
        demand, frame = self._pair(_identity(), None)
        assert is_frame_valid_for_demand(frame, demand)
        demand, frame = self._pair(None, _identity())
        assert is_frame_valid_for_demand(frame, demand)

    def test_revision_mismatch_still_rejects_with_identity(self):
        demand, frame = self._pair(_identity(), _identity())
        object.__setattr__(frame, "revision", 4)
        assert not is_frame_valid_for_demand(frame, demand)

    def test_with_source_content_revision_replace(self):
        base = _identity()
        bumped = base.with_source_content_revision(15)
        assert bumped.source_content_revision == 15
        assert base.allows(bumped)
        assert not bumped.allows(base)


# ═══ S05：面板登记身份后的迟到拒绝 ═══════════════════════════════════════

class _PanelHarness:
    """最小 ScopePanel（offscreen，不触发完整 UI 构建）。"""

    @staticmethod
    def _panel(qapp):
        from src.features.color.ui.scope_panel import ScopePanel
        panel = ScopePanel.__new__(ScopePanel)
        panel._gpu_demand_revision = 5
        panel._scope_source = 'graded'
        panel._gpu_display_active = True
        presented = []
        panel._present_gpu_frame = lambda frame: presented.append(frame)
        panel._activate_gpu_display = lambda: None
        panel._current_mode = 'waveform'
        return panel, presented


class TestPanelIdentityGuard:
    @pytest.fixture()
    def _qapp(self):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        yield app

    def test_matching_identity_presented(self, _qapp):
        import src.features.color.core.scope_diagnostics as diag_mod
        from src.features.color.core.scope_diagnostics import ScopeDiagnostics
        diag = ScopeDiagnostics(enabled=True)
        original = diag_mod.scope_diagnostics
        diag_mod.scope_diagnostics = diag
        try:
            panel, presented = _PanelHarness._panel(_qapp)
            identity = _identity()
            panel.set_gpu_demand_identity(identity)
            frame = ScopeGpuFrame(
                revision=5, source_revision=12,
                sample_identity=identity.with_source_content_revision(12))
            panel.update_gpu_scope(frame)
            assert presented == [frame]
            assert diag.count("panel", "identity_mismatch") == 0
        finally:
            diag_mod.scope_diagnostics = original

    def test_revision_collision_with_stale_identity_rejected(self, _qapp):
        """S05 核心：revision 撞号但身份是旧 clip/旧图版本的迟到帧。"""
        import src.features.color.core.scope_diagnostics as diag_mod
        from src.features.color.core.scope_diagnostics import ScopeDiagnostics
        diag = ScopeDiagnostics(enabled=True)
        original = diag_mod.scope_diagnostics
        diag_mod.scope_diagnostics = diag
        try:
            panel, presented = _PanelHarness._panel(_qapp)
            panel.set_gpu_demand_identity(_identity())
            late = ScopeGpuFrame(
                revision=5, source_revision=10,
                sample_identity=_identity(session_id="st-previous"))
            panel.update_gpu_scope(late)
            assert presented == []
            assert diag.count("panel", "identity_mismatch") == 1
        finally:
            diag_mod.scope_diagnostics = original

    def test_frame_without_identity_rejected_when_demand_has_one(self, _qapp):
        import src.features.color.core.scope_diagnostics as diag_mod
        from src.features.color.core.scope_diagnostics import ScopeDiagnostics
        diag = ScopeDiagnostics(enabled=True)
        original = diag_mod.scope_diagnostics
        diag_mod.scope_diagnostics = diag
        try:
            panel, presented = _PanelHarness._panel(_qapp)
            panel.set_gpu_demand_identity(_identity())
            panel.update_gpu_scope(ScopeGpuFrame(revision=5,
                                                 source_revision=10))
            assert presented == []
        finally:
            diag_mod.scope_diagnostics = original


# ═══ S06：单采样多模式 / 颜色 metadata ═══════════════════════════════════

class TestSingleSampleMultiMode:
    @pytest.mark.parametrize("mode", ["split", "quad", "all"])
    def test_one_analysis_sample_for_all_modes(self, mode):
        """同帧一次采样供所有模式（S06 §1）。"""
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {"layers": []}
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._do_render_color_output = Mock(return_value=True)
        calls = []
        r._scope_sample_front = (
            lambda *a, **k: calls.append((a, k)))
        accumulate = {
            "waveform": "_scope_accumulate_waveform",
            "vectorscope": "_scope_accumulate_vectorscope",
            "parade": "_scope_accumulate_parade",
            "luma": "_scope_accumulate_luma",
            "histogram": "_scope_accumulate_histogram",
            "cie": "_scope_accumulate_cie",
        }
        for attr in accumulate.values():
            setattr(r, attr, Mock())
        displays = [
            "_scope_display_waveform", "_scope_display_vectorscope",
            "_scope_display_parade", "_scope_display_luma",
            "_scope_display_histogram", "_scope_display_cie",
            "_scope_display_falsecolor",
        ]
        for attr in displays:
            setattr(r, attr, Mock())
        r._ensure_gpu_scope_pool = lambda *_a: {
            "wf_display": Mock(glo=71), "vs_display": Mock(glo=72),
            "parade_display": Mock(glo=73), "luma_display": Mock(glo=74),
            "hist_display": Mock(glo=75), "cie_display": Mock(glo=76),
            "fc_display": Mock(glo=77), "curves_hist_display": Mock(glo=78),
            "curves_hist_display_size": (1024, 512),
        }
        r._scope_mask_uv_rect = lambda *_a: (0.0, 0.0, 1.0, 1.0)

        from src.features.color.core.async_renderer import AsyncRenderer
        AsyncRenderer._do_render_gpu_scope(
            r, ScopeDemand(revision=5, source_revision=10, mode=mode,
                           display_size=(100, 100)))
        assert len(calls) == 1, (
            f"{mode} 必须共享同一次分析采样（S06），实采样 {len(calls)} 次")

    def test_color_metadata_carried_by_demand(self):
        """S06：CIE/假色的 transfer/primaries 由 demand 显式声明并进入
        分析调用（模式独立输出，颜色 metadata 不来自全局隐藏状态）。"""
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = {"layers": []}
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._do_render_color_output = Mock(return_value=True)
        r._scope_sample_front = Mock()
        seen = {}
        r._scope_accumulate_cie = Mock(
            side_effect=lambda pool, demand: seen.update(
                transfer=demand.scope_transfer,
                primaries=demand.scope_primaries))
        r._scope_display_cie = Mock()
        r._ensure_gpu_scope_pool = lambda *_a: {
            "cie_display": Mock(glo=76)}
        r._scope_mask_uv_rect = lambda *_a: (0.0, 0.0, 1.0, 1.0)

        from src.features.color.core.async_renderer import AsyncRenderer
        AsyncRenderer._do_render_gpu_scope(
            r, ScopeDemand(revision=5, source_revision=10, mode="cie",
                           display_size=(100, 100),
                           scope_transfer="pq", scope_primaries="bt2020"))
        assert seen == {"transfer": "pq", "primaries": "bt2020"}
        r._scope_accumulate_cie.assert_called_once()

    def test_color_context_identity_covers_scope_metadata(self):
        """S06：颜色域切换改变身份 → 旧帧被拒（缓存未失效可检出）。"""
        demand_ctx = ("pq", "bt2020")
        frame_ctx = ("srgb", "bt709")
        assert not ScopeSampleIdentity(
            color_context=demand_ctx).allows(
            ScopeSampleIdentity(color_context=frame_ctx))


# ═══ S05：E09 身份构造（controller 提交侧）═══════════════════════════════

class TestControllerIdentityConstruction:
    def _controller(self, engine, panel):
        """LayerOpsMixin 最小宿主（E09 _submit_gpu_scope_demand 依赖）。"""
        from src.app.controllers.layer_controller import LayerOpsMixin
        host = LayerOpsMixin.__new__(LayerOpsMixin)
        host.engine = engine
        host.scope_panel = panel
        host._gpu_scope_last_submit = 0.0
        host._gpu_scope_last_activity = 0.0
        host._curves_scope_completed_key = None
        host._curves_scope_pending_key = None
        host._latest_gpu_scope_demand = None
        return host

    def test_demand_carries_full_identity(self):
        engine = Mock()
        engine.renderer = Mock()
        engine.renderer.gpu_scope_enabled = True
        engine.scope_source_revision = 11
        engine.is_interacting = False
        engine.active_color_graph_stack_id = "st-9"
        engine.color_graph_revision = 4
        engine._tracking_frame_context = lambda: ("clip-1", 33)
        engine.get_active_layer = lambda: None
        engine.request_gpu_scope = Mock()
        panel = Mock()
        panel.demand_state.return_value = {
            "mode": "waveform", "display_size": (100, 100),
            "vectorscope_zoom": 1.0, "scope_transfer": "hlg",
            "scope_primaries": "bt2020", "falsecolor_mode": "sdr",
            "zebra_threshold": 1.0,
        }
        panel.gpu_demand_revision = 6
        host = self._controller(engine, panel)
        host._is_scope_panel_visible = lambda: True
        host._is_curves_panel_visible = lambda: False
        host._scope_playback_is_active = lambda: False
        host._scope_scrub_is_active = lambda: False
        host._current_curve_mode = lambda: "Custom"
        host._active_curve_canvas = lambda: Mock(
            histogram_display_metrics=lambda: ((1024, 512), 1.0))
        host._gpu_scope_diag = None

        host._submit_gpu_scope_demand(is_playing=False)

        engine.request_gpu_scope.assert_called_once()
        demand = engine.request_gpu_scope.call_args.args[0]
        identity = demand.sample_identity
        assert identity.session_id == "st-9"
        assert identity.graph_revision == 4
        assert identity.source_content_revision == 11
        assert identity.frame == 33
        assert identity.color_context == ("hlg", "bt2020")
        # S03：节点选择只导航——graded GPU 需求恒 target=output
        assert identity.target == TARGET_OUTPUT
        panel.set_gpu_demand_identity.assert_called_once_with(identity)
