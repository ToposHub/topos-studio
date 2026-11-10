"""R03/R05/R06：代理所有权 / Relink 身份与命令 / AI 取消边界回归。

对应《媒体池后端完成情况复核与整改计划》（2026-11-10）：
- R03：代理编码写私有 staging、成功原子发布；失败/超时只清理 staging，
  既有同名文件字节保持不变；菜单入口走统一 DiskProxyService（队列/
  预检/取消由服务管理），任务注销同步取消服务上的在途生成；
- R05：Relink 模态前冻结工程+素材身份，模态后守卫（切工程/素材已变
  即丢弃）；单/批量重连走命令事务，单步 Undo 恢复完整旧素材（含
  代理）；
- R06：外层 asset job 取消后 work 在 provider 执行前拒绝启动（不
  ingest/不发布），request 按 成功/失败/取消 三分收尾。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.features.edit.core.project import EditProject


def _offline_ready_project(tmp_path: Any, name: str = "Relink audit"):
    old = tmp_path / "old.mp4"
    old.write_bytes(b"old")
    new = tmp_path / "new.mp4"
    new.write_bytes(b"new")
    project = EditProject.create(name).import_asset(
        str(old), "video", {"fps": 24.0, "total_frames": 240})
    return project, old, new


class TestR03ProxyOwnership:
    def test_failed_cli_keeps_preexisting_target(self, tmp_path: Any) -> None:
        """复现（repro R03）：CLI 失败不得删除既有同名代理文件。"""
        from src.features.edit.core import proxy_generator as pg
        source = tmp_path / "shot.mp4"
        source.write_bytes(b"test-source")
        target = tmp_path / "shot_proxy.mp4"
        target.write_bytes(b"unrelated-user-file")

        class FailedProcess:
            returncode = 1

            def communicate(self, timeout: Any = None):
                return b"", b"forced error"

        with patch.object(pg.subprocess, "Popen",
                          return_value=FailedProcess()), \
             patch.object(pg, "find_ffmpeg", return_value="never-run"):
            result = pg._generate_video_proxy_cli(
                pg.MediaAsset("a", str(source), "video", "shot.mp4", {}),
                str(target), pg._staging_path(str(target)), pg.ProxyConfig())
        assert result is None
        assert target.read_bytes() == b"unrelated-user-file"   # 既有文件保留
        assert source.exists()
        assert set(tmp_path.iterdir()) == {source, target}     # staging 已清理

    def test_staging_is_private_and_publish_is_atomic(
            self, tmp_path: Any) -> None:
        from src.features.edit.core import proxy_generator as pg
        final = tmp_path / "clip_proxy.mp4"
        staging = pg._staging_path(str(final))
        assert staging != str(final)
        assert staging.startswith(str(tmp_path))
        with open(staging, "wb") as fh:
            fh.write(b"ENCODED")
        assert pg._publish_proxy(staging, str(final)) == str(final)
        assert final.read_bytes() == b"ENCODED"
        assert not (tmp_path / staging).exists()

    def test_insufficient_space_fails_before_encoding(
            self, tmp_path: Any, monkeypatch: Any) -> None:
        from src.features.edit.core import proxy_generator as pg
        source = tmp_path / "big.mov"
        source.write_bytes(b"x" * 1024)

        import shutil as real_shutil
        usage_cls = real_shutil.disk_usage(str(tmp_path)).__class__

        def tiny_disk(path: str):
            return usage_cls(0, 0, 0)   # free = 0：任何预留都不够

        monkeypatch.setattr(pg.shutil, "disk_usage", tiny_disk)
        asset = pg.MediaAsset("a", str(source), "video", "big.mov", {})
        assert pg.ProxyGeneratorWorker.generate_proxy(asset) is None
        assert not (tmp_path / "big_proxy.mov").exists()

    def test_menu_entry_routes_through_unified_service(
            self, tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
        """R03：菜单不再直接构造旧 worker，而是调统一 DiskProxyService。"""
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui import media_pool_panel as mpp
        panel = mpp.MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        source = tmp_path / "menu.mov"
        source.write_bytes(b"menu-source")
        project = EditProject.create("Menu proxy").import_asset(
            str(source), "video", {"fps": 25.0})
        panel.set_project(project)
        aid = project.get_asset_by_path(str(source)).asset_id

        service = MagicMock()
        service.generate_proxy.return_value = "task-1"
        monkeypatch.setattr(mpp, "get_disk_proxy_service",
                            lambda: service)
        monkeypatch.setattr(mpp, "check_ffmpeg_available",
                            lambda: True)
        monkeypatch.setattr(mpp.QMessageBox, "information", MagicMock())
        monkeypatch.setattr(mpp.QMessageBox, "warning", MagicMock())
        panel._on_generate_proxy(aid)

        assert service.generate_proxy.call_count == 1
        kwargs = service.generate_proxy.call_args.kwargs
        assert kwargs["source_path"] == str(source)
        assert kwargs["asset_id"] == aid
        for key in ("on_complete", "on_progress", "on_error"):
            assert callable(kwargs[key])

        # 注销任务 → 同步取消服务上的在途生成
        panel._invalidate_proxy_job(aid)
        service.cancel_generation.assert_called_once_with(str(source))
        panel.clear_project()
        panel.close()
        panel.deleteLater()

    def test_invalidate_all_cancels_tracked_sources(
            self, qapp: Any, monkeypatch: Any) -> None:
        pytest.importorskip("PyQt6.QtWidgets")
        from src.features.edit.ui import media_pool_panel as mpp
        panel = mpp.MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        panel._proxy_jobs = {"a": "/x/a.mov", "b": "/y/b.mov"}
        service = MagicMock()
        monkeypatch.setattr(mpp, "get_disk_proxy_service",
                            lambda: service)
        panel._invalidate_all_proxy_jobs()
        assert {c.args[0] for c in service.cancel_generation.call_args_list} \
            == {"/x/a.mov", "/y/b.mov"}
        panel.close()
        panel.deleteLater()


class TestR05RelinkIdentityAndCommand:
    def test_modal_project_switch_drops_relink(self, tmp_path: Any,
                                               qapp: Any) -> None:
        """复现（repro R05）：模态期间切工程后不得写入新工程。"""
        pytest.importorskip("PyQt6.QtWidgets")
        from dataclasses import replace as dc_replace
        from src.features.edit.ui import media_pool_panel as mpp
        project, old, target = _offline_ready_project(tmp_path)
        aid = next(iter(project.assets))
        other = tmp_path / "other.mp4"
        other.write_bytes(b"other")
        p2 = dc_replace(
            project, project_id="different-project",
            assets={aid: dc_replace(project.assets[aid],
                                    path=str(other))})
        panel = mpp.MediaPoolPanel()
        panel._offline_refresh_timer.stop()
        panel.set_project(project)

        def modal_switch(*args: Any, **kwargs: Any):
            panel.set_project(p2)
            return str(target), ""

        with patch.object(mpp.QFileDialog, "getOpenFileName",
                          side_effect=modal_switch):
            mpp.MediaPoolPanel._on_relink_asset(panel, aid)
        # p2 的素材保持自己的路径；重连意图被守卫丢弃
        assert panel._project.project_id == "different-project"
        assert panel._project.assets[aid].path == str(other)
        panel.clear_project()
        panel.close()
        panel.deleteLater()

    def test_single_relink_command_single_step_undo_restores_all(
            self, tmp_path: Any) -> None:
        from src.features.edit.core.commands import (
            ProjectCommandStack,
            RelinkAssetCommand,
        )
        from src.features.edit.core.project import update_asset_proxy
        project, old, new = _offline_ready_project(tmp_path)
        aid = next(iter(project.assets))
        project = update_asset_proxy(project, aid, "/proxies/old.mp4",
                                     "ready")
        stack = ProjectCommandStack()
        project = stack.execute(
            RelinkAssetCommand.create(aid, str(new)), project)
        assert project.assets[aid].path == str(new)
        assert project.assets[aid].proxy_path is None      # 旧代理失效
        assert project.assets[aid].proxy_status == "none"
        project = stack.undo(project)
        assert project.assets[aid].path == str(old)
        assert project.assets[aid].proxy_path == "/proxies/old.mp4"
        assert project.assets[aid].proxy_status == "ready"

    def test_batch_relink_command_restores_assets(self, tmp_path: Any) -> None:
        from src.features.edit.core.commands import (
            BatchRelinkAssetsCommand,
            ProjectCommandStack,
        )
        from src.features.edit.core.project import update_asset_proxy
        project = EditProject.create("Batch relink")
        paths = {}
        for name in ("a", "b"):
            old = tmp_path / f"{name}.mov"
            old.write_bytes(name.encode())
            new = tmp_path / f"{name}-new.mov"
            new.write_bytes(name.encode())
            paths[name] = (old, new)
            project = project.import_asset(str(old), "video", {})
        aids = {}
        for name, (old, _new) in paths.items():
            aids[name] = project.get_asset_by_path(str(old)).asset_id
        project = update_asset_proxy(project, aids["a"], "/p/old.mp4",
                                     "ready")
        stack = ProjectCommandStack()
        project = stack.execute(
            BatchRelinkAssetsCommand.create(
                {aid: str(paths[n][1]) for n, aid in aids.items()}),
            project)
        assert project.assets[aids["a"]].path == str(paths["a"][1])
        assert project.assets[aids["a"]].proxy_path is None
        project = stack.undo(project)
        for name, (old, _new) in paths.items():
            assert project.assets[aids[name]].path == str(old)
        assert project.assets[aids["a"]].proxy_path == "/p/old.mp4"

    def test_relink_after_rename_undo_redo_consistent(self, tmp_path: Any) -> None:
        """重连走命令后：撤销更早的重命名不再连带丢掉重连结果。"""
        from src.features.edit.core.commands import (
            ProjectCommandStack,
            RelinkAssetCommand,
            RenameAssetCommand,
        )
        project, _old, new = _offline_ready_project(tmp_path)
        aid = next(iter(project.assets))
        stack = ProjectCommandStack()
        project = stack.execute(RenameAssetCommand.create(aid, "renamed"),
                                project)
        project = stack.execute(
            RelinkAssetCommand.create(aid, str(new)), project)
        project = stack.undo(project)   # 撤销重连 → 回旧路径，名字保留
        assert project.assets[aid].path.endswith("old.mp4")
        assert project.assets[aid].display_name == "renamed"
        project = stack.redo(project)
        assert project.assets[aid].path.endswith("new.mp4")


class TestR06CancelBlocksPublish:
    def _service_with_request(self, asset_ids: tuple):
        from src.features.edit.ai_editing.analysis_request import (
            AnalysisRecipe,
            AnalysisRequest,
            RequestState,
        )
        from src.features.edit.ai_editing.analysis_service import (
            AnalysisService,
        )
        service = AnalysisService()
        request = AnalysisRequest.create(
            project_id="p", asset_ids=asset_ids,
            recipe_id=AnalysisRecipe.FAST_LOCAL,
        ).with_state(RequestState.RUNNING)
        service._requests[request.request_id] = request
        return service, request

    def _fake_scheduler(self):
        class Scheduler:
            def __init__(self) -> None:
                self.items = []

            def submit(self, lane: Any, job: Any, **kwargs: Any) -> None:
                self.items.append((job, kwargs))

            def notify_finished(self, job_id: str) -> None:
                pass

        return Scheduler()

    def test_cancelled_running_job_rejects_publish(self):
        """复现（repro R06）：外层取消后 provider 不得执行/入库。"""
        from src.features.edit.ai_editing.analysis_scheduler import ResourceLane
        from src.features.edit.ai_editing.jobs import JobKind
        service, request = self._service_with_request(("a",))
        scheduler = self._fake_scheduler()
        summary, _err = service._execute_via_scheduler(
            request.request_id, request, scheduler=scheduler,
            lane=ResourceLane.ANALYSIS_LOCAL, fps="24/1",
            provider_ids=(), on_asset_done=None, on_request_done=None)
        outer, kwargs = scheduler.items[0]
        outer.mark_running()
        outer.request_cancel()

        from src.features.edit.ai_editing.analysis_models import (
            AssetAnalysis,
        )

        def pretend_provider(asset_id: str, fingerprint: Any,
                             **kwargs: Any):
            inner = service.jobs.submit(JobKind.ANALYSIS, label="inner")
            inner.mark_running()
            service.ingest(asset_id, AssetAnalysis(asset_id, fingerprint), ())
            inner.complete({})
            return inner, ""

        service.request_analysis = pretend_provider
        kwargs["work"]()
        assert outer.status.value == "cancelled"
        assert summary.status.value == "cancelled"
        assert service.get_analysis("a") is None      # 未入库（拒绝发布）

    def test_all_pending_cancelled_finalizes_cancelled(self):
        from src.features.edit.ai_editing.analysis_request import RequestState
        from src.features.edit.ai_editing.analysis_scheduler import ResourceLane
        service, request = self._service_with_request(("a", "b"))
        scheduler = self._fake_scheduler()
        summary, _err = service._execute_via_scheduler(
            request.request_id, request, scheduler=scheduler,
            lane=ResourceLane.ANALYSIS_LOCAL, fps="24/1",
            provider_ids=(), on_asset_done=None, on_request_done=None)
        scheduler.items[0][1]["on_cancel"]()
        scheduler.items[1][1]["on_cancel"]()
        assert service.get_request(request.request_id).state \
            == RequestState.CANCELLED
        assert summary.status.value == "cancelled"
