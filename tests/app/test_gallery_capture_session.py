"""G06 抓帧会话一致性测试：请求锁定／阶段复核／取消与结果回填。

覆盖计划 §5 G06 门槛：换 clip／换源／取消／关闭后不抓到上一帧；结果
metadata 与请求一致；拒绝无文件；完成后的刷新只在用户仍停在抓帧时的
album 才发生（不抢回已切走的选择）。
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.features.color.core.gallery_capture_service import (
    GalleryCaptureCoordinator,
    GalleryCaptureFacts,
    GalleryCaptureSession,
    REASON_CANCELLED,
    REASON_CONTEXT_CHANGED,
    REASON_RENDER_FAILED,
    REASON_USER_DECLINED,
)
from src.features.color.core.gallery_contracts import (
    GalleryCaptureRequest,
    GalleryOperationResult,
    make_gallery_request_id,
)


def _request(**overrides) -> GalleryCaptureRequest:
    fields = dict(request_id=make_gallery_request_id(), album_id="legacy:User")
    fields.update(overrides)
    return GalleryCaptureRequest(**fields)


def _facts(**overrides) -> GalleryCaptureFacts:
    fields = dict(album_id="legacy:User", media_path="/tmp/a.mov",
                  clip_id="c1", asset_id="a1", timeline_id="t1",
                  source_frame=42, timeline_frame=100)
    fields.update(overrides)
    return GalleryCaptureFacts(**fields)


class TestSession:
    def test_success_runs_prepare_then_persist_once(self):
        facts = _facts()
        session = GalleryCaptureSession(
            _request(), facts, context_probe=lambda: facts)
        calls = []
        result = session.run(
            prepare_frame=lambda: calls.append("prepare") or object(),
            persist_bundle=lambda img: calls.append("persist")
            or GalleryOperationResult(
                request_id="r", status="success", affected_still_ids=("S",)))
        assert result.succeeded
        assert calls == ["prepare", "persist"]

    def test_clip_changed_between_phases_cancels_before_persist(self):
        facts = _facts()
        probe_facts = {"n": 0}

        def probe():
            probe_facts["n"] += 1
            # begin 用原事实；prepare 之后换 clip
            return facts if probe_facts["n"] == 1 else _facts(clip_id="c2")

        session = GalleryCaptureSession(_request(), facts,
                                        context_probe=probe)
        persisted = []
        result = session.run(
            prepare_frame=lambda: object(),
            persist_bundle=lambda img: persisted.append(img)
            or GalleryOperationResult(request_id="r", status="success"))
        assert result.status == "cancelled"
        assert result.reason == REASON_CONTEXT_CHANGED
        assert "clip_id" in result.warnings[0]
        assert persisted == []            # 不把上一帧写进新上下文

    def test_source_frame_change_blocks_but_album_change_does_not(self):
        """帧号变化（抓错帧向量）取消；用户切走 album 不阻断写入。"""
        facts = _facts()
        session = GalleryCaptureSession(
            _request(), facts,
            context_probe=lambda: _facts(source_frame=99))
        result = session.run(prepare_frame=lambda: object(),
                             persist_bundle=lambda img: None)
        assert result.reason == REASON_CONTEXT_CHANGED

        session2 = GalleryCaptureSession(
            _request(), facts, context_probe=lambda: _facts(album_id="other"))
        result2 = session2.run(
            prepare_frame=lambda: object(),
            persist_bundle=lambda img: GalleryOperationResult(
                request_id="r", status="success"))
        assert result2.succeeded     # 目的地在请求里已锁定

    def test_cancel_token_before_run_skips_everything(self):
        facts = _facts()
        session = GalleryCaptureSession(_request(), facts,
                                        context_probe=lambda: facts)
        session.cancel(REASON_CANCELLED)
        calls = []
        result = session.run(
            prepare_frame=lambda: calls.append("p") or object(),
            persist_bundle=lambda img: calls.append("w"))
        assert result.status == "cancelled" and result.reason == REASON_CANCELLED
        assert calls == []

    def test_prepare_failure_rejected_without_persist(self):
        facts = _facts()
        session = GalleryCaptureSession(_request(), facts,
                                        context_probe=lambda: facts)
        persisted = []
        result = session.run(
            prepare_frame=lambda: None,
            persist_bundle=lambda img: persisted.append(img))
        assert result.status == "rejected"
        assert result.reason == REASON_RENDER_FAILED
        assert persisted == []          # 拒绝无文件

    def test_context_probe_none_treated_as_context_lost(self):
        facts = _facts()
        session = GalleryCaptureSession(_request(), facts,
                                        context_probe=lambda: None)
        result = session.run(prepare_frame=lambda: object(),
                             persist_bundle=lambda img: None)
        assert result.status == "cancelled"
        assert result.reason == REASON_CONTEXT_CHANGED

    def test_persist_result_propagates(self):
        facts = _facts()
        session = GalleryCaptureSession(_request(), facts,
                                        context_probe=lambda: facts)
        declined = GalleryOperationResult(request_id="r",
                                          status="cancelled",
                                          reason=REASON_USER_DECLINED)
        result = session.run(prepare_frame=lambda: object(),
                             persist_bundle=lambda img: declined)
        assert result is declined


class TestCoordinator:
    def test_cancel_all_invalidates_active_sessions(self):
        coordinator = GalleryCaptureCoordinator()
        facts = _facts()
        sessions = [GalleryCaptureSession(_request(), facts,
                                          context_probe=lambda: facts)
                    for _ in range(3)]
        for session in sessions:
            coordinator.register(session)
        assert coordinator.active_count == 3
        assert coordinator.cancel_all("user") == 3
        results = [s.run(prepare_frame=lambda: object(),
                         persist_bundle=lambda img: None) for s in sessions]
        assert all(r.status == "cancelled" and r.reason == "user"
                   for r in results)
        for session in sessions:
            coordinator.finish(session)
        assert coordinator.active_count == 0

    def test_shutdown_cancels_and_blocks_new_registrations(self):
        coordinator = GalleryCaptureCoordinator()
        facts = _facts()
        first = GalleryCaptureSession(_request(), facts,
                                      context_probe=lambda: facts)
        coordinator.register(first)
        assert coordinator.mark_shutdown() == 1
        assert coordinator.shutdown is True
        late = GalleryCaptureSession(_request(), facts,
                                     context_probe=lambda: facts)
        coordinator.register(late)      # 关闭后注册立即取消
        assert late.cancelled_reason == "shutdown"


class TestControllerIntegration:
    def _make_ctrl(self, tmp_path, monkeypatch, folder="User"):
        from src.app.controllers.layer_controller import LayerOpsMixin
        from src.features.color.core.gallery_repository import (
            GalleryFolderRepository,
        )
        monkeypatch.chdir(tmp_path)
        ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
        import numpy as np
        rng = np.random.default_rng(3)
        image = (rng.random((32, 48, 3)) * 255).astype(np.uint8)
        ctrl.engine = SimpleNamespace(img_high=image)
        ctrl.engine.grab_viewport_image = lambda *a, **k: image
        ctrl.status_bar = MagicMock()
        ctrl.current_file_is_video = False
        ctrl.current_file_path = "/tmp/src.mov"
        ctrl.save_router = MagicMock()

        def _fake_save(path, *_a, **_k):
            with open(path, "wb") as fh:
                fh.write(b"PK-fake")
            return True
        ctrl.save_router._save_single_asset_project.side_effect = _fake_save
        presets_root = os.path.join(os.getcwd(), "Presets")
        os.makedirs(os.path.join(presets_root, folder), exist_ok=True)
        ctrl.presets_panel = MagicMock()
        ctrl.presets_panel.get_current_folder.return_value = os.path.join(
            presets_root, folder)
        ctrl.presets_panel._repository = GalleryFolderRepository(presets_root)
        ctrl.presets_panel.presets_root = presets_root
        ctrl._page_context_manager = SimpleNamespace(
            get_current_context=lambda: SimpleNamespace(
                clip_id="c1", asset_id="a1", timeline_frame=5,
                source_frame=9, timeline_id="t1"))
        ctrl._current_edit_project = None
        ctrl._get_composite_graph_data = lambda: None
        ctrl._get_composite_state_data = lambda: None
        return ctrl, presets_root

    def _disable_overwrite_dialog(self, monkeypatch):
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", MagicMock())

    def test_capture_builds_request_with_context_facts(self, tmp_path,
                                                       monkeypatch):
        ctrl, _ = self._make_ctrl(tmp_path, monkeypatch)
        self._disable_overwrite_dialog(monkeypatch)
        ctrl.on_capture_still()
        coordinator = ctrl._gallery_capture_coordinator()
        assert coordinator.active_count == 0        # 会话已收尾
        files = os.listdir(ctrl.presets_panel.get_current_folder())
        assert any(f.endswith(".topos") for f in files)
        # meta 带请求时刻的来源快照（clip/asset/timeline）
        import json
        meta_file = next(f for f in files if f.endswith(".meta.json"))
        with open(os.path.join(ctrl.presets_panel.get_current_folder(),
                               meta_file)) as fh:
            meta = json.load(fh)
        assert meta["clip_id"] == "c1" and meta["timeline_id"] == "t1"
        assert meta["album_id"] == "legacy:User"
        assert meta["still_id"].startswith("S-")

    def test_refresh_skipped_when_user_switched_album(self, tmp_path,
                                                      monkeypatch):
        ctrl, _ = self._make_ctrl(tmp_path, monkeypatch)
        self._disable_overwrite_dialog(monkeypatch)
        scanned = []
        original_folder = ctrl.presets_panel.get_current_folder.return_value
        # get_current_folder 调用序：build(1) → persist(2) → 完成后复核(3)
        # 第 3 次（完成后的刷新复核）时用户已切到 System
        calls = {"n": 0}

        def folder_now():
            calls["n"] += 1
            if calls["n"] >= 3:
                return os.path.join(os.path.dirname(original_folder),
                                    "System")
            return original_folder
        ctrl.presets_panel.get_current_folder.side_effect = folder_now
        ctrl.presets_panel.scan_presets.side_effect = (
            lambda: scanned.append(1))
        ctrl.on_capture_still()
        # persist 已写入原 folder；完成后 panel 已在 System → 不重扫
        assert scanned == []
        assert any(f.endswith(".topos")
                   for f in os.listdir(original_folder))

    def test_refresh_happens_when_still_on_capture_album(self, tmp_path,
                                                         monkeypatch):
        ctrl, _ = self._make_ctrl(tmp_path, monkeypatch)
        self._disable_overwrite_dialog(monkeypatch)
        scanned = []
        ctrl.presets_panel.scan_presets.side_effect = lambda: scanned.append(1)
        ctrl.on_capture_still()
        assert scanned == [1]

    def test_readonly_album_rejected_without_files(self, tmp_path,
                                                   monkeypatch):
        if os.geteuid() == 0:
            pytest.skip("root ignores permission bits")
        ctrl, _ = self._make_ctrl(tmp_path, monkeypatch)
        folder = ctrl.presets_panel.get_current_folder()
        os.chmod(folder, 0o555)
        try:
            ctrl.on_capture_still()
            assert os.listdir(folder) == []      # 拒绝无文件
            ctrl.status_bar.showMessage.assert_called()
        finally:
            os.chmod(folder, 0o755)

    def test_shutdown_coordinator_wired_into_pipeline(self):
        """mark_shutdown 后在场会话取消（main_window 3.8 步骤的合同）。"""
        coordinator = GalleryCaptureCoordinator()
        facts = _facts()
        session = GalleryCaptureSession(_request(), facts,
                                        context_probe=lambda: facts)
        coordinator.register(session)
        coordinator.mark_shutdown()
        result = session.run(prepare_frame=lambda: object(),
                             persist_bundle=lambda img: None)
        assert result.reason == "shutdown"
