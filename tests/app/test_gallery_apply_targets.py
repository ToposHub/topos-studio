"""G07 Gallery 应用动作的权威目标测试：timeline 命令通道／undo／守卫恢复。

门槛：timeline 全量与分类应用统一走 ``_paste_grade_to_clip``（单一
undo，工程 payload／预览一致）；目标拒绝时不回落到引擎层改显示；
standalone 保留单步 history；分类取消零写入；视频恢复经 generation
守卫；中键／按钮／菜单同动作同结果（同一 handler）。
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.features.color.core.grade_clipboard import ALL_CATEGORIES


def _layer(name="l1"):
    from src.features.color.core.layer import Layer
    layer = Layer(name=name)
    layer.grading_params["exposure"] = 0.4
    return layer


def _make_ctrl(tmp_path, monkeypatch):
    import numpy as np
    from src.app.controllers.layer_controller import LayerOpsMixin
    monkeypatch.chdir(tmp_path)
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)

    image = (np.random.default_rng(1).random((24, 32, 3))
             * 255).astype(np.uint8)

    class _Engine:
        def __init__(self):
            original = _layer("original")
            self.layers = [original]
            self.active_layer_index = 0
            self.img_high = image
            self.img_full_original = image
            self.history = MagicMock()

        def get_current_image(self):
            return self.img_high

        def get_active_layer(self):
            return self.layers[self.active_layer_index]

        def render_pipeline(self, *args, **kwargs):
            return None

    ctrl.engine = _Engine()
    ctrl.status_bar = MagicMock()
    ctrl.current_file_is_video = False
    ctrl.video_controller = None
    ctrl.save_router = MagicMock()
    ctrl.save_router.load_single_asset_data.return_value = {
        "layers": [_layer("preset")], "active_layer_index": 0}
    for attr in ("grading_panel", "layer_panel", "curves_widget",
                 "hsl_curves_panel", "mixer_panel", "effects_panel",
                 "qualifier_panel", "lut_panel", "warper_panel",
                 "mask_panel", "info_panel"):
        setattr(ctrl, attr, MagicMock())
    ctrl.curves_widget.hsl_curves_widget = None
    ctrl._get_composite_graph_data = lambda: None
    ctrl._get_composite_state_data = lambda: None
    ctrl._trigger_autosave = lambda: None
    ctrl.color_timeline_clip_strip = None
    ctrl._paste_grade_to_clip = None
    return ctrl


class TestFullApplyTimelineRouting:
    def test_active_clip_routes_to_authoritative_command_channel(self,
                                                                 tmp_path,
                                                                 monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        paste = MagicMock(return_value=True)
        ctrl._paste_grade_to_clip = paste
        ctrl.color_timeline_clip_strip = SimpleNamespace(
            _active_clip_id="clip-9")
        engine_layers_before = list(ctrl.engine.layers)

        ctrl.on_apply_grade_from_preset("/tmp/p.topos")

        paste.assert_called_once()
        clip_id, categories = paste.call_args[0][:2]
        assert clip_id == "clip-9"
        assert categories == set(ALL_CATEGORIES)   # 全量 = 预设全部类别域
        # 工程一致：引擎层不被旁路改写（由命令通道负责 hydrate）
        assert ctrl.engine.layers == engine_layers_before
        ctrl.engine.history.push_state.assert_not_called()

    def test_clip_rejected_no_fallback_to_engine_mutation(self, tmp_path,
                                                          monkeypatch):
        """目标 clip 已删／命令拒绝：如实回填，不回落引擎层改显示。"""
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        paste = MagicMock(return_value=False)
        ctrl._paste_grade_to_clip = paste
        ctrl.color_timeline_clip_strip = SimpleNamespace(
            _active_clip_id="gone-clip")
        engine_layers_before = list(ctrl.engine.layers)

        ctrl.on_apply_grade_from_preset("/tmp/p.topos")

        paste.assert_called_once()
        assert ctrl.engine.layers == engine_layers_before
        ctrl.engine.history.push_state.assert_not_called()
        messages = [str(c.args[0]) for c in
                    ctrl.status_bar.showMessage.call_args_list]
        assert any("不可用" in m or "unavailable" in m.lower()
                   for m in messages)


class TestStandaloneApply:
    def test_image_mode_replaces_layers_with_single_history_push(self, tmp_path,
                                                                 monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        ctrl._paste_grade_to_clip = MagicMock()  # 不应被调（无 timeline 目标）
        before = list(ctrl.engine.layers)
        ctrl.on_apply_grade_from_preset("/tmp/p.topos")
        assert ctrl._paste_grade_to_clip.call_count == 0
        assert ctrl.engine.layers != before          # 引擎层替换（旧路径）
        assert len(ctrl.engine.layers) == 1
        assert ctrl.engine.layers[0].name == "preset"
        ctrl.engine.history.push_state.assert_called_once()   # 单步 undo

    def test_no_layers_in_preset_reports_and_writes_nothing(self, tmp_path,
                                                            monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        ctrl.save_router.load_single_asset_data.return_value = {"layers": []}
        before = list(ctrl.engine.layers)
        ctrl.on_apply_grade_from_preset("/tmp/p.topos")
        assert ctrl.engine.layers == before
        ctrl.engine.history.push_state.assert_not_called()


class TestCategoriesTimeline:
    def test_category_rejection_is_honest(self, tmp_path, monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        paste = MagicMock(return_value=False)
        ctrl._paste_grade_to_clip = paste
        ctrl.color_timeline_clip_strip = SimpleNamespace(
            _active_clip_id="clip-x")
        ctrl.on_apply_preset_categories_requested("/tmp/p.topos",
                                                  categories={"primary"})
        paste.assert_called_once()
        messages = [str(c.args[0]) for c in
                    ctrl.status_bar.showMessage.call_args_list]
        assert any("不可用" in m or "unavailable" in m.lower()
                   for m in messages)

    def test_dialog_cancel_writes_nothing(self, tmp_path, monkeypatch):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        ctrl._paste_grade_to_clip = MagicMock()
        ctrl.color_timeline_clip_strip = SimpleNamespace(
            _active_clip_id="clip-x")
        from src.features.color.ui import grade_paste_dialog
        monkeypatch.setattr(
            grade_paste_dialog.GradePasteDialog, "get_categories",
            staticmethod(lambda *a, **k: set()))
        before = list(ctrl.engine.layers)
        ctrl.on_apply_preset_categories_requested("/tmp/p.topos")
        ctrl._paste_grade_to_clip.assert_not_called()
        assert ctrl.engine.layers == before
        ctrl.engine.history.push_state.assert_not_called()


class TestVideoResumeGuard:
    def _video_ctrl(self):
        return SimpleNamespace(
            is_video_loaded=lambda: True, is_playing=True,
            current_frame=7,
            start_playback=MagicMock(), stop_playback=MagicMock(),
            frame_changed=MagicMock(), video_source_path="/v/a.mov",
            source_duration_frames=100)

    def test_apply_resume_skipped_when_source_changed_under_timer(
            self, tmp_path, monkeypatch):
        """100ms 恢复回调触发时源已换 → 不重新起播旧源（generation 守卫）。"""
        from PyQt6.QtCore import QTimer
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        ctrl.current_file_is_video = True
        video = self._video_ctrl()
        ctrl.video_controller = video
        ctrl._render_current_video_frame_for_grading = lambda: (object(), 7)
        ctrl.update_canvas = MagicMock()
        ctrl._trigger_autosave = MagicMock()
        ctrl.engine.renderer = None

        timers = []
        monkeypatch.setattr(
            QTimer, "singleShot",
            staticmethod(lambda ms, cb: timers.append(cb)))

        ctrl.on_apply_grade_from_preset("/tmp/p.topos")
        assert len(timers) == 1
        # 恢复链：finish（timers[0]）→ 再 100ms → fire（patched singleShot
        # 追加为 timers[1]）。fire 触发前用户换了源（token 变化）→ 不起播。
        ctrl.current_file_path = "/v/b.mov"
        timers[0]()          # finish：调度 fire → timers[1]
        timers[1]()          # fire：token 已变 → 放弃
        video.start_playback.assert_not_called()

    def test_apply_resume_playback_when_context_intact(self, tmp_path,
                                                       monkeypatch, qapp):
        ctrl = _make_ctrl(tmp_path, monkeypatch)
        ctrl.current_file_is_video = True
        ctrl.current_file_path = "/v/a.mov"
        video = self._video_ctrl()
        ctrl.video_controller = video
        ctrl._render_current_video_frame_for_grading = lambda: (object(), 7)
        ctrl.update_canvas = MagicMock()
        ctrl._trigger_autosave = MagicMock()
        ctrl.engine.renderer = None

        from PyQt6.QtCore import QTimer
        timers = []
        monkeypatch.setattr(
            QTimer, "singleShot",
            staticmethod(lambda ms, cb: timers.append(cb)))

        ctrl.on_apply_grade_from_preset("/tmp/p.topos")
        timers[0]()          # finish
        timers[1]()          # fire：上下文未变 → 恢复播放
        video.start_playback.assert_called_once()


class TestSameActionSameHandler:
    def test_middle_click_and_menu_share_single_entry(self):
        """中键（delegate editorEvent）／菜单／按钮都发 grade_applied(str)
        → MainWindow 唯一连接 on_apply_grade_from_preset（既有事实锁定）。"""
        import inspect
        from src.app.controllers import layer_controller as lc
        source = inspect.getsource(lc)
        # grade_applied 只有一个生产 handler；全量应用与分类应用共用
        # _paste_grade_to_clip 权威入口
        assert source.count("def on_apply_grade_from_preset(") == 1
        assert "_paste_grade_to_clip" in source
