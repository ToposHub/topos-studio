"""wd12-g65 批 D（edit UI b 段）16 条 Major 级审计条目的回归测试。

覆盖 REMEDIATION_PLAN 批次条目：
  AUD-19622 ai_variant_list 帧率换算
  AUD-19720 clip_audio_effect_dialog_adapter 宽泛 except 静默降级
  AUD-19801/19802/19803 edit_effects_panel（悬停预览泄漏/线程亲和/搜索防抖）
  AUD-19900/19901/19902 edit_page（字幕轨语言/隐藏页门控/场景剪切映射）
  AUD-20001..20008 edit_properties_panel（Reset/联动/去抖/重接线）

所有测试只驱动 UI 层（qapp fixture），不依赖 GPU/真实媒体。
"""

from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.edit_core

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QHideEvent, QIcon
from PyQt6.QtWidgets import QLabel, QLineEdit

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TransitionInstance,
)
from src.features.edit.ui.edit_page import EditPage
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel


class _Recorder:
    """记录信号全部参数。"""

    def __init__(self, signal) -> None:
        self.calls: list[tuple] = []
        signal.connect(lambda *args: self.calls.append(args))


# ================================================================
# AUD-19622 — AiVariantList 时长换算不再硬编码 24.0 fps
# ================================================================

class TestAud19622VariantDuration:
    def _duration_label(self, list_widget) -> str:
        card = list_widget._container_layout.itemAt(0).widget()
        for lbl in card.findChildren(QLabel):
            if "时长" in lbl.text():
                return lbl.text()
        return ""

    def test_seconds_use_project_fps(self, qapp):
        from src.features.edit.ai_editing.assistant_events import VariantCard
        from src.features.edit.ui.ai_variant_list import AiVariantList

        w = AiVariantList()
        w.set_variants(
            (VariantCard(variant_id="v1", label="A", duration_frames=720),),
            fps=30.0,
        )
        # 720 / 30 = 24.0s（旧代码按 24fps 显示 30.0s）
        assert "24" in self._duration_label(w)

    def test_unknown_fps_shows_frames_not_wrong_seconds(self, qapp):
        from src.features.edit.ai_editing.assistant_events import VariantCard
        from src.features.edit.ui.ai_variant_list import AiVariantList

        w = AiVariantList()
        w.set_variants(
            (VariantCard(variant_id="v1", label="A", duration_frames=720),),
            fps=None,
        )
        text = self._duration_label(w)
        # 不再猜测帧率：显示帧数
        assert "720" in text
        assert "30.0" not in text

    def test_zero_duration_shows_zero_seconds(self, qapp):
        from src.features.edit.ai_editing.assistant_events import VariantCard
        from src.features.edit.ui.ai_variant_list import AiVariantList

        w = AiVariantList()
        w.set_variants((VariantCard(variant_id="v1", label="A"),), fps=25.0)
        assert "0" in self._duration_label(w)


# ================================================================
# AUD-19720 — from_dict 降级必须留下日志
# ================================================================

class TestAud19720AdapterDegradationLogged:
    def test_corrupt_params_log_warning_and_fall_back(self, qapp, caplog):
        from src.features.edit.ui.clip_audio_effect_dialog_adapter import (
            ClipAudioEffectDialogManager,
        )

        mgr = ClipAudioEffectDialogManager()
        logger_name = "src.features.edit.ui.clip_audio_effect_dialog_adapter"
        with caplog.at_level("WARNING", logger=logger_name):
            dialog = mgr.open_dialog(
                effect_type="reverb",
                clip_id="clip_1",
                effect_id="fx_1",
                params_dict={"decay": 99.0},  # 超出 [0.1, 10] → ValueError
            )
            assert dialog is not None
            dialog.close()
        joined = "\n".join(r.message for r in caplog.records)
        assert "clip_1" in joined and "fx_1" in joined
        assert "DEFAULT" in joined  # 降级事实必须可见


# ================================================================
# AUD-19801/19802/19803 — edit_effects_panel
# ================================================================

_MINIMAL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


class TestAud19801HoverPreviewOwnership:
    def test_popup_and_movie_parented_to_card(self, qapp, tmp_path):
        from src.features.edit.ui.edit_effects_panel import _MotionGraphicPreviewButton

        gif = tmp_path / "preview.gif"
        gif.write_bytes(_MINIMAL_GIF)
        card = _MotionGraphicPreviewButton("mg:t", "T", "D", gif, None)
        card._show_preview()
        assert card._preview_popup is not None
        assert card._preview_movie is not None
        # 关键契约：popup 与 movie 都挂在本卡片下，卡片销毁时一并销毁。
        assert card._preview_popup.parent() is card
        assert card._preview_movie.parent() is card
        card._hide_preview()
        assert card._preview_popup is None

    def test_hide_event_closes_preview(self, qapp, tmp_path):
        """tree.clear() 销毁路径兜底：hideEvent 必须关闭悬停预览。"""
        from src.features.edit.ui.edit_effects_panel import _MotionGraphicPreviewButton

        gif = tmp_path / "preview.gif"
        gif.write_bytes(_MINIMAL_GIF)
        card = _MotionGraphicPreviewButton("mg:t", "T", "D", gif, None)
        card._show_preview()
        assert card._preview_popup is not None
        card.hideEvent(QHideEvent())
        assert card._preview_popup is None
        assert card._preview_movie is None

    def test_populate_closes_hover_previews_before_clear(self, qapp, tmp_path):
        from src.features.edit.ui.edit_effects_panel import (
            EditEffectsPanel,
            _MotionGraphicPreviewButton,
        )

        panel = EditEffectsPanel()
        gif = tmp_path / "preview.gif"
        gif.write_bytes(_MINIMAL_GIF)
        card = _MotionGraphicPreviewButton("mg:t", "T", "D", gif, None)
        card.setParent(panel.video_effects_tree.viewport())  # 模拟悬停中的卡片
        card._show_preview()
        assert card._preview_popup is not None
        panel._populate_video_effects(panel.video_effects_tree)
        assert card._preview_popup is None  # clear 前已被面板级遍历关闭


class TestAud19802RegistryListenerThreadAffinity:
    def test_foreign_thread_defers_and_coalesces(self, qapp):
        from src.features.edit.ui.edit_effects_panel import EditEffectsPanel

        panel = EditEffectsPanel()
        worker = threading.Thread(target=panel._refresh_motion_graphic_generators)
        worker.start()
        worker.join()
        # 排队投递后：重建未发生（等 UI 线程事件循环），pending 置位。
        assert panel._motion_graphic_refresh_pending is True
        # 第二次跨线程回调被合并（同一 pending 标志）。
        worker = threading.Thread(target=panel._refresh_motion_graphic_generators)
        worker.start()
        worker.join()
        assert panel._motion_graphic_refresh_pending is True
        # 模拟事件循环送达 queued invocation。
        panel._refresh_motion_graphic_generators_in_ui_thread()
        assert panel._motion_graphic_refresh_pending is False
        assert panel.video_effects_tree.topLevelItemCount() > 0  # 重建发生

    def test_ui_thread_refresh_is_direct(self, qapp):
        from src.features.edit.ui.edit_effects_panel import EditEffectsPanel

        panel = EditEffectsPanel()
        panel._refresh_motion_graphic_generators()  # 测试线程 == 面板线程
        assert panel._motion_graphic_refresh_pending is False
        assert panel.video_effects_tree.topLevelItemCount() > 0


class TestAud19803SearchDebounceAndIconCache:
    def test_keystroke_only_starts_timer(self, qapp):
        from src.features.edit.ui.edit_effects_panel import EditEffectsPanel

        panel = EditEffectsPanel()
        full_count = panel.video_effects_tree.topLevelItemCount()
        assert full_count > 1  # 全量树有多个分类
        panel.search_input.setText("blur")  # 每键只重启定时器，不立即重建
        assert panel._search_debounce_timer.isActive()
        assert panel.video_effects_tree.topLevelItemCount() == full_count  # 未重建
        # 定时器到期 → 单次重建为过滤结果（单一 filter 分类）。
        panel._search_debounce_timer.timeout.emit()
        panel._search_debounce_timer.stop()  # 手动 emit 不停表，补齐语义
        assert not panel._search_debounce_timer.isActive()
        assert panel.video_effects_tree.topLevelItemCount() == 1

    def test_icon_cache_hits(self, qapp):
        from src.features.edit.ui import edit_effects_panel

        effect_icon_first = edit_effects_panel._effect_preview_icon("blur")
        before = edit_effects_panel._effect_preview_icon.cache_info().hits
        again = edit_effects_panel._effect_preview_icon("blur")
        info = edit_effects_panel._effect_preview_icon.cache_info()
        assert info.hits == before + 1
        assert isinstance(again, QIcon)
        assert again is effect_icon_first  # 缓存命中返回同一实例


# ================================================================
# AUD-19900/19901/19902 — edit_page
# ================================================================

class TestAud19900SubtitleLanguageCommand:
    def test_source_no_longer_calls_missing_method(self, qapp):
        from src.features.edit.ui import edit_page as ep_mod

        assert not hasattr(ep_mod.EditPage, "_execute_subtitle_command")
        source = inspect.getsource(ep_mod.EditPage._on_subtitle_language_requested)
        assert "timeline_view.execute_command" in source
        # 旧缺陷是对不存在方法的调用（注释里提及不算）。
        assert "self._execute_subtitle_command" not in source

    def test_slot_executes_command_without_attribute_error(self, qapp):
        from src.features.edit.ui import edit_page as ep_mod

        page = ep_mod.EditPage()
        track = MagicMock()
        track.track_type = "subtitle"
        track.caption_metadata = None
        timeline = MagicMock()
        timeline.get_track_by_id.return_value = track
        view = MagicMock()
        view._timeline = timeline
        panel = MagicMock()
        panel.get_timeline_view.return_value = view
        page._timeline_panel = panel

        with patch(
            "PyQt6.QtWidgets.QInputDialog.getText", return_value=("zh-CN", True)
        ):
            page._on_subtitle_language_requested("sub-track-1")

        view.execute_command.assert_called_once()
        cmd = view.execute_command.call_args[0][0]
        assert cmd.updates == {"language": "zh-CN"}


class _PreviewGateShim(QObject):
    """绑定 EditPage 方法、只携带门控所需状态的最小载体。"""

    timeline_frame_requested = pyqtSignal(int)

    def __init__(self, active: bool) -> None:
        super().__init__()
        self._current_frame = 42
        self._is_active = active
        # 隐藏场景必须真实模拟多页宿主（current_page != "Edit"）；
        # 无宿主的独立页面视为可见（与 _preview_page_is_active 契约一致）。
        self._main_window = None if active else SimpleNamespace(
            current_page="Color"
        )


_PreviewGateShim._preview_page_is_active = EditPage._preview_page_is_active
_PreviewGateShim._preview_refresh_allowed = EditPage._preview_refresh_allowed
_PreviewGateShim.request_current_timeline_frame = (
    EditPage.request_current_timeline_frame
)


class TestAud19901HiddenPageGate:
    def test_hidden_page_does_not_emit_frame_request(self, qapp):
        shim = _PreviewGateShim(active=False)
        emitted: list[int] = []
        shim.timeline_frame_requested.connect(emitted.append)
        shim.request_current_timeline_frame()
        assert emitted == []

    def test_active_page_emits_frame_request(self, qapp):
        shim = _PreviewGateShim(active=True)
        emitted: list[int] = []
        shim.timeline_frame_requested.connect(emitted.append)
        shim.request_current_timeline_frame()
        assert emitted == [42]

    def test_standalone_page_without_host_still_emits(self, qapp):
        """无多页宿主的独立页面（单测/独立嵌入）不可能是隐藏背景页。"""
        shim = _PreviewGateShim(active=False)
        shim._main_window = None
        emitted: list[int] = []
        shim.timeline_frame_requested.connect(emitted.append)
        shim.request_current_timeline_frame()
        assert emitted == [42]


class _FakeTimelineForSceneCut:
    """给 _map_scene_cut_candidates_for_overlay 用的最小时间线。"""

    def __init__(self, video_tracks):
        self.video_tracks = tuple(video_tracks)

    def get_clip_by_id(self, clip_id):
        for track in self.video_tracks:
            for clip in track.clips:
                if clip.clip_id == clip_id:
                    return clip
        return None


class TestAud19902SceneCutCandidateMapping:
    def _candidate(self, frame: int):
        from src.features.edit.core.scene_cut.models import SceneCutCandidate

        return SceneCutCandidate(
            source_frame=frame, pts=None, seconds=frame / 24.0
        )

    def _analysis(self, asset_id="a1", clip_id=None, frames=(35,)):
        return SimpleNamespace(
            asset_id=asset_id,
            clip_id=clip_id,
            candidates=tuple(self._candidate(f) for f in frames),
        )

    def test_source_frame_maps_through_clip_source_start(self, qapp):
        page = EditPage()
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=100, timeline_duration=50,
            source_start=10, source_duration=50,
        )
        page._current_timeline = _FakeTimelineForSceneCut(
            [EditTrack(track_id="v1", track_type="video", clips=(clip,))]
        )
        mapped = page._map_scene_cut_candidates_for_overlay(self._analysis())
        assert len(mapped) == 1
        assert mapped[0].timeline_frame == 125  # 100 + (35 - 10)
        assert mapped[0].source_frame == 35

    def test_asset_not_on_timeline_drops_all(self, qapp):
        page = EditPage()
        clip = ClipInstance(
            clip_id="c1", asset_id="other", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        page._current_timeline = _FakeTimelineForSceneCut(
            [EditTrack(track_id="v1", track_type="video", clips=(clip,))]
        )
        assert page._map_scene_cut_candidates_for_overlay(self._analysis()) == ()

    def test_out_of_range_candidate_dropped(self, qapp):
        page = EditPage()
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        page._current_timeline = _FakeTimelineForSceneCut(
            [EditTrack(track_id="v1", track_type="video", clips=(clip,))]
        )
        mapped = page._map_scene_cut_candidates_for_overlay(
            self._analysis(frames=(500,))
        )
        assert mapped == ()

    def test_pre_mapped_candidates_pass_through(self, qapp):
        """已是时间线坐标的候选直接透传（无时间线也不丢弃）。"""
        from src.features.edit.core.scene_cut.models import SceneCutCandidate

        page = EditPage()
        pre = SceneCutCandidate(
            source_frame=24, pts=None, seconds=1.0, timeline_frame=10
        )
        page._current_timeline = None
        mapped = page._map_scene_cut_candidates_for_overlay(
            SimpleNamespace(asset_id=None, clip_id=None, candidates=(pre,))
        )
        assert mapped == (pre,)

    def test_clip_id_binding_wins(self, qapp):
        page = EditPage()
        clip_a = ClipInstance(
            clip_id="ca", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        clip_b = ClipInstance(
            clip_id="cb", asset_id="a1", track_id="v2",
            timeline_start=200, timeline_duration=10,
            source_start=30, source_duration=10,
        )
        page._current_timeline = _FakeTimelineForSceneCut([
            EditTrack(track_id="v1", track_type="video", clips=(clip_a,)),
            EditTrack(track_id="v2", track_type="video", clips=(clip_b,)),
        ])
        # analysis 绑定 cb：帧 35 落在 cb 的源范围 [30, 39] 内。
        mapped = page._map_scene_cut_candidates_for_overlay(
            self._analysis(clip_id="cb", frames=(35,))
        )
        assert len(mapped) == 1
        assert mapped[0].timeline_frame == 205  # 200 + (35 - 30)


# ================================================================
# edit_properties_panel 公共 fixture
# ================================================================

def _mock_clip(effects=(), clip_id="test-clip", asset_id="asset-1") -> MagicMock:
    clip = MagicMock()
    clip.clip_id = clip_id
    clip.transform = MagicMock(
        translate_x=0.0, translate_y=0.0,
        scale_x=1.0, scale_y=1.0, rotation=0.0,
        pivot_mode="center", pivot_x=0.0, pivot_y=0.0,
    )
    clip.crop = MagicMock(
        left=0.0, right=0.0, top=0.0, bottom=0.0, feather=0.0,
    )
    clip.opacity = 1.0
    clip.speed = MagicMock(speed=1.0, reverse=False, method="linear")
    clip.effects = effects
    # W07：MagicMock 的 is_param_animated 属性自动成 Mock（恒真）——
    # R10 关键帧路径会劫持静态 transform 路由（提前 return，不走
    # _emit_transform_param 的 scale lock 联动）。静态路径测试须显式
    # 声明参数未动画。
    clip.is_param_animated = lambda _name: False
    clip.asset_id = asset_id
    clip.name = "Test"
    clip.timeline_start = 0
    clip.timeline_end = 100
    return clip


# ================================================================
# AUD-20001 — Reset 写入被重置的 section（而非全局选中特效）
# ================================================================

class TestAud20001EffectRoutingBySectionId:
    def test_param_routes_to_section_effect_id(self, qapp):
        from src.features.edit.core.effect_registry import get_effect_registry

        reg = get_effect_registry()
        blur_a = reg.get_definition("blur").create_instance(order=0).with_effect_id("fx-2")
        blur_b = reg.get_definition("blur").create_instance(order=1).with_effect_id("fx-9")
        panel = EditPropertiesPanel()
        panel.set_clip(_mock_clip(effects=(blur_a, blur_b)))
        # 模拟"选中态失效"（如刚删除过特效）——旧代码此时 Reset 完全无效，
        # 或把参数写到全局选中特效上。
        panel._selected_effect_id = None
        rec = _Recorder(panel.effect_param_changed)
        panel._on_section_param_changed(
            "clip:test-clip:effect:fx-2", "blur_amount", 0.5
        )
        assert rec.calls == [("test-clip", "fx-2", "blur_amount", 0.5)]

        rec2 = _Recorder(panel.effect_param_changed)
        panel._on_section_param_changed(
            "clip:test-clip:effect:fx-9", "blur_amount", 0.25
        )
        assert rec2.calls == [("test-clip", "fx-9", "blur_amount", 0.25)]


# ================================================================
# AUD-20002 — scale lock 联动同步对侧滑块
# ================================================================

class TestAud20002SyncPairedSlider:
    def test_slider_peer_synced_when_scale_locked(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_clip(_mock_clip())
        panel._scale_locked = True
        section = panel._sections["clip:test-clip:transform"]
        info_y = section._param_widgets["scale_y"]
        initial_slider = info_y["slider"].value()
        rec = _Recorder(panel.clip_param_changed)
        panel._on_section_param_changed(
            "clip:test-clip:transform", "scale_x", 2.0
        )
        assert info_y["widget"].value() == pytest.approx(2.0)
        expected = panel._value_to_slider(2.0, info_y)
        assert expected != initial_slider  # 初始 1.0 对应的滑块位
        assert info_y["slider"].value() == expected
        assert len(rec.calls) == 1  # 联动在 _updating 锁内，不产生第二条命令


# ================================================================
# AUD-20003 — 链锁下拖 DragValue 同步自身滑块
# ================================================================

class TestAud20003EffectXyOwnSliderSync:
    def test_chained_drag_updates_own_and_peer_widgets(self, qapp):
        from src.features.composite.nodes.base_node import ParamDef
        from src.shared.ui.widgets import DragValue
        from src.shared.ui.widgets.topos_controls import ToposClickableSlider

        panel = EditPropertiesPanel()
        row = panel._create_effect_xy_row(
            _mock_clip(),
            "fx-xy",
            ParamDef(name="radius_x", ptype="float", min_val=0.0,
                     max_val=10.0, step=0.5, decimals=1, default=1.0),
            ParamDef(name="radius_y", ptype="float", min_val=0.0,
                     max_val=10.0, step=0.5, decimals=1, default=1.0),
            1.0,
            1.0,
        )
        drags = row.findChildren(DragValue)
        sliders = row.findChildren(ToposClickableSlider)
        assert len(drags) == 2 and len(sliders) == 2
        drag_x, drag_y = drags[0], drags[1]
        slider_x, slider_y = sliders[0], sliders[1]
        assert slider_x.value() == 2  # 初始 1.0 → (1.0-0)/0.5
        rec = _Recorder(panel.effect_params_changed)
        drag_x.setValue(3.0)  # 链锁默认 ON
        # 自身滑块同步（旧代码链锁分支不同步自身滑块）
        assert slider_x.value() == 6  # (3.0 - 0) / 0.5
        # 对侧 drag+slider 同步
        assert drag_y.value() == pytest.approx(3.0)
        assert slider_y.value() == 6
        assert len(rec.calls) == 1
        clip_id, effect_id, pairs = rec.calls[0]
        assert (clip_id, effect_id) == ("test-clip", "fx-xy")
        assert ("radius_x", 3.0) in pairs and ("radius_y", 3.0) in pairs


# ================================================================
# AUD-20004 / AUD-20007 — Solid Color 重接线与 Reset
# ================================================================

_SOLID_DEFAULTS = {
    "mode": "checker",
    "color_r": 0.1, "color_g": 0.2, "color_b": 0.3, "color_a": 1.0,
    "color2_r": 1.0, "color2_g": 1.0, "color2_b": 1.0, "color2_a": 1.0,
    "checker_size": 64,
    "start_x": 0.0, "start_y": 0.5, "end_x": 1.0, "end_y": 0.5,
    "center_x": 0.5, "center_y": 0.5, "radial_radius": 0.5,
    "feather": 0.0,
}


def _solid_panel(qapp, params=None) -> EditPropertiesPanel:
    panel = EditPropertiesPanel()
    panel.set_clip(
        _mock_clip(),
        media_type="solid",
        solid_color=(0.1, 0.2, 0.3),
        generator_params=dict(params if params is not None else _SOLID_DEFAULTS),
    )
    return panel


class TestAud20004SolidNumericRewire:
    def test_drag_keeps_slider_in_sync(self, qapp):
        panel = _solid_panel(qapp)
        section = panel._sections["clip:test-clip:solid_color"]
        info = section._param_widgets["checker_size"]
        drag, slider = info["widget"], info["slider"]
        rec = _Recorder(panel.generator_params_changed)
        drag.setValue(120)
        assert slider.value() == 120  # 旧代码：滑块停在 64
        assert rec.calls
        clip_id, asset_id, params = rec.calls[-1]
        assert (clip_id, asset_id) == ("test-clip", "asset-1")
        assert params["checker_size"] == 120

    def test_slider_keeps_drag_in_sync(self, qapp):
        panel = _solid_panel(qapp)
        section = panel._sections["clip:test-clip:solid_color"]
        info = section._param_widgets["checker_size"]
        drag, slider = info["widget"], info["slider"]
        rec = _Recorder(panel.generator_params_changed)
        slider.setValue(200)
        assert drag.value() == 200
        assert rec.calls
        assert rec.calls[-1][2]["checker_size"] == 200

    def test_gradient_rows_rewired(self, qapp):
        params = dict(_SOLID_DEFAULTS, mode="gradient", start_x=0.25)
        panel = _solid_panel(qapp, params=params)
        section = panel._sections["clip:test-clip:solid_color"]
        info = section._param_widgets["start_x"]
        drag, slider = info["widget"], info["slider"]
        rec = _Recorder(panel.generator_params_changed)
        drag.setValue(1.0)
        assert slider.value() == panel._value_to_slider(1.0, info)
        assert rec.calls and rec.calls[-1][2]["start_x"] == 1.0


class TestAud20007SolidReset:
    def test_reset_emits_default_params_once(self, qapp):
        panel = _solid_panel(qapp)
        section = panel._sections["clip:test-clip:solid_color"]
        # 先改一个值（此 emit 不计入 reset 断言）。
        section._param_widgets["checker_size"]["widget"].setValue(300)
        rec = _Recorder(panel.generator_params_changed)
        panel._on_section_reset("clip:test-clip:solid_color")
        assert len(rec.calls) == 1
        clip_id, asset_id, params = rec.calls[0]
        assert (clip_id, asset_id) == ("test-clip", "asset-1")
        assert params["mode"] == "solid"
        assert params["checker_size"] == 64
        assert panel._current_generator_params["mode"] == "solid"
        # section 被重建（新实例，mode-aware 行回到默认 mode）。
        new_section = panel._sections["clip:test-clip:solid_color"]
        assert new_section is not section
        assert new_section.parameters["mode"] == "solid"


# ================================================================
# AUD-20008 — Transition / Subtitle Reset
# ================================================================

class TestAud20008TransitionReset:
    def test_video_transition_reset_emits_defaults_and_syncs_widgets(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_transition(TransitionInstance(
            transition_id="tr1", transition_type="cross_dissolve",
            track_id="v1", left_clip_id="c0", right_clip_id="c1",
            duration=30, alignment="end_on_cut",
            params={"curve": "ease_in", "direction": "up", "softness": 0.3},
        ))
        rec = _Recorder(panel.transition_param_changed)
        panel._on_section_reset("transition:tr1")
        emitted = {call[1]: call[2] for call in rec.calls}
        assert emitted == {
            "duration": 12, "alignment": "centered",
            "curve": "linear", "direction": "left", "softness": 0.02,
        }
        section = panel._sections["transition:tr1"]
        dur_info = section._param_widgets["duration"]
        assert dur_info["widget"].value() == 12
        assert dur_info["slider"].value() == 12

    def test_audio_transition_reset_skips_spatial_params(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_transition(TransitionInstance(
            transition_id="tr2", transition_type="audio:constant_power",
            track_id="a1", left_clip_id="c0", right_clip_id="c1",
            duration=30, alignment="start_on_cut",
        ))
        rec = _Recorder(panel.transition_param_changed)
        panel._on_section_reset("transition:tr2")
        emitted = {call[1]: call[2] for call in rec.calls}
        assert emitted == {"duration": 12, "alignment": "centered"}

    def test_foreign_transition_reset_ignored(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_transition(TransitionInstance(
            transition_id="tr1", transition_type="cross_dissolve",
            track_id="v1", left_clip_id="c0", right_clip_id="c1", duration=30,
        ))
        rec = _Recorder(panel.transition_param_changed)
        panel._on_section_reset("transition:some-other-id")
        assert rec.calls == []


def _sub_clip(text: str = "hello", **overrides) -> SubtitleClip:
    fields = dict(
        clip_id="sub-1", track_id="s1",
        timeline_start=0, timeline_duration=72, text=text,
    )
    fields.update(overrides)
    return SubtitleClip(**fields)


class TestAud20008SubtitleReset:
    def test_text_section_reset_emits_and_clears_editor(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_subtitle_clip(_sub_clip(text="keep this"), 24.0)
        rec = _Recorder(panel.subtitle_param_changed)
        panel._on_section_reset("subtitle:sub-1:s1:text")
        assert rec.calls == [("sub-1", "s1", "text", "")]
        assert panel._subtitle_text_edit.toPlainText() == ""

    def test_font_section_reset_emits_defaults(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_subtitle_clip(
            _sub_clip(font_family="Helvetica", italic=True), 24.0
        )
        rec = _Recorder(panel.subtitle_param_changed)
        panel._on_section_reset("subtitle:sub-1:s1:font")
        emitted = {call[2]: call[3] for call in rec.calls}
        assert emitted["font_family"] == "Arial"
        assert emitted["italic"] is False


# ================================================================
# AUD-20005 — 字幕 Family 单一提交入口
# ================================================================

class TestAud20005FamilyCommit:
    def _family_combo(self, panel):
        section = panel._sections["subtitle:sub-1:s1:font"]
        return section._param_widgets["font_family"]["widget"]

    def test_selection_commits_once_after_debounce(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_subtitle_clip(_sub_clip(), 24.0)
        combo = self._family_combo(panel)
        if combo.count() < 2:
            pytest.skip("系统字体不足 2 个，跳过下拉选择路径")
        rec = _Recorder(panel.subtitle_param_changed)
        # 模拟从下拉选择：Qt 会同时发 currentIndexChanged + editTextChanged。
        combo.setCurrentIndex(0 if combo.currentIndex() != 0 else 1)
        assert rec.calls == []  # debounce 窗口内不提交
        assert panel._subtitle_family_timer.isActive()
        panel._subtitle_family_timer.timeout.emit()  # 模拟 250ms 到期
        assert len(rec.calls) == 1  # 旧代码：一次选择 = 两条命令
        assert rec.calls[0][:2] == ("sub-1", "s1")

    def test_typing_coalesced_into_single_commit(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_subtitle_clip(_sub_clip(), 24.0)
        combo = self._family_combo(panel)
        rec = _Recorder(panel.subtitle_param_changed)
        combo.setCurrentText("Z")       # 每键只重启 debounce
        combo.setCurrentText("Ze")
        combo.setCurrentText("Zent")
        assert rec.calls == []
        panel._subtitle_family_timer.timeout.emit()
        assert len(rec.calls) == 1  # 旧代码：每字符一条命令
        assert rec.calls[0][3] == "Zent"

    def test_same_value_not_recommitted(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_subtitle_clip(_sub_clip(font_family="Arial"), 24.0)
        combo = self._family_combo(panel)
        rec = _Recorder(panel.subtitle_param_changed)
        combo.setCurrentText("Arial")  # 与 last_committed 相同
        panel._subtitle_family_timer.timeout.emit()
        assert rec.calls == []


# ================================================================
# AUD-20006 — 轨道名重命名去抖
# ================================================================

class TestAud20006TrackNameDebounce:
    def _name_edit(self, panel) -> QLineEdit:
        section = panel._sections["track:v1"]
        edits = section.findChildren(QLineEdit)
        assert edits
        return edits[0]

    def test_typing_commits_single_rename(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_track(EditTrack(track_id="v1", name="V1", track_type="video"))
        edit = self._name_edit(panel)
        rec = _Recorder(panel.track_param_changed)
        edit.setText("T")
        edit.setText("Tr")
        edit.setText("Track A")
        assert rec.calls == []  # debounce 窗口内逐字符不提交
        panel._track_name_timer.timeout.emit()
        assert rec.calls == [("v1", "name", "Track A")]  # 旧代码：3 条命令

    def test_empty_name_never_committed(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_track(EditTrack(track_id="v1", name="V1", track_type="video"))
        edit = self._name_edit(panel)
        rec = _Recorder(panel.track_param_changed)
        edit.setText("   ")
        panel._track_name_timer.timeout.emit()
        assert rec.calls == []

    def test_editing_finished_flushes(self, qapp):
        panel = EditPropertiesPanel()
        panel.set_track(EditTrack(track_id="v1", name="V1", track_type="video"))
        edit = self._name_edit(panel)
        rec = _Recorder(panel.track_param_changed)
        edit.setText("Final")
        edit.editingFinished.emit()
        assert rec.calls == [("v1", "name", "Final")]
        assert not panel._track_name_timer.isActive()
