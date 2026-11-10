"""BR02：导入完整原子性、全成员冲突与身份提交验收。

对应《Gallery/Viewport 后端复查整改计划》BR02（2026-11-10）：
- 六角色任意第 N 项安装失败零残文件（意图 journal 覆盖全部 finals）；
- 孤立 sidecar／大小写冲突不覆盖（显式名拒绝、自动名顺延）；
- still_id 换发在 staging 完成：写失败整笔拒绝，不报 success；
- 身份作用域＝目标相册（同 still 复制进另一相册合法）；指纹＝完整
  bundle 内容（角色集合＋逐角色 sha256），不再只比 grade hash；
- commit 前取消零落地；并发导入目录互斥；崩溃遗留按意图恢复。
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from src.features.color.core.gallery_bundle_service import GalleryBundleService
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_transfer_service import (
    IMPORT_TXN_PREFIX,
    GalleryTransferService,
)
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta


def _seed(folder: str, stem: str, *, still_id: str = "S-origin000000001",
          thumb: bytes = b"thumb", grade_layers=None) -> None:
    os.makedirs(folder, exist_ok=True)
    grade = json.dumps({"layers": [
        grade_layers or [{"name": "g", "active_lut_path": "/luts/a.cube"}]]})
    with zipfile.ZipFile(os.path.join(folder, f"{stem}.topos"), "w") as zf:
        zf.writestr("project.json", grade)
    for suffix, data in ((".thumb.jpg", thumb), (".still.jpg", b"still")):
        with open(os.path.join(folder, f"{stem}{suffix}"), "wb") as fh:
            fh.write(data)
    save_preset_meta(os.path.join(folder, f"{stem}.topos"), PresetMeta(
        still_id=still_id))


@pytest.fixture()
def svc(tmp_path: Path):
    root = tmp_path / "Presets"
    (root / "User").mkdir(parents=True)
    (root / "System").mkdir()
    repository = GalleryFolderRepository(str(root))
    service = GalleryTransferService(repository,
                                     GalleryBundleService(str(root)))
    return service, str(root)


def _export(service: GalleryTransferService, root: str, stem: str = "Look",
            folder: str = "User") -> str:
    _seed(os.path.join(root, folder), stem)
    dest = os.path.join(root, f"{stem}.toposbundle")
    assert service.export_bundle(os.path.join(root, folder), stem,
                                 dest).succeeded
    return dest


class TestInstallAtomicity:
    @pytest.mark.parametrize("fail_at", [1, 2, 3, 4])
    def test_nth_install_failure_leaves_no_partial_files(
            self, svc, monkeypatch, fail_at: int):
        """第 N 个成员安装失败：已装的前 N−1 个全部撤下，零残文件。"""
        service, root = svc
        archive = _export(service, root)
        target = os.path.join(root, "System")
        real_replace = os.replace
        installs = {"n": 0}

        def fail_nth(src, dst):
            if os.path.dirname(os.path.abspath(dst)) == \
                    os.path.abspath(target):
                installs["n"] += 1
                if installs["n"] == fail_at:
                    raise OSError("injected install failure")
            return real_replace(src, dst)

        monkeypatch.setattr(
            "src.features.color.core.gallery_transfer_service.os.replace",
            fail_nth)
        result = service.import_bundle(archive, target)
        monkeypatch.setattr(os, "replace", real_replace)
        assert result.status == "rejected"
        leftovers = sorted(os.listdir(target))
        assert leftovers == [], leftovers        # 含 meta sidecar，零残留

    def test_crash_mid_install_recovered_by_intent_journal(
            self, svc, monkeypatch):
        """安装中崩溃（KeyboardInterrupt）：journal 意图驱动下次恢复撤装。"""
        import src.features.color.core.gallery_transfer_service as mod
        service, root = svc
        archive = _export(service, root)
        target = os.path.join(root, "System")
        real_replace = os.replace
        installs = {"n": 0}

        def die_on_second(src, dst):
            if os.path.dirname(os.path.abspath(dst)) == \
                    os.path.abspath(target):
                installs["n"] += 1
                if installs["n"] == 2:
                    raise KeyboardInterrupt("process death mid install")
            return real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", die_on_second)
        with pytest.raises(KeyboardInterrupt):
            service.import_bundle(archive, target)
        monkeypatch.setattr(mod.os, "replace", real_replace)
        assert [n for n in os.listdir(target) if n.startswith("Look")]
        # 下一次导入先恢复：按 journal finals 撤掉已装成员
        service._recover_import_txns(target)
        assert [n for n in os.listdir(target)
                if n.startswith("Look")] == []
        assert not [n for n in os.listdir(target)
                    if n.startswith(IMPORT_TXN_PREFIX)]

    def test_cancel_before_commit_writes_nothing(self, svc):
        """commit 前取消：零落地，状态如实 cancelled。"""
        service, root = svc
        archive = _export(service, root)
        target = os.path.join(root, "System")
        result = service.import_bundle(
            archive, target, cancel_check=lambda: True)
        assert result.status == "cancelled"
        assert result.reason == "cancelled"
        assert os.listdir(target) == []

    def test_progress_callback_reports_extraction_and_install(self, svc):
        service, root = svc
        archive = _export(service, root)
        target = os.path.join(root, "System")
        events: list = []
        result = service.import_bundle(
            archive, target, progress_callback=lambda d, t:
            events.append((d, t)))
        assert result.succeeded
        assert events                     # 有进度回填
        assert events[-1][0] >= len(events) // 2   # 安装阶段也推进


class TestNameConflicts:
    def test_explicit_name_with_orphan_sidecar_rejected_no_overwrite(
            self, svc):
        """显式名撞孤立 sidecar：拒绝，孤儿文件字节不动。"""
        service, root = svc
        archive = _export(service, root)
        target = os.path.join(root, "System")
        orphan = os.path.join(target, "Taken.thumb.jpg")
        original = b"OLD USER THUMBNAIL"
        with open(orphan, "wb") as fh:
            fh.write(original)
        result = service.import_bundle(archive, target, new_name="Taken")
        assert result.status == "rejected"
        assert result.reason == "orphan_sidecar_conflict"
        with open(orphan, "rb") as fh:
            assert fh.read() == original

    def test_auto_name_skips_orphan_sidecar(self, svc):
        """自动命名也避开任意成员名占用（不再只查 .topos）。"""
        service, root = svc
        archive = _export(service, root)
        target = os.path.join(root, "System")
        with open(os.path.join(target, "Look.thumb.jpg"), "wb") as fh:
            fh.write(b"orphan")
        result = service.import_bundle(archive, target)
        assert result.succeeded
        assert result.affected_still_ids == ("Look_1",)
        with open(os.path.join(target, "Look.thumb.jpg"), "rb") as fh:
            assert fh.read() == b"orphan"          # 孤儿未被覆盖

    def test_explicit_name_conflict_rejected_not_suffixed(self, svc):
        """显式名撞既有 .topos：拒绝（不再静默顺延用户指定的名字）。"""
        service, root = svc
        archive = _export(service, root)
        target = os.path.join(root, "System")
        _seed(target, "Look", still_id="S-other22222222222")  # 不同身份
        result = service.import_bundle(archive, target, new_name="Look")
        assert result.status == "rejected"
        assert result.reason == "name_conflict"
        # 既有 bundle 原样，未遭覆盖
        from src.features.color.core.preset_meta import load_preset_meta
        meta = load_preset_meta(os.path.join(target, "Look.topos"))
        assert meta.still_id == "S-other22222222222"


class TestIdentityCommit:
    def test_reallocate_save_failure_rejects_whole_import(self, svc,
                                                          monkeypatch):
        """staging 内身份换发写失败：整笔拒绝（identity_save_failed）。"""
        service, root = svc
        # 目标相册已有同 ID 不同内容 → 触发换发
        _seed(os.path.join(root, "User"), "Look",
              still_id="S-origin000000001", thumb=b"DIFFERENT")
        archive = os.path.join(root, "in.toposbundle")
        other = os.path.join(root, "other")
        _seed(other, "Look", thumb=b"thumb2")
        assert service.export_bundle(other, "Look", archive,
                                     include_exr=False).succeeded
        import src.features.color.core.gallery_transfer_service as mod
        monkeypatch.setattr(mod, "save_preset_meta", lambda *a, **k: False)
        target = os.path.join(root, "User")
        result = service.import_bundle(archive, target)
        monkeypatch.undo()
        assert result.status == "rejected"
        assert result.reason == "identity_save_failed"
        # 原目标 bundle 不受影响，目标目录零新文件
        assert os.path.isfile(os.path.join(target, "Look.topos"))
        assert not [n for n in os.listdir(target) if n.startswith("Look_")]

    def test_same_id_into_another_album_is_allowed(self, svc):
        """身份作用域＝目标相册：同一 still 复制进另一相册是合法操作。"""
        service, root = svc
        archive = _export(service, root, folder="User")
        system = os.path.join(root, "System")
        result = service.import_bundle(archive, system)
        assert result.succeeded
        meta = service._repository  # 仅为可读性；实际断言 meta 内容
        del meta
        from src.features.color.core.preset_meta import load_preset_meta
        imported = load_preset_meta(os.path.join(system, "Look.topos"))
        assert imported.still_id == "S-origin000000001"

    def test_same_grade_different_reference_reallocates(self, svc):
        """指纹超出 grade：同 ID、同 grade、不同参考图 → 换发新 ID。"""
        service, root = svc
        layers = [{"name": "g", "active_lut_path": "/luts/a.cube"}]
        _seed(os.path.join(root, "User"), "Look",
              still_id="S-same111111111111", thumb=b"local")
        other = os.path.join(root, "other")
        _seed(other, "Look", still_id="S-same111111111111",
              thumb=b"remote-different", grade_layers=layers)
        archive = os.path.join(root, "r.toposbundle")
        assert service.export_bundle(other, "Look", archive,
                                     include_exr=False).succeeded
        result = service.import_bundle(archive, os.path.join(root, "User"))
        assert result.succeeded
        from src.features.color.core.preset_meta import load_preset_meta
        stem = result.affected_still_ids[0]
        meta = load_preset_meta(
            os.path.join(root, "User", f"{stem}.topos"))
        assert meta.still_id != "S-same111111111111"
        assert meta.extra_fields["origin_still_id"] == "S-same111111111111"

    def test_success_reports_actual_still_id(self, svc):
        service, root = svc
        archive = _export(service, root)
        result = service.import_bundle(archive, os.path.join(root, "System"))
        assert result.succeeded
        assert any("still_id" in w for w in result.warnings)


class TestConcurrency:
    def test_concurrent_import_busy(self, svc):
        import threading

        service, root = svc
        archive = _export(service, root)
        target = os.path.join(root, "System")
        from src.features.color.core.gallery_txn_core import (
            acquire_directories, release_directories,
        )
        readied, release = threading.Event(), threading.Event()

        def hold():
            keys = acquire_directories(target)
            readied.set()
            release.wait(5)
            release_directories(keys)

        worker = threading.Thread(target=hold)
        worker.start()
        assert readied.wait(5)
        try:
            result = service.import_bundle(archive, target)
            assert result.status == "rejected"
            assert result.reason == "album_busy"
        finally:
            release.set()
            worker.join(5)
        assert service.import_bundle(archive, target).succeeded
