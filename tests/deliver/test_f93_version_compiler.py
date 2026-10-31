"""F9.3 · 多版本交付编译器与 PCM 音频校验——验收测试。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.deliver.core.version_compiler import (
    compile_version_jobs,
    count_audio_frames,
)
from src.features.edit.ai_editing.editorial_advanced import (
    LinkedVersion,
    VersionDerivationPlan,
)

pytestmark = pytest.mark.edit_core


def _plan():
    return VersionDerivationPlan(
        master_plan_id="master-1",
        versions=(
            LinkedVersion(version_id="v_wide", master_plan_id="master-1",
                          canvas_aspect="16:9", duration_target_seconds=60,
                          language="zh", subtitle_strategy="burn_in"),
            LinkedVersion(version_id="v_vertical",
                          master_plan_id="master-1",
                          canvas_aspect="9:16",
                          duration_target_seconds=60, language="en",
                          subtitle_strategy="soft"),
            LinkedVersion(version_id="v_square",
                          master_plan_id="master-1",
                          canvas_aspect="1:1",
                          duration_target_seconds=30, language="zh",
                          subtitle_strategy="none"),
        ))


def _base_config():
    return {
        "output_path": "/out/master.mp4",
        "codec": "h264", "fps": 24,
        "audio_codec": "aac",
    }


class TestCompileVersionJobs:
    def test_three_versions_three_jobs_with_tracing(self):
        result = compile_version_jobs(
            _plan(), _base_config(), source_project_revision=42,
            source_snapshot_hash="snap-hash")
        assert result.ok and len(result.jobs) == 3
        wide = result.jobs[0]
        assert wide.config["width"] == 1920 and wide.config["height"] == 1080
        assert wide.config["burn_subtitles"] is True   # burn_in
        assert wide.config["subtitle_language"] == "zh"
        # 版本溯源（恢复验证源版本）
        assert wide.config["source_project_revision"] == 42
        assert wide.config["source_snapshot_hash"] == "snap-hash"
        # 画幅差异
        vertical = result.jobs[1]
        assert vertical.config["width"] == 1080
        assert vertical.config["height"] == 1920
        assert vertical.config["burn_subtitles"] is False  # soft
        assert vertical.config["subtitle_language"] == "en"

    def test_output_paths_unique_per_version(self):
        result = compile_version_jobs(_plan(), _base_config())
        paths = [j.output_path for j in result.jobs]
        assert len(set(paths)) == 3
        assert all("v_" in p for p in paths)

    def test_path_conflict_refused_no_overwrite(self):
        """命名冲突先检查：已有输出不覆盖（成功输出保护）。"""
        first = compile_version_jobs(_plan(), _base_config())
        result = compile_version_jobs(
            _plan(), _base_config(),
            existing_output_paths=[j.output_path for j in first.jobs])
        assert not result.ok
        assert any("冲突" in r for r in result.refusals)

    def test_offline_assets_refused_no_partial_jobs(self):
        """素材依赖先检查：离线 → 拒绝清单（不产半套 job）。"""
        result = compile_version_jobs(
            _plan(), _base_config(), all_assets_online=False)
        assert not result.ok
        assert any("离线" in r for r in result.refusals)

    def test_missing_timeline_refused(self):
        result = compile_version_jobs(
            _plan(), _base_config(), timeline_exists=False)
        assert not result.ok and "时间线" in result.refusals[0]

    def test_unknown_aspect_refused_per_version(self):
        bad = VersionDerivationPlan(
            master_plan_id="m", versions=(
                LinkedVersion(version_id="v1", master_plan_id="m",
                              canvas_aspect="21:9",
                              duration_target_seconds=60),))
        result = compile_version_jobs(bad, _base_config())
        assert not result.ok
        assert any("21:9" in r for r in result.refusals)

    def test_template_placeholder_in_path(self):
        config = {**_base_config(),
                  "output_path": "/out/{version}.mp4"}
        result = compile_version_jobs(_plan(), config)
        assert result.jobs[0].output_path == "/out/v_wide.mp4"


class TestAudioFrameCount:
    def test_real_video_audio_frames_counted(self, tmp_path):
        """PCM 级校验（probe 跳过 AudioFrame 的缺口）：真实含音轨文件。"""
        import av
        import numpy as np

        path = tmp_path / "a.mp4"
        container = av.open(str(path), mode="w", format="mp4")
        astream = container.add_stream("aac", rate=48000)
        astream.layout = "stereo"
        rng = np.random.default_rng(1)
        for _ in range(10):
            # fltp planar: shape = (channels, samples)
            arr = (rng.uniform(-0.5, 0.5,
                               (2, 1024))).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(
                arr, format="fltp", layout="stereo")
            frame.sample_rate = 48000
            for pkt in astream.encode(frame):
                container.mux(pkt)
        for pkt in astream.encode(None):
            container.mux(pkt)
        container.close()

        result = count_audio_frames(str(path))
        assert result is not None
        assert result["audio_streams"] == 1
        assert result["audio_frames"] >= 8   # 10 帧 + 编码器冲刷
        assert result["audio_samples"] > 8000

    def test_missing_file_returns_none(self):
        assert count_audio_frames("/nonexistent.mp4") is None


# ================================================================
# M10（S03）· 真实 job 类型消费
# ================================================================

class TestRealJobConsumption:
    def test_derived_resolution_reaches_real_video_export_job(self):
        """S03 验收向量：3840×2160 母版派生 9:16 → 真实 VideoExportJob
        的 video_config.resolution 为 (1080, 1920)（此前只写 width/height，
        job 读 resolution 键仍取基线）。"""
        from src.features.deliver.core.video_export_job import VideoExportJob
        from src.features.edit.ai_editing.editorial_advanced import (
            LinkedVersion,
        )
        plan = VersionDerivationPlan("master", (
            LinkedVersion("v916", "master", "9:16", 20),))
        compiled = compile_version_jobs(plan, {
            "output_path": "/out/m.mp4", "resolution": (3840, 2160)})
        assert compiled.ok
        job = VideoExportJob.from_flat_config(compiled.jobs[0].config)
        assert tuple(job.video_config.resolution) == (1080, 1920)

    def test_offline_hard_gate_zero_jobs(self):
        from src.features.edit.ai_editing.editorial_advanced import (
            LinkedVersion,
        )
        plan = VersionDerivationPlan("master", (
            LinkedVersion("v", "master", "9:16", 20),))
        compiled = compile_version_jobs(
            plan, {"output_path": "/out/m.mp4"}, all_assets_online=False)
        assert not compiled.ok
        assert compiled.jobs == () and compiled.refusals
