"""SR06：抓帧快照冻结 grade 载荷与脚本 coordinator 登记（可落地核心）。

对应《第二轮复查计划》SR06（2026-11-10）已交付部分：
- prepare 阶段冻结 grade 序列化载荷（layers／raw_params／active index／
  tracking／stabilization 只读视图）、合成图数据与源素材路径——commit
  全程消费快照，不再读 live engine（覆盖对话框／脚本重入窗口内的
  grade 改动不混装进本次 bundle）；
- 脚本抓帧路径与 UI 路径登记到**同一** coordinator（关闭/取消可中断
  所有尚未 commit 的请求）。

未交付部分（独立源视频权威帧 probe、TOCTOU 深化）在完成报告如实声明。
"""
from __future__ import annotations

import copy
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.color.core.gallery_capture_service import (
    CaptureFrameSnapshot,
)


def _layer(name: str = "prepare-time layer"):
    return SimpleNamespace(name=name, params={"exposure": 1.0})


def _make_ctrl(tmp_path, monkeypatch):
    from src.app.controllers.layer_controller import LayerOpsMixin

    monkeypatch.chdir(tmp_path)
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    import numpy as np

    image = np.zeros((16, 16, 3), dtype=np.uint8) + 96
    ctrl.engine = SimpleNamespace(
        img_high=image, color_graph_revision=7,
        layers=[_layer()], active_layer_index=0,
        current_raw_params={"wb": 5200})
    ctrl.engine.grab_viewport_image = lambda *a, **k: image
    ctrl.engine.render_full_image_for_scope = MagicMock(return_value=None)
    ctrl.status_bar = MagicMock()
    ctrl.current_file_is_video = False
    ctrl.current_file_path = "/tmp/source-image.png"
    ctrl.save_router = MagicMock()
    ctrl.save_router._save_single_asset_project.side_effect = (
        lambda path, *_a, **_k: open(path, "wb").write(b"PK-fake") or True)
    presets_root = os.path.join(os.getcwd(), "Presets")
    os.makedirs(os.path.join(presets_root, "User"), exist_ok=True)
    os.makedirs(os.path.join(presets_root, "System"), exist_ok=True)
    from src.features.color.core.gallery_repository import (
        GalleryFolderRepository,
    )
    ctrl.presets_panel = MagicMock()
    ctrl.presets_panel.get_current_folder.return_value = os.path.join(
        presets_root, "User")
    ctrl.presets_panel._repository = GalleryFolderRepository(presets_root)
    ctrl.presets_panel.presets_root = presets_root
    ctrl._page_context_manager = SimpleNamespace(
        get_current_context=lambda: SimpleNamespace(
            clip_id="c1", asset_id="a1", timeline_frame=5, source_frame=9,
            timeline_id="t1"))
    ctrl._current_edit_project = None
    ctrl._get_composite_graph_data = lambda: {"graph": "prepare"}
    ctrl._get_composite_state_data = lambda: {"state": "prepare"}
    ctrl._new_still_name = lambda: "Still_sr06"
    return ctrl, presets_root


class TestFrozenGradePayload:
    def test_snapshot_freezes_grade_and_context(self, tmp_path, monkeypatch):
        ctrl, _ = _make_ctrl(tmp_path, monkeypatch)
        snapshot = ctrl._prepare_capture_image(False)
        assert snapshot is not None
        assert snapshot.frozen_engine is not None
        assert snapshot.frozen_engine.layers[0].name == \
            "prepare-time layer"
        assert snapshot.frozen_engine.current_raw_params == {"wb": 5200}
        assert snapshot.source_asset_path == "/tmp/source-image.png"
        assert snapshot.composite_graph_data == {"graph": "prepare"}
        assert snapshot.grade_revision == 7

    def test_live_engine_mutation_does_not_leak_into_commit(
            self, tmp_path, monkeypatch):
        """prepare 后 live grade 被改：落盘仍用冻结副本（同代际）。"""
        ctrl, _ = _make_ctrl(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox",
            MagicMock())
        snapshot = ctrl._prepare_capture_image(False)
        # 模拟覆盖对话框窗口内的 live 改动：换 layer + 换源路径 + 换合成图
        ctrl.engine.layers = [_layer("live-mutated layer")]
        ctrl.engine.current_raw_params = {"wb": 9999}
        ctrl.current_file_path = "/tmp/changed-after-prepare.png"
        ctrl._get_composite_graph_data = lambda: {"graph": "live"}
        ctrl._get_composite_state_data = lambda: {"state": "live"}

        saved_calls = []
        ctrl.save_router._save_single_asset_project.side_effect = (
            lambda path, image_path, engine, **kw:
            saved_calls.append((image_path, engine, kw))
            or open(path, "wb").write(b"PK-fake") or True)
        assert ctrl._commit_still_capture(
            os.path.join(os.getcwd(), "Presets", "User",
                         "Still_sr06.topos"),
            snapshot, False, None)
        assert saved_calls, "SaveRouter must be called"
        image_path, engine, kwargs = saved_calls[0]
        assert engine is snapshot.frozen_engine      # 冻结视图，非 live
        assert engine.layers[0].name == "prepare-time layer"
        assert engine.current_raw_params == {"wb": 5200}
        assert image_path == "/tmp/source-image.png"  # 源路径同批冻结
        assert kwargs["composite_graph_data"] == {"graph": "prepare"}
        assert kwargs["composite_viewer_state"] == {"state": "prepare"}

    def test_legacy_input_without_snapshot_keeps_live_engine(
            self, tmp_path, monkeypatch):
        """无快照的兼容入口：回退 live 读（已声明的遗留路径）。"""
        ctrl, _ = _make_ctrl(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox",
            MagicMock())
        saved_calls = []
        ctrl.save_router._save_single_asset_project.side_effect = (
            lambda path, image_path, engine, **kw:
            saved_calls.append((image_path, engine))
            or open(path, "wb").write(b"PK-fake") or True)
        legacy_image = ctrl._prepare_capture_image(False).image
        assert ctrl._commit_still_capture(
            os.path.join(os.getcwd(), "Presets", "User",
                         "Still_sr06.topos"),
            legacy_image, False, None)
        assert saved_calls[0][1] is ctrl.engine


class TestFrozenViewIsolation:
    def test_deep_copy_isolates_layers(self):
        from src.app.controllers.layer_controller import (
            _FrozenEngineGradeView,
        )
        layer = _layer()
        engine = SimpleNamespace(
            layers=[layer], active_layer_index=0,
            current_raw_params={"a": 1})
        view = _FrozenEngineGradeView(engine)
        layer.name = "mutated after freeze"
        engine.current_raw_params["a"] = 2
        engine.active_layer_index = 5
        assert view.layers[0].name == "prepare-time layer"
        assert view.current_raw_params == {"a": 1}
        assert view.active_layer_index == 0

    def test_snapshot_defaults_back_compatible(self):
        snapshot = CaptureFrameSnapshot(image=object())
        assert snapshot.frozen_engine is None
        assert snapshot.source_asset_path is None
        assert snapshot.composite_graph_data is None


class TestScriptCoordinatorRegistration:
    def test_script_capture_registers_and_finishes_session(
            self, tmp_path, monkeypatch):
        ctrl, _ = _make_ctrl(tmp_path, monkeypatch)
        coordinator = MagicMock()
        ctrl._gallery_capture_coordinator = lambda: coordinator
        ctrl._refresh_gallery_after_capture = lambda folder: None
        path = ctrl.script_capture_still("Scripted")
        assert path is not None and path.endswith("Scripted.topos")
        assert coordinator.register.call_count == 1
        assert coordinator.finish.call_count == 1
        registered = coordinator.register.call_args[0][0]
        finished = coordinator.finish.call_args[0][0]
        assert registered is finished          # 同一会话的登记/注销配对

    def test_ui_path_still_registers_once(self, tmp_path, monkeypatch):
        ctrl, _ = _make_ctrl(tmp_path, monkeypatch)
        coordinator = MagicMock()
        ctrl._gallery_capture_coordinator = lambda: coordinator
        ctrl._refresh_gallery_after_capture = lambda folder: None
        ctrl._make_capture_playback_restorer = lambda playing: (lambda: None)
        ctrl.on_capture_still()
        assert coordinator.register.call_count == 1
        assert coordinator.finish.call_count == 1
