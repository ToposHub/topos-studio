"""N11 · 归档依赖闭包测试。

审计句：asset_usage_index 只有 clip＋绑定 WAV——快照保留源/候选 take
未收集，include_unused=False 会跳过快照唯一引用素材（审计复现）；
LUT records hash 为空、archived_path 用原机绝对路径（与媒体相对路径
契约不同）。
"""

from __future__ import annotations

import sys
from dataclasses import replace as _dc_replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.asset_usage_index import (  # noqa: E402
    build_asset_usage_index)
from src.features.edit.core.project import (  # noqa: E402
    EditProject, MediaAsset, ProjectBin)
from src.features.edit.core.project_archive import collect_project
from src.features.edit.core.project_snapshots import take_snapshot


def _project_with_assets(tmp_path: Path, n: int = 2):
    assets = {}
    for i in range(n):
        p = tmp_path / f"m{i}.mov"
        p.write_bytes(bytes([i]) * 32)
        assets[f"a{i}"] = MediaAsset(f"a{i}", str(p), "video", f"m{i}", {})
    return EditProject(
        project_id="n11-proj", name="N11", assets=assets,
        bins={"root": ProjectBin("root", "Root", None, ())},
        root_bin_id="root")


def _seq_ref(asset_id: str):
    """最小序列替身（索引只读 track/clips 属性）。"""
    from types import SimpleNamespace as NS
    clip = NS(clip_id="c1", asset_id=asset_id,
              timeline_start=0, timeline_duration=10)
    track = NS(track_id="t1", name="V1", track_type="video", clips=(clip,))
    return NS(timeline_id="seq1", name="S1", all_tracks=(track,))


class TestSnapshotClosure:
    def test_snapshot_only_asset_is_used(self, tmp_path):
        project = _project_with_assets(tmp_path)
        # a0 有 clip；a1 无 clip——拍快照（payload 含全部资产表）
        project, _meta = take_snapshot(project, "snap1")
        index = build_asset_usage_index([_seq_ref("a0")], project)
        assert index.snapshot_only_asset_ids == ("a1",)
        assert index.is_used("a1") is True  # 快照引用 = used
        assert "a1" in index.used_asset_ids()
        assert index.unused_asset_ids == ()  # 不再落入 unused

    def test_no_snapshot_unused_unchanged(self, tmp_path):
        project = _project_with_assets(tmp_path)
        index = build_asset_usage_index([_seq_ref("a0")], project)
        assert index.unused_asset_ids == ("a1",)
        assert index.snapshot_only_asset_ids == ()

    def test_collect_keeps_snapshot_only_media(self, tmp_path):
        """审计复现修复：include_unused=False 不丢快照唯一引用素材。"""
        project = _project_with_assets(tmp_path)
        project, _meta = take_snapshot(project, "keep")
        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch"),
            sequences=[_seq_ref("a0")], include_unused=False)
        collected = {r.asset_id for r in report.records
                     if r.status == "copied"}
        assert "a1" in collected  # 快照源保留
        skipped = set(report.skipped_unused or ())
        assert "a1" not in skipped


class TestLutRecordContract:
    def test_lut_record_relative_hashed(self, tmp_path):
        lut = tmp_path / "grade.cube"
        lut.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 24, encoding="ascii")
        media = tmp_path / "m.mov"
        media.write_bytes(b"M" * 16)
        from src.shared.project.color_stack import ColorStackData
        project = EditProject(
            project_id="n11-lut", name="L",
            assets={"a0": MediaAsset("a0", str(media), "video", "m", {})},
            bins={"root": ProjectBin("root", "Root", None, ())},
            root_bin_id="root",
            color_stacks={"s1": ColorStackData(
                stack_id="s1", asset_id="a0", name="S",
                lut_path=str(lut))})
        _archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch"),
            sequences=[], include_unused=True)
        lut_records = [r for r in report.records
                       if r.asset_id.startswith("lut:")]
        assert lut_records, "应收集到 LUT 记录"
        rec = lut_records[0]
        assert rec.archived_path.startswith("luts/")  # 相对路径契约
        assert not rec.archived_path.startswith("/")  # 不再绝对路径
        assert len(rec.sha256) == 64  # hash 非空
        assert rec.file_size > 0


class TestCompositeGraphLutClosure:
    def test_composition_lut_collected(self, tmp_path):
        """N11 余项：Composite 图 lut 节点的外部 LUT 随归档。"""
        from types import SimpleNamespace as NS
        lut = tmp_path / "comp.cube"
        lut.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 24, encoding="ascii")
        media = tmp_path / "m.mov"
        media.write_bytes(b"M" * 16)
        comp = NS(nodes=[{"node_type": "lut",
                          "parameters": {"lut_path": str(lut)}}])
        project = EditProject(
            project_id="n11-comp", name="C",
            assets={"a0": MediaAsset("a0", str(media), "video", "m", {})},
            bins={"root": ProjectBin("root", "Root", None, ())},
            root_bin_id="root",
            compositions={"comp1": comp})
        _archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch"),
            sequences=[], include_unused=True)
        lut_records = [r for r in report.records
                       if r.asset_id == f"lut:comp.cube"]
        assert lut_records and lut_records[0].status == "copied"


class TestSnapshotPayloadPathRewrite:
    def test_media_and_lut_paths_rewritten_in_snapshots(self, tmp_path):
        """N11 尾项：归档后保留快照 payload 内路径指向包内。"""
        lut = tmp_path / "g.cube"
        lut.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 24, encoding="ascii")
        media = tmp_path / "snap_src.mov"
        media.write_bytes(b"S" * 32)
        project = _project_with_assets(tmp_path, n=1)
        # 栈带 LUT 后拍快照（payload 含当时资产表+栈）
        from src.shared.project.color_stack import ColorStackData
        from dataclasses import replace as _r
        project = _r(
            project,
            color_stacks={"s1": ColorStackData(
                stack_id="s1", asset_id="a0", name="S",
                grading_params={"exposure": 0.0},
                lut_path=str(lut))})
        project, _meta = take_snapshot(project, "keep")
        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch"),
            sequences=[], include_unused=True)
        entry = archived.project_snapshots["keep"]
        payload = entry["payload"]
        blob = repr(payload)
        old_media = str(media)
        old_lut = str(lut)
        assert old_media not in blob  # 原机媒体路径不再出现
        assert old_lut not in blob    # 原机 LUT 路径不再出现
        assert str(tmp_path / "arch") in blob  # 包内路径已写入
