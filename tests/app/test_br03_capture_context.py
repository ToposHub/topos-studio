"""BR03：抓帧锁定目标／帧／grade／工程上下文验收。

对应《Gallery/Viewport 后端复查整改计划》BR03（2026-11-10）：
- 目的地由请求锁定（album_id → repository 解析），用户切走相册仍写回
  原相册，meta 的 album 快照同样来自请求；
- grade_revision／project_id 是真实 probe（engine.color_graph_revision／
  当前工程）且属阻断字段——发起后调 grade 或换工程 → 提交前取消；
- 覆盖确认对话框返回后再次复核上下文；
- album revision／可写性在提交前复核：变化 → 拒绝，不写半新半旧；
- persist 全链路沿用发起时的 request_id；
- JPEG／EXR 消费 prepare 的同一代际快照，persist 不二次渲染。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.features.color.core.gallery_capture_service import (
    GalleryCaptureFacts,
    GalleryCaptureSession,
)
from src.features.color.core.gallery_contracts import (
    GalleryCaptureRequest,
    GalleryOperationResult,
)
from src.features.color.core.gallery_repository import GalleryFolderRepository


def _make_ctrl(tmp_path, monkeypatch):
    from src.app.controllers.layer_controller import LayerOpsMixin

    monkeypatch.chdir(tmp_path)
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    import numpy as np

    image = (np.zeros((16, 16, 3), dtype=np.uint8) + 96)
    ctrl.engine = SimpleNamespace(img_high=image, color_graph_revision=7)
    ctrl.engine.grab_viewport_image = lambda *a, **k: image
    ctrl.engine.render_full_image_for_scope = MagicMock(return_value=None)
    ctrl.status_bar = MagicMock()
    ctrl.current_file_is_video = False
    ctrl.current_file_path = "/tmp/src.mov"
    ctrl.save_router = MagicMock()
    ctrl.save_router._save_single_asset_project.side_effect = (
        lambda path, *_a, **_k: open(path, "wb").write(b"PK-fake") or True)
    presets_root = os.path.join(os.getcwd(), "Presets")
    os.makedirs(os.path.join(presets_root, "User"), exist_ok=True)
    os.makedirs(os.path.join(presets_root, "System"), exist_ok=True)
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
    ctrl._get_composite_graph_data = lambda: None
    ctrl._get_composite_state_data = lambda: None
    ctrl._new_still_name = lambda: "Still_br03"
    return ctrl, presets_root


class TestDestinationLocked:
    def test_user_switches_album_write_goes_to_requested_album(
            self, tmp_path, monkeypatch):
        """prepare 与 persist 之间用户切走相册：仍写回请求锁定的相册。"""
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", MagicMock())
        user = os.path.join(root, "User")
        system = os.path.join(root, "System")
        request = GalleryCaptureRequest(
            request_id="br03-dest", album_id="legacy:User",
            expected_target_revision=0)
        facts = GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=7)
        written = []
        ctrl._new_still_name = lambda: "Locked"
        ctrl._commit_still_capture = (
            lambda path, *_: written.append(path) or True)
        captured: dict = {}

        def prepare():
            ctrl.presets_panel.get_current_folder.return_value = system
            return object()

        result = GalleryCaptureSession(
            request, facts, lambda: facts).run(
            prepare, lambda image: ctrl._persist_still_capture(
                image, False, captured, request=request))
        assert result.succeeded
        assert written and os.path.dirname(written[0]) == user
        assert captured["folder"] == user

    def test_meta_album_id_comes_from_request_not_ui(self, tmp_path,
                                                     monkeypatch):
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        user = os.path.join(root, "User")
        system = os.path.join(root, "System")
        request = GalleryCaptureRequest(
            request_id="br03-meta", album_id="legacy:User")
        facts = GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=7)

        def prepare():
            ctrl.presets_panel.get_current_folder.return_value = system
            return ctrl._prepare_capture_image(False)

        GalleryCaptureSession(request, facts, lambda: facts).run(
            prepare, lambda image: ctrl._persist_still_capture(
                image, False, {}, request=request))
        import json
        meta_path = os.path.join(user, "Still_br03.meta.json")
        assert os.path.isfile(meta_path)
        with open(meta_path) as fh:
            meta = json.load(fh)
        assert meta["album_id"] == "legacy:User"   # 请求锁定的相册

    def test_album_removed_rejected_without_write(self, tmp_path,
                                                  monkeypatch):
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        request = GalleryCaptureRequest(
            request_id="br03-gone", album_id="legacy:Ghost")
        facts = GalleryCaptureFacts(album_id="legacy:Ghost",
                                    grade_revision=7)
        result = GalleryCaptureSession(
            request, facts, lambda: facts).run(
            lambda: object(), lambda image:
            ctrl._persist_still_capture(image, False, {}, request=request))
        assert result.status == "rejected"
        assert result.reason == "album_unavailable"

    def test_album_revision_changed_rejected(self, tmp_path, monkeypatch):
        """发起后相册结构变化（revision 不一致）：拒绝，不写半新半旧。"""
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        user = os.path.join(root, "User")
        revision = GalleryFolderRepository(root).album_for_folder(
            user).revision
        request = GalleryCaptureRequest(
            request_id="br03-rev", album_id="legacy:User",
            expected_target_revision=revision)
        facts = GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=7)

        def prepare():
            with open(os.path.join(user, "intruder.txt"), "w") as fh:
                fh.write("album changed since request")
            return object()

        result = GalleryCaptureSession(
            request, facts, lambda: facts).run(
            prepare, lambda image: ctrl._persist_still_capture(
                image, False, {}, request=request))
        assert result.status == "rejected"
        assert result.reason == "album_revision_changed"
        assert not [n for n in os.listdir(user)
                    if n.startswith("Still_br03")]


class TestContextGuards:
    def test_grade_revision_change_cancels_before_persist(self, tmp_path,
                                                          monkeypatch):
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        request = GalleryCaptureRequest(
            request_id="br03-grade", album_id="legacy:User")
        facts = GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=3)
        persisted = []
        result = GalleryCaptureSession(
            request, facts,
            lambda: GalleryCaptureFacts(album_id="legacy:User",
                                        grade_revision=4)).run(
            lambda: object(),
            lambda image: persisted.append(image)
            or GalleryOperationResult(request_id="br03-grade",
                                      status="success"))
        assert result.status == "cancelled"
        assert "grade_revision" in result.warnings[0]
        assert persisted == []

    def test_project_switch_cancels_before_persist(self, tmp_path,
                                                   monkeypatch):
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        request = GalleryCaptureRequest(
            request_id="br03-proj", album_id="legacy:User",
            project_id="proj-a")
        facts = GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=7, project_id="proj-a")
        result = GalleryCaptureSession(
            request, facts,
            lambda: GalleryCaptureFacts(album_id="legacy:User",
                                        grade_revision=7,
                                        project_id="proj-b")).run(
            lambda: object(), lambda image: GalleryOperationResult(
                request_id="br03-proj", status="success"))
        assert result.status == "cancelled"
        assert result.reason == "context_changed"

    def test_probe_fills_real_grade_revision_and_project(
            self, tmp_path, monkeypatch):
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        ctrl._current_edit_project = SimpleNamespace(project_id="p-42")
        facts = ctrl._probe_gallery_capture_facts(False)
        assert facts.grade_revision == 7      # engine.color_graph_revision
        assert facts.project_id == "p-42"

    def test_persist_result_keeps_request_id(self, tmp_path, monkeypatch):
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", MagicMock())
        request = GalleryCaptureRequest(
            request_id="br03-rid", album_id="legacy:User")
        facts = GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=7)
        result = GalleryCaptureSession(
            request, facts, lambda: facts).run(
            lambda: ctrl._prepare_capture_image(False),
            lambda image: ctrl._persist_still_capture(
                image, False, {}, request=request))
        assert result.succeeded
        assert result.request_id == "br03-rid"


class TestSingleGenerationSnapshot:
    def test_persist_does_not_rerender_hdr(self, tmp_path, monkeypatch):
        """EXR 帧来自 prepare 快照：persist 期间二次渲染被拒绝。"""
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", MagicMock())
        snapshot = ctrl._prepare_capture_image(False)
        assert snapshot is not None and snapshot.image is not None

        def _no_live_render(*_a, **_k):
            raise AssertionError("persist must consume the prepare snapshot")

        ctrl.engine.render_full_image_for_scope = _no_live_render
        request = GalleryCaptureRequest(
            request_id="br03-snap", album_id="legacy:User")
        facts = GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=7)
        result = GalleryCaptureSession(
            request, facts, lambda: facts).run(
            lambda: snapshot, lambda image: ctrl._persist_still_capture(
                image, False, {}, request=request))
        assert result.succeeded              # 快照路径不触发二次渲染
        user = os.path.join(root, "User")
        assert os.path.isfile(os.path.join(user, "Still_br03.topos"))

    def test_overwrite_dialog_return_rechecks_context(self, tmp_path,
                                                      monkeypatch):
        """覆盖框返回 Yes 后上下文已变（grade 改动）→ 取消，不写盘。"""
        ctrl, root = _make_ctrl(tmp_path, monkeypatch)
        user = os.path.join(root, "User")
        request = GalleryCaptureRequest(
            request_id="br03-dlg", album_id="legacy:User")
        facts = GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=7)
        ctrl._new_still_name = lambda: "Still_br03"   # 触发覆盖确认
        with open(os.path.join(user, "Still_br03.topos"), "wb") as fh:
            fh.write(b"OLD")                      # 目标在场 → 弹覆盖框
        written = []
        ctrl._commit_still_capture = (
            lambda path, *_: written.append(path) or True)
        box = MagicMock()
        from PyQt6.QtWidgets import QMessageBox
        box.StandardButton = QMessageBox.StandardButton
        box.question.return_value = QMessageBox.StandardButton.Yes
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", box)

        from src.features.color.core.gallery_capture_service import (
            CaptureFrameSnapshot,
        )
        snapshot = CaptureFrameSnapshot(image=object(), hdr_frame=None)
        # probe 调用序：begin(1) → prepare 后(2) → 对话框返回后的
        # check_before_write(3)。第 3 次返回新 revision（对话框后变化）。
        probes = {"n": 0}

        def probe():
            probes["n"] += 1
            return facts if probes["n"] <= 2 else \
                GalleryCaptureFacts(album_id="legacy:User",
                                    grade_revision=9)

        session = GalleryCaptureSession(request, facts, probe)
        result = session.run(
            lambda: snapshot, lambda image: ctrl._persist_still_capture(
                image, False, {}, request=request, session=session))
        assert result.status == "cancelled"
        assert result.reason == "context_changed"
        assert written == []                 # 复核失败不落盘
        del user
