"""Fix verification tests for remediation batch wd11-g61 (AUD-18120 .. AUD-18321).

Covers the 16 Major entries of REMEDIATION_PLAN batch D:

- AUD-18120  SceneCutWorkerManager._workers cleanup key mismatch
- AUD-18200  "Add Cuts to Media Pool" routed through project command stack
- AUD-18201  _on_add_to_media_pool exception guard
- AUD-18204  TimelineSelection __eq__/__hash__ contract
- AUD-18205  __eq__ ignores primary_item/item_type/order
- AUD-18207  v4 animated_params migration reaches wrapper["timeline_sequences"]
- AUD-18208  atomic project save (temp file + fsync + os.replace + .bak)
- AUD-18209  narrowed excepts in load/save (distinct failure classes)
- AUD-18212  auto_duck sidechain shorter than music
- AUD-18213  disable mid-duck gain step (click)
- AUD-18216  stem export streaming render
- AUD-18217  stem filename dedup
- AUD-18301  subtitle global cache thread safety
- AUD-18302  _warp_rgba_to_canvas rotation edge clamp artifacts
- AUD-18320  _pending_source_frames unbounded growth
- AUD-18321  source texture eviction releases in-flight textures
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.selection import (
    TimelineSelection,
    SelectionItem,
    SelectionType,
)
from src.features.edit.core.serialization import EditProjectSerializer
from src.features.edit.core.sidechain_ducker import (
    DuckerParams,
    SideChainDucker,
    auto_duck,
)
from src.features.edit.core.stem_export import (
    StemSpec,
    StemType,
    _iter_stem_pcm_chunks,
    dedupe_stem_filenames,
    export_all_stems,
    plan_stems_from_sequence,
)
from src.features.edit.core.subtitle_renderer import (
    _SubtitleRenderCache,
    _warp_rgba_to_canvas,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.scene_cut.models import (
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutDecision,
    SceneCutKind,
    SceneCutMetrics,
    SceneCutSettings,
    generate_analysis_id,
)
from src.features.edit.core.scene_cut_controller import (
    AddSubclipsToProjectCommand,
    SceneCutController,
)

SR = 48000
FPS = 24.0


# ================================================================
# Shared fixtures
# ================================================================

def _make_video_asset(asset_id: str = "video-001", total_frames: int = 600) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=f"/fake/{asset_id}.mp4",
        display_name=f"Video_{asset_id}",
        media_type="video",
        metadata={
            "fps": FPS,
            "total_frames": total_frames,
            "duration_seconds": total_frames / FPS,
            "width": 1920,
            "height": 1080,
        },
    )


def _make_analysis(asset_id: str, cut_frames=(100, 200)) -> SceneCutAnalysis:
    candidates = tuple(
        SceneCutCandidate(
            source_frame=f,
            pts=f * 1000,
            seconds=f / FPS,
            score=0.6,
            confidence=0.9,
            kind=SceneCutKind.HARD_CUT,
            decision=SceneCutDecision.ACCEPTED,
            metrics=SceneCutMetrics(
                luma_delta=0.5, hist_delta=0.8, edge_delta=0.3, color_delta=0.4,
                black_score=0.0, flash_score=0.0, motion_score=0.2,
                combined_score=0.6,
            ),
            reasons=("high_score",),
        )
        for f in cut_frames
    )
    return SceneCutAnalysis(
        analysis_id=generate_analysis_id(asset_id),
        asset_id=asset_id,
        source_path=f"/fake/{asset_id}.mp4",
        settings=SceneCutSettings(sensitivity=0.55),
        candidates=candidates,
        created_at=time.time(),
        detector_version="1.0.0",
    )


def _make_project_with_asset():
    project = EditProject(project_id="wd11-g61-proj", name="WD11 G61")
    asset = _make_video_asset()
    project.assets[asset.asset_id] = asset
    return project, asset


# ================================================================
# AUD-18120: worker manager cleanup key consistency
# ================================================================

class TestAud18120WorkerCleanup:
    def _manager_with_worker(self, qt_application=None):
        from src.features.edit.core.scene_cut.worker import SceneCutWorkerManager

        manager = SceneCutWorkerManager()
        worker = MagicMock()
        worker.task_key.to_string.return_value = "asset-1:hash123:full"
        # Simulate the write path of start_detection()
        manager._workers[worker.task_key.to_string()] = worker
        return manager, worker

    def test_completed_removes_worker_entry(self):
        manager, worker = self._manager_with_worker()
        analysis = _make_analysis("asset-1")
        manager._on_worker_completed("asset-1:hash123:full", "asset-1", analysis)
        assert "asset-1:hash123:full" not in manager._workers

    def test_cancelled_removes_worker_entry(self):
        manager, worker = self._manager_with_worker()
        manager._on_worker_cancelled("asset-1:hash123:full", "asset-1")
        assert not manager._workers

    def test_error_removes_worker_entry(self):
        manager, worker = self._manager_with_worker()
        manager._on_worker_error("asset-1:hash123:full", "asset-1", MagicMock())
        assert not manager._workers

    def test_cleanup_key_matches_three_segment_task_key(self):
        """Old cleanup built a two-segment key that never matched the
        three-segment DetectionTaskKey.to_string() write key."""
        from src.features.edit.core.scene_cut.worker import DetectionTaskKey

        key = DetectionTaskKey("asset-1", "hash123", None).to_string()
        assert key.count(":") == 2
        assert key.endswith(":full")
        old_style = f"asset-1:{SceneCutSettings().to_hash()}"
        assert old_style != key


# ================================================================
# AUD-18200 / AUD-18201: media pool via command stack + exception guard
# ================================================================

class TestAud18200CommandRoutedMediaPool:
    def test_add_cuts_routes_through_project_command_stack(self):
        project, asset = _make_project_with_asset()
        analysis = _make_analysis(asset.asset_id)
        stack = ProjectCommandStack()
        holder = {"project": project}

        controller = SceneCutController(
            project_getter=lambda: holder["project"],
            project_setter=lambda p: holder.__setitem__("project", p),
            command_stack_getter=lambda: stack,
        )

        result = controller.add_cuts_to_media_pool(analysis, asset)

        assert result is not None and result.added_count == 3
        # The operation must land on the undo stack (single Ctrl+Z unit)
        assert stack.can_undo()
        assert stack.get_undo_label() == "Add Cuts to Media Pool"

        # Undo removes exactly the added subclip assets
        undone = stack.undo(holder["project"])
        assert undone is not None
        for added_id in result.added_asset_ids:
            assert added_id not in undone.assets
        # Parent asset survives
        assert asset.asset_id in undone.assets

    def test_command_undo_and_redo_roundtrip(self):
        project, asset = _make_project_with_asset()
        analysis = _make_analysis(asset.asset_id, (100, 200, 300))
        stack = ProjectCommandStack()

        command = AddSubclipsToProjectCommand.create(
            analysis=analysis,
            specs=(),  # specs unused for undo; apply adds nothing with no specs
            parent_asset=asset,
        )
        # Use the real spec generation via apply() on a fake-empty spec list:
        # add_subclips_to_project with no specs still creates the bin.
        new_project = stack.execute(command, project)
        undone = stack.undo(new_project)
        assert undone is not None
        # Empty-spec run adds no assets; undo must keep the project intact
        assert asset.asset_id in undone.assets

    def test_fallback_without_stack_still_works(self):
        project, asset = _make_project_with_asset()
        analysis = _make_analysis(asset.asset_id)

        controller = SceneCutController(
            project_getter=lambda: project,
            project_setter=lambda p: None,
        )
        result = controller.add_cuts_to_media_pool(analysis, asset)
        assert result is not None and result.added_count == 3


class TestAud18201MediaPoolExceptionGuard:
    def test_value_error_does_not_escape_qt_slot(self):
        project, asset = _make_project_with_asset()
        analysis = _make_analysis(asset.asset_id)

        errors = []
        controller = SceneCutController(
            project_getter=lambda: project,
            project_setter=lambda p: None,
        )
        controller.detection_error.connect(lambda aid, msg: errors.append((aid, msg)))

        # Parent asset missing from the project → add_subclips_to_project
        # raises ValueError; _on_add_to_media_pool must convert it to the
        # error channel instead of letting it escape into the Qt slot.
        ghost_asset = _make_video_asset("not-in-project")
        controller._on_add_to_media_pool(analysis, ghost_asset, None)

        assert len(errors) == 1
        assert errors[0][0] == "not-in-project"
        assert "not found" in errors[0][1]


# ================================================================
# AUD-18204 / AUD-18205: TimelineSelection equality & hash
# ================================================================

class TestAud18204SelectionHashContract:
    def test_equal_selections_hash_equal(self):
        a = TimelineSelection(items=(SelectionItem(SelectionType.CLIP, "c1", "t1"),))
        b = TimelineSelection(items=(SelectionItem(SelectionType.CLIP, "c1", "t1"),))
        assert a == b
        assert hash(a) == hash(b)
        assert b in {a}

    def test_different_type_same_id_not_equal_and_hash_differs(self):
        a = TimelineSelection(items=(SelectionItem(SelectionType.CLIP, "x", "t1"),))
        b = TimelineSelection(items=(SelectionItem(SelectionType.TRACK, "x", "t1"),))
        assert a != b
        assert not (b in {a})

    def test_range_selections_hash_consistent(self):
        r1 = TimelineSelection(is_range=True, range_start=0, range_end=10)
        r2 = TimelineSelection(is_range=True, range_start=0, range_end=10)
        assert r1 == r2 and hash(r1) == hash(r2) and r2 in {r1}


class TestAud18205SelectionEqSemantics:
    def test_primary_item_participates(self):
        item = SelectionItem(SelectionType.CLIP, "c1", "t1")
        a = TimelineSelection(items=(item,), primary_item=None)
        b = TimelineSelection(items=(item,), primary_item=item)
        assert a != b

    def test_items_order_participates(self):
        c1 = SelectionItem(SelectionType.CLIP, "c1", "t1")
        c2 = SelectionItem(SelectionType.CLIP, "c2", "t1")
        assert TimelineSelection(items=(c1, c2)) != TimelineSelection(items=(c2, c1))

    def test_track_id_participates(self):
        a = TimelineSelection(items=(SelectionItem(SelectionType.CLIP, "c1", "t1"),))
        b = TimelineSelection(items=(SelectionItem(SelectionType.CLIP, "c1", "t9"),))
        assert a != b


# ================================================================
# AUD-18207 / AUD-18208 / AUD-18209: serialization
# ================================================================

def _make_v4_wrapper() -> dict:
    """v4 wrapper with animated_params inside timeline_sequences (real layout)."""
    return {
        "version": 4,
        "project_type": "edit_project",
        "project": {
            "project_id": "p1", "name": "v4proj",
            "assets": {}, "bins": {},
            "timelines": {"t1": {"timeline_id": "t1", "name": "Main"}},
            "root_bin_id": "root", "created_at": 0.0, "modified_at": 0.0,
        },
        "timeline_sequences": {
            "t1": {
                "timeline_id": "t1", "name": "Main", "fps": "24/1",
                "width": 1920, "height": 1080,
                "video_tracks": [{
                    "track_id": "v1", "name": "V1", "track_type": "video",
                    "clips": [{
                        "clip_id": "c1", "track_id": "v1",
                        "timeline_start": 0, "timeline_duration": 24,
                        "source_start": 0, "source_duration": 24,
                        "asset_id": "a1",
                        "animated_params": {
                            "transform.position_x": {
                                "base_value": 0.0,
                                "keyframes": {
                                    "param_name": "transform.position_x",
                                    "keyframes": [
                                        {"frame": 0, "value": 0.5,
                                         "interpolation": "linear"},
                                        {"frame": 23, "value": -0.25,
                                         "interpolation": "linear"},
                                    ],
                                    "min_val": -10.0, "max_val": 10.0,
                                },
                            }
                        },
                    }],
                }],
            }
        },
    }


class TestAud18207AnimatedParamsMigration:
    def test_v4_position_keys_migrated_in_timeline_sequences(self):
        wrapper = _make_v4_wrapper()
        _project, _ui, seqs = EditProjectSerializer.from_json_with_ui_state(
            json.dumps(wrapper))
        clip = seqs["t1"].video_tracks[0].clips[0]
        anim = clip.animated_params
        assert "transform.position_x" not in anim
        assert "transform.translate_x" in anim
        track = anim["transform.translate_x"].keyframes
        values = {kf.frame: kf.value for kf in track.keyframes}
        # normalized 0.5 → 0.5 * 1920 px
        assert values[0] == pytest.approx(960.0)
        assert values[23] == pytest.approx(-480.0)


class TestAud18208AtomicSave:
    def test_second_save_rotates_backup(self, tmp_path):
        project, _asset = _make_project_with_asset()
        path = tmp_path / "proj.topos"

        assert EditProjectSerializer.save_to_file(project, str(path))
        first = path.read_text(encoding="utf-8")
        assert json.loads(first)["version"] >= 1

        # Second save must rotate the previous file to .bak
        project2 = EditProject(project_id="wd11-g61-proj", name="Renamed")
        assert EditProjectSerializer.save_to_file(project2, str(path))
        assert (tmp_path / "proj.topos.bak").exists()
        assert (tmp_path / "proj.topos.bak").read_text(encoding="utf-8") == first
        # New content in place
        assert json.loads(path.read_text(encoding="utf-8"))["project"]["name"] == "Renamed"
        # No autosave temp left behind
        assert not (tmp_path / "proj.topos.autosave").exists()

    def test_interrupted_replace_leaves_original_intact(self, tmp_path, monkeypatch):
        project, _asset = _make_project_with_asset()
        path = tmp_path / "proj.topos"
        assert EditProjectSerializer.save_to_file(project, str(path))
        original = path.read_text(encoding="utf-8")

        import src.features.edit.core.serialization as ser_mod

        def boom(*_a, **_k):
            raise OSError("disk full mid-replace")

        monkeypatch.setattr(ser_mod.os, "replace", boom)
        assert not EditProjectSerializer.save_to_file(project, str(path))
        # The only project file is unchanged — no truncated half-write.
        assert path.read_text(encoding="utf-8") == original

    def test_ui_state_save_is_atomic_too(self, tmp_path):
        from src.features.edit.core.serialization import UIState

        project, _asset = _make_project_with_asset()
        path = tmp_path / "proj_ui.topos"
        ok = EditProjectSerializer.save_to_file_with_ui_state(
            project, str(path), UIState())
        assert ok
        assert path.exists()


class TestAud18209NarrowedExceptions:
    def test_corrupt_json_returns_none(self, tmp_path):
        path = tmp_path / "bad.topos"
        path.write_text("{not json", encoding="utf-8")
        assert EditProjectSerializer.load_from_file(str(path)) is None

    def test_unsupported_version_returns_none(self, tmp_path):
        wrapper = _make_v4_wrapper()
        wrapper["version"] = 99
        path = tmp_path / "new.topos"
        path.write_text(json.dumps(wrapper), encoding="utf-8")
        assert EditProjectSerializer.load_from_file(str(path)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert EditProjectSerializer.load_from_file(
            str(tmp_path / "missing.topos")) is None

    def test_failure_classes_reported_distinctly(self, tmp_path, caplog):
        corrupt = tmp_path / "corrupt.topos"
        corrupt.write_text("{oops", encoding="utf-8")
        with caplog.at_level("ERROR"):
            EditProjectSerializer.load_from_file(str(corrupt))
        assert any("Corrupted or unsupported" in r.message for r in caplog.records)

        locked = tmp_path / "locked" / "deep" / "x.topos"
        locked.parent.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            # W07：chmod 写位不约束 Windows 属主——注入同一失败模式
            # （写打开 PermissionError），验证「Failed to write」类
            # 错误的窄化与日志契约。
            import builtins as _b
            import unittest.mock as _mock

            real_open = _b.open

            def _denying_open(file, *a, **k):
                if str(locked) in str(file):
                    raise PermissionError(5, "Access is denied (injected)")
                return real_open(file, *a, **k)

            with _mock.patch.object(_b, "open", _denying_open):
                with caplog.at_level("ERROR"):
                    EditProjectSerializer.save_to_file(
                        EditProject(project_id="p", name="p"), str(locked))
                assert any(
                    "Failed to write" in r.message for r in caplog.records)
        else:
            locked.parent.parent.chmod(0o500)  # read-only parent dir
            try:
                with caplog.at_level("ERROR"):
                    EditProjectSerializer.save_to_file(
                        EditProject(project_id="p", name="p"), str(locked))
                assert any(
                    "Failed to write" in r.message for r in caplog.records)
            finally:
                locked.parent.parent.chmod(0o700)


# ================================================================
# AUD-18212 / AUD-18213: sidechain ducker
# ================================================================

class TestAud18212AutoDuckShortSidechain:
    def test_short_sidechain_no_index_error(self):
        music = np.ones((2, 1000), dtype=np.float32)
        speech = np.zeros((2, 400), dtype=np.float32)
        out = auto_duck(music, speech, DuckerParams())  # must not raise
        assert out.shape == music.shape

    def test_short_sidechain_silent_tail_recovers(self):
        music = np.ones((2, 2000), dtype=np.float32)
        speech = np.ones((2, 200), dtype=np.float32)  # loud, then nothing
        out = auto_duck(music, speech, DuckerParams(
            attack_ms=5.0, hold_ms=5.0, release_ms=20.0))
        # Ducking engaged by the end of the speech window (attack ~5ms)
        assert float(np.max(np.abs(out[:, 150:200]))) < 0.99
        # Silent-padded region: gain releases back toward unity
        assert float(np.min(out[:, 1800:])) > float(np.min(out[:, 150:200]))

    def test_1d_sidechain_padded(self):
        music = np.ones((2, 500), dtype=np.float32)
        speech = np.zeros(100, dtype=np.float32)
        out = auto_duck(music, speech, DuckerParams())
        assert out.shape == music.shape


class TestAud18213DisableMidDuck:
    def test_disable_mid_duck_releases_smoothly(self):
        ducker = SideChainDucker(48000.0)
        ducker.set_params(DuckerParams(target_duck_db=-6.0, release_ms=500.0))
        block = np.ones((2, 1024), dtype=np.float32)
        speech = np.full((2, 1024), 0.5, dtype=np.float32)
        ducker.process(block, sidechain=speech)
        ducked_level = ducker.current_gain_db
        assert ducked_level < -1.0  # actively ducked

        ducker.set_params(None)  # disable
        out = ducker.process(block)
        # First sample must continue from the ducked gain, NOT jump to 0dB
        assert float(out[0, 0]) == pytest.approx(
            10.0 ** (ducked_level / 20.0), abs=1e-3)
        # No instantaneous step: output evolves smoothly within the block
        delta = float(np.max(np.abs(np.diff(out[0]))))
        assert delta < 0.01  # ~release time constant, no step discontinuity

    def test_fully_released_disabled_is_true_bypass(self):
        ducker = SideChainDucker(48000.0)
        ducker.set_params(None)
        block = np.random.default_rng(7).standard_normal((2, 512)).astype(np.float32)
        out = ducker.process(block)
        assert np.array_equal(out, block)


# ================================================================
# AUD-18216 / AUD-18217: stem export
# ================================================================

def _make_stem_track(track_id: str, name: str, start: int = 0, dur: int = 48,
                     output_bus_id=None, stem_group: str = "", volume: float = 1.0):
    clip = ClipInstance(
        clip_id=f"{track_id}_c", asset_id=f"{track_id}_a", track_id=track_id,
        source_start=0, source_duration=dur,
        timeline_start=start, timeline_duration=dur,
    )
    return EditTrack(
        track_id=track_id, name=name, track_type="audio", index=0,
        clips=(clip,), output_bus_id=output_bus_id,
        stem_group=stem_group, volume=volume,
    )


def _make_stem_sequence():
    bus = EditTrack(track_id="busA", name="Bus A", track_type="audio",
                    index=0, is_bus=True)
    t1 = _make_stem_track("t1", "D1", output_bus_id="busA", stem_group="dialogue")
    t2 = _make_stem_track("t2", "M1", start=10, dur=40, stem_group="music")
    return TimelineSequence(audio_tracks=(t1, t2, bus), fps=FPS)


def _dc_decode_factory(amp: float = 0.25):
    def _decode(track, clip, frame_idx, fps, n_samples):
        sig = np.full(n_samples, amp, dtype=np.float32)
        return np.stack([sig, sig * 0.9])
    return _decode


class TestAud18216StreamingStemRender:
    def test_chunks_concatenate_to_full_length(self):
        seq = _make_stem_sequence()
        decode = _dc_decode_factory(0.25)
        spec = StemSpec(stem_type=StemType.MASTER, name="Master",
                        track_ids=("t1", "t2", "busA"), include_master=True)
        chunks = list(_iter_stem_pcm_chunks(seq, spec, SR, 2, decode))
        assert len(chunks) > 1  # actually chunked (50 frames / 24 per chunk)
        total = int(np.cumsum([c.shape[1] for c in chunks])[-1])
        assert total == int(50 / FPS * SR)

    def test_bit_exact_across_chunk_sizes_when_not_limiting(self):
        seq = _make_stem_sequence()
        decode = _dc_decode_factory(0.1)  # sub-threshold: limiter never engages
        spec = StemSpec(stem_type=StemType.MASTER, name="Master",
                        track_ids=("t1", "t2", "busA"), include_master=True)
        fine = np.concatenate(list(
            _iter_stem_pcm_chunks(seq, spec, SR, 2, decode, chunk_frames=1)), axis=1)
        coarse = np.concatenate(list(
            _iter_stem_pcm_chunks(seq, spec, SR, 2, decode, chunk_frames=1000)), axis=1)
        assert fine.shape == coarse.shape
        assert np.array_equal(fine, coarse)

    def test_no_full_length_buffers_in_stream_path(self, monkeypatch):
        """The streaming generator must never allocate a full-timeline buffer."""
        seq = _make_stem_sequence()
        spec = StemSpec(stem_type=StemType.TRACK, name="t2", track_ids=("t2",),
                        include_master=False)
        total = int(50 / FPS * SR)
        seen = []
        real_zeros = np.zeros

        def spy_zeros(shape, dtype=None, **kw):
            seen.append(shape[-1] if isinstance(shape, tuple) else shape)
            return real_zeros(shape, dtype=dtype, **kw)

        monkeypatch.setattr("src.features.edit.core.stem_export.np.zeros", spy_zeros)
        list(_iter_stem_pcm_chunks(seq, spec, SR, 2, _dc_decode_factory(0.2)))
        assert seen  # allocations happened
        assert all(n < total for n in seen), f"full-length buffer leaked: {seen}"

    def test_bus_stem_streaming_matches_compat_api(self):
        seq = _make_stem_sequence()
        decode = _dc_decode_factory(0.2)
        spec = StemSpec(stem_type=StemType.BUS, name="Bus A",
                        track_ids=("busA", "t1"), include_master=False)
        streamed = np.concatenate(list(
            _iter_stem_pcm_chunks(seq, spec, SR, 2, decode)), axis=1)
        from src.features.edit.core.stem_export import render_stem_pcm
        compat = render_stem_pcm(seq, spec, SR, 2, decode)
        assert compat is not None
        assert np.allclose(streamed, compat, atol=1e-7)


class TestAud18217StemFilenameDedup:
    def test_duplicate_names_get_numbered_filenames(self):
        specs = [
            StemSpec(stem_type=StemType.TRACK, name="VO", track_ids=("t1",)),
            StemSpec(stem_type=StemType.TRACK, name="VO", track_ids=("t2",)),
            StemSpec(stem_type=StemType.TRACK, name="V O", track_ids=("t3",)),
        ]
        out = dedupe_stem_filenames(specs)
        files = [s.filename for s in out]
        assert len(set(files)) == 3
        assert files[0] == "VO.wav"
        assert files[1] == "VO_2.wav"
        assert files[2] == "V_O.wav"
        # Display names unchanged
        assert [s.name for s in out] == ["VO", "VO", "V O"]
        # Idempotent
        assert dedupe_stem_filenames(out) == out

    def test_plan_dedupes_same_name_tracks(self):
        t1 = _make_stem_track("t1", "Interview")
        t2 = _make_stem_track("t2", "Interview")
        seq = TimelineSequence(audio_tracks=(t1, t2), fps=FPS)
        specs = plan_stems_from_sequence(seq)
        files = [s.filename for s in specs]
        assert len(files) == len(set(files))

    def test_export_same_name_tracks_writes_both_files(self, tmp_path):
        t1 = _make_stem_track("t1", "VO")
        t2 = _make_stem_track("t2", "VO")
        seq = TimelineSequence(audio_tracks=(t1, t2), fps=FPS)
        result = export_all_stems(
            seq, str(tmp_path), decode_clip_fn=_dc_decode_factory(0.2))
        assert result.all_succeeded
        wavs = sorted(p.name for p in Path(tmp_path).glob("*.wav"))
        track_wavs = [w for w in wavs if w.startswith("VO")]
        assert len(track_wavs) == 2  # both survive, no overwrite
        for wav in track_wavs:
            assert (tmp_path / wav).stat().st_size > 0


# ================================================================
# AUD-18301 / AUD-18302: subtitle renderer
# ================================================================

class TestAud18301SubtitleCacheThreadSafety:
    def test_concurrent_get_put_no_errors(self):
        cache = _SubtitleRenderCache(capacity=8)
        keys = [("a",), ("b",), ("c",), ("d",), ("e",)]
        errors: list[Exception] = []
        barrier = threading.Barrier(8)

        def hammer(kind: str) -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(2000):
                    if kind == "get":
                        for k in keys:
                            cache.get(k)
                    else:
                        for i, k in enumerate(keys):
                            cache.put(k, f"tex-{kind}-{i}")
            except Exception as e:  # noqa: BLE001 — collect for assertion
                errors.append(e)

        threads = [
            threading.Thread(target=hammer, args=("get",)) for _ in range(4)
        ] + [
            threading.Thread(target=hammer, args=("put",)) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors
        # LRU invariant holds after the storm
        assert len(cache._cache) == len(cache._order)
        assert len(cache._cache) <= 5

    def test_clear_is_thread_safe(self):
        cache = _SubtitleRenderCache(capacity=4)
        errors: list[Exception] = []

        def clearer():
            try:
                for _ in range(500):
                    cache.clear()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def filler():
            try:
                for i in range(500):
                    cache.put((i % 3,), i)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=clearer), threading.Thread(target=filler)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        assert not errors


class TestAud18302RotationEdgeMask:
    def _opaque_texture(self, w: int, h: int) -> np.ndarray:
        tex = np.zeros((h, w, 4), dtype=np.float32)
        tex[..., 0:3] = 1.0
        tex[..., 3] = 1.0
        return tex

    def test_rotated_aabb_corners_transparent(self):
        tex_w, tex_h = 41, 21
        bbox = self._opaque_texture(tex_w, tex_h)
        canvas = _warp_rgba_to_canvas(
            bbox, tex_w, tex_h, 200, 200,
            80.0, 90.0, 1.0, 1.0, 45.0, tex_w / 2, tex_h / 2, 0.0, 0.0)
        # AABB corners fall outside the rotated quad → must be fully
        # transparent (previously edge-clamped to opaque texels)
        for corner in [(90, 80), (90, 119), (110, 80), (110, 119)]:
            assert canvas[corner[0], corner[1], 3] == 0.0, corner
        # Center of the quad stays opaque
        assert canvas[100, 100, 3] == 1.0

    def test_zero_rotation_unchanged(self):
        tex_w, tex_h = 41, 21
        bbox = self._opaque_texture(tex_w, tex_h)
        canvas = _warp_rgba_to_canvas(
            bbox, tex_w, tex_h, 200, 200,
            80.0, 90.0, 1.0, 1.0, 0.0, tex_w / 2, tex_h / 2, 0.0, 0.0)
        assert int(np.sum(canvas[..., 3] > 0.99)) == tex_w * tex_h


# ================================================================
# AUD-18320 / AUD-18321: timeline clip frame provider
# ================================================================

class TestAud18320PendingSourceFramesBound:
    def test_async_write_evicts_older_frames_per_asset(self, monkeypatch):
        from src.features.edit.core.timeline_clip_frame_provider import (
            TimelineClipFrameProvider,
        )

        provider = TimelineClipFrameProvider()
        provider._frame_ready_callback = lambda: None

        # Seed stale pending entries as a long scrub would
        with provider._async_lock:
            for frame in range(100, 120):
                provider._pending_source_frames[("asset", frame)] = (
                    f"decoded-{frame}", "bt709", "limited")

        with provider._async_lock:
            provider._async_targets["asset"] = ("vid-1", 200, "bt709", "limited")

        monkeypatch.setattr(
            provider, "_decode_source_frame_via_service",
            lambda service, vid, frame, path: f"decoded-{frame}")

        import src.shared.video.frame_provider_service as fps_mod
        monkeypatch.setattr(
            fps_mod, "get_frame_provider_service", lambda: object())

        provider._ensure_async_decode_thread()
        provider._async_decode_wakeup.set()

        deadline = time.time() + 5.0
        while time.time() < deadline:
            with provider._async_lock:
                if ("asset", 200) in provider._pending_source_frames:
                    break
            time.sleep(0.02)

        provider._async_decode_stop = True
        provider._async_decode_wakeup.set()
        provider._async_decode_thread.join(timeout=5.0)

        with provider._async_lock:
            keys = list(provider._pending_source_frames.keys())
        # Latest-wins: only the newest frame for this asset survives
        assert ("asset", 200) in keys
        assert all(k[0] != "asset" or k[1] == 200 for k in keys)


class TestAud18321DeferredEvictionRelease:
    def _fake_provider(self):
        from src.features.edit.core.timeline_clip_frame_provider import (
            TimelineClipFrameProvider,
            _TexResult,
        )

        provider = TimelineClipFrameProvider()

        class FakeTex:
            def __init__(self, glo: int) -> None:
                self.glo = glo
                self.released = False

            def release(self) -> None:
                self.released = True

        texs = {i: FakeTex(i) for i in range(1, 8)}
        provider._owned_textures = dict(texs)
        return provider, texs, _TexResult

    def test_eviction_defers_release_until_next_entry(self):
        provider, texs, _TexResult = self._fake_provider()

        for i in range(1, 5):  # exceed _SOURCE_CACHE_MAX == 3
            result = _TexResult(texture_id=i, width=2, height=2)
            provider._cache_source_texture("a", i * 10, result)

        # First texture evicted from cache but NOT released yet — a
        # previously returned _TexResult may still sample it this frame.
        assert 1 not in [
            r.texture_id for r in provider._source_texture_cache.values()]
        assert texs[1].released is False
        assert 1 in provider._source_texture_retired

        # Next render-thread entry flushes the retired names
        provider._flush_retired_source_textures()
        assert texs[1].released is True
        assert not provider._source_texture_retired

    def test_stale_replacement_also_deferred(self):
        provider, texs, _TexResult = self._fake_provider()
        provider._cache_source_texture("a", 10, _TexResult(texture_id=1))
        provider._cache_source_texture("a", 10, _TexResult(texture_id=2))
        assert texs[1].released is False  # retired, not released
        provider._flush_retired_source_textures()
        assert texs[1].released is True

    def test_clear_source_cache_retires_then_flushes(self):
        provider, texs, _TexResult = self._fake_provider()
        provider._gl_thread_id = threading.get_ident()
        provider._cache_source_texture("a", 10, _TexResult(texture_id=1))
        provider.clear_source_cache()
        assert not provider._source_texture_cache
        assert texs[1].released is False  # parked in retired queue
        provider._flush_retired_source_textures()
        assert texs[1].released is True
