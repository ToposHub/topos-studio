"""Focused regressions found while auditing the media-pool backend handoff."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.features.edit.core.media_management_worker import (
    KIND_CONSOLIDATE, MediaManagementWorker, estimate_collect_bytes,
)
from src.features.edit.core.consolidate import ConsolidateProgress
from src.features.edit.core.project import EditProject


def test_collect_used_only_estimate_keeps_supplied_sequences(tmp_path: Any) -> None:
    used = tmp_path / "used.mov"
    unused = tmp_path / "unused.mov"
    used.write_bytes(b"u" * 37)
    unused.write_bytes(b"x" * 91)
    project = EditProject.create("Used only")
    project = project.import_asset(str(used), "video", {})
    project = project.import_asset(str(unused), "video", {})
    asset_id = project.get_asset_by_path(str(used)).asset_id
    clip = SimpleNamespace(asset_id=asset_id, clip_id="c", timeline_start=0, timeline_duration=1)
    track = SimpleNamespace(track_id="v", name="V", track_type="video", clips=(clip,))
    sequence = SimpleNamespace(timeline_id="t", name="Timeline", all_tracks=(track,))
    estimate = estimate_collect_bytes(project, include_unused=False, sequences=iter([sequence]))
    # R04：估算含 LUT/清单余量（_SIDECAR_SLACK_BYTES），但"只统计已用
    # 素材"的语义不变——未用素材 91 字节不进入估算。
    from src.features.edit.core.media_management_worker import _SIDECAR_SLACK_BYTES
    assert estimate == 37 + _SIDECAR_SLACK_BYTES


def test_consolidate_worker_forwards_actual_progress_fields(qapp: Any) -> None:
    def work(progress_cb: Any, cancel_check: Any) -> tuple[None, None]:
        progress_cb(ConsolidateProgress("segment", "a", "clip", 2, 4, 1, 4))
        progress_cb(ConsolidateProgress("done", "", "", 4, 4, 4, 4))
        return None, None

    worker = MediaManagementWorker(KIND_CONSOLIDATE, work)
    events = []
    worker.signals.progress.connect(lambda kind, done, total: events.append((kind, done, total)))
    worker.run()
    assert events == [(KIND_CONSOLIDATE, 1, 4), (KIND_CONSOLIDATE, 4, 4)]


def test_metadata_csv_invalid_headers_report_warnings_instead_of_crashing() -> None:
    from src.features.edit.core.metadata_interchange import plan_metadata_import
    project = EditProject.create("CSV headers")
    for text in ("asset_id,unknown\na,value\n", "rating\n3\n"):
        plan = plan_metadata_import(project, text)
        assert isinstance(plan.warnings, tuple)
        assert plan.warnings
        assert not plan.asset_changes


def test_multicam_long_recording_sampling_reads_canonical_duration(tmp_path: Any, qapp: Any, monkeypatch: Any) -> None:
    from unittest.mock import MagicMock
    from PyQt6.QtCore import QObject, pyqtSignal
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.features.edit.core.multicam import build_multicam_group_from_assets, stamp_multicam_group_on_assets
    from src.features.edit.core.project import MediaAsset
    import src.features.edit.core.multicam_sync as sync_module

    project = EditProject.create("Long sync")
    assets = []
    for identity, duration in (("a", 600.0), ("b", 720.0)):
        path = tmp_path / f"{identity}.mp4"
        path.write_bytes(b"test")
        assets.append(MediaAsset(identity, str(path), "video", identity,
                                 {"fps": 25.0, "duration_seconds": duration, "total_frames": int(duration * 25)}))
    group = build_multicam_group_from_assets([a.asset_id for a in assets])
    for asset in stamp_multicam_group_on_assets(assets, group):
        project = project.with_media_asset_added(asset)
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller._current_edit_project = project
    controller._multicam_clip_at_playhead = lambda: SimpleNamespace(asset_id="a")
    controller.edit_page = SimpleNamespace(_fps=25.0)
    controller.status_bar = MagicMock()
    controller.multicam_panel = MagicMock()
    starts = []

    class Worker(QObject):
        sync_finished = pyqtSignal(dict)
        sync_failed = pyqtSignal(str)
        finished = pyqtSignal()

        def __init__(self, jobs: Any, reference_asset_id: str, *, segment_starts: Any) -> None:
            super().__init__()
            starts.append(segment_starts)

        def start(self) -> None:
            pass

    monkeypatch.setattr(sync_module, "MulticamSyncWorker", Worker)
    controller._on_multicam_sync_requested("a")
    assert starts and len(starts[0]) == 3
    assert starts[0][0] == 0.0
    assert starts[0][-1] == 600.0 - controller._SYNC_SEGMENT_SECONDS


def test_source_range_preserves_individually_set_endpoints() -> None:
    from src.features.edit.core.asset_source_range import get_insert_range, set_asset_in_out
    project = EditProject.create("Ranges").import_asset("/tmp/review-range.mov", "video", {"total_frames": 100, "fps": 25.0})
    identity = next(iter(project.assets))
    for start, end, expected in ((10, None, (10, 99)), (None, 49, (0, 49))):
        updated = set_asset_in_out(project, identity, start, end)
        assert get_insert_range(updated.assets[identity]) == expected


def test_marker_creation_rejects_coercible_noninteger_frames() -> None:
    import pytest
    from src.features.edit.core.asset_source_range import AssetMarkerValidationError, make_asset_marker
    for frame in (True, 1.8, "3"):
        with pytest.raises(AssetMarkerValidationError):
            make_asset_marker(frame)
    for color in ("#gggggg", "#zz0000"):
        with pytest.raises(AssetMarkerValidationError):
            make_asset_marker(1, color=color)


def test_keyword_in_rule_matches_any_asset_keyword_case_insensitively() -> None:
    from dataclasses import replace
    from src.features.edit.core.smart_bin_rules import matches_rule_tree, validate_rule_tree
    project = EditProject.create("Keywords").import_asset("/tmp/review-keywords.mov", "video", {})
    asset = replace(next(iter(project.assets.values())), keywords=("Night",))
    rule = {"field": "keywords", "cmp": "in", "value": ["night", "Hero"]}
    validate_rule_tree(rule)
    assert matches_rule_tree(asset, rule)
    assert not matches_rule_tree(replace(asset, keywords=("Day",)), rule)


def test_numeric_rules_reject_invalid_values_and_reversed_bounds() -> None:
    import pytest
    from src.features.edit.core.smart_bin_rules import SmartBinRuleError, validate_rule_tree
    for comparator, value in ((">=", "bad"), ("==", True), (">", float("nan")),
                              ("<=", float("inf")),
                              ("between", [5, 2]), ("between", [0, float("nan")])):
        with pytest.raises(SmartBinRuleError):
            validate_rule_tree({"field": "width", "cmp": comparator, "value": value})
    validate_rule_tree({"field": "width", "cmp": "between", "value": [0, 5]})


def test_transcription_rejects_nonfinite_times_and_malformed_entries() -> None:
    import pytest
    from src.features.edit.ai_editing.transcription_job import (
        TranscriptionJobError, TranscriptionJobRequest, TranscriptionSegment, segments_from_dicts,
    )
    for start, end in ((float("nan"), 2), (0, float("inf")), (False, 1)):
        with pytest.raises(TranscriptionJobError):
            TranscriptionSegment(start, end, "invalid")
    for fps in (float("nan"), float("inf"), True):
        with pytest.raises(TranscriptionJobError):
            TranscriptionJobRequest("a", "p", 0, (1, 2), time_range_frames=(0, 10), fps=fps)
    with pytest.raises(TranscriptionJobError):
        segments_from_dicts([None, {"start_seconds": 0, "end_seconds": 1, "text": "valid"}])


def test_cancelling_one_pending_asset_does_not_finish_multiasset_request() -> None:
    from src.features.edit.ai_editing.analysis_service import AnalysisService
    from src.features.edit.ai_editing.analysis_request import AnalysisRequest, AnalysisRecipe, RequestState
    from src.features.edit.ai_editing.analysis_scheduler import ResourceLane
    from src.features.edit.ai_editing.jobs import JobKind

    class Scheduler:
        def __init__(self) -> None:
            self.items = []

        def submit(self, lane: Any, job: Any, **kwargs: Any) -> None:
            self.items.append((job, kwargs))

        def notify_finished(self, job_id: str) -> None:
            pass

    service = AnalysisService()
    request = AnalysisRequest.create(project_id="p", asset_ids=("a", "b"), recipe_id=AnalysisRecipe.FAST_LOCAL).with_state(RequestState.RUNNING)
    service._requests[request.request_id] = request
    scheduler = Scheduler()
    completed = []
    service._execute_via_scheduler(request.request_id, request, scheduler=scheduler,
        lane=ResourceLane.ANALYSIS_LOCAL, fps="25/1", provider_ids=(), on_asset_done=None,
        on_request_done=lambda *args: completed.append(args))
    scheduler.items[0][1]["on_cancel"]()
    assert service.get_request(request.request_id).state == RequestState.RUNNING
    assert completed == []

    def successful_analysis(*args: Any, **kwargs: Any) -> tuple[Any, str]:
        job = service.jobs.submit(JobKind.ANALYSIS, label="Test provider")
        job.mark_running()
        job.complete({})
        return job, ""

    service.request_analysis = successful_analysis
    scheduler.items[1][1]["work"]()
    assert len(completed) == 1
    # R06：取消与失败三分——1 取消 + 1 成功、无失败 → 请求完成
    #（被取消的 asset 不再计入 errors 把整体拖成 FAILED）。
    assert service.get_request(request.request_id).state == RequestState.COMPLETED
