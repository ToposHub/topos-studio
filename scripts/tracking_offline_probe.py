"""
离线评估脚本（阶段 2）：对合成素材逐帧输出跟踪误差与置信度。

用法：
    .venv/bin/python scripts/tracking_offline_probe.py [--fixture translation]
                                                       [--out report.json]

输出 JSON 结构：
    {
      "<fixture>": {
        "frames": { "<frame>": {"status", "corner_error_px", "confidence",
                                "reason", "diagnostics": {...}} },
        "summary": {"mean_corner_error_px", "max_corner_error_px",
                    "status_counts", "failure_reasons", "elapsed_s"}
      }
    }

无 ground truth 的帧只输出状态与置信度，不输出误差。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.make_tracking_fixtures import generate_all  # noqa: E402
from src.shared.tracking.tracker_2d import (  # noqa: E402
    Tracker2D, TrackRequest, TrackDirection,
)
from src.shared.tracking.types import TrackingMode, TrackStatus  # noqa: E402

MODES = {
    "translation": TrackingMode.TRANSLATION,
    "trs": TrackingMode.TRANSLATION_ROTATION_SCALE,
    "perspective": TrackingMode.HOMOGRAPHY,
    "occlusion": TrackingMode.TRANSLATION,
}


def corner_error(H_est: np.ndarray, H_gt: np.ndarray, ref_corners: np.ndarray) -> float:
    """参考帧四角分别经估计与 GT 单应性变换后的平均距离（像素）。"""
    est = cv2.perspectiveTransform(
        ref_corners.reshape(1, -1, 2).astype(np.float32),
        H_est.astype(np.float32)).reshape(-1, 2)
    gt = cv2.perspectiveTransform(
        ref_corners.reshape(1, -1, 2).astype(np.float32),
        H_gt.astype(np.float32)).reshape(-1, 2)
    return float(np.linalg.norm(est - gt, axis=1).mean())


def probe_fixture(name: str, fixture_dir: str) -> Dict:
    with open(os.path.join(fixture_dir, f"{name}.ground_truth.json")) as fh:
        manifest = json.load(fh)
    video_path = os.path.join(fixture_dir, f"{name}.mp4")
    W, H = manifest["width"], manifest["height"]
    ref = manifest["reference_frame"]
    frames_meta = manifest["frames"]
    px, py, pw, ph = manifest["pattern_rect_reference"]
    pattern = (px / W, py / H, pw / W, ph / H)
    margin = 0.15
    search = (max(0.0, pattern[0] - margin), max(0.0, pattern[1] - margin),
              min(1.0, pattern[2] + 2 * margin), min(1.0, pattern[3] + 2 * margin))
    ref_corners = np.array(
        [[px, py], [px + pw, py], [px + pw, py + ph], [px, py + ph]], dtype=np.float64)

    tracker = Tracker2D()
    transforms, observations, diagnostics = {}, {}, {}
    t0 = time.time()
    for direction in (TrackDirection.FORWARD, TrackDirection.BACKWARD):
        req = TrackRequest(
            media_path=video_path, pattern_region=pattern, search_region=search,
            reference_frame=ref, frame_start=0, frame_end=len(frames_meta) - 1,
            mode=MODES[name], direction=direction,
        )
        result = tracker.track(req)
        transforms.update(result.transforms)
        observations.update(result.observations)
        diagnostics.update(result.diagnostics)
    elapsed = time.time() - t0

    frames_out: Dict[str, Dict] = {}
    errors, status_counts, failure_reasons = [], {}, {}
    for f, meta in enumerate(frames_meta):
        t = transforms.get(f)
        if t is None:
            frames_out[str(f)] = {"status": "missing"}
            continue
        status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1
        entry = {
            "status": t.status.value,
            "confidence": round(float(t.confidence), 4),
        }
        if t.status == TrackStatus.FAILED:
            entry["reason"] = t.failure_reason.value
            failure_reasons[entry["reason"]] = failure_reasons.get(entry["reason"], 0) + 1
        else:
            H_gt = np.array(meta["homography"], dtype=np.float64).reshape(3, 3)
            err = corner_error(t.matrix_np, H_gt, ref_corners)
            entry["corner_error_px"] = round(err, 3)
            errors.append(err)
        diag = diagnostics.get(f)
        if diag is not None:
            entry["diagnostics"] = {
                "point_count": diag.point_count,
                "inlier_count": diag.inlier_count,
                "inlier_ratio": round(diag.inlier_ratio, 3),
                "fb_pass_ratio": round(diag.fb_pass_ratio, 3),
                "spread": round(diag.spread, 3),
                "reproj_error": round(diag.reproj_error, 3),
                "sharpness": round(diag.sharpness, 1),
            }
        frames_out[str(f)] = entry

    errors_arr = np.array(errors) if errors else np.array([0.0])
    return {
        "frames": frames_out,
        "summary": {
            "mean_corner_error_px": round(float(errors_arr.mean()), 3),
            "max_corner_error_px": round(float(errors_arr.max()), 3),
            "p95_corner_error_px": round(float(np.percentile(errors_arr, 95)), 3),
            "status_counts": status_counts,
            "failure_reasons": failure_reasons,
            "elapsed_s": round(elapsed, 2),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="tracking offline probe")
    parser.add_argument("--fixture", default=None, choices=list(MODES),
                        help="只评估指定素材（默认全部）")
    parser.add_argument("--out", default=None, help="JSON 报告输出路径")
    args = parser.parse_args()

    fixture_dir = generate_all(force=False)
    names = [args.fixture] if args.fixture else list(MODES)

    report = {}
    for name in names:
        report[name] = probe_fixture(name, fixture_dir)
        s = report[name]["summary"]
        print(f"{name:12s} mean={s['mean_corner_error_px']:.2f}px "
              f"max={s['max_corner_error_px']:.2f}px "
              f"statuses={s['status_counts']} reasons={s['failure_reasons']}")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print(f"report -> {args.out}")


if __name__ == "__main__":
    main()
