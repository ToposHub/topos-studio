"""Read-only helper probes for the 2026-10-25 plan audit.

Run with the project Python from any cwd. All copied media stays in a temporary
directory; no project files, source media, GPU objects, or GUI state are changed.
An exit code of zero means the probes ran; inspect each issue_present value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np

from src.features.color.ui.shot_match_panel import controller_measure_fn
from src.features.edit.core.color_chain import chain_extra_params, extras_active
from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin
from src.features.edit.core.project_archive import collect_project
from src.features.edit.core.project_snapshots import take_snapshot
from src.features.edit.core.snapshot_backup_scheduler import SnapshotBackupScheduler
from src.shared.project.color_stack import ColorStackData


def empty_project() -> EditProject:
    return EditProject(
        project_id="audit-probe", name="Read-only audit",
        assets={}, bins={"root": ProjectBin(
            bin_id="root", name="Root", parent_id=None, asset_ids=())},
        root_bin_id="root")


def run_probes() -> dict[str, Any]:
    project = empty_project()
    scheduler = SnapshotBackupScheduler()
    reported_success = scheduler.tick(project, now=1000.0)
    backup_probe = {
        "issue_present": reported_success and not project.project_snapshots,
        "reported_success": reported_success,
        "reported_backup_count": scheduler.backups_taken,
        "persisted_snapshot_names": sorted(project.project_snapshots),
        "result_exported_by_tick": isinstance(reported_success, EditProject),
    }

    representative_frames = {"a": 12, "b": 40}
    requested_clips: list[str] = []

    def clip_frame(clip_id: str) -> int:
        requested_clips.append(clip_id)
        return representative_frames[clip_id]

    cached_image = np.full((2, 2, 3), 0.5, np.float32)
    measure = controller_measure_fn(
        SimpleNamespace(render_cached_image=lambda: cached_image), clip_frame)
    first, second = measure("a"), measure("b")
    same_cached_frame = np.array_equal(first, second)
    shot_probe = {
        "issue_present": same_cached_frame,
        "requested_clips": requested_clips,
        "representative_frames": representative_frames,
        "different_clips_return_same_cached_pixels": same_cached_frame,
        "returned_current_cache_object": first is cached_image
        and second is cached_image,
    }

    stack = ColorStackData(stack_id="trim", layers=({
        "active_lut_path": "/audit-missing.cube",
        "curves_data": {"master": [[0, 0], [1, 0.5]]},
    },))
    extras = chain_extra_params(
        SimpleNamespace(color_stacks={"trim": stack}, color_groups={}),
        "tl", SimpleNamespace(clip_id="c", color_trim_stack_id="trim"))
    advanced_marked = any(e.get("_unsupported_advanced") for e in extras)
    layer_probe = {
        "issue_present": not advanced_marked and not extras_active(extras),
        "extra_params": extras,
        "advanced_marked": advanced_marked,
        "extras_active": extras_active(extras),
    }

    with TemporaryDirectory(prefix="topos-audit-probe-") as temporary_dir:
        root = Path(temporary_dir)
        source = root / "retained.wav"
        source.write_bytes(b"retained snapshot source")
        project = EditProject(
            project_id="audit-probe", name="Retained source audit",
            assets={"a": MediaAsset("a", str(source), "audio", "a", {})},
            bins={"root": ProjectBin(
                bin_id="root", name="Root", parent_id=None, asset_ids=("a",))},
            root_bin_id="root")
        project, _meta = take_snapshot(project, "retained")
        _archived, report = collect_project(
            project, target_dir=str(root / "archive"),
            sequences=[SimpleNamespace(timeline_id="tl", all_tracks=())],
            include_unused=False)
        archive_probe = {
            "issue_present": "a" in report.skipped_unused,
            "snapshot_names": sorted(project.project_snapshots),
            "skipped_asset_ids": list(report.skipped_unused),
            "copied_asset_ids": [r.asset_id for r in report.copied],
        }

    return {
        "schema": "topos.upgrade_audit.mac_logic_probes.v1",
        "scope": "helper probes; no GUI/GPU or platform acceptance",
        "probes": {
            "snapshot_scheduler_discards_result": backup_probe,
            "shot_measure_reuses_current_cache": shot_probe,
            "layer_advanced_chain_unmarked": layer_probe,
            "snapshot_retained_source_not_archived": archive_probe,
        },
    }


if __name__ == "__main__":
    print(json.dumps(run_probes(), ensure_ascii=False, indent=2))
