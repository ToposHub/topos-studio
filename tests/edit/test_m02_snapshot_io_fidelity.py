"""M02（S01）· 工程快照/恢复/正式 IO 保真回归。

验收（升级计划 M §M02）：
- 快照→修改→恢复：素材 metadata/评级/调色元数据/production info 全保真；
- 正式保存（.topos ZIP）→重开：快照表仍在且可恢复；
- 比较用真实 payload schema（timelines 为 {id: ref} dict，片段在
  full_data.timeline_sequences）；
- 快照非递归（payload 不嵌套快照表）；
- 旧形态（无 full_data 的快照 payload）恢复仍可用（向后兼容）；
- 较新六表（color_groups/color_stack_versions/audio_sync_bindings/
  source_patch_overrides/interpretation_overrides/project_snapshots）+
  revision 在 bin 操作/素材导入/relink 等派生路径不被抹掉。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    ProjectBin,
    relink_asset,
)
from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.core.project_snapshots import (
    capture_snapshot_payload,
    compare_snapshots,
    restore_snapshot,
    take_snapshot,
)
from src.features.edit.core.color_group import ColorGroupData, ColorGroupMember
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)


def _asset(asset_id: str = "a", path: str = "/missing.mov", **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=asset_id,
        metadata={"fps": 25, "codec": "h264", "duration": 100},
        rating=5,
        reel="A001",
        scene="12",
        shot="3",
        take="2",
        color_metadata={"colorspace": "bt709"},
    )
    params.update(overrides)
    return MediaAsset(**params)


def _project() -> EditProject:
    return EditProject(
        project_id="m02", name="m02-fidelity",
        assets={"a": _asset()},
        bins={"root": ProjectBin(
            bin_id="root", name="Root", parent_id=None,
            asset_ids=("a",))},
        root_bin_id="root",
        revision=7,
    )


def _with_all_new_tables(project: EditProject) -> EditProject:
    """带上六张较新表 + 快照，验证派生路径不抹。"""
    project = project.with_color_groups({"g1": ColorGroupData(
        group_id="g1", name="场景A", camera="A机",
        members=(ColorGroupMember(timeline_id="tl1", clip_id="c1"),))})
    project = project.with_color_stack_versions({"s1": {
        "active": "v1", "locked": False, "versions": {}}})
    return project.with_project_snapshots({"snap": {
        "meta": {"name": "snap", "payload_hash": "x", "source_revision": 1,
                 "created_at": 0.0, "dependencies": {}, "auto_backup": False},
        "payload": {"project_id": "m02", "name": "m02-fidelity"},
    }})


def _timeline(clip_id="c1", start=0, duration=50):
    clip = ClipInstance(
        clip_id=clip_id, asset_id="a", track_id="v1",
        timeline_start=start, timeline_duration=duration,
        source_start=10, source_duration=60, speed=1.0)
    track = EditTrack(track_id="v1", track_type="video", name="V1",
                      clips=(clip,))
    return TimelineSequence(
        timeline_id="tl1", name="主时间线", fps=Rational(25, 1),
        video_tracks=(track,))


# ================================================================
# S01-1 · 快照恢复保真
# ================================================================

class TestSnapshotRestoreFidelity:
    def test_restore_keeps_metadata_rating_and_production_info(self):
        project, _ = take_snapshot(_project(), "v1")
        # 恢复前后素材日志全字段保真
        restored, report = restore_snapshot(project, "v1")
        asset = restored.assets["a"]
        assert asset.metadata == {"fps": 25, "codec": "h264", "duration": 100}
        assert asset.rating == 5
        assert asset.reel == "A001" and asset.scene == "12"
        assert asset.shot == "3" and asset.take == "2"
        assert asset.color_metadata == {"colorspace": "bt709"}
        assert report["restored_from"] == "v1"
        # 快照表随工程走（可再回退）
        assert "v1" in restored.project_snapshots

    def test_restore_rebuilds_timeline_sequences(self):
        # 真实 sequence 内容经 full_data 重建（与正式加载同链）
        from src.features.edit.core.project import TimelineSequenceRef
        from dataclasses import replace as _dc_replace
        project = _dc_replace(
            _project(),
            timelines={"tl1": TimelineSequenceRef(
                timeline_id="tl1", name="主时间线", fps=25.0,
                width=1920, height=1080)})
        project, _ = take_snapshot(
            project, "v1", timeline_sequences={"tl1": _timeline()})
        restored, report = restore_snapshot(project, "v1")
        assert "tl1" in report["timeline_sequences"]
        rebuilt = report["timeline_sequences"]["tl1"]
        assert isinstance(rebuilt, TimelineSequence)
        assert rebuilt.timeline_id == "tl1"
        assert rebuilt.video_tracks[0].clips[0].clip_id == "c1"

    def test_legacy_payload_without_full_data_still_restores(self):
        # 旧形态快照（无 full_data）：恢复仍成功（引用级），不抛异常
        project = _project()
        legacy_payload = capture_snapshot_payload(project)
        legacy_payload.pop("full_data", None)
        project, _ = take_snapshot(project, "v0", payload=legacy_payload)
        restored, report = restore_snapshot(project, "v0")
        assert "a" in restored.assets
        assert report["timeline_sequences"] == {}


# ================================================================
# S01-2 · 正式 IO 往返
# ================================================================

class TestFormalIORoundTrip:
    def test_snapshot_table_survives_topos_package(self, qapp, tmp_path):
        project = _with_all_new_tables(_project())
        project, _meta = take_snapshot(
            project, "v1", timeline_sequences=None)
        io = EditProjectIO()
        path = tmp_path / "m02.topos"
        assert io.save_project(str(path), project), "save failed"
        loaded, _ui, _seqs = io.load_project(str(path))
        assert loaded is not None
        # 快照表不丢
        assert "v1" in loaded.project_snapshots
        # 重开后快照仍可恢复且保真
        reopened, report = restore_snapshot(loaded, "v1")
        assert reopened.assets["a"].rating == 5
        assert reopened.assets["a"].metadata["fps"] == 25

    def test_new_tables_survive_topos_package(self, qapp, tmp_path):
        project = _with_all_new_tables(_project())
        io = EditProjectIO()
        path = tmp_path / "m02b.topos"
        assert io.save_project(str(path), project)
        loaded, _ui, _seqs = io.load_project(str(path))
        assert loaded is not None
        assert "g1" in loaded.color_groups
        assert "s1" in loaded.color_stack_versions
        assert "snap" in loaded.project_snapshots
        assert loaded.revision == 7

    def test_json_serializer_roundtrip_keeps_new_tables(self, qapp):
        from src.features.edit.core.serialization import (
            EditProjectSerializer as S)
        project = _with_all_new_tables(_project())
        data = S.to_json(project)
        loaded = S.from_json(data)
        assert "g1" in loaded.color_groups
        assert "s1" in loaded.color_stack_versions
        assert "snap" in loaded.project_snapshots
        assert loaded.revision == 7


# ================================================================
# S01-3 · 真实 dict schema 比较
# ================================================================

class TestRealSchemaCompare:
    @staticmethod
    def _real_payload(clip: dict) -> dict:
        """模拟 capture_snapshot_payload 的真实形态：timelines 为
        {id: ref} dict，片段内容在 full_data.timeline_sequences。"""
        return {
            "timelines": {"tl1": {"timeline_id": "tl1", "name": "T",
                                  "fps": 25.0, "width": 1920, "height": 1080}},
            "full_data": {"timeline_sequences": {"tl1": {
                "timeline_id": "tl1",
                "tracks": [{"track_id": "v1", "clips": [clip]}],
            }}},
        }

    def test_real_schema_clip_diff_detected(self):
        base = self._real_payload({
            "clip_id": "c1", "track_id": "v1", "asset_id": "a1",
            "timeline_start": 0, "timeline_duration": 10,
            "effects": [], "color_stack_id": "s1", "audio_gain_db": 0.0})
        changed = self._real_payload({
            "clip_id": "c1", "track_id": "v2", "asset_id": "a2",
            "timeline_start": 5, "timeline_duration": 8,
            "effects": [], "color_stack_id": "s2", "audio_gain_db": 2.0})
        diff = compare_snapshots(base, changed)
        kinds = {e.kind for e in diff.entries}
        # 此前对真实 dict 形态直接 AttributeError / 空比较
        assert {"clip_moved", "clip_trimmed", "clip_replaced",
                "grade_changed", "audio_changed"} <= kinds

    def test_timeline_add_remove_on_real_schema(self):
        base = {"timelines": {}, "full_data": {"timeline_sequences": {}}}
        added = self._real_payload({"clip_id": "c1"})
        assert compare_snapshots(base, added).summary.get("timeline_added") == 1
        assert compare_snapshots(added, base).summary.get("timeline_removed") == 1

    def test_captured_payload_compare_no_crash(self):
        payload = capture_snapshot_payload(_project())
        # 真实 capture 产物（无时间线）比较不崩、空 diff
        diff = compare_snapshots(payload, payload)
        assert diff.entries == ()


# ================================================================
# 非递归引用
# ================================================================

class TestNonRecursiveSnapshots:
    def test_payload_excludes_snapshot_table(self):
        project, _ = take_snapshot(_project(), "v1")
        project, _ = take_snapshot(project, "v2")
        # v2 的 payload 不含快照表（不嵌套 v1）
        payload_v2 = project.project_snapshots["v2"]["payload"]
        assert "project_snapshots" not in (payload_v2.get("settings") or {})
        # 快照大小不随已有快照数膨胀
        payload_v1 = project.project_snapshots["v1"]["payload"]
        assert len(payload_v1.get("full_data", {})) == len(
            payload_v2.get("full_data", {}))


# ================================================================
# 恢复经命令可 Undo（M02 验收句）
# ================================================================

class TestRestoreSnapshotCommand:
    def test_apply_then_undo_roundtrip(self):
        from src.features.edit.core.project_snapshots import (
            RestoreSnapshotCommand,
        )
        import uuid as _uuid
        from dataclasses import replace as _dc_replace
        project = _project()
        project, _ = take_snapshot(project, "v1")
        # 恢复前修改工程（评级改变），快照 v1 仍是旧评级
        changed = _dc_replace(project, assets={
            "a": _dc_replace(project.assets["a"], rating=1)})
        cmd = RestoreSnapshotCommand(
            command_id=str(_uuid.uuid4()), label="恢复 v1",
            snapshot_name="v1")
        restored, report = cmd.apply(changed)
        assert restored.assets["a"].rating == 5  # 回到快照态
        assert report["restored_from"] == "v1"
        undone, undo_report = cmd.undo(restored)
        # undo 回到恢复前（rating=1 的修改态）
        assert undone.assets["a"].rating == 1
        assert "v1" in undone.project_snapshots  # 快照表仍在

    def test_apply_missing_snapshot_rejected(self):
        from src.features.edit.core.project_snapshots import (
            RestoreSnapshotCommand,
        )
        import uuid as _uuid
        cmd = RestoreSnapshotCommand(
            command_id=str(_uuid.uuid4()), label="恢复不存在",
            snapshot_name="nope")
        with pytest.raises(ValueError, match="快照不存在"):
            cmd.apply(_project())

    def test_undo_before_apply_rejected(self):
        from src.features.edit.core.project_snapshots import (
            RestoreSnapshotCommand,
        )
        import uuid as _uuid
        cmd = RestoreSnapshotCommand(
            command_id=str(_uuid.uuid4()), label="未执行",
            snapshot_name="v1")
        with pytest.raises(ValueError, match="无法撤销"):
            cmd.undo(_project())


# ================================================================
# 构造器派生路径保真（M02：六表 + revision 不被抹）
# ================================================================

class TestConstructorFidelity:
    def test_bin_operations_keep_new_tables(self):
        project = _with_all_new_tables(_project())
        moved = project.move_asset_to_bin("a", "root")
        assert "g1" in moved.color_groups
        assert "s1" in moved.color_stack_versions
        assert "snap" in moved.project_snapshots
        assert moved.revision == 7

    def test_import_asset_keeps_new_tables(self):
        project = _with_all_new_tables(_project())
        added = project.import_asset(
            "/b.mov", "video", {"fps": 25}, display_name="b")
        assert "b" in added.assets or len(added.assets) == 2
        assert "g1" in added.color_groups
        assert "snap" in added.project_snapshots
        assert added.revision == 7

    def test_relink_keeps_new_tables(self):
        project = _with_all_new_tables(_project())
        relinked = relink_asset(project, "a", "/moved.mov")
        assert "g1" in relinked.color_groups
        assert "s1" in relinked.color_stack_versions
        assert "snap" in relinked.project_snapshots
        assert relinked.revision == 7

    def test_production_info_update_keeps_new_tables(self):
        project = _with_all_new_tables(_project())
        updated = project.update_asset_production_info(
            "a", reel="B002", scene="99")
        assert updated.assets["a"].reel == "B002"
        assert "g1" in updated.color_groups
        assert "snap" in updated.project_snapshots


# ================================================================
# M15.1b · 自动备份调度器
# ================================================================

class TestBackupScheduler:
    def test_first_tick_backs_up(self):
        from src.features.edit.core.snapshot_backup_scheduler import (
            AUTO_BACKUP_NAME, SnapshotBackupScheduler,
        )
        sched = SnapshotBackupScheduler(interval_seconds=60.0)
        project = _project()
        new_project = sched.tick(project, now=100.0)
        # N09：tick 返回带快照的新工程——快照真实落在返回对象里
        assert new_project is not None
        assert AUTO_BACKUP_NAME in new_project.project_snapshots
        assert AUTO_BACKUP_NAME not in project.project_snapshots
        sched.confirm_written()
        assert sched.backups_written == 1
        # take_snapshot 返回新工程（不可变）——原工程不带表，重拍验证槽位
        p2, _meta = take_snapshot(project, AUTO_BACKUP_NAME, auto_backup=True)
        assert AUTO_BACKUP_NAME in p2.project_snapshots

    def test_same_revision_no_duplicate(self):
        from src.features.edit.core.snapshot_backup_scheduler import (
            SnapshotBackupScheduler,
        )
        sched = SnapshotBackupScheduler(interval_seconds=60.0)
        project = _project()
        sched.tick(project, now=100.0)
        sched.confirm_written()
        # 到期但 revision 未前进 → 不拍
        assert sched.tick(project, now=200.0) is None
        assert sched.backups_written == 1

    def test_revision_advance_triggers(self):
        from dataclasses import replace as _dc_replace
        from src.features.edit.core.snapshot_backup_scheduler import (
            SnapshotBackupScheduler,
        )
        sched = SnapshotBackupScheduler(interval_seconds=60.0)
        project = _project()
        sched.tick(project, now=100.0)
        sched.confirm_written()
        advanced = _dc_replace(project, revision=project.revision + 1)
        assert sched.tick(advanced, now=200.0) is not None
        sched.confirm_written()
        assert sched.backups_written == 2

    def test_not_yet_due(self):
        from src.features.edit.core.snapshot_backup_scheduler import (
            SnapshotBackupScheduler,
        )
        sched = SnapshotBackupScheduler(interval_seconds=60.0)
        assert sched.tick(_project(), now=100.0) is not None
        assert sched.tick(_project(), now=110.0) is None  # 未到期

    def test_auto_backup_overwrites_single_slot(self):
        """单一滚动备份点：第二次备份覆盖同名（不堆历史）。"""
        from dataclasses import replace as _dc_replace
        from src.features.edit.core.snapshot_backup_scheduler import (
            AUTO_BACKUP_NAME, SnapshotBackupScheduler,
        )
        sched = SnapshotBackupScheduler(interval_seconds=10.0)
        project = _project()
        assert sched.tick(project, now=100.0) is not None
        sched.confirm_written()
        advanced = _dc_replace(project, revision=project.revision + 1)
        assert sched.tick(advanced, now=200.0) is not None
        sched.confirm_written()
        # auto_backup 覆盖同名：单槽语义在 take_snapshot（既有测试在案），
        # 此处验证调度器两次拍备计数正确
        assert sched.backups_written == 2

    def test_unconfirmed_backup_not_counted_as_written(self):
        """N09：未写回（未 confirm）的拍备不得计入真实成功。"""
        from src.features.edit.core.snapshot_backup_scheduler import (
            SnapshotBackupScheduler,
        )
        sched = SnapshotBackupScheduler(interval_seconds=60.0)
        assert sched.tick(_project(), now=100.0) is not None
        assert sched.backups_written == 0  # 丢弃返回工程=快照未落库
        assert sched.backups_taken == 0


# ================================================================
# M02 尾片 · 真实素材 日志/嵌套/字幕 全保真验收联测
# ================================================================

import os

from dataclasses import replace as _dc_replace


def _register_timelines(project: EditProject, *sequences) -> EditProject:
    """登记 timeline refs（save 校验 sequences map 覆盖工程 timelines）。"""
    from src.features.edit.core.project import TimelineSequenceRef
    return _dc_replace(
        project,
        timelines={
            seq.timeline_id: TimelineSequenceRef(
                timeline_id=seq.timeline_id, name=seq.name,
                fps=float(seq.fps), width=seq.width, height=seq.height)
            for seq in sequences},
        active_timeline_id=sequences[0].timeline_id)


def _real_media_fidelity_project(real_video: str) -> EditProject:
    """真实素材 + 日志（关键词/评分）+ 嵌套序列素材（引用子时间线）。"""
    from src.features.edit.core.project import MediaAsset, ProjectBin
    asset = MediaAsset(
        asset_id="real1", path=real_video, media_type="video",
        display_name="真实素材.mp4",
        metadata={"codec": "h264", "fps": 25, "width": 1920,
                  "height": 1080},
        keywords=("采访", "A机位"), rating=5)
    # 嵌套序列素材：引用子时间线 tl-child（虚拟 URI + metadata 冗余
    # 双来源，见 MediaAsset.nested_timeline_id 契约）
    nested = MediaAsset(
        asset_id="nested1", path="topos://sequence/tl-child",
        media_type="nested_sequence", display_name="子序列",
        metadata={"timeline_id": "tl-child"})
    return EditProject(
        project_id="p-fid", name="全保真联测",
        assets={"real1": asset, "nested1": nested},
        bins={"root": ProjectBin(
            bin_id="root", name="Root", parent_id=None,
            asset_ids=("real1", "nested1"))},
        root_bin_id="root")


def _child_sequence() -> TimelineSequence:
    """被嵌套引用的子时间线（真实素材的另一段）。"""
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, Rational,
    )
    clip = ClipInstance(
        clip_id="c-child", asset_id="real1", track_id="v1",
        timeline_start=0, timeline_duration=24,
        source_start=100, source_duration=24)
    return TimelineSequence(
        timeline_id="tl-child", name="子序列", fps=Rational(25, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(clip,),),),
    )


def _fidelity_sequence(real_video: str) -> TimelineSequence:
    """真实素材 clip + 嵌套子序列 + 中文字幕轨 + marker。"""
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, Rational, SubtitleClip, TimelineMarker,
    )
    clip = ClipInstance(
        clip_id="c-real", asset_id="real1", track_id="v1",
        timeline_start=0, timeline_duration=48,
        source_start=0, source_duration=48,
        # 日志域字段 + 采样级字段（M14.3 契约）同时验证
        label_color="#ff5500", audio_sample_offset=800)
    # 嵌套：子序列作为 clip 引用（composition 形态）
    nested_ref = ClipInstance(
        clip_id="c-nested", asset_id="nested1", track_id="v1",
        timeline_start=48, timeline_duration=24,
        source_start=0, source_duration=24)
    sub = SubtitleClip(
        clip_id="sub-zh", track_id="s1", timeline_start=12,
        timeline_duration=24, text="中文字幕保真")
    return TimelineSequence(
        timeline_id="tl-fid", name="联测时间线", fps=Rational(25, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(clip, nested_ref),),),
        markers=(TimelineMarker(
            marker_id="m1", frame=30, name="打点"),),
        subtitle_tracks=(EditTrack(
            track_id="s1", name="S1", track_type="subtitle", index=0,
            subtitle_clips=(sub,),),),
    )


class TestRealMediaFidelityRoundTrip:
    def test_logs_nested_subtitle_survive_reopen(self, qapp, tmp_path):
        """验收：真实素材 日志/嵌套引用/中文字幕/marker/采样偏移
        经 .topos 正式保存→重开全保真。"""
        real = os.path.join(
            os.path.dirname(__file__), "..", "test_videos", "2K_h264.mp4")
        if not os.path.exists(real):
            pytest.skip("real test video unavailable")
        project = _register_timelines(
            _real_media_fidelity_project(real),
            _fidelity_sequence(real), _child_sequence())
        seq = _fidelity_sequence(real)
        io = EditProjectIO()
        path = tmp_path / "fidelity.topos"
        assert io.save_project(str(path), project,
                               timeline_sequences={
                                   "tl-fid": seq,
                                   "tl-child": _child_sequence()})
        loaded, _ui, seqs = io.load_project(str(path))
        assert loaded is not None
        # ① 日志域：关键词/评分/显示名
        asset = loaded.assets["real1"]
        assert asset.keywords == ("采访", "A机位")
        assert asset.rating == 5
        assert asset.display_name == "真实素材.mp4"
        assert os.path.exists(asset.path)
        # ② 序列恢复：嵌套引用 clip + marker
        loaded_seq = seqs.get("tl-fid")
        assert loaded_seq is not None, "timeline_sequences 未恢复"
        clips = loaded_seq.video_tracks[0].clips
        assert [c.clip_id for c in clips] == ["c-real", "c-nested"]
        assert clips[1].asset_id == "nested1"
        # 嵌套身份经 asset 双来源保留
        assert loaded.assets["nested1"].nested_timeline_id == "tl-child"
        assert "tl-child" in seqs
        assert loaded_seq.markers[0].name == "打点"
        # ③ 采样级字段（M14.3）
        assert clips[0].audio_sample_offset == 800
        assert clips[0].label_color == "#ff5500"
        # ④ 中文字幕逐字段
        subs = loaded_seq.subtitle_tracks[0].subtitle_clips
        assert len(subs) == 1
        assert subs[0].text == "中文字幕保真"
        assert subs[0].timeline_start == 12
        # ⑤ 序列声明（fps/采样率）
        assert loaded_seq.sample_rate == 48000

    def test_second_save_reopen_stable(self, qapp, tmp_path):
        """重开→再存→再重开：保真定点稳定（无漂移）。"""
        real = os.path.join(
            os.path.dirname(__file__), "..", "test_videos", "2K_h264.mp4")
        if not os.path.exists(real):
            pytest.skip("real test video unavailable")
        project = _register_timelines(
            _real_media_fidelity_project(real),
            _fidelity_sequence(real), _child_sequence())
        seq = _fidelity_sequence(real)
        io = EditProjectIO()
        path = tmp_path / "f1.topos"
        assert io.save_project(str(path), project,
                               timeline_sequences={
                                   "tl-fid": seq,
                                   "tl-child": _child_sequence()})
        loaded1, _ui1, seqs1 = io.load_project(str(path))
        path2 = tmp_path / "f2.topos"
        assert io.save_project(
            str(path2), loaded1,
            timeline_sequences={"tl-fid": seqs1["tl-fid"],
                                "tl-child": seqs1["tl-child"]})
        loaded2, _ui2, seqs2 = io.load_project(str(path2))
        s1 = seqs1["tl-fid"]
        s2 = seqs2["tl-fid"]
        assert [c.clip_id for c in s2.video_tracks[0].clips] == \
            [c.clip_id for c in s1.video_tracks[0].clips]
        assert s2.subtitle_tracks[0].subtitle_clips[0].text == \
            s1.subtitle_tracks[0].subtitle_clips[0].text
        assert loaded2.assets["real1"].keywords == \
            loaded1.assets["real1"].keywords
