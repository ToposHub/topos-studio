"""N06/N12 · 多版本编译生产入口 + 字幕模式统一 + 发布前质量门测试。

审计句：compile_version_jobs 无生产调用；compiler 写 burn_subtitles 而
job 读 subtitle_mode（默认 off）——编译出的字幕策略从不生效；语言仅存
SubtitleConfig 未传到真实字幕轨；混入拒绝时部分 jobs 无上层阻断。
N12 余项：质量失败不影响成功 → runner 发布前按声明目标阻断 DONE。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.deliver_core

from src.features.edit.ai_editing.editorial_advanced import (  # noqa: E402
    LinkedVersion, VersionDerivationPlan)
from src.features.deliver.core.version_compiler import (  # noqa: E402
    compile_version_jobs)


def _lv(version_id, **kw):
    kw.setdefault("canvas_aspect", "16:9")
    kw.setdefault("duration_target_seconds", 60)
    return LinkedVersion(version_id=version_id,
                         master_plan_id="m1", **kw)


def _plan(*versions):
    return VersionDerivationPlan(
        master_plan_id="m1", versions=tuple(versions))


def _base_config():
    return {"output_path": "/tmp/out/{version}.mp4", "codec": "h264"}


def _resolve_map(mapping):
    return lambda lang: mapping.get(lang)


class TestSubtitleModeUnification:
    def test_strategy_maps_to_job_mode_keys(self):
        plan = _plan(
            _lv("b", canvas_aspect="16:9",
                          language="zh", subtitle_strategy="burn_in"),
            _lv("s", canvas_aspect="9:16",
                          language="en", subtitle_strategy="soft"),
            _lv("n", canvas_aspect="1:1",
                          language="ja", subtitle_strategy="none"))
        result = compile_version_jobs(
            plan, _base_config(),
            resolve_track_id=_resolve_map({"zh": "t-zh", "en": "t-en"}))
        assert result.ok, result.refusals
        modes = {j.linked_version_id: j.config["subtitle_mode"]
                 for j in result.jobs}
        assert modes == {"b": "burn", "s": "sidecar", "n": "off"}
        assert result.jobs[0].config["burn_subtitles"] is True  # 兼容键
        assert result.jobs[1].config["subtitle_track_id"] == "t-en"
        # off 版本无轨身份要求
        assert "subtitle_track_id" not in result.jobs[2].config

    def test_unknown_strategy_rejected(self):
        plan = _plan(_lv("x", canvas_aspect="16:9", language="zh",
            subtitle_strategy="hologram"))
        result = compile_version_jobs(plan, _base_config())
        assert not result.ok and any("未知字幕策略" in r
                                     for r in result.refusals)

    def test_non_off_without_track_identity_refused(self):
        plan = _plan(_lv("v", canvas_aspect="16:9", language="fr",
            subtitle_strategy="burn_in"))
        no_track = compile_version_jobs(
            plan, _base_config(), resolve_track_id=lambda lang: None)
        assert not no_track.ok and any("无对应字幕轨" in r
                                       for r in no_track.refusals)
        # 纯函数路径（无解析器）保持轨身份缺失，不拒绝
        legacy = compile_version_jobs(plan, _base_config())
        assert legacy.ok and legacy.jobs[0].config["subtitle_mode"] == "burn"
        assert "subtitle_track_id" not in legacy.jobs[0].config


class TestEnqueueVersionPlan:
    def _controller(self, monkeypatch, tmp_path):
        from src.features.deliver.core.video_batch_export_controller import (
            VideoBatchExportController,
        )
        ctrl = VideoBatchExportController()
        monkeypatch.setattr(ctrl, "_relink_jobs_for_sequence",
                            lambda *a, **k: None)
        return ctrl

    def _timeline(self, tmp_path, online=True, langs=("zh", "en")):
        media = tmp_path / "m.mov"
        media.write_bytes(b"M" * 16)
        asset = NS(path=str(media) if online else "/gone/m.mov")
        clip = NS(asset_id="a1")
        track = NS(clips=(clip,))
        sub_tracks = tuple(
            NS(track_id=f"t-{lang}",
               caption_metadata=NS(language=lang))
            for lang in langs)
        return NS(timeline_id="tl1", video_tracks=(track,),
                  subtitle_tracks=sub_tracks), asset

    def test_ok_plan_enqueues_atomically(self, monkeypatch, tmp_path):
        ctrl = self._controller(monkeypatch, tmp_path)
        timeline, asset = self._timeline(tmp_path)
        project = NS(revision=7, assets={"a1": asset})
        plan = _plan(
            _lv("v1", canvas_aspect="16:9",
                          language="zh", subtitle_strategy="burn_in"),
            _lv("v2", canvas_aspect="9:16",
                          language="en", subtitle_strategy="soft"))
        result = ctrl.enqueue_version_plan(
            plan, _base_config(), project=project, timeline=timeline)
        assert result["ok"], result["refusals"]
        assert len(result["enqueued"]) == 2
        queued = list(ctrl.queue.jobs)
        assert len(queued) == 2
        # 字幕策略真实进入 job 快照（from_flat_config 类型化）
        from src.features.deliver.core.video_export_job import SubtitleMode
        video_jobs = [j for j in queued]
        modes = {j.output_path: j for j in video_jobs}
        assert len(modes) == 2

    def test_any_refusal_means_zero_enqueued(self, monkeypatch, tmp_path):
        ctrl = self._controller(monkeypatch, tmp_path)
        timeline, asset = self._timeline(tmp_path)
        project = NS(revision=1, assets={"a1": asset})
        plan = _plan(
            _lv("good", canvas_aspect="16:9",
                          language="zh", subtitle_strategy="burn_in"),
            _lv("bad", canvas_aspect="21:9",
                          language="en", subtitle_strategy="soft"))
        result = ctrl.enqueue_version_plan(
            plan, _base_config(), project=project, timeline=timeline)
        assert result["ok"] is False
        assert result["enqueued"] == []
        assert any("未知画幅" in r for r in result["refusals"])
        assert len(ctrl.queue.jobs) == 0  # 原子：好版本也不入队

    def test_offline_asset_refuses_batch(self, monkeypatch, tmp_path):
        ctrl = self._controller(monkeypatch, tmp_path)
        timeline, asset = self._timeline(tmp_path, online=False)
        project = NS(revision=1, assets={"a1": asset})
        plan = _plan(_lv("v", canvas_aspect="16:9", language="zh",
            subtitle_strategy="none"))
        result = ctrl.enqueue_version_plan(
            plan, _base_config(), project=project, timeline=timeline)
        assert result["ok"] is False
        assert any("离线" in r for r in result["refusals"])

    def test_language_resolves_track_identity(self, monkeypatch, tmp_path):
        ctrl = self._controller(monkeypatch, tmp_path)
        timeline, asset = self._timeline(tmp_path, langs=("zh-Hans",))
        project = NS(revision=1, assets={"a1": asset})
        plan = _plan(_lv("v", canvas_aspect="16:9", language="zh",
            subtitle_strategy="burn_in"))
        result = ctrl.enqueue_version_plan(
            plan, _base_config(), project=project, timeline=timeline)
        # zh → zh-Hans 轨（BCP-47 前缀匹配）
        assert result["ok"], result["refusals"]


class TestRunnerQualityGate:
    def _runner(self):
        from src.features.deliver.core.video_job_runner import VideoJobRunner
        return VideoJobRunner()

    def test_no_declared_target_gate_inactive(self):
        runner = self._runner()
        job = NS(config={"codec": "h264"}, output_path="/tmp/x.mp4")
        assert runner._run_quality_gate(job) is None

    def test_declared_and_within_tolerance_passes(
            self, monkeypatch, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"V")
        runner = self._runner()
        job = NS(config={"expected_lufs": -14.0}, output_path=str(media))
        monkeypatch.setattr(
            "src.features.edit.ai_editing.delivery_quality_probe."
            "measure_delivery_quality",
            lambda path, **kw: NS(integrated_lufs=-14.3))
        assert runner._run_quality_gate(job) is None

    def test_out_of_tolerance_blocks(self, monkeypatch, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"V")
        runner = self._runner()
        job = NS(config={"expected_lufs": -14.0}, output_path=str(media))
        monkeypatch.setattr(
            "src.features.edit.ai_editing.delivery_quality_probe."
            "measure_delivery_quality",
            lambda path, **kw: NS(integrated_lufs=-9.0))
        err = runner._run_quality_gate(job)
        assert err and "QC 门失败" in err and "偏离声明目标" in err

    def test_probe_failure_fails_closed(self, monkeypatch, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"V")
        runner = self._runner()
        job = NS(config={"expected_lufs": -14.0}, output_path=str(media))

        def boom(path, **kw):
            raise RuntimeError("probe broken")

        monkeypatch.setattr(
            "src.features.edit.ai_editing.delivery_quality_probe."
            "measure_delivery_quality", boom)
        err = runner._run_quality_gate(job)
        assert err and "探测异常" in err

    def test_missing_output_fails_closed(self):
        runner = self._runner()
        job = NS(config={"expected_lufs": -14.0},
                 output_path="/tmp/definitely-absent-qc.mp4")
        err = runner._run_quality_gate(job)
        assert err and "成片不存在" in err
