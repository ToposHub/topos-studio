"""
生成跟踪器验收用的四类合成测试素材（DaVinci 风格跟踪器 阶段 0）。

四类素材：
1. translation  - 只有平移的高对比度平面卡片
2. trs          - 平移 + 缩放 + 旋转
3. perspective  - 透视变化的平面（卡片绕轴倾斜）
4. occlusion    - 遮挡、出画和重新出现

每个素材输出：
- <name>.mp4              合成视频（mp4v 编码）
- <name>.ground_truth.json 逐帧参考轨迹（单应性矩阵 + 四角坐标 + 状态标记）

地面真值约定：
- `homography` 是 3x3 行主序矩阵（9 个浮点数），把 **参考帧坐标系** 下的点
  映射到 **当前帧坐标系**：p_cur ~ H @ p_ref。参考帧处为单位矩阵。
- `corners` 是卡片矩形四角在当前帧中的像素坐标 [TL, TR, BR, BL]。
- `occluded=True` 表示该帧卡片被遮挡物覆盖（跟踪应标记 weak/failed）。
- `visible=False` 表示该帧卡片完全出画。
- 每帧左下角绘有二进制帧号条（12 位，LSB 在左），用于验证按帧读取的正确性。

用法：
    .venv/bin/python tools/make_tracking_fixtures.py [--force] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import math
import os
from typing import Callable, Dict, List, Tuple

import cv2
import numpy as np

WIDTH = 1280
HEIGHT = 720
FPS = 25
FRAME_COUNT = 125

# 卡片尺寸（像素，参考帧中的未变换尺寸）
CARD_W = 320
CARD_H = 220
# 参考帧中的卡片中心
REF_CENTER = np.array([WIDTH * 0.5, HEIGHT * 0.5], dtype=np.float64)

BARCODE_CELLS = 12
BARCODE_CELL = 20
BARCODE_X0 = 16
BARCODE_Y0 = HEIGHT - 16 - BARCODE_CELL

FIXTURE_NAMES = ("translation", "trs", "perspective", "occlusion", "scene_cut", "motion_blur")


# ================================================================
# 基础绘制
# ================================================================

def make_background(seed: int = 42) -> np.ndarray:
    """生成静态纹理背景（跟踪特征来源，全程不变）。"""
    rng = np.random.default_rng(seed)
    coarse = rng.integers(24, 72, size=(HEIGHT // 16, WIDTH // 16), dtype=np.uint8)
    bg = cv2.resize(coarse, (WIDTH, HEIGHT), interpolation=cv2.INTER_CUBIC)
    bg = cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR)
    # 大尺度渐变增加低频层次
    ys = np.linspace(0.0, 1.0, HEIGHT, dtype=np.float32)[:, None]
    xs = np.linspace(0.0, 1.0, WIDTH, dtype=np.float32)[None, :]
    grad = (ys * 28 + xs * 16).astype(np.uint8)
    bg = cv2.add(bg, cv2.cvtColor(grad, cv2.COLOR_GRAY2BGR))
    # 静态圆环与直线纹理
    for i in range(6):
        cx = int((i * 227 + 100) % WIDTH)
        cy = int((i * 173 + 80) % HEIGHT)
        cv2.circle(bg, (cx, cy), 34 + i * 9, (90 + i * 8, 70, 60), 2)
    for i in range(8):
        y = 60 + i * 84
        cv2.line(bg, (0, y), (WIDTH, y - 30), (48, 56, 64), 1)
    return bg


def _scene_b_frame(frame_index: int) -> np.ndarray:
    """F7.1：镜头切换后的第二场景——完全不同的背景，无卡片。"""
    bg = np.full((HEIGHT, WIDTH, 3), (40, 90, 150), dtype=np.uint8)
    for y in range(0, HEIGHT, 64):
        cv2.line(bg, (0, y), (WIDTH, y), (30, 70, 120), 2)
    for x in range(0, WIDTH, 96):
        cv2.line(bg, (x, 0), (x, HEIGHT), (35, 80, 130), 1)
    draw_barcode(bg, frame_index)
    return bg


def make_card() -> Tuple[np.ndarray, np.ndarray]:
    """生成高对比度卡片图像及其 alpha 蒙版。"""
    card = np.full((CARD_H, CARD_W, 3), 235, dtype=np.uint8)
    # 棋盘格
    sq = CARD_W // 10
    for r in range(0, CARD_H, sq):
        for c in range(0, CARD_W, sq):
            if ((r // sq) + (c // sq)) % 2 == 0:
                card[r:r + sq, c:c + sq] = (28, 28, 28)
    # 同心圆
    cv2.circle(card, (CARD_W // 2, CARD_H // 2), 72, (200, 40, 40), -1)
    cv2.circle(card, (CARD_W // 2, CARD_H // 2), 52, (240, 220, 60), -1)
    cv2.circle(card, (CARD_W // 2, CARD_H // 2), 24, (30, 30, 200), -1)
    # 四角三角标记（区分朝向）
    cv2.fillPoly(card, [np.array([[6, 6], [66, 6], [6, 66]])], (40, 190, 40))
    cv2.fillPoly(card, [np.array([[CARD_W - 6, 6], [CARD_W - 66, 6], [CARD_W - 6, 66]])], (220, 120, 30))
    # 边框
    cv2.rectangle(card, (2, 2), (CARD_W - 3, CARD_H - 3), (10, 10, 10), 4)
    # 非周期性斑点纹理：打破棋盘格的空间周期性，
    # 避免金字塔 LK 光流锁定在零位移局部极小（阶段 2 诊断结论）。
    rng = np.random.default_rng(2024)
    for _ in range(60):
        x = int(rng.integers(14, CARD_W - 14))
        y = int(rng.integers(14, CARD_H - 14))
        r = int(rng.integers(3, 9))
        v = int(rng.integers(20, 230))
        color = (v, int(v * 0.8) % 256, int(v * 0.6) % 256)
        cv2.circle(card, (x, y), r, color, -1)
    alpha = np.ones((CARD_H, CARD_W), dtype=np.float32)
    return card, alpha


def draw_barcode(frame: np.ndarray, frame_index: int) -> None:
    """在左下角绘制二进制帧号条，用于验证按帧读取。"""
    cv2.rectangle(
        frame,
        (BARCODE_X0 - 4, BARCODE_Y0 - 4),
        (BARCODE_X0 + BARCODE_CELLS * BARCODE_CELL + 4, BARCODE_Y0 + BARCODE_CELL + 4),
        (0, 0, 0), -1,
    )
    for i in range(BARCODE_CELLS):
        bit = (frame_index >> i) & 1
        x = BARCODE_X0 + i * BARCODE_CELL
        color = (255, 255, 255) if bit else (0, 0, 0)
        cv2.rectangle(frame, (x, BARCODE_Y0), (x + BARCODE_CELL - 2, BARCODE_Y0 + BARCODE_CELL - 2), color, -1)


def read_barcode(frame_bgr: np.ndarray) -> int:
    """从帧中解码二进制帧号条（供测试使用）。"""
    value = 0
    for i in range(BARCODE_CELLS):
        x = BARCODE_X0 + i * BARCODE_CELL
        cell = frame_bgr[BARCODE_Y0 + 4:BARCODE_Y0 + BARCODE_CELL - 6,
                         x + 4:x + BARCODE_CELL - 6]
        if cell.size == 0:
            continue
        if float(cell.mean()) > 128.0:
            value |= (1 << i)
    return value


# ================================================================
# 变换矩阵构造
# ================================================================

def reference_corners() -> np.ndarray:
    """参考帧中的卡片四角 [TL, TR, BR, BL]。"""
    cx, cy = REF_CENTER
    w2, h2 = CARD_W / 2.0, CARD_H / 2.0
    return np.array([
        [cx - w2, cy - h2],
        [cx + w2, cy - h2],
        [cx + w2, cy + h2],
        [cx - w2, cy + h2],
    ], dtype=np.float64)


def homography_from_corners(dst: np.ndarray) -> np.ndarray:
    """由参考四角与当前四角求单应性（参考 -> 当前）。"""
    H, _ = cv2.findHomography(reference_corners().astype(np.float32), dst.astype(np.float32))
    if H is None:
        raise RuntimeError("homography solve failed")
    return H.astype(np.float64)


def trs_homography(tx: float, ty: float, rotation_deg: float, scale: float) -> np.ndarray:
    """构造平移+旋转+缩放单应性（绕参考中心）。"""
    cx, cy = REF_CENTER
    R = cv2.getRotationMatrix2D((cx, cy), rotation_deg, scale).astype(np.float64)
    H = np.eye(3, dtype=np.float64)
    H[:2, :] = R
    H[0, 2] += tx
    H[1, 2] += ty
    return H


def decompose_homography(H: np.ndarray) -> Dict[str, float]:
    """从单应性分解出近似平移/旋转/缩放（用于 ground truth 展示字段）。"""
    ref = reference_corners()
    dst = cv2.perspectiveTransform(ref.reshape(1, -1, 2).astype(np.float32),
                                   H.astype(np.float32)).reshape(-1, 2)
    ref_c = ref.mean(axis=0)
    dst_c = dst.mean(axis=0)
    tx = float(dst_c[0] - ref_c[0])
    ty = float(dst_c[1] - ref_c[1])
    ref_v = ref[1] - ref[0]
    dst_v = dst[1] - dst[0]
    rotation_deg = float(math.degrees(math.atan2(dst_v[1], dst_v[0])
                                      - math.atan2(ref_v[1], ref_v[0])))
    scale = float(np.linalg.norm(dst_v) / np.linalg.norm(ref_v))
    return {"tx": tx, "ty": ty, "rotation_deg": rotation_deg, "scale": scale}


def warp_card_into(frame: np.ndarray, card: np.ndarray, alpha: np.ndarray, H: np.ndarray) -> None:
    """把卡片按单应性贴到帧上。

    Args:
        H: 参考帧坐标 → 当前帧坐标的单应性（与 ground truth 同语义）。

    注意：``cv2.warpPerspective`` 的矩阵是「目标→源」映射，而卡片像素位于
    局部坐标系 (0..CARD_W, 0..CARD_H)，因此必须先构造「卡片局部角点 →
    当前帧位置」的单应性，否则内容会渲染到 H⁻¹ 位置（与 GT 不符）。
    """
    card_local = np.array(
        [[0.0, 0.0], [CARD_W, 0.0], [CARD_W, CARD_H], [0.0, CARD_H]],
        dtype=np.float32,
    )
    dst_corners = cv2.perspectiveTransform(
        reference_corners().reshape(1, -1, 2).astype(np.float32),
        H.astype(np.float32),
    ).reshape(-1, 2)
    H_local, _ = cv2.findHomography(card_local, dst_corners.astype(np.float32))
    if H_local is None:
        raise RuntimeError("card local homography solve failed")
    warped = cv2.warpPerspective(card, H_local, (WIDTH, HEIGHT), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT)
    warped_a = cv2.warpPerspective(alpha, H_local, (WIDTH, HEIGHT), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_CONSTANT)
    mask3 = np.dstack([warped_a] * 3)
    frame[:] = (warped.astype(np.float32) * mask3
                + frame.astype(np.float32) * (1.0 - mask3)).astype(np.uint8)


# ================================================================
# 四类轨迹定义
# ================================================================

MotionFn = Callable[[int], Dict]


def _motion_translation(f: int) -> Dict:
    ref = FRAME_COUNT // 2
    t = f - ref
    tx = 2.8 * t
    ty = 0.9 * t + 14.0 * math.sin(0.06 * t)
    return {"H": trs_homography(tx, ty, 0.0, 1.0), "occluded": False, "visible": True}


def _motion_trs(f: int) -> Dict:
    ref = FRAME_COUNT // 2
    t = f - ref
    tx = 120.0 * math.sin(0.05 * t)
    ty = 60.0 * math.cos(0.04 * t) - 60.0
    rotation = 11.0 * math.sin(0.035 * t)
    scale = 1.0 + 0.18 * math.sin(0.045 * t + 0.6)
    return {"H": trs_homography(tx, ty, rotation, scale), "occluded": False, "visible": True}


def _motion_perspective(f: int) -> Dict:
    ref = FRAME_COUNT // 2
    t = f - ref
    phase = 0.05 * t
    w2, h2 = CARD_W / 2.0, CARD_H / 2.0
    cx, cy = REF_CENTER
    tx = 80.0 * math.sin(phase * 0.7)
    # 左右交替的透视倾斜（模拟卡片绕竖轴摆动）
    sx = 0.45 * math.sin(phase)
    sy = 0.30 * math.sin(phase + 1.3)
    dst = np.array([
        [cx - w2 + tx + 26.0 * sx, cy - h2 + 18.0 * sy],
        [cx + w2 + tx - 26.0 * sx, cy - h2 - 14.0 * sy],
        [cx + w2 + tx - 20.0 * sx, cy + h2 + 16.0 * sy],
        [cx - w2 + tx + 20.0 * sx, cy + h2 - 18.0 * sy],
    ], dtype=np.float64)
    return {"H": homography_from_corners(dst), "occluded": False, "visible": True}


def _motion_occlusion(f: int) -> Dict:
    # 匀速向右：从画面左侧外进入，中途被遮挡一段，最后从右侧完全出画。
    # 参考帧 f=62 时 tx=0（卡片在画面中央）。
    # 遮挡结束后的“重新出现” + 两端出画覆盖“出画和重新出现”需求。
    tx = 16.0 * (f - 62)
    ty = 0.4 * (f - 62)
    cx = REF_CENTER[0] + tx
    # 完全出画：卡片左右边缘均越过画框边界（允许 24px 压缩余量）
    off_right = cx - CARD_W / 2.0 > WIDTH - 24
    off_left = cx + CARD_W / 2.0 < 24
    occluded = 40 <= f < 60
    return {
        "H": trs_homography(tx, ty, 0.0, 1.0),
        "occluded": occluded,
        "visible": not (off_right or off_left),
    }


def _motion_scene_cut(f: int) -> Dict:
    """F7.1 镜头切换镜头：f=70 处画面跳变为完全不同的第二场景。

    切换后卡片消失 → 跟踪必须 FAILED 且不污染后续帧；切换前为标准
    平移。ground truth 用 scene_cut=True 标记切换帧及之后。
    """
    cut = f >= 70
    tx = -10.0 * (f - 62)
    return {
        "H": trs_homography(tx, 0.0, 0.0, 1.0),
        "occluded": False,
        "visible": not cut,  # 切换后原卡片不再存在
        "scene_cut": cut,
    }


def _motion_motion_blur(f: int) -> Dict:
    """F7.1 独立运动模糊镜头：f∈[40,85] 高速平移 + 强方向性模糊。

    blur_amount 供渲染端叠加方向模糊（模糊核随速度增大）。
    """
    speed = 34.0 if 40 <= f < 85 else 6.0
    tx = speed * (f - 62)
    blur = min(45.0, abs(speed) * 1.2) if 40 <= f < 85 else 3.0
    return {
        "H": trs_homography(tx, 0.3 * (f - 62), 0.0, 1.0),
        "occluded": False,
        "visible": True,
        "blur_amount": blur,
    }


MOTIONS: Dict[str, MotionFn] = {
    "translation": _motion_translation,
    "trs": _motion_trs,
    "perspective": _motion_perspective,
    "occlusion": _motion_occlusion,
    "scene_cut": _motion_scene_cut,
    "motion_blur": _motion_motion_blur,
}


# ================================================================
# 素材生成
# ================================================================

def generate_fixture(name: str, out_dir: str) -> str:
    """生成单个素材，返回视频路径。"""
    motion = MOTIONS[name]
    bg = make_background(seed=42 + hash(name) % 97)
    card, alpha = make_card()
    video_path = os.path.join(out_dir, f"{name}.mp4")
    writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise RuntimeError(f"cannot open video writer: {video_path}")

    frames_meta: List[Dict] = []
    ref_idx = FRAME_COUNT // 2
    ref_H = motion(ref_idx)["H"]
    ref_H_inv = np.linalg.inv(ref_H)

    try:
        for f in range(FRAME_COUNT):
            state = motion(f)
            if state.get("scene_cut"):
                # F7.1：切换帧起渲染完全不同的第二场景（卡片消失）
                frame = _scene_b_frame(f)
            else:
                frame = bg.copy()
            H_ref_cur = state["H"]
            if state["visible"]:
                warp_card_into(frame, card, alpha, H_ref_cur)
            if name == "occlusion" and state["occluded"]:
                # 深灰色遮挡条覆盖卡片区域
                corners = cv2.perspectiveTransform(
                    reference_corners().reshape(1, -1, 2).astype(np.float32),
                    H_ref_cur.astype(np.float32)).reshape(-1, 2)
                x0 = max(0, int(corners[:, 0].min()) - 8)
                y0 = max(0, int(corners[:, 1].min()) - 8)
                x1 = min(WIDTH, int(corners[:, 0].max()) + 8)
                y1 = min(HEIGHT, int(corners[:, 1].max()) + 8)
                cv2.rectangle(frame, (x0, y0), (x1, y1), (18, 18, 20), -1)
                cv2.putText(frame, "OCCLUDED", (x0 + 20, y0 + 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, (200, 200, 200), 2)
            if name == "occlusion" and (f < 22 or f > 102):
                # 出入画阶段叠加运动模糊，验证模糊下的跟踪鲁棒性
                frame = cv2.blur(frame, (9, 3))
            blur_amount = state.get("blur_amount", 0.0)
            if blur_amount > 0.0:
                # F7.1 独立运动模糊镜头：方向性水平模糊（核随速度）
                k = max(3, int(round(blur_amount)) | 1)
                frame = cv2.blur(frame, (k, 1))
            draw_barcode(frame, f)
            writer.write(frame)

            # ground truth 使用「参考帧坐标 -> 当前帧坐标」的单应性
            H = H_ref_cur @ ref_H_inv
            # Ground truth 的 homography 语义是「参考帧 -> 当前帧」；
            # corners 必须使用同一个已归一化的 H。此前这里使用
            # H_ref_cur，导致 perspective fixture 的参考帧 H=I 但 corners
            # 仍带有参考姿态偏移，Corner Pin 误差被人为抬高。
            corners = cv2.perspectiveTransform(
                reference_corners().reshape(1, -1, 2).astype(np.float32),
                H.astype(np.float32)).reshape(-1, 2)
            meta: Dict = {
                "frame": f,
                "homography": [float(v) for v in H.ravel()],
                "corners": [[float(x), float(y)] for x, y in corners],
                "occluded": bool(state["occluded"]),
                "visible": bool(state["visible"]),
            }
            meta.update({k: float(v) for k, v in decompose_homography(H).items()})
            frames_meta.append(meta)
    finally:
        writer.release()

    manifest = {
        "fixture": name,
        "video": f"{name}.mp4",
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "frame_count": FRAME_COUNT,
        "reference_frame": ref_idx,
        "pattern_rect_reference": [
            float(REF_CENTER[0] - CARD_W / 2.0), float(REF_CENTER[1] - CARD_H / 2.0),
            float(CARD_W), float(CARD_H),
        ],
        "frames": frames_meta,
    }
    gt_path = os.path.join(out_dir, f"{name}.ground_truth.json")
    with open(gt_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
    return video_path


def default_output_dir() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "tests", "test_videos", "tracking_fixtures")


def generate_all(out_dir: str | None = None, force: bool = False) -> str:
    """生成全部四类素材，返回输出目录。已存在且非 force 时跳过。"""
    out = out_dir or default_output_dir()
    os.makedirs(out, exist_ok=True)
    for name in FIXTURE_NAMES:
        video_path = os.path.join(out, f"{name}.mp4")
        gt_path = os.path.join(out, f"{name}.ground_truth.json")
        if not force and os.path.exists(video_path) and os.path.exists(gt_path):
            continue
        generate_fixture(name, out)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="生成跟踪器合成测试素材")
    parser.add_argument("--out", default=None, help="输出目录（默认 tests/test_videos/tracking_fixtures）")
    parser.add_argument("--force", action="store_true", help="强制重新生成已存在的素材")
    args = parser.parse_args()
    out = generate_all(args.out, force=args.force)
    print(f"tracking fixtures ready at: {out}")
    for name in FIXTURE_NAMES:
        print(f"  - {name}.mp4 + {name}.ground_truth.json")


if __name__ == "__main__":
    main()
