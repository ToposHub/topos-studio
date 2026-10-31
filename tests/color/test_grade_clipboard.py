"""P1-COLOR-16：分类 Copy/Paste Grade 验收测试。

计划表验收标准：
- 从一个 clip 复制时可按类别选择（primary/curves/HSL/mixer/LUT/windows/
  qualifier/tracking/effects/plugins/RAW）
- 一次粘贴一个 undo（UpdateColorStackCommand 单命令）
- 不相关字段不被意外覆盖（未选类别保持原值）
- 粘贴可预览变更清单（describe）
"""

import numpy as np
import pytest

from src.features.color.core.grade_clipboard import (
    ALL_CATEGORIES,
    GRADE_CATEGORY_KEYS,
    GRADE_CATEGORY_LABELS,
    GradeClipboard,
)
from src.features.color.core.layer import Layer
from src.shared.project.color_stack import ColorStackData


def _rich_layer(name: str = "src") -> Layer:
    """构造带全部参数组数据的图层。"""
    layer = Layer(name)
    layer.grading_params["lift"] = (0.05, -0.02, 0.01)
    layer.grading_params["gain"] = (1.1, 1.0, 0.95)
    layer.curves_data = {"M": [[0, 0], [128, 140], [255, 255]],
                         "R": [[0, 0], [255, 250]]}
    layer.hsl_curve_data = {"hue": [[0, 0], [255, 255]]}
    layer.mixer_params = {"r_out": [1.0, 0.1, 0.0]}
    layer.warper_params = np.linspace(-0.05, 0.05, 108).astype(np.float32) \
        .reshape(6, 6, 3)
    layer.effects_params = {"enabled": True, "sharpen": 25}
    layer.active_lut_path = "/tmp/test.cube"
    layer.lut_intensity = 0.65
    layer.soft_clip_params = {"highlight": 0.8}
    layer.qualifier_params = {"h_low": 0.3, "h_high": 0.5}
    layer.plugins = [{"instance_id": "p1", "plugin_id": "blur",
                      "params": {}, "enabled": True}]
    return layer


class TestGradeClipboardCategories:
    def test_category_registry_covers_plan(self):
        """类别注册表覆盖计划表要求的全部类别。"""
        expected = {"primary", "curves", "hsl", "mixer", "lut", "windows",
                    "qualifier", "effects", "plugins", "tracking", "raw"}
        assert expected <= ALL_CATEGORIES
        for cat in ALL_CATEGORIES:
            assert cat in GRADE_CATEGORY_LABELS, f"{cat} 缺 UI 标签"

    def test_copy_captures_all_nonempty_categories(self):
        """复制富图层后，非空类别全部进入 captured 集合。"""
        clip = GradeClipboard()
        clip.copy_grade([_rich_layer()],
                        trackings={"t1": {"frames": [1, 2]}},
                        raw_params={"exposure": 0.5})
        assert {"primary", "curves", "hsl", "mixer", "warper", "effects",
                "lut", "soft_clip", "qualifier", "plugins",
                "tracking", "raw"} <= clip.copied_categories

    def test_describe_lists_copied_categories(self):
        """粘贴预览清单按注册表顺序列出已捕获类别（人类可读）。"""
        clip = GradeClipboard()
        clip.copy_grade([_rich_layer()])
        desc = clip.describe()
        assert any("基础调色" in d for d in desc)
        assert any("LUT" in d for d in desc)
        assert any("窗口/蒙版" in d for d in desc)
        # 未捕获的 tracking/raw 不在清单
        assert not any("跟踪" in d for d in desc)

    def test_snapshot_isolated_from_source(self):
        """快照与源层隔离：复制后修改源不影响粘贴结果。"""
        src = _rich_layer()
        clip = GradeClipboard()
        clip.copy_grade([src])
        src.grading_params["lift"] = (0.9, 0.9, 0.9)
        src.active_lut_path = "/changed.cube"

        tgt = Layer("tgt")
        clip.paste_onto_layers([tgt])
        assert tgt.grading_params["lift"] == (0.05, -0.02, 0.01)
        assert tgt.active_lut_path == "/tmp/test.cube"


class TestSelectivePaste:
    def test_unselected_categories_not_touched(self):
        """核心验收：未选类别不被意外覆盖。"""
        src = _rich_layer()
        clip = GradeClipboard()
        clip.copy_grade([src])

        tgt = _rich_layer()
        tgt.grading_params["lift"] = (0.5, 0.5, 0.5)      # 目标已有值
        tgt.qualifier_params = {"h_low": 0.9}
        tgt.plugins = [{"instance_id": "keep", "plugin_id": "keep"}]
        tgt.effects_params = {"enabled": False, "sharpen": 0}  # 目标独有值

        clip.paste_onto_layers([tgt], categories={"lut"})

        # LUT 类别被应用
        assert tgt.active_lut_path == "/tmp/test.cube"
        assert tgt.lut_intensity == 0.65
        # 其他类别保持目标原值
        assert tgt.grading_params["lift"] == (0.5, 0.5, 0.5)
        assert tgt.qualifier_params["h_low"] == 0.9
        assert tgt.plugins[0]["plugin_id"] == "keep"
        assert tgt.effects_params == {"enabled": False, "sharpen": 0}

    def test_primary_paste_replaces_whole_group(self):
        """primary 是整组语义：源的全部 primary 键覆盖目标（DaVinci 行为）。"""
        src = _rich_layer()
        clip = GradeClipboard()
        clip.copy_grade([src])

        tgt = Layer("tgt")
        tgt.grading_params["gain"] = (0.2, 0.2, 0.2)
        clip.paste_onto_layers([tgt], categories={"primary"})
        assert tgt.grading_params["gain"] == (1.1, 1.0, 0.95)

    def test_curves_paste_rebuilds_lut(self):
        """粘贴 curves 类别时重建曲线 LUT（与 from_dict 同路径）。"""
        src = _rich_layer()
        src.curves_lut = None  # 只靠 curves_data
        clip = GradeClipboard()
        clip.copy_grade([src])

        tgt = Layer("tgt")
        clip.paste_onto_layers([tgt], categories={"curves"})
        assert tgt.curves_data == src.curves_data
        assert tgt.curves_lut is not None
        assert tgt.curves_lut.shape[-1] == 3

    def test_warper_ndarray_restored(self):
        """warper 类别粘贴后恢复为数值 ndarray。"""
        src = _rich_layer()
        clip = GradeClipboard()
        clip.copy_grade([src])
        tgt = Layer("tgt")
        clip.paste_onto_layers([tgt], categories={"warper"})
        assert isinstance(tgt.warper_params, np.ndarray)
        assert tgt.warper_params.shape == (6, 6, 3)
        np.testing.assert_allclose(tgt.warper_params, src.warper_params,
                                   atol=1e-6)

    def test_single_source_broadcasts_to_multiple_targets(self):
        """单层源广播到多层目标（多图层粘贴语义）。"""
        src = _rich_layer()
        clip = GradeClipboard()
        clip.copy_grade([src])
        targets = [Layer("a"), Layer("b"), Layer("c")]
        applied = clip.paste_onto_layers(targets, categories={"primary", "lut"})
        assert applied == 3
        for t in targets:
            assert t.grading_params["lift"] == (0.05, -0.02, 0.01)
            assert t.active_lut_path == "/tmp/test.cube"

    def test_empty_clipboard_paste_is_noop(self):
        clip = GradeClipboard()
        tgt = Layer("tgt")
        assert clip.paste_onto_layers([tgt]) == 0
        assert clip.is_empty()

    def test_unknown_categories_ignored(self):
        """不在注册表中的类别被静默忽略（不崩溃）。"""
        src = _rich_layer()
        clip = GradeClipboard()
        clip.copy_grade([src], categories={"primary", "nonexistent"})
        tgt = Layer("tgt")
        clip.paste_onto_layers([tgt], categories={"nonexistent"})
        assert tgt.grading_params["lift"] != (0.05, -0.02, 0.01)


class TestStackLevelChannels:
    def test_tracking_and_raw_roundtrip(self):
        """栈级类别（tracking/raw）复制并应用。"""
        clip = GradeClipboard()
        clip.copy_grade(
            [Layer("l")],
            trackings={"clipA": {"frames": [0, 1, 2]}},
            raw_params={"exposure": 1.2, "wb": 5600},
        )
        assert "tracking" in clip.copied_categories
        assert "raw" in clip.copied_categories
        assert clip.trackings == {"clipA": {"frames": [0, 1, 2]}}
        assert clip.raw_params["exposure"] == 1.2

    def test_empty_stack_data_not_captured(self):
        """空 tracking/raw 不进入捕获（粘贴清单不虚报）。"""
        clip = GradeClipboard()
        clip.copy_grade([Layer("l")], trackings={}, raw_params={})
        assert "tracking" not in clip.copied_categories
        assert "raw" not in clip.copied_categories
        assert clip.trackings is None
        assert clip.raw_params is None


class TestUpdateColorStackTrackingsChannel:
    """trackings 通道贯穿 project.update_color_stack 与 undo 命令。"""

    def test_project_update_and_command_undo(self):
        from src.features.edit.core.commands import UpdateColorStackCommand
        from src.features.edit.core.project import EditProject

        project, stack_id = EditProject.create(name="t").create_color_stack()

        cmd = UpdateColorStackCommand.create(
            stack_id=stack_id,
            trackings={"layer0": {"frames": [5, 6]}},
        )
        p2 = cmd.apply(project)
        assert p2.color_stacks[stack_id].trackings == {
            "layer0": {"frames": [5, 6]}}

        p3 = cmd.undo(p2)
        assert p3.color_stacks[stack_id].trackings == (
            project.color_stacks[stack_id].trackings)

    def test_command_none_trackings_preserves(self):
        """trackings=None 时命令不触碰既有跟踪数据。"""
        from src.features.edit.core.commands import UpdateColorStackCommand
        from src.features.edit.core.project import EditProject

        project, stack_id = EditProject.create(name="t").create_color_stack()
        p1 = project.update_color_stack(stack_id, trackings={"k": {"f": [1]}})
        cmd = UpdateColorStackCommand.create(stack_id=stack_id,
                                             grading_params={"gain": (1, 1, 1)})
        p2 = cmd.apply(p1)
        assert p2.color_stacks[stack_id].trackings == {"k": {"f": [1]}}


# ---------------------------------------------------------------------------
# 复查修复回归：tracking 持久化接线 + 全局剪贴板隔离
# ---------------------------------------------------------------------------

class TestTrackingPersistedInUnifiedPaths:
    """P0-COLOR-02 复查：trackings 必须进入统一保存/提交路径。"""

    def test_extract_full_color_data_includes_trackings(self):
        from types import SimpleNamespace
        from src.shared.project.save_router import SaveRouter

        fake_store = SimpleNamespace(
            to_dict=lambda: {"b1": {"frames": [1, 2]}})
        engine = SimpleNamespace(
            layers=[], active_layer_index=0,
            current_raw_params={}, tracking_store=fake_store)
        data = SaveRouter._extract_full_color_data_from_engine(None, engine)
        assert data.get("trackings") == {"b1": {"frames": [1, 2]}}

    def test_extract_without_store_or_empty_omits_key(self):
        from types import SimpleNamespace
        from src.shared.project.save_router import SaveRouter

        engine = SimpleNamespace(
            layers=[], active_layer_index=0,
            current_raw_params={}, tracking_store=None)
        data = SaveRouter._extract_full_color_data_from_engine(None, engine)
        assert "trackings" not in data

        engine.tracking_store = SimpleNamespace(to_dict=lambda: {})
        data = SaveRouter._extract_full_color_data_from_engine(None, engine)
        assert "trackings" not in data  # 空 payload 不写空键


def test_auto_color_timeline_path_does_not_touch_global_clipboard():
    """P1-COLOR-21 复查：Auto Color 用临时剪贴板，不覆盖用户已复制内容。"""
    from types import SimpleNamespace
    from src.app.controllers.layer_controller import LayerOpsMixin
    from src.features.color.core.grade_clipboard import GradeClipboard

    class _Host(LayerOpsMixin):
        pass

    host = _Host()
    active_layer = Layer("active")
    host.engine = SimpleNamespace(
        layers=[active_layer], active_layer_index=0)
    host.color_timeline_clip_strip = SimpleNamespace(_active_clip_id="c1")
    host.status_bar = SimpleNamespace(showMessage=lambda *a, **k: None)

    captured = {}
    def _paste(clip_id, categories, clipboard=None):
        captured["clipboard"] = clipboard
        return True
    host._paste_grade_to_clip = _paste

    # 预置"用户已复制"的全局剪贴板
    global_clip = GradeClipboard()
    donor = Layer("donor")
    donor.grading_params["gain"] = (0.2, 0.2, 0.2)
    global_clip.copy_grade([donor])
    before_categories = set(global_clip.copied_categories)
    before_describe = list(global_clip.describe())
    host._get_grade_clipboard = lambda: global_clip

    suggestion = SimpleNamespace(
        temp=0.0, tint=0.0, exposure=1.5, contrast=0.0,
        saturation=0.0, absolute=True)
    host._apply_auto_suggestion(suggestion, {"exposure"})

    # 粘贴用的是临时剪贴板（不是全局实例），且含已捕获类别
    assert captured["clipboard"] is not global_clip
    assert "primary" in captured["clipboard"].copied_categories
    # 全局剪贴板保持用户内容不被写脏（类别集合与快照值均不变）
    assert global_clip.copied_categories == before_categories
    assert global_clip.describe() == before_describe



# ---------------------------------------------------------------------------
# P1-COLOR-16 复查：多目标粘贴（Paste to All Clips）
# ---------------------------------------------------------------------------

def _clipboard_with_primary():
    from src.features.color.core.grade_clipboard import GradeClipboard
    clip = GradeClipboard()
    donor = Layer("donor")
    donor.grading_params["gain"] = (0.1, 0.1, 0.1)
    clip.copy_grade([donor])
    return clip


def _resolve_paste_all_handler():
    """从 edit_controller 模块解析 _on_grade_paste_all_requested 未绑定方法。"""
    from src.app.controllers import edit_controller as ec_mod
    for name in dir(ec_mod):
        obj = getattr(ec_mod, name)
        if isinstance(obj, type) and hasattr(obj, "_on_grade_paste_all_requested"):
            return obj._on_grade_paste_all_requested
    raise AssertionError("host class with handler not found")


class TestPasteGradeToAllClips:
    def test_applies_to_every_video_clip_skipping_audio(self, monkeypatch):
        """批量粘贴遍历全部视频 clip（跳过音频轨），类别沿用对话框结果。"""
        from types import SimpleNamespace
        import src.features.color.ui.grade_paste_dialog as gpd

        monkeypatch.setattr(
            gpd.GradePasteDialog, "get_categories",
            staticmethod(lambda parent, avail: {"primary"}))

        timeline = SimpleNamespace(all_tracks=[
            SimpleNamespace(track_type="video", clips=[
                SimpleNamespace(clip_id="c1"),
                SimpleNamespace(clip_id="c2")]),
            SimpleNamespace(track_type="audio", clips=[
                SimpleNamespace(clip_id="a1")]),
        ])
        pasted = []
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=timeline),
            status_bar=SimpleNamespace(showMessage=lambda *a, **k: None),
            _get_grade_clipboard=_clipboard_with_primary,
        )
        host._find_timeline_clip = (
            lambda cid: SimpleNamespace(
                clip_id=cid, asset_id="a", color_stack_id=f"s-{cid}"))
        host._schedule_idle_color_timeline_clip_strip_refresh = lambda: None

        def _paste(clip_id, categories, clipboard=None, command_sink=None):
            pasted.append((clip_id, set(categories)))
            return True
        host._paste_grade_to_clip = _paste

        _resolve_paste_all_handler()(host, "t1")
        assert [cid for cid, _ in pasted] == ["c1", "c2"]
        assert pasted[0][1] == {"primary"}

    def test_empty_clipboard_is_noop(self):
        from types import SimpleNamespace
        from src.features.color.core.grade_clipboard import GradeClipboard

        called = []
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=None),
            status_bar=SimpleNamespace(showMessage=lambda *a, **k: None),
            _get_grade_clipboard=lambda: GradeClipboard(),
        )
        host._paste_grade_to_clip = lambda *a, **k: called.append(a)
        _resolve_paste_all_handler()(host, "t1")
        assert called == []

    def test_no_timeline_is_noop(self):
        from types import SimpleNamespace
        called = []
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=None),
            status_bar=SimpleNamespace(showMessage=lambda *a, **k: None),
            _get_grade_clipboard=_clipboard_with_primary,
        )
        host._paste_grade_to_clip = lambda *a, **k: called.append(a)
        _resolve_paste_all_handler()(host, "t1")
        assert called == []

    def test_batch_paste_is_single_undo_step(self, monkeypatch):
        """批量粘贴提交一条 ProjectMacroCommand：一次 undo 恢复全部镜头。

        P1-COLOR-16 残留收口：多目标粘贴从每镜头一步 undo 改为 macro
        单步（同 media_pool 的 _paste_assets 模式）。
        """
        from types import MethodType, SimpleNamespace
        import src.features.color.ui.grade_paste_dialog as gpd
        from src.app.controllers import edit_controller as ec_mod
        from src.features.edit.core.commands import ProjectCommandStack
        from src.features.edit.core.project import EditProject

        monkeypatch.setattr(
            gpd.GradePasteDialog, "get_categories",
            staticmethod(lambda parent, avail: {"primary"}))

        p0, stack_a = EditProject.create(name="t").create_color_stack()
        project, stack_b = p0.create_color_stack()
        clips = {
            "c1": SimpleNamespace(clip_id="c1", asset_id="a1",
                                  color_stack_id=stack_a),
            "c2": SimpleNamespace(clip_id="c2", asset_id="a2",
                                  color_stack_id=stack_b),
        }
        timeline = SimpleNamespace(all_tracks=[
            SimpleNamespace(track_type="video",
                            clips=[clips["c1"], clips["c2"]]),
            SimpleNamespace(track_type="audio", clips=[
                SimpleNamespace(clip_id="a1")]),
        ])
        stack = ProjectCommandStack()
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=timeline),
            status_bar=SimpleNamespace(showMessage=lambda *a, **k: None),
            _get_grade_clipboard=_clipboard_with_primary,
            _current_edit_project=project,
            media_pool_panel=SimpleNamespace(get_command_stack=lambda: stack),
            edit_project_dirty=False,
        )
        host._find_timeline_clip = lambda cid: clips.get(cid)
        host._schedule_idle_color_timeline_clip_strip_refresh = lambda: None
        host._grade_clip_layers = MethodType(
            ec_mod.EditControllerMixin._grade_clip_layers, host)
        host._paste_grade_to_clip = MethodType(
            ec_mod.EditControllerMixin._paste_grade_to_clip, host)

        _resolve_paste_all_handler()(host, "t1")

        # 粘贴生效：两个镜头的栈都拿到剪贴板里的 primary gain
        after = host._current_edit_project
        for sid in (stack_a, stack_b):
            assert after.color_stacks[sid].grading_params.get("gain") == (
                0.1, 0.1, 0.1)
        assert host.edit_project_dirty is True

        # 单步 undo：一次回退恢复全部镜头
        undone = stack.undo(after)
        assert undone is not None
        for sid in (stack_a, stack_b):
            assert undone.color_stacks[sid].grading_params == (
                project.color_stacks[sid].grading_params)
        assert stack.undo(undone) is None  # undo 栈已空 → 确实只有一步

        # 单步 redo：一次重做恢复粘贴结果
        redone = stack.redo(undone)
        for sid in (stack_a, stack_b):
            assert redone.color_stacks[sid].grading_params.get("gain") == (
                0.1, 0.1, 0.1)


class TestVideoDenoiseStackChannel:
    """P1-COLOR-13: per-clip 视频降噪参数随调色栈持久化。"""

    STACK_VD = {"spatial": {"enabled": True}, "temporal": {"enabled": False}}

    def test_command_channel_and_undo(self):
        from src.features.edit.core.commands import UpdateColorStackCommand
        from src.features.edit.core.project import EditProject

        project, stack_id = EditProject.create(name="t").create_color_stack()
        cmd = UpdateColorStackCommand.create(
            stack_id=stack_id, video_denoise=dict(self.STACK_VD))
        p2 = cmd.apply(project)
        assert p2.color_stacks[stack_id].video_denoise == self.STACK_VD

        p3 = cmd.undo(p2)
        assert p3.color_stacks[stack_id].video_denoise is None

    def test_command_none_preserves_existing(self):
        from src.features.edit.core.commands import UpdateColorStackCommand
        from src.features.edit.core.project import EditProject

        project, stack_id = EditProject.create(name="t").create_color_stack()
        p1 = project.update_color_stack(
            stack_id, video_denoise=dict(self.STACK_VD))
        cmd = UpdateColorStackCommand.create(
            stack_id=stack_id, grading_params={"gain": (1, 1, 1)})
        p2 = cmd.apply(p1)
        assert p2.color_stacks[stack_id].video_denoise == self.STACK_VD

    def test_save_router_extraction(self):
        from types import SimpleNamespace
        from src.shared.project.save_router import SaveRouter

        engine = SimpleNamespace(
            layers=[], active_layer_index=0, current_raw_params={},
            tracking_store=None,
            video_denoise_params=SimpleNamespace(
                to_dict=lambda: dict(self.STACK_VD)))
        data = SaveRouter._extract_full_color_data_from_engine(None, engine)
        assert data.get("video_denoise") == self.STACK_VD

        engine.video_denoise_params = None  # 从未开启 → 不写字段
        data = SaveRouter._extract_full_color_data_from_engine(None, engine)
        assert "video_denoise" not in data
