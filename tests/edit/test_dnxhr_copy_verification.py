"""A DNxHR packet copy counts as a delivery only once its packets are proven (plan 3.4).

An exact cut of a DNxHR HQX master is delivered by copying the master's
compressed packets.  That is only a delivery if the packets in the file are
the master's: ``verify_dnxhr_copy_runs`` re-reads the first and last packet
of every run in the master and in the delivery and compares SHA-256 digests.
A copy that does not verify is discarded and the range is rendered again on
the regular path, so a file that cannot be proven is never left as a
delivery.

The queue-wait telemetry of the render pipeline (plan 3.4) is checked on the
regular path: one ``queue_wait`` sample per frame the consumer was held up,
never one per frame that went straight through.
"""

from __future__ import annotations

import time
from fractions import Fraction
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.edit.core import timeline_export as export_module
from src.features.edit.core.dnxhr_packet_copy import copy_dnxhr_hqx_mov
from src.features.edit.core.timeline_export import (
    ExportCodec,
    ExportRangeType,
    TimelineExportConfig,
    verify_dnxhr_copy_runs,
)

MASTER = Path(__file__).resolve().parents[1] / "test_videos" / "4K_DNxHR HQX 10bit.mov"
FPS = Fraction(25, 1)
SIZE = (3840, 2160)


@pytest.fixture()
def master() -> Path:
    pytest.importorskip("av")
    if not MASTER.is_file():
        pytest.skip("the 4K DNxHR HQX master is unavailable")
    return MASTER


def _copy(source: Path, target: Path, runs, fps: Fraction = FPS) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    assert copy_dnxhr_hqx_mov(
        str(source), str(target), runs, width=SIZE[0], height=SIZE[1], fps=fps,
        continue_export=lambda: True, progress=lambda _done: None)
    return target


class TestVerification:
    def test_a_real_copy_verifies_with_the_probe_count(self, master, tmp_path) -> None:
        delivery = _copy(master, tmp_path / "cut.mov", [(25, 1), (275, 1)])
        assert verify_dnxhr_copy_runs(str(master), str(delivery), [(25, 1), (275, 1)], fps=FPS) == (
            True, "sha256:2-of-2-packets")

    def test_a_long_run_is_probed_at_both_ends(self, master, tmp_path) -> None:
        delivery = _copy(master, tmp_path / "run.mov", [(25, 40)])
        assert verify_dnxhr_copy_runs(str(master), str(delivery), [(25, 40)], fps=FPS) == (
            True, "sha256:2-of-40-packets")
        assert verify_dnxhr_copy_runs(
            str(master), str(delivery), [(25, 20), (100, 20)], fps=FPS)[0] is False   # other runs

    def test_a_delivery_of_another_cut_does_not_verify(self, master, tmp_path) -> None:
        delivery = _copy(master, tmp_path / "a.mov", [(25, 10)])
        verified, evidence = verify_dnxhr_copy_runs(str(master), str(delivery), [(30, 10)], fps=FPS)
        # Same count, other packets: the first packet already tells.
        assert verified is False and evidence == "packet_hash_mismatch:30"

    def test_a_delivery_with_the_wrong_number_of_packets_does_not_verify(self, master, tmp_path) -> None:
        delivery = _copy(master, tmp_path / "short.mov", [(25, 5)])
        verified, evidence = verify_dnxhr_copy_runs(str(master), str(delivery), [(25, 9)], fps=FPS)
        assert verified is False and evidence == "delivery_packet_count:5!=9"

    def test_nothing_to_probe(self, master, tmp_path) -> None:
        delivery = _copy(master, tmp_path / "one.mov", [(25, 1)])
        assert verify_dnxhr_copy_runs(str(master), str(delivery), [], fps=FPS) == (False, "no_runs")

    def test_a_frame_rate_that_does_not_match_the_master_is_refused(self, master, tmp_path) -> None:
        delivery = _copy(master, tmp_path / "r.mov", [(25, 4)])
        verified, evidence = verify_dnxhr_copy_runs(
            str(master), str(delivery), [(25, 4)], fps=Fraction(30000, 1001))
        assert verified is False and evidence == "probe_time_base_mismatch"

    def test_a_missing_file_is_an_error_for_the_caller(self, master, tmp_path) -> None:
        with pytest.raises(Exception):                      # av raises its own error type
            verify_dnxhr_copy_runs(str(master), str(tmp_path / "nope.mov"), [(25, 1)], fps=FPS)


# ---------------------------------------------------------------------------
# Through the exporter
# ---------------------------------------------------------------------------

FRAMES = (25, 33)                  # eight frames


def _timeline(master: Path):
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef, probe_media_metadata
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence

    metadata = probe_media_metadata(str(master))
    assert metadata
    project = EditProject.create("DNxHR cut").with_media_asset_added(MediaAsset(
        asset_id="master", path=str(master), media_type="video", display_name=master.name,
        metadata=metadata))
    clip = ClipInstance(clip_id="v1", asset_id="master", track_id="v1", timeline_start=0,
                        timeline_duration=FRAMES[1], source_start=0, source_duration=FRAMES[1])
    sequence = TimelineSequence(
        timeline_id="cut", name="Cut", fps=Rational(25, 1), width=SIZE[0], height=SIZE[1],
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip,)),))
    project = project.with_timeline_ref_added(
        TimelineSequenceRef(timeline_id="cut", name="Cut", fps=25, width=SIZE[0], height=SIZE[1],
                            created_at=time.time()), set_active=True)
    return project, sequence


def _export(project, sequence, path: Path):
    """One export the way the Deliver page runs it, and the exporter's telemetry."""
    from src.features.deliver.ui.deliver_page import ExportWorker
    from src.features.edit.core.timeline_export import create_timeline_exporter

    config = TimelineExportConfig(
        timeline_id=sequence.timeline_id, output_path=str(path), codec=ExportCodec.DNXHR_HQX,
        range_type=ExportRangeType.CUSTOM, start_frame=FRAMES[0], end_frame=FRAMES[1],
        include_audio=False)
    exporter = create_timeline_exporter(project, sequence, progress_callback=lambda *_args: None)
    worker = ExportWorker(exporter, config)
    worker.start()
    assert worker.wait(300_000), "the export worker did not finish"
    return exporter.export_stage_telemetry_snapshot


def _delivered(path: Path) -> int:
    import av

    with av.open(str(path)) as container:
        video = container.streams.video[0]
        assert (video.codec_context.width, video.codec_context.height) == SIZE
        assert video.codec_context.format.name == "yuv422p10le"
        return sum(1 for packet in container.demux(video) if packet.size)


def _stage(snapshot: dict, name: str) -> dict:
    return next(stage for stage in snapshot["stages"] if stage["name"] == name)


@pytest.mark.real_media
def test_a_verified_packet_copy_is_the_delivery(master, tmp_path) -> None:
    project, sequence = _timeline(master)
    path = tmp_path / "out.mov"
    snapshot = _export(project, sequence, path)
    assert snapshot["delivery_path"] == "dnxhr_packet_copy" and snapshot["fallback_root_cause"] is None
    assert _delivered(path) == FRAMES[1] - FRAMES[0]
    # Nothing was decoded or rendered: the master's packets were copied.
    assert _stage(snapshot, "decode")["sample_count"] == 0
    assert _stage(snapshot, "render")["sample_count"] == 0


@pytest.mark.real_media
def test_an_unverified_copy_is_discarded_and_the_range_is_rendered_again(master, tmp_path, monkeypatch) -> None:
    project, sequence = _timeline(master)
    path = tmp_path / "out.mov"
    monkeypatch.setattr(export_module, "verify_dnxhr_copy_runs",
                        lambda *_args, **_kwargs: (False, "forced"))
    snapshot = _export(project, sequence, path)

    # The file is complete, and it is the regular render's, not the copy's.
    assert _delivered(path) == FRAMES[1] - FRAMES[0]
    assert snapshot["delivery_path"] != "dnxhr_packet_copy"
    assert snapshot["fallback_root_cause"] == "dnxhr_packet_copy_unverified:forced"
    assert _stage(snapshot, "render")["sample_count"] == FRAMES[1] - FRAMES[0]

    # Queue waits: one sample per frame the consumer was held up, each at
    # least one poll; never a sample for a frame that went straight through.
    waits = _stage(snapshot, "queue_wait")
    assert waits["backend"].endswith("_pipeline_consumer_empty")
    assert 0 < waits["sample_count"] <= FRAMES[1] - FRAMES[0]
    assert waits["wall_p50_ms"] >= 100.0


@pytest.mark.real_media
def test_a_verifier_that_raises_fails_closed(master, tmp_path, monkeypatch) -> None:
    project, sequence = _timeline(master)
    path = tmp_path / "out.mov"

    def explode(*_args, **_kwargs):
        raise OSError("master became unreadable")

    monkeypatch.setattr(export_module, "verify_dnxhr_copy_runs", explode)
    snapshot = _export(project, sequence, path)
    assert _delivered(path) == FRAMES[1] - FRAMES[0]
    assert snapshot["fallback_root_cause"] == "dnxhr_packet_copy_unverified:verify_error:OSError"


@pytest.mark.real_media
def test_metal_hqx_render_failure_uses_existing_opengl_delivery(
    master: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, sequence = _timeline(master)
    path = tmp_path / "out.mov"
    monkeypatch.setenv("TOPOS_BACKEND_GRAPHICS", "metal")
    monkeypatch.setenv("TOPOS_EXPORT_GPU_RENDER", "1")
    monkeypatch.setattr(export_module.TimelineExporter, "_try_copy_dnxhr_hqx", lambda *_args: None)
    monkeypatch.setattr(export_module.TimelineExporter, "_try_render_planar_passthrough", lambda *_args: None)

    def render_failure(*_args: object) -> None:
        raise RuntimeError("simulated Metal device failure")

    monkeypatch.setattr(export_module.TimelineExporter, "_render_metal_dnxhr_hqx", render_failure)
    snapshot = _export(project, sequence, path)
    assert _delivered(path) == FRAMES[1] - FRAMES[0]
    assert snapshot["fallback_root_cause"] == "metal_dnxhr_hqx_render_failed:RuntimeError"
    assert _stage(snapshot, "render")["backend"] == "opengl"
