"""阶段 2 跟踪引擎测试共享辅助（素材加载、请求构造、误差计算）。"""

import json
import os
from typing import Dict

import cv2
import numpy as np

from src.shared.tracking.tracker_2d import (
    Tracker2D,
    TrackRequest,
    TrackDirection,
)
from src.shared.tracking.types import TrackingMode

# 素材工具不可用时由调用方跳过
try:
    from tools.make_tracking_fixtures import generate_all
    TOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover
    TOOLS_AVAILABLE = False

SEARCH_MARGIN = 0.15


def fixture_dir() -> str:
    """生成（或复用）合成素材目录。"""
    return generate_all(force=False)


def load_manifest(directory: str, name: str) -> dict:
    path = os.path.join(directory, f"{name}.ground_truth.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_request(
    directory: str,
    name: str,
    mode: TrackingMode,
    direction: TrackDirection = TrackDirection.FORWARD,
    generation: int = 0,
) -> TrackRequest:
    """按素材清单构造标准跟踪请求（search = pattern 外扩 0.15）。"""
    manifest = load_manifest(directory, name)
    W, H = manifest["width"], manifest["height"]
    px, py, pw, ph = manifest["pattern_rect_reference"]
    pattern = (px / W, py / H, pw / W, ph / H)
    m = SEARCH_MARGIN
    search = (
        max(0.0, pattern[0] - m), max(0.0, pattern[1] - m),
        min(1.0, pattern[2] + 2 * m), min(1.0, pattern[3] + 2 * m),
    )
    return TrackRequest(
        media_path=os.path.join(directory, f"{name}.mp4"),
        pattern_region=pattern,
        search_region=search,
        reference_frame=manifest["reference_frame"],
        frame_start=0,
        frame_end=len(manifest["frames"]) - 1,
        mode=mode,
        direction=direction,
        generation=generation,
    )


def reference_corners(manifest: dict) -> np.ndarray:
    px, py, pw, ph = manifest["pattern_rect_reference"]
    return np.array(
        [[px, py], [px + pw, py], [px + pw, py + ph], [px, py + ph]],
        dtype=np.float64,
    )


def corner_error(H_est: np.ndarray, H_gt: np.ndarray, ref_corners: np.ndarray) -> float:
    """参考帧四角分别经估计与 GT 单应性变换后的平均距离（像素）。"""
    pts = ref_corners.reshape(1, -1, 2).astype(np.float32)
    est = cv2.perspectiveTransform(pts, H_est.astype(np.float32)).reshape(-1, 2)
    gt = cv2.perspectiveTransform(pts, H_gt.astype(np.float32)).reshape(-1, 2)
    return float(np.linalg.norm(est - gt, axis=1).mean())


def track_both_directions(
    directory: str, name: str, mode: TrackingMode, tracker: Tracker2D = None
) -> Dict:
    """前向 + 后向跟踪合并，返回 {frame: TrackTransform}。"""
    tracker = tracker or Tracker2D()
    transforms = {}
    for direction in (TrackDirection.FORWARD, TrackDirection.BACKWARD):
        req = build_request(directory, name, mode, direction)
        transforms.update(tracker.track(req).transforms)
    return transforms
