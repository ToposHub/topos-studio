"""M23.1（R08）· Tracker 手工修正/增量重跟领域服务回归。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.composite_core

from src.features.composite.core.tracker_corrections import (
    apply_track_update,
    get_manual_corrections,
    plan_incremental_retrack,
    set_manual_correction,
)


def _track_data():
    return {
        "version": 1,
        "media_path": "/m.mov",
        "width": 1920, "height": 1080,
        "reference_frame": 0,
        "mode": "translation",
        "transforms": {
            "0": {"tx": 0.0, "ty": 0.0, "status": "valid"},
            "1": {"tx": 1.0, "ty": 0.0, "status": "valid"},
            "2": {"tx": 2.0, "ty": 0.5, "status": "failed"},
            "3": {"tx": 0.0, "ty": 0.0, "status": "failed"},
            "4": {"tx": 0.0, "ty": 0.0, "status": "failed"},
            "5": {"tx": 5.0, "ty": 1.0, "status": "valid"},
            "9": {"tx": 9.0, "ty": 1.0, "status": "low_confidence"},
        },
    }


class TestManualCorrections:
    def test_set_and_read_correction(self):
        data = _track_data()
        out = set_manual_correction(data, 3, {"tx": 3.1, "ty": 0.2})
        # 入参不被修改（不可变更新语义）
        assert "manual_corrections" not in data or not data.get(
            "manual_corrections")
        corr = get_manual_corrections(out)
        assert corr["3"] == {"tx": 3.1, "ty": 0.2}
        # transforms 同帧标 manual（下游 bake 直接消费修正值）
        assert out["transforms"]["3"]["status"] == "manual"
        assert out["transforms"]["3"]["tx"] == pytest.approx(3.1)


class TestPlanIncrementalRetrack:
    def test_failed_segments_planned_with_anchors(self):
        data = _track_data()
        plan = plan_incremental_retrack(data)
        segs = plan.segments
        assert [(s.start_frame, s.end_frame) for s in segs] == [(2, 4), (9, 9)]
        # 段锚 = 段前最近 valid 帧
        assert segs[0].anchor_frame == 1
        assert segs[0].anchor_transform["tx"] == pytest.approx(1.0)
        assert segs[1].anchor_frame == 5

    def test_manual_frames_are_anchors_not_failures(self):
        data = set_manual_correction(_track_data(), 3, {"tx": 3.1, "ty": 0.2})
        plan = plan_incremental_retrack(data)
        spans = [(s.start_frame, s.end_frame) for s in plan.segments]
        # 帧 3 是 manual——失败段被它切成 (2,2) 和 (4,4)
        assert spans == [(2, 2), (4, 4), (9, 9)]
        assert plan.manual_corrections["3"]["tx"] == pytest.approx(3.1)

    def test_no_failures_empty_plan(self):
        data = _track_data()
        data["transforms"] = {
            "0": {"tx": 0, "ty": 0, "status": "valid"},
            "1": {"tx": 1, "ty": 0, "status": "valid"}}
        plan = plan_incremental_retrack(data)
        assert plan.segments == ()
        assert plan.total_frames == 0

    def test_empty_data_empty_plan(self):
        assert plan_incremental_retrack(None).segments == ()


class TestApplyTrackUpdate:
    def test_merge_respects_plan_range_and_manual(self):
        data = set_manual_correction(_track_data(), 3, {"tx": 3.1, "ty": 0.2})
        plan = plan_incremental_retrack(data)
        new = {
            2: {"tx": 2.5, "ty": 0.6},
            3: {"tx": 99.0, "ty": 99.0},   # manual 帧——不被覆盖
            4: {"tx": 4.1, "ty": 0.9},
            5: {"tx": -1.0, "ty": 0.0},    # 段外——不被覆盖
        }
        out = apply_track_update(data, new, plan)
        t = out["transforms"]
        assert t["2"]["tx"] == pytest.approx(2.5)
        assert t["2"]["status"] == "valid"
        assert t["3"]["status"] == "manual"      # 修正保留
        assert t["3"]["tx"] == pytest.approx(3.1)
        assert t["4"]["tx"] == pytest.approx(4.1)
        assert t["5"]["tx"] == pytest.approx(5.0)  # 段外不动
        # 手工修正表保留
        assert get_manual_corrections(out)["3"]["tx"] == pytest.approx(3.1)


class TestTrackerNodeDelegates:
    def test_node_roundtrip_manual_correction_and_retrack(self):
        from src.features.composite.nodes.tracker_node import TrackerNode
        node = TrackerNode("t1")
        node.set_track_data(_track_data())
        node.apply_manual_correction(3, {"tx": 3.1, "ty": 0.2})
        assert node.get_manual_corrections()["3"]["tx"] == pytest.approx(3.1)
        plan = node.plan_incremental_retrack()
        spans = [(s.start_frame, s.end_frame) for s in plan.segments]
        assert spans == [(2, 2), (4, 4), (9, 9)]  # manual 帧为锚切开段
        node.merge_retrack_results({
            2: {"tx": 2.5, "ty": 0.6},
            3: {"tx": 99.0, "ty": 0.0},  # 不覆盖修正
            9: {"tx": 9.5, "ty": 1.1},
        })
        data = node.get_track_data()
        assert data["transforms"]["2"]["tx"] == pytest.approx(2.5)
        assert data["transforms"]["3"]["tx"] == pytest.approx(3.1)
        assert data["transforms"]["9"]["tx"] == pytest.approx(9.5)
