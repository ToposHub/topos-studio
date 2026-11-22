"""Independent edge-case regressions for bounded analysis and render metadata."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from src.features.edit.core.monitor_service import MonitorService, MonitorSessionState
from src.features.edit.core.render_artifacts import ArtifactRegistry, RenderArtifact, freeze_staleness
from src.features.edit.core.waveform_pyramid import WaveformPyramid, plan_decode_window


def _artifact(artifact_id: str, **changes: object) -> RenderArtifact:
    return replace(RenderArtifact(artifact_id, "freeze", "p", "s", 1, "sound"), **changes)


def test_pyramid_budget_includes_every_channel_and_level() -> None:
    pcm = np.zeros((6, 160_000), np.float32)
    pcm[5, 100_013] = -0.9
    pyramid = WaveformPyramid(pcm, max_bytes=512)
    assert pyramid.memory_bytes() <= 512
    arrays = [array for level in pyramid._levels
              for array in (level.minimum, level.maximum, level.rms)]
    assert pyramid.memory_bytes() == sum(array.nbytes for array in arrays)
    assert all(not array.flags.writeable for array in arrays)
    assert pyramid.range_extremes(100_000, 101_000, 5)[0] == pytest.approx(-0.9)


def test_coarse_pyramid_weighted_rms_includes_short_last_tile() -> None:
    pcm = np.zeros((2, 100_100), np.float32)
    pcm[:, -700:] = 0.75
    pyramid = WaveformPyramid(pcm, max_bytes=200)
    assert pyramid.range_extremes(0, pcm.shape[1], 0)[2] == pytest.approx(
        float(np.sqrt(np.mean(pcm[0].astype(np.float64) ** 2))), rel=2e-7)


@pytest.mark.parametrize("budget", [-1, 0, 7])
def test_decode_budget_too_small_for_frame_is_rejected(budget: int) -> None:
    with pytest.raises(ValueError, match="budget"):
        plan_decode_window(0, 1000, 1, 100, 2, max_bytes=budget)


def test_negative_prefetch_is_rejected() -> None:
    with pytest.raises(ValueError, match="prefetch"):
        plan_decode_window(0, 1000, 1, 100, 2, prefetch_samples=-10)


def test_stale_registry_does_not_revert_external_update_or_resurrect_delete(tmp_path: Path) -> None:
    old = ArtifactRegistry(tmp_path)
    old.register(_artifact("a"))
    recent = ArtifactRegistry(tmp_path)
    recent.register(_artifact("a", status="ready", file_uri="a.wav"))
    old.register(_artifact("b"))
    assert ArtifactRegistry(tmp_path).get("a").status == "ready"
    recent.remove("a")
    old.register(_artifact("c"))
    assert ArtifactRegistry(tmp_path).get("a") is None


def test_registry_remove_reads_current_disk_even_if_previously_loaded(tmp_path: Path) -> None:
    stale = ArtifactRegistry(tmp_path)
    assert stale.list() == ()
    ArtifactRegistry(tmp_path).register(_artifact("a"))
    assert stale.remove("a")
    assert ArtifactRegistry(tmp_path).list() == ()


def test_failed_registry_write_does_not_publish_memory_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ArtifactRegistry(tmp_path)
    registry.register(_artifact("a"))
    before = registry.path.read_bytes()

    def fail_replace(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("src.features.edit.core.render_artifacts.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        registry.register(_artifact("b"))
    assert registry.get("b") is None
    assert registry.path.read_bytes() == before
    assert list(registry.path.parent.glob("*.tmp")) == []


def test_registry_preserves_unknown_fields_and_refuses_corrupt_rewrite(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path)
    registry.register(_artifact("a"))
    data = json.loads(registry.path.read_text())
    data["extension"] = {"future": True}
    data["artifacts"][0]["future_note"] = "keep"
    registry.path.write_text(json.dumps(data))
    registry.register(_artifact("b"))
    saved = json.loads(registry.path.read_text())
    assert saved["extension"] == data["extension"]
    assert saved["artifacts"][0]["future_note"] == "keep"
    registry.path.write_text("{broken")
    with pytest.raises(ValueError):
        registry.register(_artifact("c"))
    assert registry.path.read_text() == "{broken"


def test_registry_concurrent_distinct_writes_are_not_lost(tmp_path: Path) -> None:
    def register(index: int) -> None:
        ArtifactRegistry(tmp_path).register(_artifact(str(index)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register, range(24)))
    assert len(ArtifactRegistry(tmp_path).list()) == 24


def test_empty_dependency_set_invalidates_previous_dependencies() -> None:
    artifact = _artifact("a", status="ready", source_dependencies=("sidechain:b",))
    assert freeze_staleness(artifact, current_sound_hash="sound", current_dependencies=())


def test_missing_listen_source_keeps_device_block_shape_and_advances_fade() -> None:
    service = MonitorService(48000)
    block = np.ones((2, 480), np.float32)
    service.set_state(MonitorSessionState(source_tap="pfl", selected_listen_ids=("missing",), muted=True))
    silent = service.render_block({"master": block})
    assert silent.shape == block.shape
    assert not silent.any()
    service.set_state(MonitorSessionState(source_tap="program", muted=True))
    assert not service.render_block({"master": block}).any()


def test_duplicate_listen_ids_do_not_double_gain() -> None:
    service = MonitorService(48000)
    service.set_state(MonitorSessionState(source_tap="pfl", selected_listen_ids=("a", "a")))
    out = service.render_block({"track:a:pre": np.ones((2, 64), np.float32)})
    np.testing.assert_array_equal(out, 1.0)


def test_registered_matrix_is_owned_by_service() -> None:
    matrix = np.full((2, 2), 0.5)
    service = MonitorService(48000, {"mono": matrix})
    matrix[:] = np.nan
    service.set_state(MonitorSessionState(mono=True, mono_matrix_id="mono"))
    assert np.isfinite(service.render_block({"master": np.ones((2, 64), np.float32)})).all()


@pytest.mark.parametrize("changes", [{"gain_db": float("nan")}, {"ramp_ms": float("inf")}, {"ramp_ms": -1.0}])
def test_invalid_monitor_parameters_are_rejected(changes: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        MonitorSessionState(**changes)


@pytest.mark.parametrize("signal", [np.ones(100), np.full(100, np.nan), np.full(100, np.inf)])
def test_reference_alignment_rejects_non_informative_audio(signal: np.ndarray) -> None:
    from src.features.edit.core.reference_mix import align_reference_offset
    with pytest.raises(ValueError):
        align_reference_offset(np.arange(100, dtype=float), signal)


def test_reference_alignment_handles_polarity_inverted_reference() -> None:
    from src.features.edit.core.reference_mix import align_reference_offset
    programme = np.random.default_rng(61).normal(size=4000)
    reference = np.r_[np.zeros(53), -programme]
    offset, confidence = align_reference_offset(programme, reference, max_offset_samples=100)
    assert offset == 53
    assert confidence > 0.99


def test_nonfinite_headroom_policy_is_rejected() -> None:
    from src.features.edit.core.reference_mix import comparison_gain
    with pytest.raises(ValueError):
        comparison_gain(-100.0, -10.0, max_boost_db=float("nan"))


def test_health_walk_budget_checked_inside_large_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import audio_health
    clock = iter([0.0, 0.4, 0.8, 1.2, 1.6])
    visited: list[str] = []
    monkeypatch.setattr(audio_health.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(audio_health.os, "walk", lambda path: [(path, [], [str(i) for i in range(10)])])
    monkeypatch.setattr(audio_health.time, "monotonic", lambda: next(clock))

    def file_size(path: str) -> int:
        visited.append(path)
        return 1024

    monkeypatch.setattr(audio_health.os.path, "getsize", file_size)
    assert audio_health.probe_dir_size_mb("cache", max_seconds=1.0) == pytest.approx(2 / 1024)
    assert len(visited) == 2


def test_mixed_monitor_layout_requires_explicit_conversion() -> None:
    service = MonitorService(48000)
    service.set_state(MonitorSessionState(source_tap="afl", selected_listen_ids=("a", "b")))
    with pytest.raises(ValueError, match="layout"):
        service.render_block({"track:a:post": np.ones((2, 64), np.float32),
                              "track:b:post": np.ones((6, 64), np.float32)})


def test_health_snapshot_is_unknown_when_mixer_is_busy() -> None:
    from src.features.edit.core.audio_playback import AudioMixer
    mixer = AudioMixer()
    mixer._mutex.lock()
    try:
        snapshot = mixer.health_snapshot()
        assert snapshot.sample_rate is None
        assert snapshot.deadline_miss_count is None
    finally:
        mixer._mutex.unlock()


def test_health_counts_actual_per_block_deadline_misses() -> None:
    from src.features.edit.core.audio_playback import AudioMixer
    mixer = AudioMixer()
    assert mixer.health_snapshot().deadline_miss_count is None
    mixer._render_times_ms.extend((1.0, 3.0, 2.0))
    mixer._render_deadlines_ms.extend((2.0, 2.0, 4.0))
    snapshot = mixer.health_snapshot({"capacity": 100, "filled": 25, "underruns": 2})
    assert snapshot.deadline_miss_count == 1
    assert snapshot.render_deadline_ms == 4.0
    assert snapshot.ring_fill_ratio == 0.25
    assert snapshot.underruns == 2


def test_ring_diagnostics_do_not_wait_for_producer_lock() -> None:
    from threading import Event, Thread
    from src.features.edit.core.realtime_playback.audio_ring_buffer import AudioRingBuffer
    ring = AudioRingBuffer()
    locked, release = Event(), Event()

    def producer() -> None:
        with ring._lock:
            locked.set()
            release.wait(5.0)

    thread = Thread(target=producer)
    thread.start()
    try:
        assert locked.wait(2.0)
        assert ring.stats(blocking=False) == {}
    finally:
        release.set()
        thread.join(2.0)
    assert ring.stats(blocking=False)["capacity"] == ring.capacity
