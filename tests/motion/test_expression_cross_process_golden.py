from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from scripts.benchmark_motion_graphic_expressions import expression_stats, render_hashes


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark_motion_graphic_expressions.py"


def test_expression_golden_is_stable_for_shuffled_cross_process_frames() -> None:
    local_frames = (12, 0, 23, 3, 7, 1)
    local = render_hashes(local_frames)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--frames", "0,1,3,7,12,23"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    child = json.loads(completed.stdout)
    assert child["scene_id"] == "expression-cross-process-v1"
    assert local == child["frame_hashes"]
    assert local == {
        "0": "a882d67385879372dc1ef9746a9811e55d2e72e64a26c8d2239117c9025db465",
        "1": "02b9d64ee5f6d4218e825f175fd101cb706ee1493a01225c57cbd4da6d8bd2a5",
        "3": "20c12609e86ab0b5de33ddc5b47c69e71380e33c399619eb46b1ee0c0f53a8c8",
        "7": "363aebcca8f532ba76ec616ea28db1e7fcc027de62c9bf9e3bb77d76021dfda8",
        "12": "2652dd07cb3c256f15a97e68384ad79d5a3272f21955c40622bcb91c28cf29c6",
        "23": "750a275007f7afad5d4324f1545a574ec7808408de2ba60f71ecdc01c00ecb62",
    }


def test_expression_single_frame_stats_report_budget_rejection() -> None:
    stats = expression_stats(7)
    assert stats[0]["within_budget"] is True
    assert stats[0]["elapsed_ms"] >= 0.0
    assert stats[1]["within_budget"] is False
    assert stats[1]["budget_exceeded"] is True
    assert stats[1]["node_count"] > 200
