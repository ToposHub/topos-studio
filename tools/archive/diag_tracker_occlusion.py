"""诊断：遮挡素材逐帧跟踪状态与误差（P0-COLOR-07 分析用）。

用法:
    .venv/bin/python tools/diag_tracker_occlusion.py [policy]
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.tracking.tracker_2d import (  # noqa: E402
    Tracker2D,
    TrackerConfig,
    TrackDirection,
)
from src.shared.tracking.types import (  # noqa: E402
    FailurePolicy,
    TrackStatus,
    TrackingMode,
)

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "test_videos", "tracking_fixtures",
)


def corner_error(H_est: np.ndarray, H_gt: np.ndarray, ref_corners: np.ndarray) -> float:
    import cv2
    pts = ref_corners.reshape(1, -1, 2).astype(np.float32)
    est = cv2.perspectiveTransform(pts, H_est.astype(np.float32)).reshape(-1, 2)
    gt = cv2.perspectiveTransform(pts, H_gt.astype(np.float32)).reshape(-1, 2)
    return float(np.linalg.norm(est - gt, axis=1).mean())


def main() -> None:
    policy_name = sys.argv[1] if len(sys.argv) > 1 else "recapture"
    policy = FailurePolicy(policy_name)

    with open(os.path.join(FIXTURE_DIR, "occlusion.ground_truth.json")) as fh:
        manifest = json.load(fh)
    px, py, pw, ph = manifest["pattern_rect_reference"]
    ref_corners = np.array(
        [[px, py], [px + pw, py], [px + pw, py + ph], [px, py + ph]], dtype=np.float64)
    W, H = manifest["width"], manifest["height"]
    pattern = (px / W, py / H, pw / W, ph / H)
    m = 0.15
    search = (max(0.0, pattern[0] - m), max(0.0, pattern[1] - m),
              min(1.0, pattern[2] + 2 * m), min(1.0, pattern[3] + 2 * m))

    from src.shared.tracking.tracker_2d import TrackRequest
    from src.shared.tracking.frame_sampler import AnalysisQuality

    tracker = Tracker2D(TrackerConfig(failure_policy=policy))
    for direction in (TrackDirection.FORWARD, TrackDirection.BACKWARD):
        req = TrackRequest(
            media_path=os.path.join(FIXTURE_DIR, "occlusion.mp4"),
            pattern_region=pattern,
            search_region=search,
            reference_frame=manifest["reference_frame"],
            frame_start=0,
            frame_end=len(manifest["frames"]) - 1,
            mode=TrackingMode.TRANSLATION,
            direction=direction,
            quality=AnalysisQuality.DRAFT,
        )
        result = tracker.track(req)
        print(f"\n===== direction={direction.name} policy={policy.name} =====")
        print(f"{'f':>4} {'gt_state':>10} {'status':>8} {'reason':>20} "
              f"{'conf':>6} {'err_px':>8}")
        for f in sorted(result.transforms):
            t = result.transforms[f]
            meta = manifest["frames"][f]
            gt_state = ("OCC" if meta["occluded"]
                        else ("INV" if not meta["visible"] else "vis"))
            err = ""
            if t.status != TrackStatus.FAILED:
                H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
                err = f"{corner_error(t.matrix_np, H_gt, ref_corners):8.2f}"
            print(f"{f:>4} {gt_state:>10} {t.status.name:>8} "
                  f"{t.failure_reason.name:>20} {t.confidence:6.3f} {err:>8}")


if __name__ == "__main__":
    main()
