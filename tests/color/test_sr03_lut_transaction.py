"""SR03：LUT 与 bundle 的统一事务、碰撞与资源管理验收。

对应《第二轮复查计划》SR03（2026-11-10）：
- 依赖在事务 staging 内验证，安装意图与 bundle finals 同一 journal
  （``resources`` 段）——**依赖安装后取消**会把资源与 bundle 一起撤掉；
- 安装名内容寻址：同前缀不同内容顺延更长前缀，绝不覆盖外来文件；
  全 hash 同名异内容明确拒绝；
- 同内容幂等复用；
- 导出后验证最终归档（manifest hash==包内字节），与导入共享限额。
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from src.features.color.core.gallery_bundle_service import GalleryBundleService
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.gallery_transfer_service import (
    BUNDLE_MANIFEST_NAME,
    GalleryTransferService,
)
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta

LUT_BYTES = b"LUT3D\n2 2 2\npayload" * 8
LUT_SHA = hashlib.sha256(LUT_BYTES).hexdigest()


def _seed(folder: Path, stem: str, lut_ref: str | None,
          still_id: str = "S-sr03") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    topos = folder / f"{stem}.topos"
    layers = ([{"name": "g", "active_lut_path": lut_ref}]
              if lut_ref else [{"name": "g"}])
    with zipfile.ZipFile(topos, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": layers}))
    (folder / f"{stem}.thumb.jpg").write_bytes(b"thumb")
    save_preset_meta(str(topos), PresetMeta(still_id=still_id))
    return topos


@pytest.fixture()
def roots(tmp_path: Path):
    (tmp_path / "a" / "Presets" / "User").mkdir(parents=True)
    (tmp_path / "b" / "Presets" / "System").mkdir(parents=True)
    return tmp_path / "a", tmp_path / "b"


def _svc(base: Path) -> GalleryTransferService:
    root = base / "Presets"
    return GalleryTransferService(
        GalleryFolderRepository(str(root)), GalleryBundleService(str(root)))


def _portable(roots, lut: Path, name: str = "look.cube") -> Path:
    a, _ = roots
    lut.write_bytes(LUT_BYTES)
    _seed(a / "Presets" / "User", "Look", lut_ref=str(lut))
    archive = a / "Look.toposbundle"
    assert _svc(a).export_bundle(
        str(a / "Presets" / "User"), "Look", str(archive)).succeeded
    return archive


class TestUnifiedResourceTransaction:
    def test_cancel_after_dependency_install_removes_resource(
            self, roots, tmp_path):
        """复现场景（repro SR03）：依赖装完再取消——零残留。"""
        a, b = roots
        archive = _portable(roots, tmp_path / "original.cube")
        service = _svc(b)
        target = b / "Presets" / "System"

        def cancel_after_lut_install() -> bool:
            luts = b / "Presets" / "LUTs"
            return luts.exists() and any(luts.iterdir())

        result = service.import_bundle(
            str(archive), str(target), cancel_check=cancel_after_lut_install)
        assert result.status == "cancelled"
        luts = b / "Presets" / "LUTs"
        assert not any(luts.glob("*.cube")) if luts.exists() else True
        assert not [p for p in target.iterdir()
                    if p.name.startswith("Look")]

    def test_journal_records_resources_before_install(self, roots, tmp_path):
        """崩溃窗口证据：journal 的 resources 段先于安装持久化。"""
        a, b = roots
        archive = _portable(roots, tmp_path / "original.cube")
        service = _svc(b)
        target = b / "Presets" / "System"
        seen_journals = []

        real_replace = os.replace

        def spy_replace(src, dst, *args, **kwargs):
            txn_dir = Path(dst).parent
            if txn_dir.name.startswith(".gallery-import-") or \
                    "System" in str(dst) and ".gallery-import-" in str(src):
                pass
            if str(dst).endswith(".cube") and "LUTs" in str(dst):
                # 资源安装时刻：journal 必须已登记该资源
                for path in target.glob(".gallery-import-*/__journal.json"):
                    seen_journals.append(json.loads(path.read_text()))
            return real_replace(src, dst, *args, **kwargs)

        import unittest.mock as mock
        with mock.patch("src.features.color.core.gallery_transfer_service."
                        "os.replace", spy_replace):
            result = service.import_bundle(str(archive), str(target))
        assert result.succeeded
        assert seen_journals, "resource install happened without journal"
        for journal in seen_journals:
            resources = journal.get("resources") or []
            assert resources
            assert resources[0]["sha256"] == LUT_SHA

    def test_crash_between_resource_and_finals_recovers_both(
            self, roots, tmp_path):
        """资源已装、bundle 未装时崩溃：恢复把资源一并撤掉。"""
        a, b = roots
        archive = _portable(roots, tmp_path / "original.cube")
        service = _svc(b)
        target = b / "Presets" / "System"

        def die_at_final_install() -> bool:
            # finals 开始安装时"进程死亡"
            return (target / "Look.topos").exists() is None or \
                (b / "Presets" / "LUTs").exists() and any(
                    (b / "Presets" / "LUTs").iterdir())

        class _Boom(Exception):
            pass

        import unittest.mock as mock
        real_replace = os.replace
        state = {"luts_seen": False}

        def crash_on_final(src, dst, *args, **kwargs):
            dst_str = str(dst)
            if dst_str.endswith(".topos") \
                    and os.path.dirname(dst_str) == str(target):
                raise KeyboardInterrupt("simulated crash")   # finals 安装
            if dst_str.endswith(".cube") and "LUTs" in dst_str:
                state["luts_seen"] = True
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch("src.features.color.core.gallery_transfer_service."
                        "os.replace", crash_on_final):
            with pytest.raises(KeyboardInterrupt):
                service.import_bundle(str(archive), str(target))
        assert state["luts_seen"]
        # 新一笔导入触发恢复：资源与 finals 一起收敛（无孤儿 LUT）
        other = a / "other.cube"
        other.write_bytes(b"OTHER LUT")
        _seed(a / "Presets" / "User", "Plain", lut_ref=None,
              still_id="S-sr03-plain")
        plain = a / "Plain.toposbundle"
        assert _svc(a).export_bundle(
            str(a / "Presets" / "User"), "Plain", str(plain)).succeeded
        result = _svc(b).import_bundle(str(plain), str(target))
        assert result.succeeded
        assert not list((b / "Presets" / "LUTs").glob("*.cube"))


class TestCollisionPolicy:
    def test_hash_prefix_collision_extends_prefix(self, roots, tmp_path):
        """同 sha8 前缀不同内容：顺延更长前缀安装，不覆盖外来文件。"""
        a, b = roots
        archive = _portable(roots, tmp_path / "original.cube")
        lut_dir = b / "Presets" / "LUTs"
        lut_dir.mkdir()
        foreign = lut_dir / f"{LUT_SHA[:8]}_original.cube"
        foreign.write_bytes(b"FOREIGN CONTENT")
        result = _svc(b).import_bundle(
            str(archive), str(b / "Presets" / "System"))
        assert result.succeeded, result.warnings
        assert foreign.read_bytes() == b"FOREIGN CONTENT"   # 外来文件不动
        installed = [p for p in lut_dir.glob("*.cube") if p != foreign]
        assert len(installed) == 1
        assert installed[0].name.startswith(f"{LUT_SHA[:12]}_")
        assert installed[0].read_bytes() == LUT_BYTES

    def test_full_hash_collision_rejected(self, roots, tmp_path):
        a, b = roots
        archive = _portable(roots, tmp_path / "original.cube")
        lut_dir = b / "Presets" / "LUTs"
        lut_dir.mkdir()
        for prefix_len in (8, 12, 16, 20, 32, 64):
            (lut_dir / f"{LUT_SHA[:prefix_len]}_original.cube").write_bytes(
                b"X" * 8)
        result = _svc(b).import_bundle(
            str(archive), str(b / "Presets" / "System"))
        assert result.status == "rejected"
        assert result.reason == "dependency_collision"
        assert not [p for p in (b / "Presets" / "System").iterdir()
                    if p.name.startswith("Look")]

    def test_same_content_reused_idempotently(self, roots, tmp_path):
        a, b = roots
        archive = _portable(roots, tmp_path / "original.cube")
        service = _svc(b)
        target = b / "Presets" / "System"
        assert service.import_bundle(str(archive), str(target)).succeeded
        assert service.import_bundle(
            str(archive), str(target)).succeeded       # 重名顺延
        assert len(list((b / "Presets" / "LUTs").glob("*.cube"))) == 1


class TestExportVerificationAndLimits:
    def test_final_archive_verified_against_manifest(self, roots, tmp_path):
        """导出后验证：包内字节与 manifest 不一致 → 拒绝且不留伪包。"""
        a, _ = roots
        lut = tmp_path / "look.cube"
        lut.write_bytes(LUT_BYTES)
        _seed(a / "Presets" / "User", "Look", lut_ref=str(lut))
        service = _svc(a)
        archive = a / "Look.toposbundle"

        real_build = service._build_export_manifest

        def build_with_wrong_hash(*args, **kwargs):
            manifest = real_build(*args, **kwargs)
            for entry in manifest.get("files", []):
                entry["sha256"] = "0" * 64          # 篡改 hash
            return manifest

        import unittest.mock as mock
        with mock.patch.object(service, "_build_export_manifest",
                               build_with_wrong_hash):
            result = service.export_bundle(
                str(a / "Presets" / "User"), "Look", str(archive))
        assert result.status == "rejected"
        assert result.reason == "hash_mismatch"
        assert not archive.exists()                # 伪完整包不落盘

    def test_export_respects_shared_member_limit(self, roots, tmp_path):
        a, _ = roots
        lut = tmp_path / "look.cube"
        lut.write_bytes(LUT_BYTES)
        _seed(a / "Presets" / "User", "Look", lut_ref=str(lut))
        service = _svc(a)
        import src.features.color.core.gallery_transfer_service as module
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(module, "IMPORT_MAX_FILES", 4)
            result = service.export_bundle(
                str(a / "Presets" / "User"), "Look",
                str(a / "Look.toposbundle"))
        assert result.status == "rejected"
        assert result.reason == "file_count_limit"
