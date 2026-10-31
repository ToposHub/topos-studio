"""阶段 7 验收：特征点锁定/删除/重新检测与点排名。"""

import os

import pytest

from src.shared.tracking.point_manager import (
    RankedPoint,
    active_observations,
    rank_observations,
    redetect_reference_points,
)
from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.types import FeatureObservation, TrackStatus

from tests.shared.tracking import tracker_support as ts


def obs(fid: int, x: float = 100.0, y: float = 100.0, error: float = 0.5,
        confidence: float = 0.9, status: TrackStatus = TrackStatus.VALID):
    return FeatureObservation(
        frame=10, feature_id=fid, x=x, y=y, error=error,
        confidence=confidence, status=status,
    )


def make_binding() -> TrackingBinding:
    return TrackingBinding(
        binding_id="b1", clip_id="c1", shape_id="s1", reference_frame=62,
    )


class TestPointRanking:
    def test_inliers_rank_above_outliers(self):
        """内点状态权重最高：弱状态点即使误差更小也排在内点之后。"""
        points = [
            obs(1, error=0.1, confidence=0.95, status=TrackStatus.WEAK),
            obs(2, error=1.0, confidence=0.7, status=TrackStatus.VALID),
        ]
        ranked = rank_observations(points)
        assert ranked[0].feature_id == 2
        assert ranked[0].is_inlier

    def test_low_error_ranks_higher(self):
        points = [obs(1, error=3.0), obs(2, error=0.2)]
        ranked = rank_observations(points)
        assert [p.feature_id for p in ranked] == [2, 1]

    def test_position_spread_contributes(self):
        """靠外的点（几何条件更好）排名更高。"""
        points = [
            obs(1, x=100.0, y=100.0, error=0.5),   # 靠近质心
            obs(2, x=110.0, y=100.0, error=0.5),   # 靠近质心
            obs(3, x=500.0, y=100.0, error=0.5),   # 靠外
        ]
        ranked = rank_observations(points)
        assert ranked[0].feature_id == 3

    def test_empty_input(self):
        assert rank_observations([]) == []

    def test_ranked_point_fields(self):
        ranked = rank_observations([obs(7, x=50.0, y=60.0)])
        assert isinstance(ranked[0], RankedPoint)
        assert ranked[0].feature_id == 7
        assert ranked[0].x == 50.0
        assert 0.0 <= ranked[0].score <= 1.0


class TestPointLockAndExclude:
    def test_exclude_removes_from_active(self):
        binding = make_binding()
        binding.exclude_feature(2)
        points = [obs(1), obs(2), obs(3)]
        active = active_observations(points, binding)
        assert [o.feature_id for o in active] == [1, 3]
        assert binding.is_feature_excluded(2)

    def test_locked_point_cannot_be_excluded(self):
        binding = make_binding()
        binding.lock_feature(2)
        binding.exclude_feature(2)   # 锁定优先：排除无效
        assert not binding.is_feature_excluded(2)
        active = active_observations([obs(1), obs(2)], binding)
        assert [o.feature_id for o in active] == [1, 2]

    def test_lock_overrides_existing_exclusion(self):
        binding = make_binding()
        binding.exclude_feature(2)
        binding.lock_feature(2)
        assert binding.is_feature_locked(2)
        assert not binding.is_feature_excluded(2)

    def test_unlock_and_restore(self):
        binding = make_binding()
        binding.lock_feature(1)
        binding.unlock_feature(1)
        assert not binding.is_feature_locked(1)
        binding.exclude_feature(2)
        binding.restore_feature(2)
        assert not binding.is_feature_excluded(2)

    def test_version_bumps_on_change(self):
        binding = make_binding()
        v0 = binding.result_version
        binding.lock_feature(1)
        binding.exclude_feature(2)
        assert binding.result_version == v0 + 2

    def test_serialization_roundtrip(self):
        binding = make_binding()
        binding.lock_feature(3)
        binding.exclude_feature(5)
        restored = TrackingBinding.from_dict(binding.to_dict())
        assert restored.locked_feature_ids == {3}
        assert restored.excluded_feature_ids == {5}

    def test_old_schema_without_point_sets_loads(self):
        """旧工程（无锁定/排除键）正常加载为空集合。"""
        data = make_binding().to_dict()
        data.pop("locked_feature_ids")
        data.pop("excluded_feature_ids")
        restored = TrackingBinding.from_dict(data)
        assert restored.locked_feature_ids == set()
        assert restored.excluded_feature_ids == set()


@pytest.mark.skipif(not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用")
class TestRedetectReferencePoints:
    def test_redetect_writes_reference_observations(self):
        fixture_dir = ts.fixture_dir()
        manifest = ts.load_manifest(fixture_dir, "translation")
        W, H = manifest["width"], manifest["height"]
        px, py, pw, ph = manifest["pattern_rect_reference"]
        binding = TrackingBinding(
            binding_id="b1", clip_id="c1", shape_id="s1",
            reference_frame=manifest["reference_frame"],
            pattern_region=(px / W, py / H, pw / W, ph / H),
        )
        count = redetect_reference_points(
            binding, os.path.join(fixture_dir, "translation.mp4")
        )
        assert count > 0
        obs_at_ref = binding.observations[binding.reference_frame]
        assert len(obs_at_ref) == count
        # 观测为原始分辨率坐标且落在 pattern 区域内
        for o in obs_at_ref:
            assert o.frame == binding.reference_frame
            assert px - 1 <= o.x <= px + pw + 1
            assert py - 1 <= o.y <= py + ph + 1
            assert o.status == TrackStatus.VALID

    def test_redetect_does_not_touch_lock_sets(self):
        fixture_dir = ts.fixture_dir()
        manifest = ts.load_manifest(fixture_dir, "translation")
        W, H = manifest["width"], manifest["height"]
        px, py, pw, ph = manifest["pattern_rect_reference"]
        binding = TrackingBinding(
            binding_id="b1", clip_id="c1", shape_id="s1",
            reference_frame=manifest["reference_frame"],
            pattern_region=(px / W, py / H, pw / W, ph / H),
        )
        binding.lock_feature(0)
        binding.exclude_feature(1)
        redetect_reference_points(
            binding, os.path.join(fixture_dir, "translation.mp4")
        )
        assert binding.locked_feature_ids == {0}
        assert binding.excluded_feature_ids == {1}
