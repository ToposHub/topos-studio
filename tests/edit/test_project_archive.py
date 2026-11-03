"""P1-13 工程收集 / 归档（Collect / Archive）测试。

验收（计划表 P1-13）：
- "收集工程可在另一台机器打开"：收集 → EditProjectIO 保存 → 重新加载，
  素材路径全部指向归档目录内副本且哈希一致
- "缺失素材报告包含所有使用位置"
- include_unused=False 只收集时间线引用素材（整文件粒度 Collect）
- 真实素材（tests/test_videos）哈希校验
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.edit_core

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.project import EditProject, MediaAsset
from features.edit.core.project_archive import (
    STATUS_COPIED,
    STATUS_HASH_MISMATCH,
    STATUS_MISSING,
    collect_project,
)
from features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence


def _asset(asset_id: str, path: str, **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=os.path.basename(path),
        metadata={},
    )
    params.update(overrides)
    return MediaAsset(**params)


def _sequence(*clip_specs) -> TimelineSequence:
    clips = tuple(
        ClipInstance(
            clip_id=cid, asset_id=aid, track_id="v1",
            timeline_start=start, timeline_duration=dur,
            source_start=start, source_duration=dur,
        )
        for cid, aid, start, dur in clip_specs
    )
    return TimelineSequence(
        timeline_id="t1", name="Timeline 1", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(
            track_id="v1", name="V1", track_type="video", index=0, clips=clips,
        ),),
    )


class TestCollectBasics:
    def test_collect_copies_and_rewrites_paths(self, tmp_path) -> None:
        for name in ("a.mov", "b.mov"):
            (tmp_path / name).write_bytes(os.urandom(1024))
        project = EditProject(
            project_id="p1", name="proj",
            assets={
                "a1": _asset("a1", str(tmp_path / "a.mov")),
                "a2": _asset("a2", str(tmp_path / "b.mov")),
            },
            root_bin_id="root",
        )
        archive_dir = tmp_path / "archive"

        archived, report = collect_project(project, target_dir=str(archive_dir))

        assert report.passed
        assert len(report.copied) == 2
        for record in report.copied:
            assert record.archived_path.startswith("media/")
            dest = archive_dir / record.archived_path
            assert dest.is_file()
            # 内容一致（哈希在记录里）
            assert record.sha256 == _hash(dest)
        # 工程路径已重写进归档目录
        for asset in archived.assets.values():
            assert str(archive_dir) in asset.path
            assert os.path.exists(asset.path)

    def test_missing_media_reported_with_usage_locations(self, tmp_path) -> None:
        (tmp_path / "a.mov").write_bytes(b"x")
        project = EditProject(
            project_id="p1", name="proj",
            assets={
                "a1": _asset("a1", str(tmp_path / "a.mov")),
                "gone": _asset("gone", str(tmp_path / "gone.mov")),
            },
            root_bin_id="root",
        )
        seq = _sequence(("c1", "gone", 0, 24), ("c2", "gone", 24, 12))

        _, report = collect_project(
            project, target_dir=str(tmp_path / "arch"), sequences=[seq]
        )

        assert report.missing[0].asset_id == "gone"
        locations = report.missing_usage["gone"]
        assert len(locations) == 2
        assert any("Timeline 1" in loc for loc in locations)
        # 缺失不算失败：其余素材照常收集
        assert report.passed
        assert len(report.copied) == 1

    def test_collect_used_only(self, tmp_path) -> None:
        for name in ("used.mov", "unused.mov"):
            (tmp_path / name).write_bytes(name.encode())
        project = EditProject(
            project_id="p1", name="proj",
            assets={
                "u1": _asset("u1", str(tmp_path / "used.mov")),
                "u2": _asset("u2", str(tmp_path / "unused.mov")),
            },
            root_bin_id="root",
        )
        seq = _sequence(("c1", "u1", 0, 48))

        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch"),
            sequences=[seq], include_unused=False,
        )

        assert report.skipped_unused == ("u2",)
        assert [r.asset_id for r in report.copied] == ["u1"]
        # 未收集素材路径保持原位
        assert archived.assets["u2"].path.endswith("unused.mov")

    def test_name_collision_deduplicated(self, tmp_path) -> None:
        src1 = tmp_path / "d1"; src1.mkdir()
        (src1 / "clip.mov").write_bytes(b"one")
        src2 = tmp_path / "d2"; src2.mkdir()
        (src2 / "clip.mov").write_bytes(b"two")
        project = EditProject(
            project_id="p1", name="proj",
            assets={
                "a1": _asset("a1", str(src1 / "clip.mov")),
                "a2": _asset("a2", str(src2 / "clip.mov")),
            },
            root_bin_id="root",
        )

        _, report = collect_project(project, target_dir=str(tmp_path / "arch"))

        assert report.passed
        names = [os.path.basename(r.archived_path) for r in report.copied]
        assert len(set(names)) == 2  # clip.mov / clip_2.mov

    def test_virtual_assets_skipped(self, tmp_path) -> None:
        (tmp_path / "a.mov").write_bytes(b"x")
        project = EditProject(
            project_id="p1", name="proj",
            assets={
                "a1": _asset("a1", str(tmp_path / "a.mov")),
                "solid": _asset(
                    "solid", "", media_type="solid", display_name="纯色",
                ),
            },
            root_bin_id="root",
        )
        _, report = collect_project(project, target_dir=str(tmp_path / "arch"))
        assert [r.asset_id for r in report.records] == ["a1"]

    def test_report_serializable(self, tmp_path) -> None:
        (tmp_path / "a.mov").write_bytes(b"x")
        project = EditProject(
            project_id="p1", name="proj",
            assets={"a1": _asset("a1", str(tmp_path / "a.mov"))},
            root_bin_id="root",
        )
        _, report = collect_project(project, target_dir=str(tmp_path / "arch"))
        data = report.to_dict()
        assert data["schema"] == "topos.project_archive.v1"
        assert data["records"][0]["status"] == STATUS_COPIED
        assert data["records"][0]["sha256"]


def _hash(path) -> str:
    import hashlib
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class TestRealMediaAndPackage:
    def test_real_video_collect_hash_verified(self, tmp_path) -> None:
        real = os.path.join(
            os.path.dirname(__file__), "..", "test_videos", "2K_h264.mp4"
        )
        if not os.path.exists(real):
            pytest.skip("real test video unavailable")
        project = EditProject(
            project_id="p1", name="proj",
            assets={"real": _asset("real", real)},
            root_bin_id="root",
        )

        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch")
        )

        assert report.passed
        record = report.copied[0]
        assert record.status == STATUS_COPIED
        dest = tmp_path / "arch" / record.archived_path
        assert _hash(dest) == record.sha256 == _hash(real)
        assert archived.assets["real"].path == str(dest)

    def test_collected_project_reopens_on_archive(
        self, qapp, tmp_path
    ) -> None:
        """验收：收集 → 保存 → 重开，工程在归档目录内自洽。"""
        from features.edit.core.edit_project_io import EditProjectIO

        real = os.path.join(
            os.path.dirname(__file__), "..", "test_videos", "2K_h264.mp4"
        )
        if not os.path.exists(real):
            pytest.skip("real test video unavailable")

        project = EditProject(
            project_id="p1", name="proj",
            assets={"real": _asset("real", real, keywords=("采访",), rating=5)},
            root_bin_id="root",
        ).add_smart_bin(__import__(
            "features.edit.core.project", fromlist=["SmartBin"]
        ).SmartBin(
            bin_id="sb", name="访谈", filter_type="custom",
            filter_params={"keywords": ["采访"]},
        ))

        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch")
        )
        assert report.passed

        io = EditProjectIO()
        topos_path = tmp_path / "arch" / "collected.topos"
        assert io.save_project(str(topos_path), archived)
        loaded, _ui, _seqs = io.load_project(str(topos_path))

        asset = loaded.assets["real"]
        assert asset.path.startswith(str(tmp_path / "arch"))
        assert os.path.exists(asset.path)  # 媒体就在包旁，可直接打开
        assert asset.keywords == ("采访",)  # 日志随包保留
        assert "sb" in loaded.smart_bins

    def test_hash_mismatch_detected_via_direct_tamper(self, tmp_path, monkeypatch) -> None:
        """校验逻辑真实生效：篡改 copy 后产物 → hash_mismatch。"""
        import shutil as _shutil

        (tmp_path / "a.mov").write_bytes(b"original")
        project = EditProject(
            project_id="p1", name="proj",
            assets={"a1": _asset("a1", str(tmp_path / "a.mov"))},
            root_bin_id="root",
        )

        real_copy2 = _shutil.copy2

        def tamper(src, dst, *args, **kwargs):
            real_copy2(src, dst, *args, **kwargs)
            if str(dst).endswith(".mov"):
                with open(dst, "ab") as f:
                    f.write(b"tampered")

        monkeypatch.setattr(
            "src.features.edit.core.project_archive.shutil.copy2", tamper
        )

        _, report = collect_project(project, target_dir=str(tmp_path / "arch"))
        assert report.failures[0].status == STATUS_HASH_MISMATCH
        assert not report.passed
        assert any("mismatch" in w.lower() for w in report.warnings)


# ================================================================
# M20（R03）· 归档依赖闭包：仅经绑定使用的 WAV 不是 unused
# ================================================================

class TestArchiveBindingClosure:
    def test_bound_wav_not_unused_and_included_in_archive(self, tmp_path):
        """include_unused=False 的归档必须包含绑定 WAV（R03 验收句：
        仅经绑定使用的 WAV 仍在依赖闭包内）。"""
        import os
        from src.features.edit.core.audio_sync import AudioSyncBinding
        from src.features.edit.core.asset_usage_index import (
            build_asset_usage_index,
        )
        from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin
        from src.features.edit.core.project_archive import collect_project

        video = MediaAsset("vid", str(tmp_path / "v.mov"), "video", "v", {})
        wav = MediaAsset("wav", str(tmp_path / "w.wav"), "audio", "w", {})
        (tmp_path / "v.mov").write_bytes(b"video-bytes")
        (tmp_path / "w.wav").write_bytes(b"wav-bytes")
        # 绑定（keep_camera_audio=True 也保留——工程恢复需要 WAV）
        project = EditProject(
            project_id="p", name="P",
            assets={"vid": video, "wav": wav},
            bins={"root": ProjectBin(
                bin_id="root", name="Root", parent_id=None,
                asset_ids=("vid", "wav"))},
            root_bin_id="root")
        from dataclasses import replace as _dc_replace
        project = _dc_replace(
            project,
            audio_sync_bindings={"vid": AudioSyncBinding(
                wav_asset_id="wav", offset_seconds=1.0,
                method="manual", keep_camera_audio=True)})

        index = build_asset_usage_index([], project)
        # 索引：绑定 WAV 不在 unused，且计入 used（供 include_unused=False）
        assert "wav" not in index.unused_asset_ids
        assert "wav" in index.used_asset_ids()
        assert "wav" in index.binding_only_asset_ids

        # 归档闭包：include_unused=False 仍收集 WAV（vid 经 clip 引用，
        # wav 仅经绑定——collect 的 used_ids 来自 usage index）
        seq = _sequence(("c1", "vid", 0, 48))
        _archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch"),
            sequences=[seq], include_unused=False)
        archived_ids = {r.asset_id for r in report.copied}
        assert "wav" in archived_ids
        assert "vid" in archived_ids
        assert "wav" not in report.skipped_unused


class TestArchiveLutClosure:
    def test_lut_collected_and_missing_reported(self, tmp_path):
        """M20.2：调色栈 LUT 随归档收集；缺失 LUT 明示（不静默丢）。"""
        from types import SimpleNamespace as NS
        from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin
        from src.features.edit.core.project_archive import collect_project

        lut = tmp_path / "look.cube"
        lut.write_text("LUT_3D_SIZE 2\n0 0 0\n1 1 1\n" + "0 0 0\n" * 6)
        stack = NS(stack_id="s1", asset_id="a", name="S",
                   lut_path=str(lut), layers=[])
        project = EditProject(
            project_id="p", name="P",
            assets={},
            bins={"root": ProjectBin(bin_id="root", name="Root",
                                     parent_id=None, asset_ids=())},
            root_bin_id="root")
        from dataclasses import replace as _dc_replace
        project = _dc_replace(project, color_stacks={"s1": stack})

        _arch, report = collect_project(
            project, target_dir=str(tmp_path / "arch"), sequences=[])
        lut_copied = [r for r in report.copied
                      if r.asset_id.startswith("lut:")]
        assert len(lut_copied) == 1
        # archived_path 契约是相对归档根目录（collect_project 报告字段注释）
        assert os.path.isfile(
            os.path.join(str(tmp_path / "arch"), lut_copied[0].archived_path))

        # 缺失 LUT：明示警告，不阻断
        stack2 = NS(stack_id="s2", asset_id="a", name="S2",
                    lut_path="/gone/missing.cube", layers=[])
        project2 = _dc_replace(project, color_stacks={"s2": stack2})
        _a2, report2 = collect_project(
            project2, target_dir=str(tmp_path / "arch2"), sequences=[])
        assert any("LUT 缺失" in w for w in report2.warnings)


# ================================================================
# M20.2b · 字体/插件/模型身份闭包（dependencies.json 随归档）
# ================================================================

class TestIdentityManifest:
    def _project(self):
        from src.features.edit.core.project import EditProject, ProjectBin
        return EditProject(
            project_id="p", name="P", assets={},
            bins={"root": ProjectBin(bin_id="root", name="Root",
                                     parent_id=None, asset_ids=())},
            root_bin_id="root")

    def _subtitle_sequence(self, family: str) -> TimelineSequence:
        from features.edit.core.timeline_v2 import SubtitleClip
        track = EditTrack(
            track_id="s1", name="S1", track_type="subtitle", index=0,
            subtitle_clips=(SubtitleClip(
                clip_id="sub", track_id="s1", timeline_start=0,
                timeline_duration=24, text="hi", font_family=family),))
        return TimelineSequence(
            timeline_id="t1", name="TL", fps=Rational(24, 1),
            subtitle_tracks=(track,))

    def test_manifest_written_with_unresolved_font_warning(self, tmp_path):
        """未解析字体：dependencies.json 记身份 + 明示警告（不静默换）。"""
        import json
        project = self._project()
        seq = self._subtitle_sequence("NoSuchFontFamilyXYZ")
        _, report = collect_project(
            project, target_dir=str(tmp_path / "arch"), sequences=[seq])
        manifest_path = tmp_path / "arch" / "dependencies.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["schema"] == "topos.archive_dependencies.v1"
        fonts = manifest["fonts"]
        assert len(fonts) == 1 and fonts[0]["family"] == "NoSuchFontFamilyXYZ"
        assert fonts[0]["status"] == "unresolved"
        assert "目标机需自备" in fonts[0]["detail"]
        assert any("NoSuchFontFamilyXYZ" in w and "字体" in w
                   for w in report.warnings)
        # 报告快照与磁盘一致
        assert report.identity_manifest["fonts"] == fonts

    def test_resolved_font_records_sha256_not_distributed(self,
                                                          tmp_path,
                                                          monkeypatch):
        """本机解析到字体：记路径+sha256 身份，但不复制进归档。"""
        import json
        # 本文件头部经 sys.path 以 features.* 导入（与 src.* 是两个模块
        # 实例）——patch 必须落在 collect_project 实际执行的模块上
        from features.edit.core import project_archive as pa
        font_dir = tmp_path / "fonts"; font_dir.mkdir()
        (font_dir / "BrandFont.ttf").write_bytes(b"font-bytes")
        monkeypatch.setattr(pa, "_FONT_DIRS", (str(font_dir),))
        project = self._project()
        seq = self._subtitle_sequence("BrandFont")
        _, report = collect_project(
            project, target_dir=str(tmp_path / "arch"), sequences=[seq])
        manifest = json.loads(
            (tmp_path / "arch" / "dependencies.json").read_text("utf-8"))
        font = manifest["fonts"][0]
        assert font["status"] == "resolved"
        assert font["sha256"] and font["path"].endswith("BrandFont.ttf")
        # 字体文件不随归档分发（许可）——归档根无 fonts 目录
        assert not (tmp_path / "arch" / "fonts").exists()
        assert not any("BrandFont" in w for w in report.warnings)

    def test_missing_plugin_identity_diagnosed(self, tmp_path):
        """启用但未注册的插件：身份缺失明示（不静默换效果）。"""
        from types import SimpleNamespace as NS
        from dataclasses import replace as _dc_replace
        from src.features.color.core.project_dependencies import (
            KIND_PLUGIN,
        )
        project = self._project()
        stack = NS(stack_id="s1", asset_id="a", name="S", lut_path=None,
                   layers=[{"plugins": [{
                       "type": "dctl", "enabled": True,
                       "plugin_id": "missing_plugin_xyz"}]}])
        project = _dc_replace(project, color_stacks={"s1": stack})
        _, report = collect_project(
            project, target_dir=str(tmp_path / "arch"), sequences=[])
        plugins = report.identity_manifest["plugins"]
        assert any(p["kind"] == KIND_PLUGIN
                   and p["key"] == "missing_plugin_xyz"
                   and p["status"] != "ok" for p in plugins)
        assert any("missing_plugin_xyz" in w and "插件" in w
                   for w in report.warnings)


class TestArchiveLutPathRewrite:
    """M20.2c · LUT 路径重写进工程载荷（归档包自洽）。"""

    def test_stack_and_layer_lut_paths_rewritten(self, tmp_path):
        """归档后工程栈级/layer 级 LUT 路径指向包内 luts/ 且文件存在。"""
        from types import SimpleNamespace as NS
        from dataclasses import replace as _dc_replace
        from src.features.edit.core.project import EditProject, ProjectBin
        from src.features.edit.core.project_archive import collect_project

        lut = tmp_path / "look.cube"
        lut.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8)
        layer_lut = tmp_path / "layer.cube"
        layer_lut.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8)
        stack = NS(stack_id="s1", asset_id="a", name="S",
                   lut_path=str(lut),
                   layers=[{"active_lut_path": str(layer_lut)}])
        project = EditProject(
            project_id="p", name="P", assets={},
            bins={"root": ProjectBin(bin_id="root", name="Root",
                                     parent_id=None, asset_ids=())},
            root_bin_id="root")
        project = _dc_replace(project, color_stacks={"s1": stack})

        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch"), sequences=[])
        new_stack = archived.color_stacks["s1"]
        assert new_stack.lut_path.startswith(str(tmp_path / "arch" / "luts"))
        assert os.path.isfile(new_stack.lut_path)
        assert new_stack.layers[0]["active_lut_path"].startswith(
            str(tmp_path / "arch" / "luts"))
        assert os.path.isfile(new_stack.layers[0]["active_lut_path"])
        # 原工程不被修改（frozen replace 语义）
        assert project.color_stacks["s1"].lut_path == str(lut)

    def test_missing_lut_path_kept_with_warning(self, tmp_path):
        """缺失 LUT：路径保持原位（警告在案），不指向不存在的包内路径。"""
        from types import SimpleNamespace as NS
        from dataclasses import replace as _dc_replace
        from src.features.edit.core.project import EditProject, ProjectBin
        from src.features.edit.core.project_archive import collect_project

        stack = NS(stack_id="s1", asset_id="a", name="S",
                   lut_path="/gone/missing.cube", layers=[])
        project = EditProject(
            project_id="p", name="P", assets={},
            bins={"root": ProjectBin(bin_id="root", name="Root",
                                     parent_id=None, asset_ids=())},
            root_bin_id="root")
        project = _dc_replace(project, color_stacks={"s1": stack})

        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arch"), sequences=[])
        assert archived.color_stacks["s1"].lut_path == "/gone/missing.cube"
        assert any("LUT 缺失" in w for w in report.warnings)
