"""阶段 2 验收：五因子置信度评分与诊断输出。"""

import numpy as np
import pytest

from src.shared.tracking.tracker_2d import Tracker2D, TrackerConfig
from src.shared.tracking.types import TrackingMode, TrackStatus

from tests.shared.tracking import tracker_support as ts

pytestmark = pytest.mark.skipif(
    not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用"
)


@pytest.fixture(scope="module")
def fixture_dir() -> str:
    return ts.fixture_dir()


@pytest.fixture(scope="module")
def translation_track(fixture_dir):
    tracker = Tracker2D()
    req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
    return tracker.track(req), req


class TestConfidenceDiagnostics:
    def test_diagnostics_present_for_every_frame(self, translation_track):
        result, req = translation_track
        for f in range(req.reference_frame + 1, req.frame_end + 1):
            assert f in result.diagnostics, f"帧 {f} 缺少诊断"
            d = result.diagnostics[f]
            assert d.frame == f
            assert d.point_count > 0
            assert 0.0 <= d.fb_pass_ratio <= 1.0
            assert 0.0 <= d.inlier_ratio <= 1.0
            assert d.reproj_error >= 0.0
            assert d.sharpness >= 0.0
            assert 0.0 <= d.confidence <= 1.0

    def test_status_matches_confidence_thresholds(self, translation_track):
        """VALID/WEAK/FAILED 必须与配置阈值一致。"""
        result, req = translation_track
        cfg = TrackerConfig()
        for f, t in result.transforms.items():
            if f == req.reference_frame:
                continue
            if t.status == TrackStatus.VALID:
                assert t.confidence >= cfg.valid_confidence
            elif t.status == TrackStatus.WEAK:
                assert cfg.weak_confidence <= t.confidence < cfg.valid_confidence
            elif t.status == TrackStatus.FAILED:
                assert t.confidence < cfg.valid_confidence

    def test_clean_fixture_high_confidence(self, translation_track):
        """干净素材上置信度应整体很高（五因子全满）。"""
        result, req = translation_track
        confs = [
            t.confidence for f, t in result.transforms.items()
            if f != req.reference_frame and t.status != TrackStatus.FAILED
        ]
        assert len(confs) > 0
        assert np.mean(confs) > 0.85

    def test_observations_exported_in_source_coordinates(self, translation_track):
        """观测点必须换算到原始分辨率（超出分析分辨率范围）。"""
        result, _ = translation_track
        all_obs = [o for obs in result.observations.values() for o in obs]
        assert len(all_obs) > 0
        xs = [o.x for o in all_obs]
        # 原始分辨率 1280 宽；分析分辨率约 540。观测最大值应显著超过分析宽度
        assert max(xs) > 600, "观测坐标疑似未换算到原始分辨率"

    def test_failed_frames_have_no_fake_confidence(self, fixture_dir):
        """遮挡帧的失败结果置信度必须为 0，不画假有效。"""
        tracker = Tracker2D()
        req = ts.build_request(fixture_dir, "occlusion", TrackingMode.TRANSLATION)
        result = tracker.track(req)
        failed = [
            t for t in result.transforms.values()
            if t.status == TrackStatus.FAILED
        ]
        assert len(failed) > 0
        for t in failed:
            assert t.confidence == 0.0
            assert np.allclose(t.matrix_np, np.eye(3))


class TestConfidenceWeights:
    def test_weights_are_configurable(self, fixture_dir):
        """改变权重配置应改变置信度（阈值配置化，无魔法数字）。"""
        cfg_default = TrackerConfig()
        # 只信任点数因子
        cfg_points_only = TrackerConfig(
            weight_points=1.0, weight_spread=0.0, weight_fb=0.0,
            weight_inlier=0.0, weight_error=0.0,
        )
        tracker_a = Tracker2D(cfg_default)
        tracker_b = Tracker2D(cfg_points_only)
        req = ts.build_request(fixture_dir, "trs", TrackingMode.TRANSLATION_ROTATION_SCALE)
        result_a = tracker_a.track(req)
        result_b = tracker_b.track(req)
        confs_a, confs_b = [], []
        for f in result_a.transforms:
            if f == req.reference_frame:
                continue
            ta, tb = result_a.transforms[f], result_b.transforms[f]
            if ta.status != TrackStatus.FAILED and tb.status != TrackStatus.FAILED:
                confs_a.append(ta.confidence)
                confs_b.append(tb.confidence)
        assert confs_a and confs_b
        # 不同权重组合下置信度序列不应完全相同
        assert not np.allclose(confs_a, confs_b), "权重配置未生效"

    def test_zero_weights_yields_zero_confidence(self, fixture_dir):
        """所有权重为 0 时置信度归一化结果为 0（除零保护）。"""
        cfg = TrackerConfig(
            weight_points=0.0, weight_spread=0.0, weight_fb=0.0,
            weight_inlier=0.0, weight_error=0.0,
        )
        tracker = Tracker2D(cfg)
        req = ts.build_request(fixture_dir, "translation", TrackingMode.TRANSLATION)
        result = tracker.track(req)
        for f, t in result.transforms.items():
            if f == req.reference_frame:
                continue
            assert t.confidence == 0.0
