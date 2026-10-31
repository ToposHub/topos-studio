"""Tests for the video export preflight.

Covers blocking conditions (no project, missing timeline, invalid range,
bad codec/container combo, unwritable output dir) and warnings (existing
file with ask policy, revision drift). The edit-feature-backed checks
(audio/effects/fonts) are exercised through a stub sequence that has the
expected attributes but empty lists — that exercises the happy path
without depending on the full audio/effects subsystem.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.deliver.core.video_export_job import (
    AudioConfig,
    OverwritePolicy,
    RangeType,
    SourceKind,
    SubtitleConfig,
    SubtitleMode,
    VideoConfig,
    VideoExportJob,
)
from src.features.deliver.core.video_export_preflight import (
    PreflightContext,
    PreflightSeverity,
    run_video_export_preflight,
)


class _StubSequence:
    """Minimal sequence stub satisfying preflight's attribute access."""

    def __init__(self, frame_count: int = 100, fps: float = 25.0):
        self.frame_count = frame_count
        self.fps = fps
        self.audio_tracks = []
        self.subtitle_tracks = []

    @property
    def all_tracks(self):
        return []


# ── context checks ──


def test_no_project_blocks():
    job = VideoExportJob(timeline_id="t1", output_path="/tmp/x.mov")
    ctx = PreflightContext(project=None)
    r = run_video_export_preflight(job, ctx)
    assert not r.ok
    assert any(i.code == "context.no_project" for i in r.blocking)


def test_missing_timeline_blocks():
    job = VideoExportJob(source_kind=SourceKind.TIMELINE, output_path="/tmp/x.mov")
    ctx = PreflightContext(project=object(), sequence=_StubSequence())
    r = run_video_export_preflight(job, ctx)
    assert any(i.code == "context.no_timeline" for i in r.blocking)


def test_timeline_not_found_blocks():
    job = VideoExportJob(timeline_id="ghost", output_path="/tmp/x.mov")
    ctx = PreflightContext(project=object(), sequence=None)
    r = run_video_export_preflight(job, ctx)
    assert any(i.code == "context.timeline_missing" for i in r.blocking)


def test_revision_drift_warns(tmp_path):
    out = tmp_path / "x.mov"
    out.write_text("")  # exists
    job = VideoExportJob(
        timeline_id="t1",
        project_revision=2,
        output_path=str(out),
        video_config=VideoConfig(codec="prores_422", container="mov"),
        overwrite_policy=OverwritePolicy.OVERWRITE,
    )
    ctx = PreflightContext(
        project=object(),
        sequence=_StubSequence(),
        project_revision=5,
    )
    r = run_video_export_preflight(job, ctx)
    assert any(i.code == "context.revision_drift" for i in r.warnings)


# ── structural checks ──


def test_bad_range_blocks():
    job = VideoExportJob(
        timeline_id="t1",
        range_type=RangeType.CUSTOM,
        start_frame=100, end_frame=50,
        output_path="/tmp/x.mov",
        video_config=VideoConfig(codec="prores_422", container="mov"),
    )
    ctx = PreflightContext(project=object(), sequence=_StubSequence())
    r = run_video_export_preflight(job, ctx)
    assert any(i.code == "range.invalid" for i in r.blocking)


def test_codec_container_incompatible_blocks():
    job = VideoExportJob(
        timeline_id="t1",
        output_path="/tmp/x.mp4",
        video_config=VideoConfig(codec="prores_422", container="mp4"),
    )
    ctx = PreflightContext(project=object(), sequence=_StubSequence())
    r = run_video_export_preflight(job, ctx)
    assert any(i.code == "codec.container_incompatible" for i in r.blocking)


def test_audio_codec_warning(tmp_path):
    # PCM s24le in an mp4 container should warn (not block)
    job = VideoExportJob(
        timeline_id="t1",
        output_path=str(tmp_path / "x.mp4"),
        video_config=VideoConfig(codec="h264", container="mp4"),
        audio_config=AudioConfig(codec="pcm_s24le"),
    )
    ctx = PreflightContext(project=object(), sequence=_StubSequence())
    r = run_video_export_preflight(job, ctx)
    assert r.ok  # warnings don't block
    assert any(i.code == "audio.codec_incompatible" for i in r.warnings)


# ── output path checks ──


def test_empty_output_blocks():
    job = VideoExportJob(timeline_id="t1", output_path="")
    ctx = PreflightContext(project=object(), sequence=_StubSequence())
    r = run_video_export_preflight(job, ctx)
    assert any(i.code == "output.empty" for i in r.blocking)


def test_unwritable_dir_blocks(tmp_path):
    out = tmp_path / "nope" / "x.mov"
    job = VideoExportJob(
        timeline_id="t1",
        output_path=str(out),
        video_config=VideoConfig(codec="prores_422", container="mov"),
    )
    ctx = PreflightContext(project=object(), sequence=_StubSequence())
    r = run_video_export_preflight(job, ctx)
    assert any(i.code == "output.dir_missing" for i in r.blocking)


def test_existing_file_with_ask_warns(tmp_path):
    out = tmp_path / "exists.mov"
    out.write_text("")
    job = VideoExportJob(
        timeline_id="t1",
        output_path=str(out),
        video_config=VideoConfig(codec="prores_422", container="mov"),
        overwrite_policy=OverwritePolicy.ASK,
    )
    ctx = PreflightContext(project=object(), sequence=_StubSequence())
    r = run_video_export_preflight(job, ctx)
    assert r.ok
    assert any(i.code == "output.exists" for i in r.warnings)


# ── disk space ──


def test_disk_space_blocking_when_low(tmp_path):
    job = VideoExportJob(
        timeline_id="t1",
        output_path=str(tmp_path / "x.mov"),
        video_config=VideoConfig(codec="prores_422", container="mov"),
    )
    ctx = PreflightContext(
        project=object(),
        sequence=_StubSequence(),
        disk_free_bytes=1_000_000,        # 1 MB
        estimated_size_bytes=2_000_000,   # 2 MB needed
    )
    r = run_video_export_preflight(job, ctx)
    assert any(i.code.startswith("disk.") for i in r.blocking)


def test_disk_space_ok_when_plenty(tmp_path):
    job = VideoExportJob(
        timeline_id="t1",
        output_path=str(tmp_path / "x.mov"),
        video_config=VideoConfig(codec="prores_422", container="mov"),
    )
    ctx = PreflightContext(
        project=object(),
        sequence=_StubSequence(),
        disk_free_bytes=10_000_000_000,   # 10 GB
        estimated_size_bytes=2_000_000,   # 2 MB
    )
    r = run_video_export_preflight(job, ctx)
    assert not any(i.code.startswith("disk.") for i in r.blocking)
    assert not any(i.code.startswith("disk.") for i in r.warnings)


# ── happy path ──


def test_happy_path_ok(tmp_path):
    job = VideoExportJob(
        timeline_id="t1",
        output_path=str(tmp_path / "x.mov"),
        video_config=VideoConfig(codec="prores_422", container="mov"),
    )
    ctx = PreflightContext(project=object(), sequence=_StubSequence())
    r = run_video_export_preflight(job, ctx)
    assert r.ok
    assert r.blocking == []


# ---------------------------------------------------------------------------
# P1-COLOR-25 复查：队列路径有损导出确认门槛
# ---------------------------------------------------------------------------

class TestLossyDeliveryConfirmation:
    @staticmethod
    def _result_with(color_warnings):
        from src.features.deliver.core.video_export_preflight import (
            PreflightResult,
        )
        result = PreflightResult()
        for code, message in color_warnings:
            result.add_warning(code, message, field="color_delivery")
        return result

    def test_no_warnings_passes_without_dialog(self, qapp, monkeypatch):
        from src.features.deliver.ui.video_export_settings_panel import (
            confirm_lossy_color_delivery,
        )
        called = []
        monkeypatch.setattr(
            "PyQt6.QtWidgets.QMessageBox.question",
            lambda *a, **k: called.append(a) or False)
        assert confirm_lossy_color_delivery(None, None) is True
        assert confirm_lossy_color_delivery(
            None, self._result_with([])) is True
        assert called == []  # 无有损警告不弹窗

    def test_user_confirm_and_reject(self, qapp, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox
        from src.features.deliver.ui.video_export_settings_panel import (
            confirm_lossy_color_delivery,
        )
        result = self._result_with([
            ("color.delivery.curve", "Curves 将被丢弃"),
            ("color.delivery.hsl", "HSL 将被丢弃"),
        ])
        answers = [QMessageBox.StandardButton.Yes,
                   QMessageBox.StandardButton.No]
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: answers.pop(0)))
        assert confirm_lossy_color_delivery(None, result) is True
        assert confirm_lossy_color_delivery(None, result) is False

    def test_non_color_warnings_do_not_trigger_dialog(self, qapp, monkeypatch):
        from src.features.deliver.ui.video_export_settings_panel import (
            confirm_lossy_color_delivery,
        )
        from src.features.deliver.core.video_export_preflight import (
            PreflightResult,
        )
        result = PreflightResult()
        result.add_warning("audio.pcm", "PCM needs mov", field="audio")
        called = []
        monkeypatch.setattr(
            "PyQt6.QtWidgets.QMessageBox.question",
            lambda *a, **k: called.append(a) or False)
        assert confirm_lossy_color_delivery(None, result) is True
        assert called == []


# ---------------------------------------------------------------------------
# R5/v1.1：Topos 音频轨 preflight——未选音频明示、不支持编解码提示省略
# ---------------------------------------------------------------------------


class _StubAudioTrack:
    """带 clip 的音频轨 stub（preflight 只看 clips 是否非空）。"""

    def __init__(self, with_clips: bool = True):
        self.track_id = "a1"
        self.name = "A1"
        self.clips = [("clip", 0, 10)] if with_clips else []
        self.strip_eq = None


class _StubSequenceWithAudio(_StubSequence):
    def __init__(self, with_clips: bool = True):
        super().__init__()
        self.audio_tracks = [_StubAudioTrack(with_clips)]


class TestToposVideoOnlyPreflight:
    def test_topos_with_audio_supported_codec_no_warning(self, tmp_path):
        """v1.1 音频轨：Topos + include_audio + 受支持编解码（aac）→
        原 topos.video_only 警告解除（音频将写入容器）。"""
        job = VideoExportJob(
            timeline_id="t1",
            output_path=str(tmp_path / "x.mov"),
            video_config=VideoConfig(codec="topos", container="mov"),
            audio_config=AudioConfig(include_audio=True, codec="aac"),
        )
        ctx = PreflightContext(project=object(), sequence=_StubSequenceWithAudio())
        r = run_video_export_preflight(job, ctx)
        assert r.ok
        assert not any(i.code == "topos.video_only" for i in r.warnings)

    def test_topos_with_unsupported_audio_codec_warns(self, tmp_path):
        """v1.1：Topos + include_audio + 不受支持编解码（mp3）→ 仍提示省略。"""
        job = VideoExportJob(
            timeline_id="t1",
            output_path=str(tmp_path / "x.mov"),
            video_config=VideoConfig(codec="topos", container="mov"),
            audio_config=AudioConfig(include_audio=True, codec="mp3"),
        )
        ctx = PreflightContext(project=object(), sequence=_StubSequenceWithAudio())
        r = run_video_export_preflight(job, ctx)
        assert any(i.code == "topos.video_only" for i in r.warnings)

    def test_all_six_tiers_supported_codec_no_warning(self, tmp_path):
        """六个 topos 档（proxy/lt/standard/hq/4444/4444xq）v1.1 一致：
        受支持编解码（aac）不再触发 topos.video_only。"""
        for codec in ("topos_proxy", "topos_lt", "topos", "topos_hq",
                      "topos_4444", "topos_4444xq"):
            job = VideoExportJob(
                timeline_id="t1",
                output_path=str(tmp_path / f"{codec}.mov"),
                video_config=VideoConfig(codec=codec, container="mov"),
                audio_config=AudioConfig(include_audio=True, codec="aac"),
            )
            ctx = PreflightContext(project=object(), sequence=_StubSequenceWithAudio())
            r = run_video_export_preflight(job, ctx)
            assert not any(i.code == "topos.video_only" for i in r.warnings), codec

    def test_include_audio_off_keeps_hint(self, tmp_path):
        """v1.1：用户显式关掉音频且有音频内容 → 维持提示（输出不含音轨）。"""
        job = VideoExportJob(
            timeline_id="t1",
            output_path=str(tmp_path / "x.mov"),
            video_config=VideoConfig(codec="topos", container="mov"),
            audio_config=AudioConfig(include_audio=False),
        )
        ctx = PreflightContext(project=object(), sequence=_StubSequenceWithAudio())
        r = run_video_export_preflight(job, ctx)
        assert any(i.code == "topos.video_only" for i in r.warnings)

    def test_empty_audio_tracks_no_warning(self, tmp_path):
        """空轨（无 clip）不算真实音频内容。"""
        job = VideoExportJob(
            timeline_id="t1",
            output_path=str(tmp_path / "x.mov"),
            video_config=VideoConfig(codec="topos", container="mov"),
            audio_config=AudioConfig(include_audio=True),
        )
        ctx = PreflightContext(project=object(), sequence=_StubSequenceWithAudio(with_clips=False))
        r = run_video_export_preflight(job, ctx)
        assert not any(i.code == "topos.video_only" for i in r.warnings)

    def test_non_topos_codec_no_warning(self, tmp_path):
        """非 topos codec（如 prores）不受纯视频约束。"""
        job = VideoExportJob(
            timeline_id="t1",
            output_path=str(tmp_path / "x.mov"),
            video_config=VideoConfig(codec="prores_422", container="mov"),
            audio_config=AudioConfig(include_audio=True, codec="aac"),
        )
        ctx = PreflightContext(project=object(), sequence=_StubSequenceWithAudio())
        r = run_video_export_preflight(job, ctx)
        assert not any(i.code == "topos.video_only" for i in r.warnings)

    def test_nested_audio_triggers_topos_video_only_preflight(self, tmp_path):
        """复验 P1-14：父序列无直接音轨、嵌套子序列有音频 → preflight
        仍感知（与真实导出的 flatten_nested_audio_tracks 同源）。

        v1.1：include_audio + 受支持编解码（aac）→ 音频写入、无告警；
        不受支持编解码（mp3）→ 提示音频将被省略（借该分支验证嵌套展开
        到达 preflight）。

        Q02（第六轮审计）：子序列夹具此前用 SimpleNamespace 且缺 ``fps``，
        flatten 的混帧率检查直接 AttributeError。真实宿主路径传入的都是
        完整 TimelineSequence，这里改用同一类型。"""
        import types

        from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack

        # 子序列：带真实 ClipInstance 的音频轨（完整 TimelineSequence，
        # flatten 会读取 child.fps 做混帧率检查）
        child_clip = ClipInstance(
            clip_id="ac1", asset_id="a-src", track_id="ca0",
            timeline_start=0, timeline_duration=50,
            source_start=0, source_duration=50)
        child_track = EditTrack(
            track_id="ca0", name="CA1", track_type="audio",
            index=0, clips=(child_clip,))

        # 真实 dataclass 序列（expand_sequence_audio_for_render 用 dc_replace）
        from src.shared.media.metadata import Rational
        from src.features.edit.core.timeline_v2 import TimelineSequence

        child = TimelineSequence(
            timeline_id="child-seq", name="child", width=64, height=48,
            fps=Rational(24, 1), audio_tracks=(child_track,))

        # 父序列：视频轨上一个嵌套片段（asset 指向 child-seq）
        nested_clip = ClipInstance(
            clip_id="nv1", asset_id="nest-asset", track_id="pv0",
            timeline_start=0, timeline_duration=50,
            source_start=0, source_duration=50)
        parent_track = EditTrack(
            track_id="pv0", name="V1", track_type="video",
            index=0, clips=(nested_clip,))
        # 真实 dataclass 序列（expand_sequence_audio_for_render 用 dc_replace）
        from src.shared.media.metadata import Rational
        from src.features.edit.core.timeline_v2 import TimelineSequence

        parent = TimelineSequence(
            name="parent", width=64, height=48, fps=Rational(24, 1),
            video_tracks=(parent_track,))

        nested_asset = types.SimpleNamespace(
            is_nested_sequence=True, nested_timeline_id="child-seq")

        def resolver(tid):
            return child if tid == "child-seq" else None

        def asset_lookup(aid):
            return nested_asset if aid == "nest-asset" else None

        project = types.SimpleNamespace(
            assets={"nest-asset": nested_asset})

        # 与真实导出同一展开函数：父（空）+ 子轨贡献 ≥ 1
        from src.features.edit.core.nested_sequence import (
            flatten_nested_audio_tracks,
        )
        expanded = flatten_nested_audio_tracks(parent, resolver, asset_lookup)
        assert len(expanded) >= 1, "子序列音频应被展开进来"

        job = VideoExportJob(
            timeline_id="parent-seq",
            output_path=str(tmp_path / "nest.mov"),
            video_config=VideoConfig(codec="topos", container="mov"),
            audio_config=AudioConfig(include_audio=True, codec="mp3"),
        )
        # ① 展开结果（真实导出所见）：有音频 + 不支持编解码 → 触发提示
        ctx = PreflightContext(
            project=project,
            sequence=types.SimpleNamespace(
                audio_tracks=expanded, video_tracks=()))
        r = run_video_export_preflight(job, ctx)
        assert any(i.code == "topos.video_only" for i in r.warnings)

        # ② 经 resolver 的 preflight 上下文同样触发（面板 wiring 路径）
        ctx2 = PreflightContext(
            project=project, sequence=parent, sequence_resolver=resolver)
        r2 = run_video_export_preflight(job, ctx2)
        assert any(i.code == "topos.video_only" for i in r2.warnings), \
            "nested 展开后的 preflight 应与真实导出一致触发告警"

        # ③ v1.1：受支持编解码（aac）→ 嵌套音频写入、无告警
        job3 = VideoExportJob(
            timeline_id="parent-seq",
            output_path=str(tmp_path / "nest-aac.mov"),
            video_config=VideoConfig(codec="topos", container="mov"),
            audio_config=AudioConfig(include_audio=True, codec="aac"),
        )
        ctx3 = PreflightContext(
            project=project,
            sequence=types.SimpleNamespace(
                audio_tracks=expanded, video_tracks=()))
        r3 = run_video_export_preflight(job3, ctx3)
        assert not any(i.code == "topos.video_only" for i in r3.warnings)

    def test_nested_audio_without_resolver_keeps_parent_tracks_only(self, tmp_path):
        """Q02（第六轮审计）：真正缺 resolver 时的行为——嵌套子序列音频
        不展开（返回父轨原样），而不是抛 AttributeError 或把父轨丢掉。
        真实宿主路径（U02 修复后）会带完整 resolver；本用例锁住降级语义。
        """
        import types

        from src.shared.media.metadata import Rational
        from src.features.edit.core.nested_sequence import (
            flatten_nested_audio_tracks,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )

        nested_clip = ClipInstance(
            clip_id="nv1", asset_id="nest-asset", track_id="pv0",
            timeline_start=0, timeline_duration=50,
            source_start=0, source_duration=50)
        parent_track = EditTrack(
            track_id="pv0", name="V1", track_type="video",
            index=0, clips=(nested_clip,))
        parent = TimelineSequence(
            name="parent", width=64, height=48, fps=Rational(24, 1),
            video_tracks=(parent_track,))
        nested_asset = types.SimpleNamespace(
            is_nested_sequence=True, nested_timeline_id="child-seq")

        def asset_lookup(aid):
            return nested_asset if aid == "nest-asset" else None

        project = types.SimpleNamespace(assets={"nest-asset": nested_asset})

        expanded = flatten_nested_audio_tracks(
            parent, None, asset_lookup,
            diagnostics=[])
        # 父序列没有直接音轨；resolver 缺失 → 只有父轨（空），子序列
        # 音频被跳过（诊断记录），不崩溃。
        assert expanded == parent.audio_tracks


# ================================================================
# M21.1c · 双任务争同一输出路径明确拒绝
# ================================================================

class TestOutputContentionRejection:
    def test_active_same_path_rejected(self):
        from src.features.deliver.core.export_queue import (
            ExportJob, ExportQueue, JobStatus,
        )
        q = ExportQueue.__new__(ExportQueue)
        q._jobs = []
        q._by_id = {}
        # 手动接信号桩（QObject 信号在 __new__ 下未初始化——测试内用
        # 真实例替代：直接用 __init__ 需要 runner，可传 None）
        from src.features.deliver.core.export_queue import ExportQueue as EQ
        q2 = EQ(runner=None)
        q2._persist = lambda: None  # 不落盘
        j1 = ExportJob(job_id="a", output_path="/out/same.mp4")
        q2.enqueue(j1)
        j2 = ExportJob(job_id="b", output_path="/out/same.mp4")
        import pytest as _pytest
        with _pytest.raises(ValueError, match="输出路径已被进行中的任务占用"):
            q2.enqueue(j2)

    def test_done_job_same_path_allows(self):
        from src.features.deliver.core.export_queue import (
            ExportJob, ExportQueue, JobStatus,
        )
        q = ExportQueue(runner=None)
        q._persist = lambda: None
        j1 = ExportJob(job_id="a", output_path="/out/x.mp4",
                       status=JobStatus.DONE)
        q.enqueue(j1, preserve_status=True)
        # 完成任务不占路径——新任务可入队（发布事务保旧成片）
        j2 = ExportJob(job_id="b", output_path="/out/x.mp4")
        q.enqueue(j2)
        assert len(q.jobs) == 2

    def test_different_paths_unaffected(self):
        from src.features.deliver.core.export_queue import ExportJob, ExportQueue
        q = ExportQueue(runner=None)
        q._persist = lambda: None
        q.enqueue(ExportJob(job_id="a", output_path="/out/1.mp4"))
        q.enqueue(ExportJob(job_id="b", output_path="/out/2.mp4"))
        assert len(q.jobs) == 2
