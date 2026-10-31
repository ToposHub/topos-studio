"""M18 · 黄金链 3（多画幅多语言交付+工程恢复）Mac 侧端到端集成。

跨机（Windows 复验/外部软件接收）按 deferred_gates 延期；本测试把
Mac 侧可自动化的整链钉住：真实素材工程 → 多画幅多语言版本编译 →
.topos 保存 → 重开 → 再编译定点一致 → 归档闭包/恢复自洽。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core


def _golden_plan():
    from src.features.edit.ai_editing.editorial_advanced import (
        LinkedVersion,
        VersionDerivationPlan,
    )
    return VersionDerivationPlan(
        master_plan_id="golden-master",
        versions=(
            LinkedVersion(version_id="v169-zh", master_plan_id="golden-master",
                          canvas_aspect="16:9", duration_target_seconds=10.0,
                          language="zh", subtitle_strategy="burn_in"),
            LinkedVersion(version_id="v916-en", master_plan_id="golden-master",
                          canvas_aspect="9:16", duration_target_seconds=10.0,
                          language="en", subtitle_strategy="soft"),
            LinkedVersion(version_id="v11-ja", master_plan_id="golden-master",
                          canvas_aspect="1:1", duration_target_seconds=8.0,
                          language="ja", subtitle_strategy="none"),
        ))


def _golden_project(real_video: str):
    from dataclasses import replace as _dc_replace
    from src.features.edit.core.project import (
        EditProject,
        MediaAsset,
        ProjectBin,
        TimelineSequenceRef,
    )
    asset = MediaAsset(
        asset_id="gold1", path=real_video, media_type="video",
        display_name="黄金链主素材.mp4",
        metadata={"codec": "h264", "fps": 25, "width": 3840,
                  "height": 2160},
        keywords=("黄金链",), rating=5)
    return EditProject(
        project_id="golden-chain3", name="黄金链3·多画幅多语言",
        assets={"gold1": asset},
        bins={"root": ProjectBin(bin_id="root", name="Root",
                                 parent_id=None, asset_ids=("gold1",))},
        root_bin_id="root",
        timelines={"tl-gold": TimelineSequenceRef(
            timeline_id="tl-gold", name="母版时间线", fps=25.0,
            width=3840, height=2160)},
        active_timeline_id="tl-gold", revision=3)


_BASE_CONFIG = {
    "project_id": "golden-chain3", "timeline_id": "tl-gold",
    "output_path": "/tmp/golden_{version}.mp4", "codec": "h264",
    "container": "mp4",
}


def _compile(project, existing=None):
    from src.features.deliver.core.version_compiler import compile_version_jobs
    return compile_version_jobs(
        _golden_plan(), dict(_BASE_CONFIG),
        source_project_revision=int(getattr(project, "revision", 0) or 0),
        timeline_exists=True, all_assets_online=True,
        existing_output_paths=existing)


class TestGoldenChain3:
    def test_chain_compile_save_reopen_recompile_identical(self, qapp,
                                                            tmp_path):
        """链路验收（Mac 侧）：编译 → 保存 → 重开 → 再编译，三版本
        输出路径/画幅/语言/字幕策略/revision 溯源定点一致。"""
        real = os.path.join(
            os.path.dirname(__file__), "..", "test_videos", "2K_h264.mp4")
        if not os.path.exists(real):
            pytest.skip("real test video unavailable")
        project = _golden_project(real)

        result1 = _compile(project)
        assert result1.ok, result1.refusals
        assert len(result1.jobs) == 3
        by_id1 = {j.linked_version_id: j for j in result1.jobs}
        assert by_id1["v916-en"].config["resolution"] == [1080, 1920]
        assert by_id1["v169-zh"].config["subtitle_language"] == "zh"
        assert by_id1["v169-zh"].config["burn_subtitles"] is True
        assert by_id1["v916-en"].config["burn_subtitles"] is False

        from src.features.edit.core.edit_project_io import EditProjectIO
        io = EditProjectIO()
        path = tmp_path / "golden_chain3.topos"
        assert io.save_project(str(path), project)
        reopened, _ui, _seqs = io.load_project(str(path))
        assert reopened is not None
        # 工程恢复保真：日志/revision/时间线引用
        assert reopened.assets["gold1"].keywords == ("黄金链",)
        assert reopened.revision == 3
        assert "tl-gold" in (reopened.timelines or {})

        result2 = _compile(reopened)
        assert result2.ok
        by_id2 = {j.linked_version_id: j for j in result2.jobs}
        for vid in ("v169-zh", "v916-en", "v11-ja"):
            assert by_id2[vid].output_path == by_id1[vid].output_path, vid
            assert by_id2[vid].config["resolution"] \
                == by_id1[vid].config["resolution"], vid
            assert by_id2[vid].config["subtitle_language"] \
                == by_id1[vid].config["subtitle_language"], vid
            assert by_id2[vid].config["source_project_revision"] == 3, vid

    def test_reopen_after_edit_invalidates_stale_snapshot(self, qapp,
                                                          tmp_path):
        """重开后 revision 前进（编辑提交）→ 再编译携带新 revision
        （恢复验证源版本——不用旧溯源续新版本）。"""
        from dataclasses import replace as _dc_replace
        from src.features.edit.core.edit_project_io import EditProjectIO
        real = os.path.join(
            os.path.dirname(__file__), "..", "test_videos", "2K_h264.mp4")
        if not os.path.exists(real):
            pytest.skip("real test video unavailable")
        project = _golden_project(real)
        io = EditProjectIO()
        path = tmp_path / "g.topos"
        assert io.save_project(str(path), project)
        reopened, _ui, _s = io.load_project(str(path))
        edited = _dc_replace(reopened, revision=reopened.revision + 1)
        result = _compile(edited)
        assert all(j.config["source_project_revision"] == 4
                   for j in result.jobs)

    def test_archive_closure_and_restore_selfcontained(self, qapp, tmp_path):
        """链尾：工程归档闭包（媒体+LUT 路径重写）→ 归档工程自洽。"""
        from src.features.edit.core.edit_project_io import EditProjectIO
        from src.features.edit.core.project_archive import collect_project
        real = os.path.join(
            os.path.dirname(__file__), "..", "test_videos", "2K_h264.mp4")
        if not os.path.exists(real):
            pytest.skip("real test video unavailable")
        project = _golden_project(real)
        io = EditProjectIO()
        # 先保存为 .topos 使归档按工程包语义收集
        pkg = tmp_path / "src.topos"
        assert io.save_project(str(pkg), project)
        loaded, _ui, _s = io.load_project(str(pkg))
        archived, report = collect_project(
            loaded, target_dir=str(tmp_path / "arch"))
        assert report.passed
        media = [r for r in report.copied if r.asset_id == "gold1"]
        assert media
        # archived_path 相对归档根（collect 契约）——拼根存在性校验
        archived_file = os.path.join(
            str(tmp_path / "arch"), media[0].archived_path)
        assert os.path.exists(archived_file)
        assert archived.assets["gold1"].path.startswith(
            str(tmp_path / "arch"))
        # 归档工程可再编译（链路在归档域不中断）
        result = _compile(archived)
        assert result.ok and len(result.jobs) == 3
