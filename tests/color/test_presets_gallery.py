"""Gallery（P1-COLOR-04，并入 Presets 面板）测试。

覆盖：
- preset_meta：sidecar（.still.jpg / .meta.json）读写、路径约定、搜索匹配
- PresetsPanel：扫描含 sidecar 的预设（tooltip/搜索）、右键菜单新信号、
  rename/delete 搬移清理 sidecar
- 控制器：on_capture_still 写 sidecar（含元数据）、分类应用
  （timeline 单 undo 通道 / standalone 单 history push）、参考帧加载
- 真实素材：4K ProRes 抓帧 → sidecar 往返
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.preset_meta import (
    PresetMeta, load_preset_meta, load_still_image, matches_query,
    meta_path_for, save_preset_meta, still_path_for, thumb_path_for,
    write_still_images,
)


def _img(h=48, w=64, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, 3)) * 255).astype(np.uint8)


# ─────────────────────────────────────────────────────────────────────
# preset_meta sidecar
# ─────────────────────────────────────────────────────────────────────

class TestPresetMeta:
    def test_sidecar_roundtrip(self, tmp_path):
        topos = str(tmp_path / "Still_1.topos")
        topos_path = tmp_path / "Still_1.topos"
        topos_path.write_bytes(b"zip placeholder")
        meta = PresetMeta(
            display_name="A001_C001.mov", clip_id="c1", asset_id="a1",
            timeline_frame=120, source_frame=88,
            source_transfer="pq", source_primaries="bt2020",
            source_range="limited", created_at="2026-08-27T10:00:00")
        assert save_preset_meta(topos, meta)
        loaded = load_preset_meta(topos)
        assert loaded == meta
        # to_dict 剔除 None 字段
        assert "clip_id" in meta.to_dict()

    def test_missing_or_broken_sidecar_is_none(self, tmp_path):
        topos = str(tmp_path / "old.topos")
        (tmp_path / "old.topos").write_bytes(b"x")
        assert load_preset_meta(topos) is None  # 旧预设无 sidecar
        (tmp_path / "old.meta.json").write_text("{ broken")
        assert load_preset_meta(topos) is None
        assert load_preset_meta(str(tmp_path / "ghost.topos")) is None

    def test_unknown_fields_ignored(self, tmp_path):
        topos = str(tmp_path / "p.topos")
        (tmp_path / "p.topos").write_bytes(b"x")
        (tmp_path / "p.meta.json").write_text(
            json.dumps({"schema": 1, "display_name": "n", "evil": 1}))
        assert load_preset_meta(topos).display_name == "n"

    def test_path_conventions(self):
        assert meta_path_for("/a/b/Still_1.topos").endswith("Still_1.meta.json")
        assert still_path_for("/a/b/Still_1.topos").endswith("Still_1.still.jpg")
        assert thumb_path_for("/a/b/Still_1.topos").endswith("Still_1.thumb.jpg")

    def test_describe(self):
        meta = PresetMeta(display_name="A001", source_frame=42,
                          source_primaries="bt2020", source_transfer="pq")
        text = meta.describe()
        assert "A001" in text and "42" in text and "bt2020" in text

    def test_write_still_images(self, tmp_path):
        topos = str(tmp_path / "s.topos")
        thumb, still = write_still_images(topos, _img(h=1080, w=1920, seed=1))
        assert thumb and os.path.isfile(thumb)
        assert still and os.path.isfile(still)
        img = load_still_image(topos)
        assert img is not None and img.dtype == np.uint8
        assert max(img.shape[:2]) <= 1920
        assert max(cv2_shape(thumb)) <= 512

    def test_load_still_falls_back_to_thumb(self, tmp_path):
        topos = str(tmp_path / "s2.topos")
        write_still_images(topos, _img(seed=2), thumb_only=True)
        assert not os.path.exists(still_path_for(topos))
        img = load_still_image(topos)
        assert img is not None

    def test_load_still_missing(self, tmp_path):
        assert load_still_image(str(tmp_path / "none.topos")) is None

    def test_matches_query(self):
        meta = PresetMeta(display_name="A001_C001.mov",
                          source_transfer="pq", source_primaries="bt2020")
        assert matches_query("sunset", meta, "SUN")
        assert matches_query("x", meta, "a001")
        assert matches_query("x", meta, "pq")
        assert not matches_query("sunset", meta, "night")
        assert matches_query("sunset", None, "")
        # 无 sidecar 的旧预设仍可按名字搜
        assert matches_query("Night", None, "night")


def cv2_shape(path):
    import cv2
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    assert img is not None
    return img.shape[:2]


# ─────────────────────────────────────────────────────────────────────
# PresetsPanel（扫描 / 搜索 / 菜单信号 / sidecar 管理）
# ─────────────────────────────────────────────────────────────────────

class TestPresetsPanelGallery:
    def _make_panel(self, tmp_path, monkeypatch):
        # AUD-25514（锁定旧行为协议）：预设根目录不再跟随 os.getcwd()
        # （换启动目录预设"丢失"，审计判定缺陷），改为稳定应用数据目录。
        # 隔离意图不变——直接把根目录解析指到 tmp_path，避免读写真实
        # AppDataLocation。chdir 保留以隔离迁移探测的 cwd。
        monkeypatch.chdir(tmp_path)
        from src.features.color.ui import presets_panel
        monkeypatch.setattr(
            presets_panel, "_resolve_presets_root",
            lambda: str(tmp_path / "Presets"))
        panel = presets_panel.PresetsPanel()
        return panel

    def _make_preset(self, folder, name, meta=None, with_still=True):
        topos = os.path.join(folder, f"{name}.topos")
        with open(topos, "wb") as f:
            f.write(b"zip")
        write_still_images(topos, _img(seed=abs(hash(name)) % 1000),
                           thumb_only=not with_still)
        if meta is not None:
            save_preset_meta(topos, meta)
        return topos

    def test_scan_with_sidecar_tooltip_and_search(self, qtbot, tmp_path, monkeypatch):
        panel = self._make_panel(tmp_path, monkeypatch)
        qtbot.addWidget(panel)
        folder = panel.get_current_folder()
        self._make_preset(folder, "Sunset",
                          PresetMeta(display_name="A001.mov", source_frame=42))
        self._make_preset(folder, "Night")
        panel.scan_presets()
        assert panel.list_widget.count() == 2

        # tooltip 含元数据
        item = panel.list_widget.item(0)  # 排序后 Night 在前
        sunset = panel.list_widget.item(1)
        assert sunset.text() == "Sunset"
        assert "A001.mov" in (sunset.toolTip() or "")
        assert sunset.data(0x0100 + 1) is not None  # UserRole+1 = meta
        assert item.data(0x0100 + 1) is None       # Night 无 sidecar

        # 搜索：名字 / 来源片段名
        panel.search_edit.setText("a001")
        assert panel.list_widget.item(1).isHidden() is False
        assert panel.list_widget.item(0).isHidden() is True
        panel.search_edit.setText("night")
        assert panel.list_widget.item(0).isHidden() is False
        assert panel.list_widget.item(1).isHidden() is True
        panel.search_edit.setText("")
        assert all(not panel.list_widget.item(i).isHidden()
                   for i in range(2))

    def test_rescan_replays_search_filter(self, qtbot, tmp_path, monkeypatch):
        panel = self._make_panel(tmp_path, monkeypatch)
        qtbot.addWidget(panel)
        folder = panel.get_current_folder()
        self._make_preset(folder, "Alpha", PresetMeta(display_name="zzz"))
        self._make_preset(folder, "Beta")
        panel.scan_presets()
        panel.search_edit.setText("alpha")
        panel.scan_presets()  # 重扫后过滤仍生效
        hidden = [panel.list_widget.item(i).isHidden()
                  for i in range(panel.list_widget.count())]
        assert hidden == [False, True]  # Alpha 可见，Beta 隐藏

    def test_context_menu_signals(self, qtbot, tmp_path, monkeypatch):
        panel = self._make_panel(tmp_path, monkeypatch)
        qtbot.addWidget(panel)
        folder = panel.get_current_folder()
        topos = self._make_preset(folder, "P1", PresetMeta(display_name="x"))
        panel.scan_presets()
        item = panel.list_widget.item(0)
        panel.list_widget.setCurrentItem(item)

        # 直接验证信号存在并发射路径负载（菜单 exec 在无头环境不可用）
        with qtbot.waitSignal(panel.apply_categories_requested) as blocker:
            panel.apply_categories_requested.emit(
                item.data(0x0100))
        assert blocker.args[0] == topos
        with qtbot.waitSignal(panel.reference_requested) as blocker:
            panel.reference_requested.emit(item.data(0x0100))
        assert blocker.args[0] == topos
        assert hasattr(panel, 'apply_categories_requested')
        assert hasattr(panel, 'reference_requested')

    def test_rename_moves_sidecars(self, qtbot, tmp_path, monkeypatch):
        panel = self._make_panel(tmp_path, monkeypatch)
        qtbot.addWidget(panel)
        folder = panel.get_current_folder()
        topos = self._make_preset(folder, "Old",
                                  PresetMeta(display_name="x"))
        panel.scan_presets()
        item = next(panel.list_widget.item(i)
                    for i in range(panel.list_widget.count())
                    if panel.list_widget.item(i).text() == "Old")

        class _FakeInput:
            @staticmethod
            def getText(*a, **k):
                return "New", True

        from PyQt6.QtWidgets import QInputDialog
        monkeypatch.setattr(QInputDialog, "getText", _FakeInput.getText)
        panel._rename_preset(item)

        assert os.path.isfile(os.path.join(folder, "New.topos"))
        assert os.path.isfile(os.path.join(folder, "New.thumb.jpg"))
        assert os.path.isfile(os.path.join(folder, "New.still.jpg"))
        assert os.path.isfile(os.path.join(folder, "New.meta.json"))
        assert not os.path.exists(topos)

    def test_delete_removes_sidecars(self, qtbot, tmp_path, monkeypatch):
        panel = self._make_panel(tmp_path, monkeypatch)
        qtbot.addWidget(panel)
        folder = panel.get_current_folder()
        topos = self._make_preset(folder, "Doomed", PresetMeta(display_name="x"))

        class _FakeBox:
            StandardButton = __import__(
                "PyQt6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.StandardButton
            @staticmethod
            def question(*a, **k):
                return _FakeBox.StandardButton.Yes

        from PyQt6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "question", _FakeBox.question)
        panel._delete_preset_files(topos)

        for suffix in (".topos", ".thumb.jpg", ".still.jpg", ".meta.json"):
            assert not os.path.exists(
                os.path.join(folder, f"Doomed{suffix}"))


# ─────────────────────────────────────────────────────────────────────
# 控制器
# ─────────────────────────────────────────────────────────────────────

class _EngineStub:
    def __init__(self, layers, image):
        self.layers = layers
        self.img_high = image
        self.img_full_original = image
        self.active_layer_index = 0
        self.tracking_store = None
        self.current_raw_params = None
        self.renderer = None
        self.history = MagicMock()
        self.loaded_reference = None

    def grab_viewport_image(self, target_size=None):
        return self.img_high

    def get_current_image(self):
        return self.img_high

    def get_active_layer(self):
        return self.layers[self.active_layer_index] if self.layers else None

    def load_reference_still(self, image):
        self.loaded_reference = image
        return image is not None


def _make_ctrl(tmp_path, monkeypatch, layers, image=None, folder="User"):
    from src.app.controllers.layer_controller import LayerOpsMixin
    monkeypatch.chdir(tmp_path)
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    ctrl.engine = _EngineStub(layers, image if image is not None else _img(seed=9))
    ctrl.status_bar = MagicMock()
    # update_from_layer / on_capture_still 触达的面板与辅助（生产环境由
    # MainWindow 提供）
    for attr in ("grading_panel", "layer_panel", "plugin_panel",
                 "curves_widget", "hsl_curves_panel", "mixer_panel",
                 "effects_panel", "qualifier_panel",
                 "lut_panel", "warper_panel", "mask_panel", "info_panel"):
        setattr(ctrl, attr, MagicMock())
    ctrl._get_composite_graph_data = lambda: None
    ctrl._get_composite_state_data = lambda: None
    ctrl.current_file_is_video = False
    ctrl.save_router = MagicMock()
    # R01：保存走 staging——SaveRouter 需真把 .topos 写到传入路径
    # （事务按 staged 文件在场才安装）。需要失败场景的测试自行覆盖。
    def _fake_save(path, *_args, **_kwargs):
        with open(path, "wb") as fh:
            fh.write(b"PK-fake-topos")
        return True
    ctrl.save_router._save_single_asset_project.side_effect = _fake_save
    ctrl.presets_panel = MagicMock()
    presets_root = os.path.join(os.getcwd(), "Presets")
    presets_user = os.path.join(presets_root, folder)
    os.makedirs(presets_user, exist_ok=True)
    ctrl.presets_panel.get_current_folder.return_value = presets_user
    # G06：抓帧会话需要真实 repository 解析目标集合（MagicMock 的
    # album_id 不是 str，会被如实拒绝）。
    from src.features.color.core.gallery_repository import (
        GalleryFolderRepository,
    )
    ctrl.presets_panel._repository = GalleryFolderRepository(presets_root)
    ctrl.presets_panel.presets_root = presets_root
    ctrl._page_context_manager = SimpleNamespace(
        get_current_context=lambda: SimpleNamespace(
            clip_id="clip-1", asset_id="asset-1",
            timeline_frame=100, source_frame=64))
    ctrl._current_edit_project = SimpleNamespace(assets={
        "asset-1": SimpleNamespace(
            display_name="A001_C001.mov",
            metadata={"color_transfer": "pq", "color_primaries": "bt2020",
                      "color_range": "limited"}),
    })
    return ctrl


class TestCaptureStillSidecar:
    def test_capture_writes_sidecar_with_meta(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        layer = Layer(name="source")
        layer.grading_params["exposure"] = 0.35
        ctrl = _make_ctrl(tmp_path, monkeypatch, [layer], _img(seed=10))
        ctrl.save_router._save_single_asset_project.return_value = True
        # 跳过覆盖确认（名字带时间戳不会碰撞，但 offscreen 下 QMessageBox 不可交互）
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox",
            MagicMock())

        ctrl.on_capture_still()

        folder = ctrl.presets_panel.get_current_folder()
        files = os.listdir(folder)
        metas = [f for f in files if f.endswith(".meta.json")]
        stills = [f for f in files if f.endswith(".still.jpg")]
        thumbs = [f for f in files if f.endswith(".thumb.jpg")]
        assert len(metas) == 1 and len(stills) == 1 and len(thumbs) == 1
        assert all(f.startswith("Still_") for f in metas + stills + thumbs)

        topos = os.path.join(
            folder, metas[0].replace(".meta.json", ".topos"))
        meta = load_preset_meta(topos)
        assert meta.clip_id == "clip-1"
        assert meta.display_name == "A001_C001.mov"
        assert meta.source_transfer == "pq"
        assert meta.timeline_frame == 100
        assert meta.created_at  # 抓取时间戳

    def test_capture_without_image_is_noop(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer()])
        ctrl.engine.img_high = None
        ctrl.on_capture_still()
        assert not os.listdir(ctrl.presets_panel.get_current_folder())

    # ── CV-B07：失败不留半份 preset；视频模式暂停/恢复政策 ──────────

    def test_capture_save_failure_cleans_sidecars_no_half_preset(
            self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer(name="s")],
                          _img(seed=11))
        ctrl.save_router._save_single_asset_project.side_effect = None
        ctrl.save_router._save_single_asset_project.return_value = False
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", MagicMock())
        ctrl.on_capture_still()
        files = os.listdir(ctrl.presets_panel.get_current_folder())
        assert files == []  # thumb/still/exr 全清，Gallery 无孤儿文件
        ctrl.status_bar.showMessage.assert_called()

    def _video_ctrl(self, tmp_path, monkeypatch, *, playing: bool,
                    render_ok: bool):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer(name="v")],
                          _img(seed=12))
        ctrl.current_file_is_video = True

        class _VideoController:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.is_playing = playing

            def is_video_loaded(self) -> bool:
                return True

            def stop_playback(self) -> None:
                self.calls.append("stop")
                self.is_playing = False

            def start_playback(self) -> None:
                self.calls.append("start")

        ctrl.video_controller = _VideoController()
        # 真实签名是 tuple[bool, int]；帧本体经引擎（render 成功时已把
        # 精确帧喂给 ImageEngine，grab_viewport_image 再取）。
        ctrl._render_current_video_frame_for_grading = (
            lambda: (bool(render_ok), 42))
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", MagicMock())
        if not render_ok:
            ctrl.save_router._save_single_asset_project.side_effect = None
            ctrl.save_router._save_single_asset_project.return_value = False
        return ctrl

    def test_video_capture_render_failure_restores_playback_writes_nothing(
            self, tmp_path, monkeypatch):
        ctrl = self._video_ctrl(tmp_path, monkeypatch, playing=True,
                                render_ok=False)
        ctrl.on_capture_still()
        assert ctrl.video_controller.calls[0] == "stop"
        assert os.listdir(ctrl.presets_panel.get_current_folder()) == []

    def test_video_capture_restores_playback_only_if_was_playing(
            self, tmp_path, monkeypatch):
        from PyQt6.QtTest import QTest
        ctrl = self._video_ctrl(tmp_path, monkeypatch, playing=True,
                                render_ok=True)
        ctrl.on_capture_still()
        QTest.qWait(160)  # 100ms 恢复定时器
        assert "start" in ctrl.video_controller.calls

        ctrl2 = self._video_ctrl(tmp_path, monkeypatch, playing=False,
                                 render_ok=True)
        ctrl2.on_capture_still()
        QTest.qWait(160)
        # 用户本来暂停：抓帧后不擅自恢复播放。
        assert ctrl2.video_controller.calls == []

    def test_video_capture_uses_requested_frame_not_stale_display(
            self, tmp_path, monkeypatch):
        """抓帧经 _render_current_video_frame_for_grading 的精确帧路径，
        不直接保存显示缓存。"""
        requested: list[bool] = []

        ctrl = self._video_ctrl(tmp_path, monkeypatch, playing=False,
                                render_ok=True)
        original = ctrl._render_current_video_frame_for_grading

        def _record():
            requested.append(True)
            return original()

        ctrl._render_current_video_frame_for_grading = _record
        ctrl.on_capture_still()
        assert requested == [True]
        # 精确帧路径已走（render → 引擎 → grab），保存经 SaveRouter。
        ctrl.save_router._save_single_asset_project.assert_called_once()
        files = os.listdir(ctrl.presets_panel.get_current_folder())
        assert any(f.endswith(".thumb.jpg") for f in files)


class TestApplyPresetCategories:
    def _preset_with(self, ctrl, exposure, curve):
        """造一个含调色的 .topos（GradeClipboard 从 Layer 对象复制，
        这里直接用引擎层抓取路径生成预设并返回路径）。"""
        from src.features.color.core.layer import Layer
        src = Layer(name="src")
        src.grading_params["exposure"] = exposure
        src.curves_data = {"master": curve}
        ctrl.engine.layers = [src]
        ctrl.save_router._save_single_asset_project.return_value = True
        ctrl.on_capture_still()
        folder = ctrl.presets_panel.get_current_folder()
        topos = [f for f in os.listdir(folder) if f.endswith(".topos")][0]
        return os.path.join(folder, topos)

    def _stub_load(self, ctrl, monkeypatch, layers):
        """让 load_single_asset_data 返回给定 Layer 列表（模拟 .topos 反序列化）。"""
        ctrl.save_router.load_single_asset_data.return_value = {
            "layers": layers, "active_layer_index": 0}

    def test_standalone_apply_all_single_history_push(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        target = Layer(name="target")
        ctrl = _make_ctrl(tmp_path, monkeypatch, [target])
        src = Layer(name="src")
        src.grading_params["exposure"] = 0.8
        src.curves_data = {"master": [(0, 0.1), (1, 0.9)]}
        self._stub_load(ctrl, monkeypatch, [src])

        # categories=None 会弹对话框——standalone 测试直接给类别集合
        ctrl.on_apply_preset_categories_requested(
            "/fake/x.topos", {"primary", "curves"})

        assert target.grading_params["exposure"] == 0.8
        assert target.curves_data == {"master": [(0, 0.1), (1, 0.9)]}
        assert ctrl.engine.history.push_state.call_count == 1  # 单一 undo
        ctrl.status_bar.showMessage.assert_called()

    def test_standalone_partial_apply_keeps_other_categories(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        target = Layer(name="target")
        ctrl = _make_ctrl(tmp_path, monkeypatch, [target])
        src = Layer(name="src")
        src.grading_params["exposure"] = 0.4
        src.curves_data = {"master": [(0, 0.2), (1, 0.8)]}
        self._stub_load(ctrl, monkeypatch, [src])

        ctrl.on_apply_preset_categories_requested("/fake/x.topos", {"primary"})

        assert target.grading_params["exposure"] == 0.4
        assert target.curves_data == {}  # 未选类别不被触碰

    def test_dialog_cancel_aborts(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer()])
        self._stub_load(ctrl, monkeypatch, [Layer()])

        class _Dialog:
            @staticmethod
            def get_categories(parent, available):
                return None  # 用户取消

        import src.features.color.ui.grade_paste_dialog as gpd
        monkeypatch.setattr(gpd, "GradePasteDialog", _Dialog)
        ctrl.on_apply_preset_categories_requested("/fake/x.topos", None)
        assert ctrl.engine.history.push_state.call_count == 0

    def test_timeline_clip_routes_to_paste_command(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer()])
        self._stub_load(ctrl, monkeypatch, [Layer()])
        ctrl.color_timeline_clip_strip = SimpleNamespace(_active_clip_id="c9")
        ctrl._paste_grade_to_clip = MagicMock(return_value=True)

        ctrl.on_apply_preset_categories_requested(
            "/fake/x.topos", {"primary", "windows"})

        args = ctrl._paste_grade_to_clip.call_args
        assert args.args[0] == "c9"
        assert set(args.args[1]) == {"primary", "windows"}
        assert ctrl.engine.history.push_state.call_count == 0  # 不走引擎 history

    def test_no_target_loaded_shows_message(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer()])
        ctrl.engine.img_high = None
        ctrl.engine.img_full_original = None
        # get_current_image 返回 None → 校验失败
        ctrl.engine.get_current_image = lambda: None
        ctrl.on_apply_preset_categories_requested("/fake/x.topos", {"primary"})
        ctrl.status_bar.showMessage.assert_called_once()


class TestPresetReference:
    def test_reference_loads_image_and_syncs_canvas(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer()])
        topos = os.path.join(ctrl.presets_panel.get_current_folder(), "ref.topos")
        write_still_images(topos, _img(seed=11))
        ctrl.canvas = MagicMock()

        ctrl.on_preset_reference_requested(topos)

        assert ctrl.engine.loaded_reference is not None
        assert ctrl.engine.loaded_reference.ndim == 3
        ctrl.canvas._sync_reference_menu_states.assert_called_once()

    def test_reference_without_image_shows_message(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer()])
        ctrl.canvas = MagicMock()
        ctrl.on_preset_reference_requested(
            os.path.join(ctrl.presets_panel.get_current_folder(), "none.topos"))
        ctrl.status_bar.showMessage.assert_called_once()
        ctrl.canvas._sync_reference_menu_states.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# 真实素材：4K ProRes 帧 → sidecar 往返
# ─────────────────────────────────────────────────────────────────────

class TestRealMaterialSidecar:
    def test_prores_frame_roundtrip(self, tmp_path):
        pytest.importorskip('cv2')
        video_dir = os.path.join(os.path.dirname(__file__), "..", "test_videos")
        path = os.path.join(video_dir, "4K_prores422HQ.mov")
        if not os.path.isfile(path):
            pytest.skip("real material not available")
        import cv2
        cap = cv2.VideoCapture(path)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            pytest.skip("cv2 cannot decode this ProRes file")
        # cv2.VideoCapture 输出即 BGR，与 write_still_images 约定一致

        topos = str(tmp_path / "real.topos")
        write_still_images(topos, frame)
        still = load_still_image(topos)
        assert still is not None
        # 参考帧按 1920 上限存储：与同尺寸重采样原图比较
        h, w = still.shape[:2]
        ref = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        diff = np.abs(still.astype(np.int16) - ref.astype(np.int16))
        assert float(np.median(diff)) <= 4.0
        mse = float(np.mean(diff.astype(np.float64) ** 2))
        psnr = 10 * np.log10(255.0 ** 2 / max(mse, 1e-9))
        assert psnr > 30.0  # q90 JPEG 保真


# =====================================================================
# F4.3 · HDR 高精度参考 still（float32 EXR sidecar）
# =====================================================================

class TestHdrStillExr:
    """验收：超 1.0 高光/负值/渐变在 EXR 中保留；旧 JPEG 标明精度限制。"""

    def _hdr_frame(self) -> "np.ndarray":
        # 高光 4.5 / 负值 -0.25 / 超白 12.0 / 渐变 0..1
        grad = np.linspace(0.0, 1.0, 64, dtype=np.float32)
        frame = np.zeros((8, 64, 3), dtype=np.float32)
        frame[0, :, 0] = 4.5      # 高光超 1.0
        frame[1, :, 1] = -0.25    # 负值
        frame[2, :, 2] = 12.0     # 远超白
        frame[3, :] = np.stack([grad, grad, grad], axis=-1)
        return frame

    def test_exr_roundtrip_preserves_hdr_values(self, tmp_path):
        from src.features.color.core.preset_meta import (
            load_still_exr, write_still_exr)
        topos = str(tmp_path / "p.topos")
        path = write_still_exr(topos, self._hdr_frame())
        assert path is not None and os.path.isfile(path)
        back = load_still_exr(topos)
        assert back is not None and back.dtype == np.float32
        assert abs(back[0, 0, 0] - 4.5) < 1e-3      # 高光不裁剪
        assert abs(back[1, 0, 1] + 0.25) < 1e-3     # 负值保留
        assert abs(back[2, 0, 2] - 12.0) < 1e-2     # 远超白保留
        assert np.allclose(back[3, :, 0], np.linspace(0, 1, 64), atol=1e-5)

    def test_exr_missing_returns_none(self, tmp_path):
        from src.features.color.core.preset_meta import load_still_exr
        assert load_still_exr(str(tmp_path / "none.topos")) is None

    def test_schema2_precision_and_display_transform(self, tmp_path):
        from src.features.color.core.preset_meta import (
            PresetMeta, load_preset_meta, save_preset_meta)
        topos = str(tmp_path / "p2.topos")
        meta = PresetMeta(
            display_name="HDR shot", still_precision="float32",
            working_space="scene-linear bt709",
            display_transform={"tonemap": "reinhard", "target_peak": 1000})
        save_preset_meta(topos, meta)
        restored = load_preset_meta(topos)
        assert restored.still_precision == "float32"
        assert restored.working_space == "scene-linear bt709"
        assert restored.display_transform["target_peak"] == 1000

    def test_legacy_jpeg_precision_declared(self):
        """旧 JPEG（无 still_precision）标明精度限制，不伪称 HDR。"""
        from src.features.color.core.preset_meta import PresetMeta
        legacy = PresetMeta(display_name="old")
        assert "精度受限" in legacy.describe()
        assert "HDR" not in legacy.describe().split("精度受限")[0]
        hdr = PresetMeta(display_name="new", still_precision="float32")
        assert "HDR still (float32)" in hdr.describe()

    def test_rename_delete_move_exr_sidecar(self, qtbot, tmp_path, monkeypatch):
        """批量 rename/delete 携带 .still.exr（与既有 sidecar 行为一致）。"""
        from src.features.color.core.preset_meta import write_still_exr
        panel = TestPresetsPanelGallery()._make_panel(tmp_path, monkeypatch)
        qtbot.addWidget(panel)
        folder = panel.get_current_folder()
        topos = os.path.join(folder, "s.topos")
        with open(topos, "wb") as fh:
            fh.write(b"zip")
        assert write_still_exr(topos, self._hdr_frame())
        exr = os.path.join(folder, "s.still.exr")
        assert os.path.isfile(exr)
        panel.scan_presets()
        item = next(panel.list_widget.item(i)
                    for i in range(panel.list_widget.count())
                    if panel.list_widget.item(i).text() == "s")

        class _FakeInput:
            @staticmethod
            def getText(*a, **k):
                return "t", True

        from PyQt6.QtWidgets import QInputDialog
        monkeypatch.setattr(QInputDialog, "getText", _FakeInput.getText)
        panel._rename_preset(item)
        assert os.path.isfile(os.path.join(folder, "t.still.exr"))
        assert not os.path.isfile(exr)

        # delete 清理 EXR
        panel._delete_preset_files(os.path.join(folder, "t.topos"))
        assert not os.path.isfile(os.path.join(folder, "t.still.exr"))


# ================================================================
# M09（S12）· 参考帧最佳精度加载
# ================================================================

class TestLoadStillBest:
    def _topos(self, tmp_path, name="p1"):
        p = tmp_path / f"{name}.topos"
        p.write_text("{}", encoding="utf-8")
        return str(p)

    def test_exr_float32_preferred_with_hdr_values(self, tmp_path):
        import cv2
        from src.features.color.core.preset_meta import (
            load_still_best, still_exr_path_for,
        )
        topos = self._topos(tmp_path)
        # HDR 帧：>1.0 高光 + 负值（EXR 保留，JPEG 不可能）
        hdr = np.array([[[0.25, 4.5, -0.1]]], dtype=np.float32)
        assert cv2.imwrite(still_exr_path_for(topos), hdr)
        img, precision = load_still_best(topos)
        assert precision == "exr_float32"
        assert img.dtype == np.float32
        assert img[..., 1].max() == pytest.approx(4.5)
        assert img[..., 2].min() == pytest.approx(-0.1)

    def test_jpeg_fallback_flagged(self, tmp_path):
        import cv2
        from src.features.color.core.preset_meta import (
            load_still_best, still_path_for,
        )
        topos = self._topos(tmp_path, "p2")
        assert cv2.imwrite(
            still_path_for(topos),
            np.full((4, 4, 3), 128, dtype=np.uint8))
        img, precision = load_still_best(topos)
        assert precision == "jpeg8"
        assert img is not None and img.dtype == np.uint8

    def test_no_still_returns_none(self, tmp_path):
        from src.features.color.core.preset_meta import load_still_best
        img, precision = load_still_best(self._topos(tmp_path, "p3"))
        assert img is None and precision == "none"


class TestWriteStillExr:
    def test_float_capture_writes_exr_sidecar(self, tmp_path):
        """M09.2：抓帧端 float 帧 → .still.exr 侧车（HDR 值保留）。"""
        from src.features.color.core.preset_meta import (
            load_still_best, write_still_images,
        )
        topos = tmp_path / "cap.topos"
        topos.write_text("{}", encoding="utf-8")
        rendered = np.full((16, 16, 3), 128, dtype=np.uint8)
        hdr = np.array([[[0.25, 4.5, -0.1]] * 16] * 16, dtype=np.float32)
        thumb, still = write_still_images(
            str(topos), rendered, hdr_image=hdr)
        assert thumb and still
        # EXR 侧车存在且 HDR 值往返（load_still_best 优先消费）
        img, precision = load_still_best(str(topos))
        assert precision == "exr_float32"
        assert img.dtype == np.float32
        assert img[..., 1].max() == pytest.approx(4.5)
        assert img[..., 2].min() == pytest.approx(-0.1)

    def test_no_hdr_image_keeps_jpeg_only(self, tmp_path):
        from src.features.color.core.preset_meta import (
            load_still_best, write_still_images,
        )
        topos = tmp_path / "cap2.topos"
        topos.write_text("{}", encoding="utf-8")
        write_still_images(
            str(topos), np.full((8, 8, 3), 100, dtype=np.uint8))
        img, precision = load_still_best(str(topos))
        assert precision == "jpeg8"
