"""阶段 7 验收：失败策略（停止/保持上一帧/重捕获）与长遮挡后的重捕获。"""

import numpy as np
import pytest

from src.shared.tracking.tracker_2d import (
    Tracker2D,
    TrackerConfig,
    TrackDirection,
)
from src.shared.tracking.types import (
    FailurePolicy,
    FailureReason,
    TrackStatus,
    TrackingMode,
)

from tests.shared.tracking import tracker_support as ts

pytestmark = pytest.mark.skipif(
    not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用"
)


@pytest.fixture(scope="module")
def fixture_dir() -> str:
    return ts.fixture_dir()


def _track(fixture_dir, policy: FailurePolicy) -> dict:
    tracker = Tracker2D(TrackerConfig(failure_policy=policy))
    req = ts.build_request(
        fixture_dir, "occlusion", TrackingMode.TRANSLATION,
        TrackDirection.BACKWARD,
    )
    return tracker.track(req)


class TestRecaptureAfterLongOcclusion:
    """遮挡素材（帧 40-59 被遮挡）：后向跟踪越过遮挡后必须真正重捕获。"""

    @pytest.fixture(scope="class")
    @classmethod
    def recapture(cls, fixture_dir):
        result = _track(fixture_dir, FailurePolicy.RECAPTURE)
        manifest = ts.load_manifest(fixture_dir, "occlusion")
        return result, manifest

    def test_recovers_after_occlusion_gap(self, recapture):
        """遮挡结束（帧 39）后的可见帧应重新捕获并达到误差阈值。"""
        result, manifest = recapture
        ref_corners = ts.reference_corners(manifest)
        recovered = []
        for f in range(15, 39):
            t = result.transforms.get(f)
            if t is None or t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(
                manifest["frames"][f]["homography"], dtype=np.float64
            ).reshape(3, 3)
            recovered.append(ts.corner_error(t.matrix_np, H_gt, ref_corners))
        assert len(recovered) >= 15, "重捕获后有效帧过少"
        assert np.mean(recovered) < 2.0

    def test_occluded_frames_still_failed(self, recapture):
        """遮挡帧（目标不可见）不得借重捕获变成假有效。"""
        result, manifest = recapture
        occluded = [
            f for f, m in enumerate(manifest["frames"]) if m["occluded"]
        ]
        failed = sum(
            1 for f in occluded
            if f in result.transforms
            and result.transforms[f].status == TrackStatus.FAILED
        )
        assert failed >= len(occluded) * 0.9

    def test_no_fake_valid_anywhere(self, recapture):
        """所有非失败帧必须与真值一致（重捕获不引入假有效）。"""
        result, manifest = recapture
        ref_corners = ts.reference_corners(manifest)
        for f, t in result.transforms.items():
            if t.status == TrackStatus.FAILED:
                continue
            H_gt = np.array(
                manifest["frames"][f]["homography"], dtype=np.float64
            ).reshape(3, 3)
            err = ts.corner_error(t.matrix_np, H_gt, ref_corners)
            assert err < 3.0, f"帧 {f} 假有效（四角误差 {err:.1f}px）"

    def test_recapture_window_configurable(self, fixture_dir):
        """重捕获窗口配置化：窗口小于遮挡长度时不恢复（诚实失败）。"""
        tracker = Tracker2D(
            TrackerConfig(
                failure_policy=FailurePolicy.RECAPTURE, recapture_max_gap=3
            )
        )
        req = ts.build_request(
            fixture_dir, "occlusion", TrackingMode.TRANSLATION,
            TrackDirection.BACKWARD,
        )
        result = tracker.track(req)
        recovered = [
            f for f in range(0, 40)
            if f in result.transforms
            and result.transforms[f].status != TrackStatus.FAILED
        ]
        assert recovered == [], "窗口不足时不应重捕获"

    def test_default_policy_is_recapture(self):
        assert TrackerConfig().failure_policy == FailurePolicy.RECAPTURE


class TestStopPolicy:
    def test_stops_at_first_failure(self, fixture_dir):
        """停止策略：首次失败后终止，后续帧无结果（不假有效）。"""
        result = _track(fixture_dir, FailurePolicy.STOP)
        frames = sorted(result.transforms)
        assert frames, "至少应有参考帧到首次失败的结果"
        failed_frames = [
            f for f in frames
            if result.transforms[f].status == TrackStatus.FAILED
        ]
        assert failed_frames, "遮挡素材必须出现失败"
        # 后向跟踪：失败帧即已到达的最早帧（之后立即终止）
        assert min(frames) == min(failed_frames), "失败后不应继续产生结果"
        # 遮挡后段（帧 0-39）完全没有结果
        assert not any(f <= 39 for f in frames)

    def test_failure_still_has_reason(self, fixture_dir):
        result = _track(fixture_dir, FailurePolicy.STOP)
        for t in result.transforms.values():
            if t.status == TrackStatus.FAILED:
                assert t.failure_reason != FailureReason.NONE


class TestHoldLastPolicy:
    @pytest.fixture(scope="class")
    @classmethod
    def hold(cls, fixture_dir):
        return _track(fixture_dir, FailurePolicy.HOLD_LAST)

    def test_failed_frames_hold_last_valid_transform(self, hold):
        """保持上一帧：失败区间沿用最后有效变换，但标记弱跟踪。

        后向跟踪按帧号升序看：保持段之后（帧号更大）的最近 VALID 帧
        即时间上的最后一次有效帧。
        """
        frames = sorted(hold.transforms)
        for i, f in enumerate(frames):
            t = hold.transforms[f]
            if t.status != TrackStatus.WEAK:
                continue
            anchor = next(
                (hold.transforms[g] for g in frames[i + 1:]
                 if hold.transforms[g].status == TrackStatus.VALID),
                None,
            )
            assert anchor is not None, f"帧 {f} 保持帧缺少对应有效帧"
            assert np.allclose(t.matrix_np, anchor.matrix_np), (
                f"帧 {f} 保持的变换与最后有效帧不一致"
            )

    def test_held_frames_not_marked_valid(self, hold):
        """保持的帧不得标记为有效（诚实弱跟踪），并携带失败原因。"""
        held = [
            t for t in hold.transforms.values()
            if t.status == TrackStatus.WEAK
        ]
        assert held, "遮挡素材应出现保持帧"
        for t in held:
            assert t.failure_reason != FailureReason.NONE
            assert t.confidence < 0.60

    def test_held_confidence_decays_over_gap(self, hold):
        """保持置信度随与最后有效帧的时间距离增大而衰减。

        后向跟踪中帧 60 是最后有效帧：升序遍历保持段（40-59）时，
        越靠近帧 60 置信度越高，远离则越低。
        """
        occluded_confs = [
            hold.transforms[f].confidence for f in range(40, 60)
            if f in hold.transforms
            and hold.transforms[f].status == TrackStatus.WEAK
        ]
        assert len(occluded_confs) >= 10
        assert all(
            occluded_confs[i + 1] >= occluded_confs[i] - 1e-9
            for i in range(len(occluded_confs) - 1)
        ), f"保持置信度未按时间距离衰减: {occluded_confs}"
        # 距离最远的保持帧应明显衰减（远低于弱阈值）
        assert occluded_confs[0] < 0.05
        assert occluded_confs[0] < occluded_confs[-1]
