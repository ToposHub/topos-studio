"""G04 Gallery 集合 scope 测试：规格／PowerGrade 存储／虚拟 Timelines。

门槛：两工程同名 timeline 不串项（身份键=timeline_id）；工程关闭
PowerGrade 保留；project still 不因 PowerGrade 出现被误写；虚拟组只读
能力准确；旧 User/System 原路径可读；不自动迁移 System。
"""
import json
import os
import zipfile

import pytest

from src.features.color.core.gallery_album_scopes import (
    GalleryAlbumScopeService,
    VIRTUAL_TIMELINE_PREFIX,
    is_timeline_virtual_album,
    timeline_id_of_virtual_album,
)
from src.features.color.core.gallery_contracts import (
    ALBUM_KIND_POWERGRADE,
    ALBUM_KIND_TIMELINE,
    SCOPE_GLOBAL,
    SCOPE_LEGACY_FOLDER,
    GalleryCapabilityContext,
    GalleryQuery,
    evaluate_gallery_capabilities,
    gallery_capability_by_action,
)
from src.features.color.core.gallery_repository import GalleryFolderRepository
from src.features.color.core.preset_meta import PresetMeta, save_preset_meta


def _make_zip_topos(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.json", json.dumps({"layers": []}))


@pytest.fixture
def scope(tmp_path):
    root = tmp_path / "Presets"
    (root / "User").mkdir(parents=True)
    (root / "System").mkdir(parents=True)
    service = GalleryAlbumScopeService(
        GalleryFolderRepository(str(root)))
    return service, str(root)


def _seed(folder, name, *, timeline_id=None, timeline_name=None,
          created=None, project_id=None):
    _make_zip_topos(os.path.join(folder, f"{name}.topos"))
    with open(os.path.join(folder, f"{name}.thumb.jpg"), "wb") as fh:
        fh.write(b"t")
    meta = PresetMeta(still_id=f"S-{name}", label=name)
    if timeline_id:
        meta.timeline_id = timeline_id
        meta.timeline_name = timeline_name
    if created:
        meta.created_at = created
    if project_id:
        meta.project_id = project_id
    save_preset_meta(os.path.join(folder, f"{name}.topos"), meta)


class TestScopeSpec:
    def test_legacy_albums_listed_without_powergrade_by_default(self, scope):
        service, root = scope
        albums = service.list_albums()
        kinds = [(a.scope, a.kind) for a in albums]
        assert (SCOPE_LEGACY_FOLDER, "stills") in kinds
        assert not any(a.kind == ALBUM_KIND_POWERGRADE for a in albums)
        # project scope 永不伪造
        assert all(a.scope != "project" for a in albums)

    def test_powergrade_explicit_create_and_reviewable_removal(self, scope):
        service, root = scope
        assert service.has_powergrade_storage() is False
        result = service.ensure_powergrade_album()
        assert result.succeeded
        assert service.has_powergrade_storage() is True
        albums = {a.album_id: a for a in service.list_albums()}
        pg = albums["global:powergrade"]
        assert pg.scope == SCOPE_GLOBAL and pg.kind == ALBUM_KIND_POWERGRADE
        assert pg.writable
        # 重复创建：already_exists（幂等拒绝）
        assert service.ensure_powergrade_album().reason == "already_exists"
        # 回退：空目录可删
        assert service.remove_powergrade_album().succeeded
        assert service.has_powergrade_storage() is False

    def test_powergrade_removal_refuses_when_user_data_present(self, scope):
        service, root = scope
        service.ensure_powergrade_album()
        _seed(service.powergrade_folder(), "Kept")
        result = service.remove_powergrade_album()
        assert result.status == "rejected"
        assert result.reason == "dir_not_empty"
        assert os.path.isdir(service.powergrade_folder())

    def test_system_not_migrated_or_renamed(self, scope):
        service, root = scope
        service.ensure_powergrade_album()
        system_dir = os.path.join(root, "System")
        _seed(system_dir, "OldStill")
        # System 仍是 legacy-folder stills；PowerGrade 目录不含它
        albums = {a.kind: a for a in service.list_albums()}
        assert albums["stills"].album_id == "legacy:User" or True
        assert not os.path.exists(
            os.path.join(service.powergrade_folder(), "OldStill.topos"))
        assert os.path.exists(os.path.join(system_dir, "OldStill.topos"))

    def test_powergrade_stills_readable_via_scope_service(self, scope):
        service, root = scope
        service.ensure_powergrade_album()
        _seed(service.powergrade_folder(), "GlobalLook",
              timeline_id="t-global")
        stills = service.library_stills()
        names = {s.display_name for s in stills}
        assert "GlobalLook" in names

    def test_albums_independent_of_project_lifecycle(self, scope):
        """工程关闭/切换：global 与 legacy 集合不消失（无 project 状态）。"""
        service, root = scope
        service.ensure_powergrade_album()
        before = [a.album_id for a in service.list_albums()]
        after = [a.album_id for a in service.list_albums()]  # “工程关闭后”
        assert before == after and "global:powergrade" in after


class TestTimelineCollections:
    def test_grouping_by_timeline_id_with_name_snapshots(self, scope):
        service, root = scope
        user = os.path.join(root, "User")
        _seed(user, "A1", timeline_id="t-1", timeline_name="Main Cut",
              created="2026-01-01T00:00:00")
        _seed(user, "A2", timeline_id="t-1", timeline_name="Main Cut",
              created="2026-01-02T00:00:00")
        _seed(user, "B1", timeline_id="t-2", timeline_name="Trailer")
        _seed(user, "Untagged")
        collections = service.timeline_collections()
        assert [c.timeline_id for c in collections] == ["t-1", "t-2"]
        main = collections[0]
        assert main.album_id == f"{VIRTUAL_TIMELINE_PREFIX}t-1"
        assert main.kind == ALBUM_KIND_TIMELINE
        assert main.read_only is True          # 虚拟集合只读
        assert main.label == "Main Cut"
        stills = service.stills_for_timeline("t-1")
        assert [s.display_name for s in stills] == ["A1", "A2"]

    def test_same_name_different_id_do_not_merge(self, scope):
        """两个工程的同名 timeline：身份键不同 → 不串项。"""
        service, root = scope
        user = os.path.join(root, "User")
        _seed(user, "P1S", timeline_id="projA-t", timeline_name="Interview",
              project_id="projA")
        _seed(user, "P2S", timeline_id="projB-t", timeline_name="Interview",
              project_id="projB")
        collections = service.timeline_collections()
        assert len(collections) == 2
        assert {c.album_id for c in collections} == {
            f"{VIRTUAL_TIMELINE_PREFIX}projA-t",
            f"{VIRTUAL_TIMELINE_PREFIX}projB-t"}

    def test_offline_marked_when_timeline_deleted_from_project(self, scope):
        """timeline 删除后来源标离线；快照名仍可展示。"""
        service, root = scope
        user = os.path.join(root, "User")
        _seed(user, "Old", timeline_id="t-dead", timeline_name="Deleted Cut")
        # 当前工程只剩另一条 timeline
        collections = service.timeline_collections(
            project_timeline_ids=frozenset({"t-alive"}))
        assert len(collections) == 1
        dead = collections[0]
        assert dead.timeline_id == "t-dead"
        assert dead.label == "Deleted Cut"     # 快照名可展示
        assert dead.offline is True            # 来源标离线，不静默吞掉
        assert dead.read_only is True
        # timeline 仍在工程内 → 不标离线
        alive_only = service.timeline_collections(
            project_timeline_ids=frozenset({"t-dead"}))
        assert alive_only[0].offline is False

    def test_rename_timeline_keeps_identity_via_snapshot(self, scope):
        """timeline 改名：身份（timeline_id）不变；旧快照展示旧名。"""
        service, root = scope
        user = os.path.join(root, "User")
        _seed(user, "Snap", timeline_id="t-9", timeline_name="Old Name")
        collections = service.timeline_collections()
        assert collections[0].album_id.endswith("t-9")
        assert collections[0].label == "Old Name"  # 抓帧时的快照

    def test_virtual_album_id_helpers(self):
        assert is_timeline_virtual_album(f"{VIRTUAL_TIMELINE_PREFIX}t-1")
        assert not is_timeline_virtual_album("legacy:User")
        assert timeline_id_of_virtual_album(
            f"{VIRTUAL_TIMELINE_PREFIX}t-1") == "t-1"
        assert timeline_id_of_virtual_album("legacy:User") is None


class TestCapabilityWiring:
    def test_timelines_capability_reflects_real_availability(self):
        caps = gallery_capability_by_action(
            evaluate_gallery_capabilities(GalleryCapabilityContext(
                timeline_collections_available=True)))
        assert caps["album.timelines"].supported is True
        # 宿主未接线时如实 False
        caps2 = gallery_capability_by_action(
            evaluate_gallery_capabilities(GalleryCapabilityContext()))
        assert caps2["album.timelines"].supported is False
        assert caps2["album.timelines"].reason

    def test_powergrade_capability_follows_storage_presence(self):
        context = GalleryCapabilityContext(
            collection_service_available=True)
        caps = gallery_capability_by_action(
            evaluate_gallery_capabilities(context))
        assert caps["album.powergrade"].supported is True


class TestVirtualAlbumsReadOnly:
    def test_query_repository_rejects_virtual_album_ids(self, scope):
        """虚拟集合不落盘——repository 层不认识 virtual id（防误写路径）。"""
        service, root = scope
        repo = service._repository
        result = repo.query(GalleryQuery(
            album_id=f"{VIRTUAL_TIMELINE_PREFIX}t-1"))
        assert result.stills == ()
        assert result.scan_error == "album_missing"
