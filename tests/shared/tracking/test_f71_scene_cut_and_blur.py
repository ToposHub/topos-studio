"""F7.1 · 镜头切换与独立运动模糊镜头——失败不污染验收。

新增镜头集（fixture 生成器扩展）：
- scene_cut：f=70 画面跳变为完全不同场景（卡片消失）——切换后跟踪
  必须 FAILED 且带原因，**不污染后续帧**（不产生假 VALID）；
- motion_blur：f∈[40,85] 高速平移 + 方向性模糊（核随速度 45px）——
  清晰段应正常跟踪，模糊段允许 WEAK/FAILED 但不得假高置信。
"""

from __future__ import annotations

import pytest

from tests.shared.tracking import tracker_support as ts
from src.shared.tracking.tracker_2d import TrackDirection
from src.shared.tracking.types import (
    FailureReason,
    TrackStatus,
    TrackingMode,
)
import numpy as np

pytestmark = pytest.mark.skipif(
    not ts.TOOLS_AVAILABLE, reason="tracking fixture 工具不可用"
)


@pytest.fixture(scope="module")
def fixture_dir() -> str:
    return ts.fixture_dir()


@pytest.fixture(scope="module")
def scene_cut_results(fixture_dir):
    tracker = ts.Tracker2D()
    forward = tracker.track(
        ts.build_request(fixture_dir, "scene_cut",
                         TrackingMode.TRANSLATION, TrackDirection.FORWARD))
    backward = tracker.track(
        ts.build_request(fixture_dir, "scene_cut",
                         TrackingMode.TRANSLATION, TrackDirection.BACKWARD))
    manifest = ts.load_manifest(fixture_dir, "scene_cut")
    return forward, backward, manifest


@pytest.fixture(scope="module")
def blur_results(fixture_dir):
    tracker = ts.Tracker2D()
    forward = tracker.track(
        ts.build_request(fixture_dir, "motion_blur",
                         TrackingMode.TRANSLATION, TrackDirection.FORWARD))
    manifest = ts.load_manifest(fixture_dir, "motion_blur")
    return forward, manifest


class TestSceneCut:
    def test_cut_frames_failed_with_reason(self, scene_cut_results):
        """切换帧起 FAILED + 有原因（不静默通过）。"""
        forward, _backward, manifest = scene_cut_results
        cut_frame = 70
        transforms = forward.transforms
        failed_seen = 0
        for f, t in transforms.items():
            if f < cut_frame:
                continue
            if t.status == TrackStatus.FAILED:
                failed_seen += 1
                assert t.failure_reason not in (
                    FailureReason.NONE, FailureReason.CANCELLED), (
                    f"帧 {f} 失败无原因")
        assert failed_seen > 0, "切换后应出现失败（卡片消失）"

    def test_no_fake_valid_after_cut(self, scene_cut_results):
        """失败不污染后续帧：切换后无假 VALID（卡片已不存在）。"""
        forward, _backward, _manifest = scene_cut_results
        fake_valid = [
            f for f, t in forward.transforms.items()
            if f >= 70 and t.status == TrackStatus.VALID
            and t.confidence > 0.8]
        assert not fake_valid, f"切换后假高置信 VALID 帧：{fake_valid[:5]}"

    def test_pre_cut_frames_track_normally(self, scene_cut_results):
        """切换前清晰段正常跟踪（前向+后向合覆盖 30-68，均值<4px）。"""
        forward, backward, manifest = scene_cut_results
        ref_corners = ts.reference_corners(manifest)
        errors = []
        for f in range(30, 68, 4):
            t = (forward.transforms.get(f)
                 if f >= manifest["reference_frame"]
                 else backward.transforms.get(f))
            if t is None or t.status == TrackStatus.FAILED:
                continue
            meta = manifest["frames"][f]
            H_gt = np.array(
                meta["homography"], dtype=np.float64).reshape(3, 3)
            errors.append(ts.corner_error(t.matrix_np, H_gt, ref_corners))
        assert errors, "切换前应有可评估帧"
        assert float(sum(errors) / len(errors)) < 4.0


class TestMotionBlur:
    def test_clear_segment_tracks(self, blur_results):
        """清晰段（f<40）正常跟踪。"""
        forward, manifest = blur_results
        # 前向参考帧 62：清晰段 42 之前需后向覆盖——合并两方向
        backward = ts.Tracker2D().track(
            ts.build_request(ts.fixture_dir(), "motion_blur",
                             TrackingMode.TRANSLATION,
                             TrackDirection.BACKWARD))
        valid = [
            f for f, t in list(forward.transforms.items())
            + list(backward.transforms.items())
            if f < 38 and t.status == TrackStatus.VALID]
        assert valid, "清晰段应有 VALID"

    def test_blurred_segment_valid_frames_accurate(self, blur_results):
        """模糊段 VALID 必须准确（置信不冒称）：四角误差冻结容差 <3px。

        实测 LK 金字塔在 45px 方向模糊核下仍精确锁定（barcode 高对比
        存活）——VALID+高置信为真，断言精度一致而非压制置信。
        """
        forward, manifest = blur_results
        ref_corners = ts.reference_corners(manifest)
        worst = 0.0
        checked = 0
        for f, t in forward.transforms.items():
            if not (42 <= f < 84) or t.status == TrackStatus.FAILED:
                continue
            meta = manifest["frames"][f]
            H_gt = np.array(
                meta["homography"], dtype=np.float64).reshape(3, 3)
            err = ts.corner_error(t.matrix_np, H_gt, ref_corners)
            worst = max(worst, float(err))
            checked += 1
        assert checked > 10, "模糊段应有被评估的 VALID 帧"
        assert worst < 3.0, f"模糊段 VALID 帧最大误差 {worst:.2f}px 超容差"
