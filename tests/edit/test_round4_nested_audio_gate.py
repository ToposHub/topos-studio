"""Nested audio delivery rejects unsupported mappings before any encoder opens."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.nested_sequence import (
    NestedAudioExpansionError, create_nested_sequence_asset,
    flatten_nested_audio_tracks,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.stem_export import (
    StemSpec, StemType, export_all_stems, render_stem_pcm,
)
from src.features.edit.core.timeline_export import (
    ExportRangeType, TimelineExporter, TimelineExportConfig,
    expand_sequence_audio_for_render,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_core


def _setup(*, speed: SpeedParams | None = None, child_fps: float = 24.0):
    audio = ClipInstance("sound", "source", "a", 0, 24, 0, 24)
    child = TimelineSequence("child", "Child", fps=child_fps, audio_tracks=(
        EditTrack("a", "Audio", "audio", 0, clips=(audio,)),))
    asset = create_nested_sequence_asset(child)
    nest = ClipInstance("nest", asset.asset_id, "v", 0, 24, 0, 24,
                        speed=speed or SpeedParams(speed=2.0))
    parent = TimelineSequence("parent", "Parent", video_tracks=(
        EditTrack("v", "Video", "video", 0, clips=(nest,)),))
    source = MediaAsset("source", "/unused.wav", "audio", "Source", {})
    project = EditProject("project", "Project", assets={asset.asset_id: asset, "source": source})
    return parent, child, project


@pytest.mark.parametrize("speed", [
    SpeedParams(speed=2.0), SpeedParams(speed=1.0, reverse=True),
    SpeedParams(speed=1.0, speed_points=(SpeedPoint(5, 2.0),)),
])
def test_preview_diagnostics_and_strict_wrapper_share_retime_identity(speed) -> None:
    parent, child, project = _setup(speed=speed)
    diagnostics = []
    assert expand_sequence_audio_for_render(
        parent, {"child": child}.get, project, diagnostics=diagnostics) is parent
    assert len(diagnostics) == 1
    assert diagnostics[0].to_dict() == {
        "code": "nested_audio_retime_unsupported", "clip_id": "nest",
        "child_timeline_id": "child", "message": diagnostics[0].message,
    }
    with pytest.raises(NestedAudioExpansionError) as error:
        expand_sequence_audio_for_render(parent, {"child": child}.get, project, strict=True)
    assert error.value.diagnostics == tuple(diagnostics)


@pytest.mark.parametrize("kind,code", [
    ("fps", "nested_audio_fps_unsupported"),
    ("missing", "nested_audio_child_missing"),
    ("resolver", "nested_audio_resolver_missing"),
    ("depth", "nested_audio_depth_exceeded"),
    ("cycle", "nested_audio_cycle"),
])
def test_unsupported_structure_has_machine_readable_reason(kind, code) -> None:
    parent, child, project = _setup(speed=SpeedParams(speed=1.0),
                                    child_fps=30.0 if kind == "fps" else 24.0)
    if kind == "cycle":
        parent = replace(parent, timeline_id="child")
    resolver = None if kind == "resolver" else {"child": child}.get
    if kind == "missing":
        resolver = {}.get
    with pytest.raises(NestedAudioExpansionError) as error:
        flatten_nested_audio_tracks(parent, resolver, project.assets.get,
                                    max_depth=0 if kind == "depth" else 8, strict=True)
    assert error.value.diagnostics[0].code == code


@pytest.mark.parametrize("skip", ["muted", "track_disabled", "clip_disabled", "outside"])
def test_inaudible_instances_do_not_block(skip) -> None:
    parent, child, project = _setup()
    track = parent.video_tracks[0]
    if skip == "muted":
        track = replace(track, is_muted=True)
    elif skip == "track_disabled":
        track = replace(track, is_enabled=False)
    elif skip == "clip_disabled":
        track = replace(track, clips=(replace(track.clips[0], is_enabled=False),))
    parent = replace(parent, video_tracks=(track,))
    assert flatten_nested_audio_tracks(
        parent, {"child": child}.get, project.assets.get, strict=True,
        frame_range=(24, 48) if skip == "outside" else (0, 24),
    ) == ()


def test_cycle_in_grandchild_is_not_silently_removed() -> None:
    parent, child, project = _setup(speed=SpeedParams(speed=1.0))
    child = replace(child, video_tracks=parent.video_tracks)
    with pytest.raises(NestedAudioExpansionError) as error:
        flatten_nested_audio_tracks(parent, {"child": child}.get, project.assets.get, strict=True)
    assert error.value.diagnostics[0].code == "nested_audio_cycle"
    assert error.value.diagnostics[0].clip_id == "nest:nest:nest"


def _exporter(parent, child, project):
    return TimelineExporter(project, parent, Mock(), sequence_resolver={"child": child}.get)


def _config(tmp_path, **kwargs):
    return TimelineExportConfig(output_path=str(tmp_path / "out.mp4"),
                                timeline_id="parent", **kwargs)


def test_actual_export_blocks_before_encoder_and_success_manifest(tmp_path) -> None:
    parent, child, project = _setup()
    exporter = _exporter(parent, child, project)
    exporter._create_encoder = Mock()
    exporter._write_success_manifest_async = Mock()
    with pytest.raises(ValueError, match="Nested audio preflight failed"):
        exporter.export_timeline(_config(tmp_path))
    exporter._create_encoder.assert_not_called()
    exporter._write_success_manifest_async.assert_not_called()
    result = exporter.last_preflight_result
    assert not result.ok
    assert result.blockers[0].code == "nested_audio_retime_unsupported"
    assert result.blockers[0].clip_id == "nest"

    from src.features.edit.core.export_manifest import build_export_manifest
    manifest = build_export_manifest(
        project, parent, preflight_result=result, hash_assets=False,
        probe_media=False, capabilities=SimpleNamespace(to_dict=lambda: {}))
    assert json.loads(json.dumps(manifest))["preflight"]["blockers"] == [
        result.blockers[0].to_dict()]


def test_video_only_actual_export_completes_without_strict_expansion(tmp_path, monkeypatch) -> None:
    parent, child, project = _setup()
    exporter = _exporter(parent, child, project)
    encoder = Mock(supports_audio=True, hw_fallback_reason="")
    exporter._create_encoder = Mock(return_value=encoder)
    exporter._create_encoder_config = Mock(return_value=SimpleNamespace(
        codec="h264", pix_fmt="yuv420p", has_alpha=False))
    exporter._resolve_export_color_metadata = Mock(return_value=None)
    exporter._pipeline_eligible = Mock(return_value=False)
    exporter._render_frame = Mock(return_value=np.zeros((2, 2, 3), dtype=np.uint8))
    exporter._encode_frame = Mock()
    exporter._write_success_manifest_async = Mock()
    monkeypatch.setattr("src.features.edit.core.delivery_qc.run_delivery_qc",
                        lambda *_: SimpleNamespace(passed=True, warning_messages=()))
    # This assertion guards both preflight and preparation, not only has_audio.
    monkeypatch.setattr("src.features.edit.core.timeline_export.expand_sequence_audio_for_render",
                        Mock(side_effect=AssertionError("video-only expanded audio")))
    result = exporter.export_timeline(_config(tmp_path, include_audio=False))
    assert result.success, result.cause
    encoder.open.assert_called_once()
    exporter._write_success_manifest_async.assert_called_once()
    encoder.add_audio_stream.assert_not_called()


def test_export_preflight_uses_current_custom_range(tmp_path, monkeypatch) -> None:
    parent, child, project = _setup()
    # Preserve a real output window while the unsupported nest lies outside it.
    other = ClipInstance("other", "source", "v", 24, 24, 0, 24)
    parent = replace(parent, video_tracks=(replace(parent.video_tracks[0], clips=(
        parent.video_tracks[0].clips[0], other)),))
    exporter = _exporter(parent, child, project)
    # Reaching this independent preflight proves nested audio accepted [24,48).
    monkeypatch.setattr("src.features.edit.core.timeline_export.preflight_audio_export",
                        Mock(side_effect=RuntimeError("range accepted")))
    with pytest.raises(RuntimeError, match="range accepted"):
        exporter.export_timeline(_config(tmp_path, range_type=ExportRangeType.CUSTOM,
                                        start_frame=24, end_frame=48))


@pytest.mark.parametrize("entry", ["master", "worker", "direct", "stem_pcm", "stem_files"])
def test_audio_only_entry_points_never_swallow_nested_failure(tmp_path, monkeypatch, entry) -> None:
    parent, child, project = _setup()
    exporter = _exporter(parent, child, project)
    exporter._encoder = Mock()
    config = _config(tmp_path)
    writer = Mock()
    monkeypatch.setattr("src.features.edit.core.stem_export._StemWavWriter", writer)
    if entry == "worker":
        exporter._audio_export_worker(0, 24, config)
        assert isinstance(exporter._audio_render_error, NestedAudioExpansionError)
        assert exporter._audio_master_pcm is None
        return
    with pytest.raises(NestedAudioExpansionError):
        if entry == "master":
            exporter._render_audio_master(0, 24, config)
        elif entry == "direct":
            exporter._direct_audio_prepare(Mock(), SimpleNamespace(timeline_start=0, frames=24), config)
        elif entry == "stem_pcm":
            render_stem_pcm(parent, StemSpec(StemType.MASTER, "Master"),
                            sequence_resolver={"child": child}.get, asset_lookup=project.assets.get)
        else:
            export_all_stems(parent, str(tmp_path), sequence_resolver={"child": child}.get,
                             asset_lookup=project.assets.get)
    writer.assert_not_called()


def test_wrapper_propagates_unexpected_domain_provider_failure() -> None:
    parent, _child, project = _setup()
    def broken_resolver(_child_id: str):
        raise RuntimeError("corrupt sequence storage")
    with pytest.raises(RuntimeError, match="corrupt sequence storage"):
        expand_sequence_audio_for_render(parent, broken_resolver, project, strict=True)


@pytest.mark.parametrize("entry", ["file", "deliver"])
def test_production_stem_entry_captures_child_sequence_snapshot(monkeypatch, entry) -> None:
    parent, child, project = _setup()
    sequences = {"child": child}
    dialog = Mock()
    monkeypatch.setattr("src.features.edit.ui.stem_export_dialog.StemExportDialog", dialog)
    if entry == "file":
        from src.app.controllers.file_controller import FileOpsMixin
        ctrl = SimpleNamespace(_active_interchange_sequence=lambda: parent,
                               _current_edit_project=project, _current_timeline_sequences=sequences)
        FileOpsMixin.export_stem_tracks(ctrl)
    else:
        from src.features.deliver.ui.deliver_page import DeliverPage
        ctrl = SimpleNamespace(_sequence=parent, _project=project, _timeline_sequences=sequences)
        DeliverPage._export_stems(ctrl)
    resolver = dialog.call_args.kwargs["sequence_resolver"]
    sequences.clear()
    assert resolver("child") is child
    dialog.return_value.exec.assert_called_once()


def test_real_stem_dialog_worker_reports_structured_nested_failure(qapp, tmp_path) -> None:
    from src.features.edit.ui.stem_export_dialog import StemExportDialog, StemExportWorker
    parent, child, project = _setup()
    resolver = {"child": child}.get
    dialog = StemExportDialog(parent, project, sequence_resolver=resolver)
    worker = StemExportWorker(parent, project, str(tmp_path),
                              (StemSpec(StemType.MASTER, "Master"),),
                              sequence_resolver=resolver)
    failures, completed = [], []
    worker.failed.connect(failures.append)
    worker.finished_ok.connect(completed.append)
    dialog._worker = worker
    worker.failed.connect(dialog._on_failed)
    worker.run()
    assert not completed
    assert len(failures) == 1 and "nest" in failures[0] and "child" in failures[0]
    assert dialog.failure_diagnostics == worker.failure_diagnostics
    assert dialog.failure_diagnostics[0].code == "nested_audio_retime_unsupported"
    assert list(tmp_path.iterdir()) == []
    assert dialog._worker is None
    dialog.close()


def test_stem_dialog_plans_virtual_tracks_for_supported_nested_audio(qapp) -> None:
    from src.features.edit.ui.stem_export_dialog import StemExportDialog
    parent, child, project = _setup(speed=SpeedParams(speed=1.0))
    dialog = StemExportDialog(parent, project, sequence_resolver={"child": child}.get)
    specs = [spec for spec, _box in dialog._spec_boxes]
    assert specs[0].track_ids == ("nest:nest:a",)
    assert any(spec.stem_type == StemType.TRACK and spec.track_ids == ("nest:nest:a",)
               for spec in specs)
    dialog.close()


@pytest.mark.parametrize("failure", ["nested", "generic"])
def test_stem_dialog_initialization_visibly_blocks_invalid_plan_without_worker(
        qapp, tmp_path, monkeypatch, failure) -> None:
    from PyQt6.QtWidgets import QLabel
    from src.features.edit.ui.stem_export_dialog import StemExportDialog
    parent, child, project = _setup(
        speed=SpeedParams(speed=1.0) if failure == "generic" else None)
    if failure == "generic":
        monkeypatch.setattr("src.features.edit.ui.stem_export_dialog.plan_stems_from_sequence",
                            Mock(side_effect=RuntimeError("broken stem plan")))
    worker = Mock()
    monkeypatch.setattr("src.features.edit.ui.stem_export_dialog.StemExportWorker", worker)
    dialog = StemExportDialog(parent, project, sequence_resolver={"child": child}.get)
    dialog.show()
    qapp.processEvents()
    assert dialog._status_label.isVisible()
    assert not dialog._export_btn.isEnabled()
    assert dialog._spec_boxes == []
    assert all("当前时间线没有音频轨" not in label.text()
               for label in dialog.findChildren(QLabel))
    if failure == "nested":
        assert dialog.failure_diagnostics[0].code == "nested_audio_retime_unsupported"
        assert "nest" in dialog._status_label.text() and "child" in dialog._status_label.text()
    else:
        assert "broken stem plan" in dialog._status_label.text()
    dialog._dir_input.setText(str(tmp_path))
    # Programmatic invocation cannot bypass a disabled button or turn the
    # planning failure into an empty successful export.
    dialog._start_export()
    assert dialog._worker is None
    worker.assert_not_called()
    assert list(tmp_path.iterdir()) == []
    dialog.close()


# ── F3.1 补齐：子音轨 crossfade 诊断 / freeze 门 / clip 级 fade 存活 ──

def _crossfade_setup():
    from src.features.edit.core.timeline_v2 import TransitionInstance

    audio1 = ClipInstance("s1", "source", "a", 0, 24, 0, 24)
    audio2 = ClipInstance("s2", "source", "a", 24, 24, 0, 24)
    child = TimelineSequence("child", "Child", audio_tracks=(
        EditTrack("a", "Audio", "audio", 0, clips=(audio1, audio2),
                  transitions=(TransitionInstance(
                      transition_id="xf", track_id="a",
                      left_clip_id="s1", right_clip_id="s2",
                      center_frame=24, duration=12),)),))
    asset = create_nested_sequence_asset(child)
    nest = ClipInstance("nest", asset.asset_id, "v", 0, 48, 0, 48)
    parent = TimelineSequence("parent", "Parent", video_tracks=(
        EditTrack("v", "Video", "video", 0, clips=(nest,)),))
    source = MediaAsset("source", "/unused.wav", "audio", "Source", {})
    project = EditProject("project", "Project",
                          assets={asset.asset_id: asset, "source": source})
    return parent, child, project


def test_child_crossfade_drop_is_diagnosed() -> None:
    """F3.1：子音轨 crossfade 丢弃不再静默——strict 交付也知情。"""
    parent, child, project = _crossfade_setup()
    diagnostics = []
    flattened = flatten_nested_audio_tracks(
        parent, {"child": child}.get,
        lambda aid: project.assets.get(aid), diagnostics=diagnostics)
    codes = [d.code for d in diagnostics]
    assert "nested_audio_transition_dropped" in codes
    with pytest.raises(NestedAudioExpansionError) as error:
        flatten_nested_audio_tracks(
            parent, {"child": child}.get,
            lambda aid: project.assets.get(aid), strict=True)
    assert any(d.code == "nested_audio_transition_dropped"
               for d in error.value.diagnostics)
    # 展开本身仍完成（虚拟轨在场），只是转场丢失
    assert any(t.track_id.startswith("nest:") for t in flattened)


def test_freeze_parent_skips_child_audio_with_diagnosis() -> None:
    """F3.1：freeze 门（此前零测试的触发形态）。"""
    parent, child, project = _setup()
    parent_track = parent.video_tracks[0]
    frozen = replace(
        parent_track.clips[0], freeze_source_frame=0)
    parent = replace(parent, video_tracks=(replace(parent_track, clips=(frozen,)),))
    diagnostics = []
    flatten_nested_audio_tracks(
        parent, {"child": child}.get,
        lambda aid: project.assets.get(aid), diagnostics=diagnostics)
    assert any(d.code == "nested_audio_retime_unsupported" for d in diagnostics)


def test_clip_level_fades_survive_flatten() -> None:
    """F3.1：clip 级 fade_in/out 随字段复制保留（此前无断言）。"""
    parent, child, project = _setup(speed=SpeedParams(speed=1.0))
    # 在子 clip 上加 fade
    child_track = child.audio_tracks[0]
    faded = replace(child_track.clips[0],
                    fade_in_duration=6, fade_out_duration=4)
    child = replace(child, audio_tracks=(replace(child_track, clips=(faded,)),))
    asset = create_nested_sequence_asset(child)
    parent_track = parent.video_tracks[0]
    nest = replace(parent_track.clips[0], asset_id=asset.asset_id,
                   speed=SpeedParams(speed=1.0))
    parent = replace(parent, video_tracks=(replace(parent_track, clips=(nest,)),))
    project = EditProject("project", "Project",
                          assets={asset.asset_id: asset, "source": MediaAsset(
                              "source", "/unused.wav", "audio", "Source", {})})
    flattened = flatten_nested_audio_tracks(
        parent, {"child": child}.get, lambda aid: project.assets.get(aid))
    virtual = next(t for t in flattened if t.track_id.startswith("nest:"))
    assert virtual.clips[0].fade_in_duration == 6
    assert virtual.clips[0].fade_out_duration == 4
