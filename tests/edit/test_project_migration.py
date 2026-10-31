"""P2-13 工程内容 schema 版本与迁移管线测试。

- 纯管线：v1 历史工程走完整链、幂等、未来版本不盲迁、单步失败不阻断
- manifest：create 盖当前版本戳、to_dict/from_dict round-trip、衍生方法透传
- IO 集成：_migrate_content_payload 读取 manifest 版本并留日志
- 落地 round-trip：保存的 .topos 带当前版本戳；人工降级为 v1（模拟旧工程）
  后加载触发迁移且内容被修复
"""

from __future__ import annotations

import io as _io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.project_migration import (
    CONTENT_MIGRATIONS,
    EDIT_CONTENT_SCHEMA_VERSION,
    EDIT_CONTENT_VERSION_KEY,
    ContentMigration,
    migrate_project_payload,
    preview_project_migrations,
)
from src.shared.project.manifest import (
    CURRENT_EDIT_CONTENT_SCHEMA_VERSION,
    ToposManifest,
)


# ─────────────────────────────────────────────────────────────────────────────
# 纯管线
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestMigrationPipeline:

    def test_legacy_payload_runs_full_chain(self):
        data = {
            "assets": {"a1": {"metadata": None}},
            "timelines": {"t1": {"clips": []}},
        }
        migrated, log = migrate_project_payload(data)
        assert EDIT_CONTENT_VERSION_KEY in migrated
        assert migrated[EDIT_CONTENT_VERSION_KEY] == EDIT_CONTENT_SCHEMA_VERSION
        assert len(log) == len(CONTENT_MIGRATIONS)
        assert any("assets_metadata_dict" in e for e in log)
        assert migrated["assets"]["a1"]["metadata"] == {}

    def test_directional_blur_mapped_at_any_depth(self):
        data = {
            "timeline_sequences": {
                "s1": {
                    "video_tracks": [
                        {"clips": [
                            {"effects": [
                                {"params": {"blur_type": "directional"}},
                            ]},
                        ]},
                    ],
                },
            },
            "graphs": {"g1": {"nodes": [
                {"parameters": {"blur_type": "directional", "radius": 4}},
            ]}},
        }
        migrated, _ = migrate_project_payload(data)
        assert migrated["timeline_sequences"]["s1"]["video_tracks"][0][
            "clips"][0]["effects"][0]["params"]["blur_type"] == "motion"
        assert migrated["graphs"]["g1"]["nodes"][0][
            "parameters"]["blur_type"] == "motion"

    def test_idempotent_second_run_is_noop(self):
        data = {"assets": {"a": {"metadata": None}},
                "x": {"blur_type": "directional"}}
        once, log1 = migrate_project_payload(data)
        twice, log2 = migrate_project_payload(once)
        assert log2 == []
        assert twice == once

    def test_current_version_untouched(self):
        data = {EDIT_CONTENT_VERSION_KEY: EDIT_CONTENT_SCHEMA_VERSION,
                "assets": {"a": {"metadata": None}}}  # 已是当前版本：不迁移
        migrated, log = migrate_project_payload(data)
        assert log == []
        assert migrated["assets"]["a"]["metadata"] is None

    def test_future_version_not_migrated(self):
        data = {EDIT_CONTENT_VERSION_KEY: EDIT_CONTENT_SCHEMA_VERSION + 5,
                "assets": {"a": {"metadata": None}}}
        migrated, log = migrate_project_payload(data)
        assert migrated is data  # 原样返回
        assert len(log) == 1
        assert "较新版本" in log[0]

    def test_preview_is_dry_run(self):
        data = {"assets": {"a": {"metadata": None}}}
        preview = preview_project_migrations(data)
        assert len(preview) == len(CONTENT_MIGRATIONS)
        assert data["assets"]["a"].get("metadata") is None  # 未修改

    def test_chain_gap_stops_safely(self, monkeypatch):
        import src.features.edit.core.project_migration as pm

        broken = (ContentMigration(1, 2, "one", "d1", lambda d: d),)
        monkeypatch.setattr(pm, "CONTENT_MIGRATIONS", broken)
        data = {"assets": {"a": {"metadata": None}}}
        migrated, log = pm.migrate_project_payload(data)
        # v2 之后没有注册的迁移步骤：链断，保守停在 v2
        assert len(log) == 1
        assert migrated[EDIT_CONTENT_VERSION_KEY] == 2

    def test_single_step_failure_logged_not_raised(self, monkeypatch):
        import src.features.edit.core.project_migration as pm

        def boom(data):
            raise RuntimeError("boom")

        failing = (ContentMigration(1, 2, "boom", "炸", boom),)
        monkeypatch.setattr(pm, "CONTENT_MIGRATIONS", failing)
        migrated, log = pm.migrate_project_payload({"assets": {}})
        assert any("迁移失败" in e and "boom" in e for e in log)

    def test_non_dict_version_defaults_to_one(self):
        data = {EDIT_CONTENT_VERSION_KEY: "corrupt", "assets": {"a": {"metadata": None}}}
        migrated, log = migrate_project_payload(data)
        assert len(log) == len(CONTENT_MIGRATIONS)


# ─────────────────────────────────────────────────────────────────────────────
# manifest 版本戳
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestManifestVersion:

    def test_create_stamps_current_version(self):
        m = ToposManifest.create(project_id="p1")
        assert m.edit_content_schema_version == CURRENT_EDIT_CONTENT_SCHEMA_VERSION

    def test_roundtrip_preserves_version(self):
        m = ToposManifest.create(project_id="p1")
        back = ToposManifest.from_dict(m.to_dict())
        assert back.edit_content_schema_version == m.edit_content_schema_version

    def test_from_dict_missing_defaults_to_legacy_one(self):
        m = ToposManifest.from_dict({"project_id": "p1"})
        assert m.edit_content_schema_version == 1

    def test_derived_methods_carry_version(self):
        m = ToposManifest.create(project_id="p1")
        m2 = m.with_modified()
        m3 = m.with_active_timeline("t1")
        assert m2.edit_content_schema_version == m.edit_content_schema_version
        assert m3.edit_content_schema_version == m.edit_content_schema_version

    def test_edit_constant_matches_shared(self):
        assert EDIT_CONTENT_SCHEMA_VERSION == CURRENT_EDIT_CONTENT_SCHEMA_VERSION


# ─────────────────────────────────────────────────────────────────────────────
# IO 集成
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
class TestIOMigrationHook:

    def _io(self):
        from src.features.edit.core.edit_project_io import EditProjectIO
        return EditProjectIO()

    def test_manifest_version_drives_migration(self):
        io = self._io()
        full_data = {
            "manifest": {EDIT_CONTENT_VERSION_KEY: 1},
            "assets": {"a": {"metadata": None}},
        }
        migrated = io._migrate_content_payload(full_data)
        assert migrated["assets"]["a"]["metadata"] == {}
        assert len(io.last_content_migrations) == len(CONTENT_MIGRATIONS)

    def test_current_version_no_log(self):
        io = self._io()
        full_data = {
            "manifest": {EDIT_CONTENT_VERSION_KEY: EDIT_CONTENT_SCHEMA_VERSION},
            "assets": {},
        }
        migrated = io._migrate_content_payload(full_data)
        assert io.last_content_migrations == ()
        assert migrated is full_data

    def test_missing_manifest_still_migrates_as_legacy(self):
        io = self._io()
        migrated = io._migrate_content_payload({"assets": {"a": {}}})
        assert migrated["assets"]["a"]["metadata"] == {}
        assert io.last_content_migrations != ()

    def test_failure_returns_raw_payload(self, monkeypatch):
        import src.features.edit.core.edit_project_io as io_mod
        io = self._io()
        monkeypatch.setattr(
            io_mod, "_BROKEN_MARKER_", True, raising=False)
        # 直接破坏 migrate 函数（模拟框架级异常）
        import src.features.edit.core.project_migration as pm
        monkeypatch.setattr(
            pm, "migrate_project_payload",
            lambda d: (_ for _ in ()).throw(RuntimeError("x")))
        raw = {"assets": {"a": {"metadata": None}}}
        out = io._migrate_content_payload(raw)
        assert out is raw


# ─────────────────────────────────────────────────────────────────────────────
# 落地 round-trip：保存带版本戳；降级为 v1 后加载触发迁移
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
class TestSaveLoadRoundTrip:

    def _project(self):
        from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin

        asset = MediaAsset(
            asset_id="vid-1", path="/fake/vid.mp4", media_type="video",
            display_name="vid", metadata={"fps": 24.0, "total_frames": 100},
        )
        root = ProjectBin(bin_id="root", name="Root", parent_id=None,
                          asset_ids=("vid-1",))
        project = EditProject(
            project_id="p1", name="MigTest",
            assets={"vid-1": asset}, bins={"root": root}, root_bin_id="root",
        )
        # save_project 契约：sequences 键必须与 project.timelines 完全一致
        from src.features.edit.core.project import TimelineSequenceRef
        return project.with_timeline_ref_added(
            TimelineSequenceRef(timeline_id="seq-1", name="Main", fps=24.0),
            set_active=True,
        )

    def _sequence(self):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            Rational,
            TimelineSequence,
        )

        clip = ClipInstance(
            clip_id="c1", asset_id="vid-1", track_id="v1",
            timeline_start=0, timeline_duration=50,
            source_start=0, source_duration=50,
        )
        track = EditTrack(track_id="v1", name="V1", track_type="video",
                          index=0, clips=(clip,))
        return TimelineSequence(
            timeline_id="seq-1", name="Main", fps=Rational(24, 1),
            video_tracks=(track,),
        )

    def _save(self, tmp_path, io):
        out = str(tmp_path / "mig.topos")
        assert io.save_project(out, self._project(),
                               timeline_sequences={"seq-1": self._sequence()},
                               create_backup=False)
        return out

    def test_saved_package_carries_current_version(self, tmp_path):
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        path = self._save(tmp_path, io)
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest[EDIT_CONTENT_VERSION_KEY] == EDIT_CONTENT_SCHEMA_VERSION

        loaded = io.load_project(path)
        assert loaded is not None
        assert io.last_content_migrations == ()

    def test_legacy_package_migrates_on_load(self, tmp_path):
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        path = self._save(tmp_path, io)

        # 降级为旧工程：manifest 版本改 1 + 素材 metadata 置空 + 注入 legacy blur
        with zipfile.ZipFile(path) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        manifest = json.loads(entries["manifest.json"])
        manifest[EDIT_CONTENT_VERSION_KEY] = 1
        entries["manifest.json"] = json.dumps(manifest).encode()
        media_pool = json.loads(entries["edit/media_pool.json"])
        first_id = next(iter(media_pool))
        media_pool[first_id]["metadata"] = None
        entries["edit/media_pool.json"] = json.dumps(media_pool).encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, blob in entries.items():
                zf.writestr(name, blob)

        loaded = io.load_project(path)
        assert loaded is not None
        log = io.last_content_migrations
        assert any("assets_metadata_dict" in e for e in log)
        assert any("blur_directional_to_motion" in e for e in log)
        # 修复后的资产 metadata 是 dict
        project = loaded[0]
        assert project.assets[first_id].metadata == {}
