"""探针：遮挡素材各类帧的 NCC 外观分数分布（用于确定门控阈值）。

测量两项：
1. appearance score：用 GT 单应性把当前帧 pattern 区域反 warp 回参考模板，
   与参考模板做 TM_CCOEFF_NORMED —— 即"完美跟踪"时的外观分。
2. recapture score：整帧 matchTemplate 的最大 NCC —— 重捕获路径的分数。
"""

from __future__ import annotations

import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.tracking.frame_sampler import (  # noqa: E402
    AnalysisQuality,
    FrameSampler,
)

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "test_videos", "tracking_fixtures",
)


def main() -> None:
    with open(os.path.join(FIXTURE_DIR, "occlusion.ground_truth.json")) as fh:
        manifest = json.load(fh)

    sampler = FrameSampler(os.path.join(FIXTURE_DIR, "occlusion.mp4"))
    ref = sampler.read_gray(manifest["reference_frame"], AnalysisQuality.DRAFT)
    S = ref.scale_matrix()          # 分析 → 原始
    S_inv = np.linalg.inv(S)        # 原始 → 分析
    scale = ref.scale

    px, py, pw, ph = manifest["pattern_rect_reference"]
    x0, y0 = int(px * scale), int(py * scale)
    x1, y1 = int(round((px + pw) * scale)), int(round((py + ph) * scale))
    template = ref.gray[y0:y1, x0:x1]
    th, tw = template.shape[:2]
    # 模板局部坐标 → 分析坐标
    T = np.eye(3, dtype=np.float64)
    T[0, 2], T[1, 2] = x0, y0

    print(f"analysis scale={scale:.3f} template={tw}x{th}")
    print(f"{'f':>4} {'gt':>4} {'appear_gt':>10} {'recap_max':>10}")

    samples = (
        list(range(0, 24)) + list(range(38, 62)) + list(range(100, 125))
        + [62, 63, 80]
    )
    for f in sorted(set(samples)):
        meta = manifest["frames"][f]
        cur = sampler.read_gray(f, AnalysisQuality.DRAFT).gray

        # 1) appearance score at GT pose
        H_gt_ana = S_inv @ np.array(meta["homography"]).reshape(3, 3) @ S
        M_fwd = H_gt_ana @ T                   # 模板局部 → 当前分析坐标
        # warpPerspective 传正向映射的逆：dst(q) = cur(M_fwd(q))
        M = np.linalg.inv(M_fwd)
        try:
            patch = cv2.warpPerspective(
                cur, M, (tw, th), flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT)
            s = cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED)
            appear = float(s[0][0])
        except cv2.error:
            appear = float("nan")

        # 2) full-frame recapture max NCC
        if tw < cur.shape[1] and th < cur.shape[0]:
            scores = cv2.matchTemplate(cur, template, cv2.TM_CCOEFF_NORMED)
            _, recap, _, _ = cv2.minMaxLoc(scores)
        else:
            recap = float("nan")

        gt = "OCC" if meta["occluded"] else ("INV" if not meta["visible"] else "vis")
        print(f"{f:>4} {gt:>4} {appear:10.3f} {recap:10.3f}")

    sampler.close()


if __name__ == "__main__":
    main()
