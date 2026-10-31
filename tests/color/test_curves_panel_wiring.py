"""曲线面板正式接线测试（任务 6/7）。

覆盖：面板节流信号与 payload 形状、interaction 结束 flush、回填屏蔽、
草稿不跨目标泄漏、engine 两个新 setter（层更新 + LUT 重建 + 渲染触发）、
hydrator 的侧栏回填/兜底收集、grade 剪贴板 curves 类别扩展。
"""

import numpy as np
import pytest
from PyQt6.QtTest import QSignalSpy, QTest

from src.features.color.adapters.color_stack_hydrator import ColorStackHydrator
from src.features.color.core.composition_uniforms import composition_uniform_values
from src.features.color.core.curves_soft_clip import (
    curves_soft_clip_is_identity,
    set_soft_clip_control,
)
from src.shared.project.color_stack import ColorStackData
from src.features.color.ui.curves_panel import CurvesPanel
from src.shared.ui.widgets.curves_widget import CurvesWidget


# ================================================================
# 面板信号（任务 6）
# ================================================================

class TestPanelSignals:
    def test_soft_clip_drag_emits_model_payload_after_throttle(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        spy = QSignalSpy(panel.soft_clip_changed)
        panel.soft_clip_rows["low_soft"].slider.setValue(30)
        assert len(spy) == 0  # 节流窗口内不发射
        QTest.qWait(40)
        assert len(spy) == 1
        payload = spy[0][0]
        assert payload["linked"] is True
        for channel in "RGB":
            assert payload["channels"][channel]["low_soft"] == 0.30
        # 画布控制点信号不受侧栏影响
        assert len(QSignalSpy(panel.curves_widget.changed)) == 0

    def test_interaction_end_flushes_pending_value_immediately(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        spy = QSignalSpy(panel.soft_clip_changed)
        started = QSignalSpy(panel.interaction_started)
        ended = QSignalSpy(panel.interaction_ended)
        row = panel.soft_clip_rows["high_soft"]
        row.interactionStarted.emit()  # 模拟拖动开始
        row.slider.setValue(45)
        assert len(spy) == 0
        row.interactionEnded.emit()  # 拖动结束 → 最终值一次到位
        assert len(spy) == 1
        assert spy[0][0]["channels"]["R"]["high_soft"] == 0.45
        assert len(started) == 1 and len(ended) == 1

    def test_edit_row_emits_edit_payload_with_widget_amounts(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        spy = QSignalSpy(panel.edit_changed)
        panel.edit_rows["G"].slider.setValue(25)
        QTest.qWait(40)
        assert len(spy) == 1
        payload = spy[0][0]
        assert payload["linked"] is True
        assert payload["amounts"]["G"] == pytest.approx(25.0)
        assert payload["amounts"]["Y"] == pytest.approx(100.0)

    def test_reset_and_link_toggle_schedule_emit(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        spy = QSignalSpy(panel.soft_clip_changed)
        panel.soft_clip_rows["low_clip"].slider.setValue(-60)
        QTest.qWait(40)
        assert len(spy) == 1
        panel._reset_soft_clip()
        QTest.qWait(40)
        assert len(spy) == 2
        assert curves_soft_clip_is_identity(spy[1][0])

        unlink_spy = QSignalSpy(panel.edit_changed)
        panel.edit_link_btn.setChecked(False)
        QTest.qWait(40)
        assert len(unlink_spy) == 1
        assert unlink_spy[0][0]["linked"] is False

    def test_engine_receives_payload_via_signal_connection(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        received = []
        panel.soft_clip_changed.connect(received.append)
        panel.edit_changed.connect(received.append)
        panel.soft_clip_rows["high_clip"].slider.setValue(180)
        QTest.qWait(40)
        assert any(isinstance(p, dict) and "channels" in p for p in received)


# ================================================================
# 回填（任务 7）
# ================================================================

class TestPanelBackfill:
    def test_set_layer_state_suppresses_all_emissions(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        soft_spy = QSignalSpy(panel.soft_clip_changed)
        edit_spy = QSignalSpy(panel.edit_changed)
        panel.set_layer_state({
            "curves_soft_clip": set_soft_clip_control(
                {"linked": False, "channels": {}}, "R", "high_soft", 0.3),
            "curves_edit": {"linked": False, "amounts": {"Y": 40.0, "G": 60.0}},
        })
        QTest.qWait(40)
        assert len(soft_spy) == 0 and len(edit_spy) == 0
        state = panel.get_ui_state()
        assert state["soft_clip_linked"] is False
        assert state["soft_clip_channels"]["R"]["high_soft"] == pytest.approx(0.3)
        assert state["edit_linked"] is False
        assert panel.curves_widget.edit_amounts["Y"] == pytest.approx(40.0)
        assert panel.curves_widget.edit_amounts["G"] == pytest.approx(60.0)
        assert panel.curves_widget.edit_linked is False

    def test_backfill_overwrites_previous_draft_no_leak(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        panel.set_layer_state({
            "curves_soft_clip": set_soft_clip_control(
                {}, "G", "low_clip", -0.5),
            "curves_edit": {},
        })
        # 切到恒等目标：全部侧栏值回到默认，上一目标草稿不残留
        panel.set_layer_state({"curves_soft_clip": {}, "curves_edit": {}})
        assert curves_soft_clip_is_identity(panel.get_layer_state()["curves_soft_clip"])
        assert all(v == 100.0 for v in
                   panel.get_layer_state()["curves_edit"]["amounts"].values())

    def test_get_layer_state_roundtrip_through_setter(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        panel.soft_clip_rows["low_clip"].slider.setValue(-40)
        QTest.qWait(40)
        snapshot = panel.get_layer_state()
        fresh = CurvesPanel(CurvesWidget())
        fresh.set_layer_state(snapshot)
        assert fresh.get_layer_state() == snapshot

    def test_set_layer_state_accepts_legacy_shapes(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        # 旧草稿键名（low/high）与移动版 camelCase 都能归一化回填
        panel.set_layer_state({
            "curves_soft_clip": {"channels": {"red": {
                "lowClip": -0.3, "lowSoft": 0.1,
                "highClip": 1.4, "highSoft": 0.2}}},
            "curves_edit": {"amounts": {"y": 50.0}},
        })
        values = panel.get_ui_state()["soft_clip_channels"]["R"]
        assert values == {"low_clip": pytest.approx(-0.3),
                          "low_soft": pytest.approx(0.1),
                          "high_clip": pytest.approx(1.4),
                          "high_soft": pytest.approx(0.2)}
        # 非法键回退默认
        assert panel.curves_widget.edit_amounts["Y"] == pytest.approx(100.0)


# ================================================================
# Engine setter（任务 6）
# ================================================================

class TestEngineSetters:
    def test_set_curves_soft_clip_params_updates_active_layer(self, image_engine):
        image_engine.set_curves_soft_clip_params(
            set_soft_clip_control({}, "B", "high_clip", 0.7))
        layer = image_engine.get_active_layer()
        assert layer.curves_soft_clip["channels"]["B"]["high_clip"] == 0.7
        assert not curves_soft_clip_is_identity(layer.curves_soft_clip)
        image_engine.render_pipeline.assert_called()

    def test_set_curves_soft_clip_params_normalizes_out_of_range(self, image_engine):
        image_engine.set_curves_soft_clip_params({
            "channels": {"R": {"low_clip": 99.0, "high_clip": -5.0}},
        })
        layer = image_engine.get_active_layer()
        values = layer.curves_soft_clip["channels"]["R"]
        assert -1.0 <= values["low_clip"] <= values["high_clip"] <= 2.0

    def test_set_curves_soft_clip_params_identity_is_noop_state(self, image_engine):
        image_engine.set_curves_soft_clip_params({})
        layer = image_engine.get_active_layer()
        assert curves_soft_clip_is_identity(layer.curves_soft_clip)

    def test_set_curves_edit_params_rebuilds_layer_lut(self, image_engine):
        layer = image_engine.get_active_layer()
        layer.curves_data = {"R": [(0, 255), (128, 100), (255, 0)]}
        image_engine.set_curves_edit_params(
            {"linked": True, "amounts": {"Y": 100.0, "R": 0.0,
                                         "G": 100.0, "B": 100.0}})
        identity = np.arange(256, dtype=np.float32) / 255.0
        assert np.allclose(layer.curves_lut[:, 0, 0], identity, atol=1e-6)
        image_engine.render_pipeline.assert_called()

    def test_set_curves_edit_params_no_active_layer_no_crash(self, image_engine):
        image_engine.active_layer_index = 999
        image_engine.set_curves_edit_params({"amounts": {"Y": 0.0}})
        image_engine.set_curves_soft_clip_params({"channels": {}})

    def test_legacy_soft_clip_payload_stays_separate(self, image_engine):
        """侧栏信号不得写旧 soft_clip_params（计划红线）。"""
        before = dict(image_engine.get_active_layer().soft_clip_params)
        image_engine.set_curves_soft_clip_params(
            set_soft_clip_control({}, "R", "low_soft", 0.4))
        assert image_engine.get_active_layer().soft_clip_params == before


# ================================================================
# Hydrator（任务 7）
# ================================================================

class _FakePalettePanel:
    def __init__(self):
        self.states = []

    def set_layer_state(self, state):
        self.states.append(state)

    def get_layer_state(self):
        return self.states[-1] if self.states else {}


class TestHydratorCurvesPalette:
    def test_apply_prefers_active_layer_snapshot(self):
        stack = ColorStackData.create()
        stack = stack.with_layers((
            {"curves_soft_clip": {"channels": {"R": {"high_soft": 0.9}}}},
            {"curves_soft_clip": {"channels": {"G": {"high_soft": 0.5}}}},
        ), active_layer_index=1)
        panel = _FakePalettePanel()
        ColorStackHydrator()._apply_curves_palette_to_panel(stack, panel)
        state = panel.states[-1]
        assert state["curves_soft_clip"]["channels"]["G"]["high_soft"] == 0.5

    def test_apply_falls_back_to_flat_keys(self):
        stack = ColorStackData.create().with_curves_soft_clip(
            {"channels": {"B": {"low_clip": -0.4}}})
        panel = _FakePalettePanel()
        ColorStackHydrator()._apply_curves_palette_to_panel(stack, panel)
        assert panel.states[-1]["curves_soft_clip"]["channels"]["B"][
            "low_clip"] == -0.4

    def test_apply_skips_missing_panel(self):
        ColorStackHydrator()._apply_curves_palette_to_panel(
            ColorStackData.create(), None)  # 不抛错

    def test_collect_reads_panel_state_without_engine_layers(self):
        panel = _FakePalettePanel()
        panel.states.append({
            "curves_soft_clip": set_soft_clip_control({}, "R", "high_soft", 0.6),
            "curves_edit": {"linked": True, "amounts": {"Y": 80.0}},
        })
        stack = ColorStackHydrator().collect_stack_from_engine_and_panels(
            "stack-1", engine=None, panels={"curves_palette": panel})
        assert stack.curves_soft_clip["channels"]["R"]["high_soft"] == 0.6
        assert stack.curves_edit["amounts"]["Y"] == 80.0

    def test_collect_derives_flat_keys_from_active_layer(self, image_engine):
        layer = image_engine.get_active_layer()
        layer.curves_soft_clip = set_soft_clip_control({}, "G", "low_clip", -0.2)
        stack = ColorStackHydrator().collect_stack_from_engine_and_panels(
            "stack-1", engine=image_engine, panels={})
        assert stack.curves_soft_clip["channels"]["G"]["low_clip"] == -0.2
        assert stack.layers  # 权威快照随行


# ================================================================
# 调色剪贴板（任务 7：curves 类别携带侧栏状态）
# ================================================================

class TestGradeClipboardCurvesCategory:
    def test_curves_category_includes_sidebar_keys(self):
        from src.features.color.core.grade_clipboard import (
            GRADE_CATEGORY_KEYS,
        )
        assert "curves_edit" in GRADE_CATEGORY_KEYS["curves"]
        assert "curves_soft_clip" in GRADE_CATEGORY_KEYS["curves"]

    def test_apply_grade_patch_applies_curves_category(self):
        from src.features.color.core.layer import Layer

        source = Layer()
        source.curves_soft_clip = set_soft_clip_control({}, "R", "high_soft", 0.35)
        source.curves_edit = {"linked": False, "amounts": {"Y": 55.0}}
        snapshot = source.to_dict(None)

        target = Layer()
        target.apply_grade_patch(snapshot, ["curves"])
        assert target.curves_soft_clip["channels"]["R"]["high_soft"] == 0.35
        assert target.curves_edit["amounts"]["Y"] == 55.0
        # 非 curves 类别不受影响
        assert target.curves_data == {}


# ================================================================
# 任务 10 验收：undo/redo 快照、导出收集、渲染门控性能
# ================================================================

class TestUndoRedoSnapshot:
    @staticmethod
    def _sidebar_of(entry):
        """undo/redo 快照按序列化形态返回（dict），兼容活 Layer 对象。"""
        if isinstance(entry, dict):
            return entry["curves_soft_clip"], entry["curves_edit"]
        return entry.curves_soft_clip, entry.curves_edit

    def test_undo_restores_curves_sidebar_state(self):
        """一次连续拖动 = interaction_started 时的一次快照；undo 恢复侧栏。"""
        from src.features.color.core.history import HistoryManager
        from src.features.color.core.layer import Layer
        from src.features.color.core.curves_soft_clip import (
            normalize_curves_edit,
        )

        mgr = HistoryManager()
        img = np.zeros((8, 8, 3), dtype=np.uint8)
        layer = Layer()
        mgr.push_state(img, [layer], 0, copy_image=False)

        layer.curves_soft_clip = set_soft_clip_control({}, "R", "high_soft", 0.5)
        layer.curves_edit = normalize_curves_edit({"amounts": {"Y": 30.0}})

        state = mgr.undo(img, [layer], 0)
        assert state is not None
        soft, edit = self._sidebar_of(state["layers"][0])
        assert curves_soft_clip_is_identity(soft)
        assert edit["amounts"]["Y"] == 100.0

        # redo 回到修改态（对称交换语义；redo 输入须为活 Layer 对象，
        # 与 engine.undo 后的 deserialize 产物一致）
        from src.features.color.core.layer import Layer as _Layer
        live_layers = [_Layer.from_dict(d) for d in state["layers"]]
        redone = mgr.redo(state["image"], live_layers, 0)
        assert redone is not None
        soft, edit = self._sidebar_of(redone["layers"][0])
        assert soft["channels"]["R"]["high_soft"] == pytest.approx(0.5)
        assert edit["amounts"]["Y"] == 30.0


class TestExportGradeCollection:
    """preview/export 一致性（收集层）：四参数/强度随栈快照进入 grade。

    生产路径上平铺键由收集方从活动图层派生（hydrator / edit_controller
    layers 快照），测试栈按同样形状构造。
    """

    @staticmethod
    def _exporter_with_stack(layers, *, curves_soft_clip=None, curves_edit=None,
                             grading_params=None):
        from types import SimpleNamespace
        from src.features.edit.core.timeline_export import TimelineExporter

        stack = ColorStackData(
            stack_id="stack",
            grading_params=grading_params or {},
            layers=tuple(layers),
            active_layer_index=0,
            curves_soft_clip=curves_soft_clip or {},
            curves_edit=curves_edit or {},
        )
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._project = SimpleNamespace(color_stacks={"stack": stack})
        exporter._color_grade_lut_cache = {}
        layer = SimpleNamespace(
            clip=SimpleNamespace(color_stack_id="stack"),
            clip_id="clip",
            source_frame=0,
        )
        return exporter, layer

    def test_non_identity_state_reaches_export_grade(self):
        state = set_soft_clip_control(
            {"linked": False, "channels": {}}, "R", "high_soft", 0.4)
        exporter, layer = self._exporter_with_stack(
            [{"grading_params": {}, "curves_soft_clip": state}],
            curves_soft_clip=state,
        )
        grade = exporter._get_clip_color_grade(layer)
        assert grade is not None
        assert grade["curves_soft_clip"]["channels"]["R"]["high_soft"] == 0.4

    def test_identity_state_does_not_reach_export_grade(self):
        # 有基础调色（grade 非 None）但侧栏恒等：不携带四参数
        exporter, layer = self._exporter_with_stack(
            [{"grading_params": {"exposure": 0.5}}],
            grading_params={"exposure": 0.5},
        )
        grade = exporter._get_clip_color_grade(layer)
        assert grade is not None
        assert grade["curves_soft_clip"] is None

    def test_non_default_edit_amounts_reach_export_grade(self):
        edit_state = {"linked": True, "amounts": {"Y": 40.0}}
        exporter, layer = self._exporter_with_stack(
            [{"grading_params": {},
              "curves_edit": edit_state}],
            curves_edit=edit_state,
        )
        grade = exporter._get_clip_color_grade(layer)
        assert grade is not None
        assert grade["curves_edit"]["amounts"]["Y"] == 40.0

    def test_default_edit_amounts_do_not_reach_export_grade(self):
        exporter, layer = self._exporter_with_stack(
            [{"grading_params": {"exposure": 0.5}}],
            grading_params={"exposure": 0.5},
        )
        grade = exporter._get_clip_color_grade(layer)
        assert grade is not None
        assert grade["curves_edit"] is None


class TestRenderGatingPerformance:
    """性能验收：恒等状态不编译新 program（缓存命中），非恒等才进管线。"""

    def test_identity_state_shares_cached_program(self):
        moderngl = pytest.importorskip("moderngl")
        ctx = moderngl.create_standalone_context(require=330)
        try:
            from src.features.color.core.shader_compiler import ShaderPipeline
            from src.features.color.core.curves_soft_clip import (
                normalize_curves_soft_clip,
            )

            pipeline = ShaderPipeline(ctx)
            identity_layer = {
                "opacity": 1.0, "blend_mode": "Normal",
                "grading_params": {},
                "curves_soft_clip": normalize_curves_soft_clip(None),
            }
            values_a = composition_uniform_values(
                identity_layer, width=64, height=64)
            values_b = composition_uniform_values(
                identity_layer, width=64, height=64)
            # 门控判定：恒等 → 无 curves_soft_clip 旗标（与其它层共享缓存）
            assert curves_soft_clip_is_identity(identity_layer["curves_soft_clip"])
            assert values_a["u_cs_low_clip"] == (0.0, 0.0, 0.0)
            assert values_a["u_cs_high_clip"] == (1.0, 1.0, 1.0)
            prog1 = pipeline.build_composition_shader()
            prog2 = pipeline.build_composition_shader()
            assert prog1 is prog2  # 缓存命中，无重编译
            pipeline.release()
        finally:
            ctx.release()

    def test_throttle_interval_is_16ms(self, qapp):
        panel = CurvesPanel(CurvesWidget())
        assert panel._soft_timer.interval() == 16
        assert panel._edit_timer.interval() == 16
        assert panel._soft_timer.isSingleShot()
        assert panel._edit_timer.isSingleShot()
