"""
2D 跟踪引擎（阶段 2，L1 核心）。

金字塔 LK 光流 + forward-backward 检查 + 按模式选择几何求解
（translation / trs / affine / homography）+ 参考模板外观门控
+ 五因子置信度评分：

    confidence = Σ wᵢ · fᵢ，f ∈ {点数, 分布, FB 通过率, 内点比, 重投影误差}

外观门控独立于置信度：候选矩阵反 warp 后与参考模板 NCC 低于阈值时
直接判失败（遮挡/漂移），防止"几何自洽但目标已丢"的假有效。

坐标系约定：
- 内部计算全部在**分析分辨率**（代理帧）坐标进行；
- 输出的 ``TrackTransform.matrix`` 已换算到**原始分辨率**，
  语义为「参考帧坐标 → 当前帧坐标」，参考帧处为单位矩阵。

失败必须给出原因枚举（不允许无解释的失败）：
特征不足 / 遮挡 / 越界 / 模糊 / 求解失败 / 取消。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

import cv2
import numpy as np

from src.shared.tracking.coordinate_map import norm_region_to_pixels
from src.shared.tracking.feature_detector import FeatureDetector, DetectorConfig
from src.shared.tracking.frame_sampler import AnalysisQuality, FrameSampler
from src.shared.tracking.homography_stability import (
    DEFAULT_MAX_AREA_RATIO,
    DEFAULT_MAX_CONDITION,
    DEFAULT_MIN_AREA_RATIO,
    DEFAULT_SIDE_VIEW_RATIO,
    check_homography_stability,
)
from src.shared.tracking.types import (
    FeatureObservation,
    FailurePolicy,
    FailureReason,
    TrackStatus,
    TrackTransform,
    TrackingMode,
)

logger = logging.getLogger(__name__)

# 参考模板的最小纹理能量（拉普拉斯方差）。低于该值视为近常量模板：
# TM_CCOEFF_NORMED 的 0/0 会被 OpenCV 规约为满分 1.0（实测 cv2 4.11：
# 常量 patch vs 常量模板 → 1.0），外观门控静默失效且诊断恒报满分。
# 这类模板显式走「无门控」路径（_ref_template 返回 None）。
TEMPLATE_MIN_TEXTURE_VAR = 1.0


class TrackDirection(Enum):
    """跟踪方向（相对参考帧）。"""

    FORWARD = "forward"    # 参考帧 → frame_end
    BACKWARD = "backward"  # 参考帧 → frame_start


@dataclass(frozen=True)
class TrackerConfig:
    """跟踪器参数（阈值全部配置化，禁止魔法数字散落算法内部）。"""

    # ---- 光流 ----
    lk_win_size: int = 21
    lk_max_level: int = 3
    lk_epsilon: float = 0.03
    lk_max_iter: int = 30

    # ---- forward-backward 检查 ----
    fb_max_distance: float = 1.6     # 往返误差阈值（分析像素）

    # ---- RANSAC ----
    ransac_reproj_threshold: float = 2.0
    ransac_confidence: float = 0.999

    # ---- 五因子置信度权重 ----
    weight_points: float = 0.20
    weight_spread: float = 0.20
    weight_fb: float = 0.20
    weight_inlier: float = 0.20
    weight_error: float = 0.20
    error_sigma: float = 1.5         # 重投影误差衰减尺度（分析像素）

    # ---- 状态阈值 ----
    valid_confidence: float = 0.60
    weak_confidence: float = 0.35

    # ---- 失败启发式 ----
    blur_laplacian_min: float = 30.0          # search 区域拉普拉斯方差下限
    out_of_bounds_fraction: float = 0.50      # 越界点比例上限
    reanchor_inlier_ratio: float = 0.35       # 内点比例低于此值触发重新检测
    redetect_min_points_ratio: float = 0.5    # 有效点低于 建议点数×该比例 时重新检测
    redetect_max_gap: int = 5                 # 距上次成功超过该帧数不再重检测（避免锚到陈旧预测位置）
    # 外观门控：求解出的候选矩阵把当前帧 pattern 区域反 warp 回参考模板坐标系，
    # 与参考模板做 NCC 比对。低于阈值说明目标内容已被替换/覆盖（遮挡）或
    # 跟踪已漂移到背景 —— 几何自洽（LK/FB/内点全通过）不代表还在跟目标。
    # 实测分布（遮挡素材）：清晰可见 ≈0.99+，遮挡/出画/错误位姿 ≈0，
    # 模糊且边缘裁切的可见帧 0.14-0.56。
    appearance_min_similarity: float = 0.45
    adaptive_appearance: bool = False
    # Adapt only from verified, sharp frames. Local continuity is stricter
    # than the first-frame gate, so an occluder cannot become the template.
    appearance_update_min_similarity: float = 0.80
    appearance_update_min_inlier_ratio: float = 0.60
    appearance_update_min_fb_ratio: float = 0.80

    # ---- 失败策略与重捕获（阶段 7） ----
    failure_policy: FailurePolicy = FailurePolicy.RECAPTURE
    recapture_max_gap: int = 30                # RECAPTURE 策略的重捕获窗口（距上次成功帧数）
    recapture_template_threshold: float = 0.60 # 模板匹配 NCC 下限（低于此值视为未找到目标）
    hold_confidence_decay: float = 0.85        # HOLD_LAST 策略每帧置信度衰减系数

    # ---- 单应性稳定性（阶段 8） ----
    homography_max_condition: float = DEFAULT_MAX_CONDITION
    homography_min_area_ratio: float = DEFAULT_MIN_AREA_RATIO
    homography_max_area_ratio: float = DEFAULT_MAX_AREA_RATIO
    homography_side_view_ratio: float = DEFAULT_SIDE_VIEW_RATIO


@dataclass(frozen=True)
class TrackRequest:
    """一次跟踪任务的完整输入（不可变，可跨线程传递）。"""

    media_path: str
    pattern_region: Tuple[float, float, float, float]   # 归一化 (x, y, w, h)，参考帧
    search_region: Tuple[float, float, float, float]    # 归一化，必须包住 pattern
    reference_frame: int
    frame_start: int
    frame_end: int
    mode: TrackingMode = TrackingMode.TRANSLATION_ROTATION_SCALE
    direction: TrackDirection = TrackDirection.FORWARD
    quality: AnalysisQuality = AnalysisQuality.DRAFT
    generation: int = 0                                 # 任务代次（陈旧结果保护）
    # Mask 窗口的基准几何（capture_base_geometry 快照）。非 None 时初始
    # 特征检测限定在遮罩形状内部（跟踪直接复用 Mask 绘制的遮罩），
    # 而不是包围盒矩形；region 矩形仍作为搜索/预测的计算域。
    shape_geometry: Optional[dict] = None


@dataclass(frozen=True)
class FrameDiagnostics:
    """单帧诊断（五因子分量 + 计数），供面板显示与离线评估。"""

    frame: int
    point_count: int
    inlier_count: int
    inlier_ratio: float
    fb_pass_ratio: float
    spread: float
    reproj_error: float
    confidence: float
    sharpness: float
    condition_number: float = 0.0   # 单应性模式的条件数（阶段 8 诊断）
    appearance_similarity: float = 1.0  # 与参考模板的 NCC 相似度（外观门控）


@dataclass(frozen=True)
class TrackResult:
    """一次跟踪的完整输出（不可变）。

    未覆盖的帧 = 无结果（按失败处理，不画假有效）。
    """

    generation: int
    cancelled: bool
    transforms: Dict[int, TrackTransform]
    observations: Dict[int, Tuple[FeatureObservation, ...]]
    diagnostics: Dict[int, FrameDiagnostics]


class Tracker2D:
    """L1 2D 跟踪引擎（无 Qt 依赖，可在工作线程直接运行）。"""

    def __init__(
        self,
        config: Optional[TrackerConfig] = None,
        detector: Optional[FeatureDetector] = None,
    ) -> None:
        self.config = config or TrackerConfig()
        self.detector = detector or FeatureDetector(DetectorConfig())

    # ================================================================
    # 主入口
    # ================================================================

    def track(
        self,
        request: TrackRequest,
        sampler: Optional[FrameSampler] = None,
        cancel_token: Optional[object] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> TrackResult:
        """执行单方向跟踪。

        Args:
            request: 跟踪请求。
            sampler: 帧采样器；为 None 时按 request.media_path 创建并负责关闭。
            cancel_token: 具有 ``is_set()`` 方法的对象，置位后尽快停止。
            progress_cb: ``cb(done_count, total_count)`` 进度回调。
        """
        cfg = self.config
        owns_sampler = sampler is None
        if owns_sampler:
            sampler = FrameSampler(request.media_path)

        transforms: Dict[int, TrackTransform] = {}
        observations: Dict[int, Tuple[FeatureObservation, ...]] = {}
        diagnostics: Dict[int, FrameDiagnostics] = {}
        cancelled = False

        try:
            ref_sample = sampler.read_gray(request.reference_frame, request.quality)
            scale = ref_sample.scale if ref_sample.scale > 0 else 1.0
            src_w, src_h = ref_sample.source_width, ref_sample.source_height

            pattern_src = norm_region_to_pixels(request.pattern_region, src_w, src_h)
            search_src = norm_region_to_pixels(request.search_region, src_w, src_h)
            pattern_ana = self._scale_rect(pattern_src, scale)
            search_ana = self._scale_rect(search_src, scale)
            pattern_area = max(1.0, pattern_ana[2] * pattern_ana[3])

            frames = self._frame_sequence(request)
            total = len(frames)

            # 参考帧：初始特征检测。透视模式也验证参考平面；如果调用方
            # 配置了无法容纳单位矩阵的阈值，整段任务必须明确失败，不能
            # 把参考帧标成成功后再输出一串看似有效的结果。
            ref_matrix = np.eye(3, dtype=np.float64)
            if request.mode == TrackingMode.HOMOGRAPHY:
                ref_stability = check_homography_stability(
                    ref_matrix, pattern_ana,
                    max_condition=cfg.homography_max_condition,
                    min_area_ratio=cfg.homography_min_area_ratio,
                    max_area_ratio=cfg.homography_max_area_ratio,
                    side_view_ratio=cfg.homography_side_view_ratio,
                )
                if not ref_stability.stable:
                    transforms[request.reference_frame] = TrackTransform.failed(
                        request.reference_frame, FailureReason.DEGENERATE
                    )
                    for f in frames:
                        transforms[f] = TrackTransform.failed(
                            f, FailureReason.DEGENERATE
                        )
                    return TrackResult(
                        generation=request.generation,
                        cancelled=False,
                        transforms=transforms,
                        observations=observations,
                        diagnostics=diagnostics,
                    )
            # 形状掩码：归一化几何按分析分辨率光栅化（分辨率无关），
            # 特征点落在遮罩形状内部；光栅化失败退化为包围盒矩形
            shape_mask = None
            if request.shape_geometry:
                from src.shared.tracking.shape_mask import rasterize_shape_mask
                ana_h, ana_w = ref_sample.gray.shape[:2]
                shape_mask = rasterize_shape_mask(
                    request.shape_geometry, ana_w, ana_h)
            ref_pts = self.detector.detect(
                ref_sample.gray, pattern_ana, mask=shape_mask)
            transforms[request.reference_frame] = TrackTransform.from_matrix(
                request.reference_frame, ref_matrix, 1.0, TrackStatus.VALID
            )
            if len(ref_pts) < request.mode.min_points:
                # 特征不足：所有帧标记失败（不允许无解释的失败）
                for f in frames:
                    transforms[f] = TrackTransform.failed(
                        f, FailureReason.INSUFFICIENT_FEATURES
                    )
                return TrackResult(
                    generation=request.generation,
                    cancelled=False,
                    transforms=transforms,
                    observations=observations,
                    diagnostics=diagnostics,
                )

            # 分析坐标系 → 原始分辨率的输出换算（单应性共轭）
            S = ref_sample.scale_matrix()
            S_inv = np.linalg.inv(S)

            # 点链状态：
            # - prev_pts：上一帧中的点（LK 逐帧推进）
            # - anchor_pts：锚帧中的对应点（几何求解基准，与 prev_pts 同序号对应）
            # - anchor_H：锚帧坐标 → 参考帧坐标（重锚时为单位阵；
            #   每帧求解出「锚→当前」后左乘得到「参考→当前」）
            anchor_pts = ref_pts.astype(np.float32)
            anchor_H = np.eye(3, dtype=np.float64)
            prev_pts = anchor_pts.copy()
            prev_H = np.eye(3, dtype=np.float64)
            prev_gray = ref_sample.gray

            # 参考模板（外观门控与模板重捕获共用）：
            # (模板图, 模板局部→分析坐标的单应性, 分析坐标矩形)
            ref_template = self._ref_template(ref_sample.gray, pattern_ana)
            appearance_template = ref_template

            criteria = (
                cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                cfg.lk_max_iter,
                cfg.lk_epsilon,
            )
            win_size = (cfg.lk_win_size, cfg.lk_win_size)
            gap_since_valid = 0  # 距上次非失败帧的间隔（限制重检测重捕获窗口）
            policy = cfg.failure_policy
            recapture_enabled = policy == FailurePolicy.RECAPTURE
            last_valid_ana: Optional[np.ndarray] = None  # 最后一次有效估计（分析坐标系）
            last_valid_conf = 0.0
            last_valid_frame: Optional[int] = None

            for done, frame in enumerate(frames):
                if cancel_token is not None and bool(
                    getattr(cancel_token, "is_set", lambda: False)()
                ):
                    cancelled = True
                    break

                cur_sample = sampler.read_gray(frame, request.quality)
                cur_gray = cur_sample.gray

                # search 区域按上一帧估计预测位置，用于模糊检测与越界检查
                search_rect_pred = self._transform_rect(search_ana, prev_H)
                sharpness = self._region_sharpness(cur_gray, search_rect_pred)

                step = self._step_frame(
                    request=request,
                    frame=frame,
                    prev_gray=prev_gray,
                    cur_gray=cur_gray,
                    prev_pts=prev_pts,
                    anchor_pts=anchor_pts,
                    anchor_H=anchor_H,
                    prev_H=prev_H,
                    search_rect_pred=search_rect_pred,
                    pattern_ana=pattern_ana,
                    pattern_area=pattern_area,
                    sharpness=sharpness,
                    allow_redetect=(
                        recapture_enabled
                        and gap_since_valid <= cfg.redetect_max_gap
                    ),
                    allow_recapture=(
                        recapture_enabled
                        and gap_since_valid <= cfg.recapture_max_gap
                    ),
                    ref_gray=ref_sample.gray,
                    ref_template=ref_template,
                    appearance_template=appearance_template,
                    criteria=criteria,
                    win_size=win_size,
                )

                if step.transform.status == TrackStatus.FAILED:
                    gap_since_valid += 1
                    # After a lost gap, reacquisition must establish the
                    # original target, not trust an adapted pre-gap patch.
                    appearance_template = ref_template
                else:
                    gap_since_valid = 0
                    last_valid_ana = step.transform.matrix_np
                    last_valid_conf = step.transform.confidence
                    last_valid_frame = frame
                    if self._can_update_appearance(step, sharpness, request.mode):
                        appearance_template = self._aligned_appearance_template(
                            cur_gray, step.transform.matrix_np, ref_template,
                        )

                if (
                    step.transform.status == TrackStatus.FAILED
                    and policy == FailurePolicy.HOLD_LAST
                    and last_valid_ana is not None
                ):
                    # 保持上一帧：沿用最后一次有效变换，但诚实标记为弱跟踪（不假有效）
                    # 置信度按与最后有效帧的时间距离衰减（与处理方向无关）
                    hold_gap = (
                        abs(frame - last_valid_frame)
                        if last_valid_frame is not None
                        else gap_since_valid
                    )
                    held_conf = min(last_valid_conf, cfg.weak_confidence) * (
                        cfg.hold_confidence_decay ** max(1, hold_gap)
                    )
                    transforms[frame] = TrackTransform.from_matrix(
                        frame,
                        S @ last_valid_ana @ S_inv,
                        max(0.0, min(1.0, held_conf)),
                        TrackStatus.WEAK,
                        step.transform.failure_reason,
                    )
                else:
                    transforms[frame] = TrackTransform.from_matrix(
                        frame,
                        S @ step.transform.matrix_np @ S_inv,
                        step.transform.confidence,
                        step.transform.status,
                        step.transform.failure_reason,
                    )
                diagnostics[frame] = FrameDiagnostics(
                    frame=frame,
                    point_count=step.point_count,
                    inlier_count=step.inlier_count,
                    inlier_ratio=step.inlier_ratio,
                    fb_pass_ratio=step.fb_pass_ratio,
                    spread=step.spread,
                    reproj_error=step.reproj_error,
                    confidence=step.confidence,
                    sharpness=sharpness,
                    condition_number=step.condition_number,
                    appearance_similarity=step.appearance_similarity,
                )
                if step.observations is not None:
                    observations[frame] = tuple(
                        self._to_source_observation(o, scale) for o in step.observations
                    )

                if step.cur_pts is not None:
                    # 点链推进：只保留有效点，参考位置经锚变换同步更新（控制累计漂移）
                    prev_pts = step.cur_pts.astype(np.float32)
                    prev_gray = cur_gray
                    if step.anchor_H is not None:
                        # 本帧重新检测并重建了锚点（锚帧 = 当前帧）
                        anchor_pts = step.anchor_pts.astype(np.float32)
                        anchor_H = step.anchor_H
                    elif step.transform.status != TrackStatus.FAILED:
                        # 每帧重锚到当前帧：新锚点 = 本帧有效点（cur_subset），
                        # 「新锚→参考」复用本帧估计，避免求逆放大误差。
                        anchor_pts = step.cur_pts.astype(np.float32)
                        anchor_H = step.transform.matrix_np
                    if step.transform.status != TrackStatus.FAILED:
                        # 失败帧的矩阵是单位阵，不能用于位置预测，保留上次有效估计
                        prev_H = step.transform.matrix_np  # 分析坐标系

                if progress_cb is not None:
                    try:
                        progress_cb(done + 1, total)
                    except Exception:  # noqa: BLE001 - 回调异常不影响跟踪
                        logger.debug("progress 回调异常", exc_info=True)

                if (
                    step.transform.status == TrackStatus.FAILED
                    and policy == FailurePolicy.STOP
                ):
                    # 停止策略：首次失败即终止，后续帧无结果（按失败处理，不假有效）
                    break
        finally:
            if owns_sampler:
                sampler.close()

        return TrackResult(
            generation=request.generation,
            cancelled=cancelled,
            transforms=transforms,
            observations=observations,
            diagnostics=diagnostics,
        )

    # ================================================================
    # 单帧跟踪
    # ================================================================

    def _step_frame(
        self,
        request: TrackRequest,
        frame: int,
        prev_gray: np.ndarray,
        cur_gray: np.ndarray,
        prev_pts: np.ndarray,
        anchor_pts: np.ndarray,
        anchor_H: np.ndarray,
        prev_H: np.ndarray,
        search_rect_pred: Tuple[float, float, float, float],
        pattern_ana: Tuple[float, float, float, float],
        pattern_area: float,
        sharpness: float,
        allow_redetect: bool,
        allow_recapture: bool,
        ref_gray: np.ndarray,
        ref_template: Optional[Tuple[np.ndarray, np.ndarray, tuple]],
        criteria: tuple,
        win_size: Tuple[int, int],
        appearance_template: Optional[Tuple[np.ndarray, np.ndarray, tuple]] = None,
    ) -> "_FrameStep":
        """单帧：LK 光流 + forward-backward 检查 + 几何求解 + 五因子置信度。"""
        cfg = self.config
        mode = request.mode
        n = len(prev_pts)

        cur_pts_raw, status, _err = cv2.calcOpticalFlowPyrLK(
            prev_gray, cur_gray, prev_pts, None,
            winSize=win_size, maxLevel=cfg.lk_max_level, criteria=criteria,
        )
        tracked = (
            status.reshape(-1) == 1
            if status is not None
            else np.zeros(n, dtype=bool)
        )

        # ---- forward-backward 检查 ----
        fb_pass = np.zeros(n, dtype=bool)
        if cur_pts_raw is not None and np.any(tracked):
            back_pts, back_status, _ = cv2.calcOpticalFlowPyrLK(
                cur_gray, prev_gray, cur_pts_raw[tracked], None,
                winSize=win_size, maxLevel=cfg.lk_max_level, criteria=criteria,
            )
            if back_pts is not None and back_status is not None:
                ok = back_status.reshape(-1) == 1
                dist = np.linalg.norm(
                    back_pts[ok] - prev_pts[tracked][ok], axis=1
                )
                fb_idx = np.where(tracked)[0][ok]
                fb_pass[fb_idx[dist <= cfg.fb_max_distance]] = True

        h, w = cur_gray.shape[:2]
        in_frame = np.zeros(n, dtype=bool)
        in_search = np.zeros(n, dtype=bool)
        if cur_pts_raw is not None:
            in_frame = (
                (cur_pts_raw[:, 0] >= 0) & (cur_pts_raw[:, 0] < w)
                & (cur_pts_raw[:, 1] >= 0) & (cur_pts_raw[:, 1] < h)
            )
            in_search = self._points_in_rect(cur_pts_raw, search_rect_pred)
        # FB 通过是最强信号；越界只参与比例统计与失败分类，不逐点剔除
        valid = fb_pass & in_frame
        fb_pass_ratio = float(fb_pass.mean()) if n else 0.0
        out_fraction = (
            1.0 - float(in_search[tracked].mean()) if np.any(tracked) else 1.0
        )

        # ---- 有效点不足：失败 + 尝试重检测重捕获 ----
        if cur_pts_raw is None or int(valid.sum()) < mode.min_points:
            return self._fail_step(
                request=request, frame=frame, sharpness=sharpness,
                out_fraction=out_fraction, fb_weak=fb_pass_ratio < 0.5,
                solve_failed=False, tracked_count=int(tracked.sum()),
                fb_pass_ratio=fb_pass_ratio, allow_redetect=allow_redetect,
                allow_recapture=allow_recapture, ref_gray=ref_gray,
                # AUD-37404: 与其余失败路径一致地传参考模板——漏传时
                # 重捕获被迫每帧重建模板，低纹理 pattern 下重复开销。
                ref_template=ref_template,
                anchor_pts=anchor_pts, cur_gray=cur_gray,
                prev_H=prev_H, pattern_ana=pattern_ana,
            )

        # anchor_pts 与 prev_pts 同序号对应，按 valid 同步筛选
        anchor_subset = anchor_pts[valid]
        cur_subset = cur_pts_raw[valid]

        H_anchor_cur, inlier_mask = self._estimate(mode, anchor_subset, cur_subset)
        if H_anchor_cur is None:
            return self._fail_step(
                request=request, frame=frame, sharpness=sharpness,
                out_fraction=out_fraction, fb_weak=fb_pass_ratio < 0.5,
                solve_failed=True, tracked_count=int(tracked.sum()),
                fb_pass_ratio=fb_pass_ratio, allow_redetect=allow_redetect,
                allow_recapture=allow_recapture, ref_gray=ref_gray,
                ref_template=ref_template,
                anchor_pts=anchor_pts, cur_gray=cur_gray,
                prev_H=prev_H, pattern_ana=pattern_ana,
            )

        inlier_mask = (
            inlier_mask.astype(bool)
            if inlier_mask is not None
            else np.ones(len(anchor_subset), dtype=bool)
        )
        inlier_count = int(inlier_mask.sum())
        if inlier_count < mode.min_points:
            return self._fail_step(
                request=request, frame=frame, sharpness=sharpness,
                out_fraction=out_fraction, fb_weak=fb_pass_ratio < 0.5,
                solve_failed=True, tracked_count=int(tracked.sum()),
                fb_pass_ratio=fb_pass_ratio, allow_redetect=allow_redetect,
                allow_recapture=allow_recapture, ref_gray=ref_gray,
                ref_template=ref_template,
                anchor_pts=anchor_pts, cur_gray=cur_gray,
                prev_H=prev_H, pattern_ana=pattern_ana,
            )

        H_ref_cur = H_anchor_cur @ anchor_H

        # ---- 外观门控：候选矩阵反 warp 后与参考模板比对 ----
        # 几何求解自洽（LK/FB/内点全通过）不代表还在跟目标：遮挡物/背景
        # 同样可以被光流稳定跟踪，产生高置信度的错误矩阵（假有效）。
        appearance = self._appearance_similarity(
            cur_gray, H_ref_cur, ref_template
        )
        local_appearance = (
            self._appearance_similarity(cur_gray, H_ref_cur, appearance_template)
            if appearance_template is not None and appearance_template is not ref_template
            else appearance
        )
        if local_appearance >= cfg.appearance_update_min_similarity:
            appearance = max(appearance, local_appearance)
        if appearance < cfg.appearance_min_similarity:
            return self._fail_step(
                request=request, frame=frame, sharpness=sharpness,
                out_fraction=out_fraction, fb_weak=fb_pass_ratio < 0.5,
                solve_failed=False, tracked_count=int(tracked.sum()),
                fb_pass_ratio=fb_pass_ratio, allow_redetect=allow_redetect,
                allow_recapture=allow_recapture, ref_gray=ref_gray,
                ref_template=ref_template,
                anchor_pts=anchor_pts, cur_gray=cur_gray,
                prev_H=prev_H, pattern_ana=pattern_ana,
                appearance_similarity=appearance,
            )

        # ---- 单应性稳定性检查（阶段 8）：退化/极端透视停止并标记 ----
        condition_number = 0.0
        if mode == TrackingMode.HOMOGRAPHY:
            stability = check_homography_stability(
                H_ref_cur, pattern_ana,
                max_condition=cfg.homography_max_condition,
                min_area_ratio=cfg.homography_min_area_ratio,
                max_area_ratio=cfg.homography_max_area_ratio,
                side_view_ratio=cfg.homography_side_view_ratio,
            )
            condition_number = (
                stability.condition_number
                if math.isfinite(stability.condition_number) else 0.0
            )
            if not stability.stable:
                logger.debug(
                    "单应性不稳定（帧 %d）：%s", frame, stability.reason
                )
                return self._fail_step(
                    request=request, frame=frame, sharpness=sharpness,
                    out_fraction=out_fraction, fb_weak=fb_pass_ratio < 0.5,
                    solve_failed=True, tracked_count=int(tracked.sum()),
                    fb_pass_ratio=fb_pass_ratio, allow_redetect=allow_redetect,
                    allow_recapture=allow_recapture, ref_gray=ref_gray,
                    ref_template=ref_template,
                    anchor_pts=anchor_pts, cur_gray=cur_gray,
                    prev_H=prev_H, pattern_ana=pattern_ana,
                    reason_override=FailureReason.DEGENERATE,
                    condition_number=condition_number,
                )

        # ---- 五因子置信度评分 ----
        inlier_ratio = inlier_count / float(len(anchor_subset))
        points_factor = min(1.0, inlier_count / float(mode.recommended_points))
        spread = self._spread_factor(cur_subset[inlier_mask], pattern_area)
        error = self._reprojection_error(
            H_anchor_cur, anchor_subset, cur_subset, inlier_mask
        )
        error_factor = math.exp(-error / cfg.error_sigma)
        weights = (
            cfg.weight_points, cfg.weight_spread, cfg.weight_fb,
            cfg.weight_inlier, cfg.weight_error,
        )
        factors = (points_factor, spread, fb_pass_ratio, inlier_ratio, error_factor)
        w_sum = sum(weights)
        confidence = (
            sum(w_ * f for w_, f in zip(weights, factors)) / w_sum
            if w_sum > 0 else 0.0
        )

        if confidence < cfg.weak_confidence:
            return self._fail_step(
                request=request, frame=frame, sharpness=sharpness,
                out_fraction=out_fraction, fb_weak=fb_pass_ratio < 0.5,
                solve_failed=False, tracked_count=int(tracked.sum()),
                fb_pass_ratio=fb_pass_ratio, allow_redetect=allow_redetect,
                allow_recapture=allow_recapture, ref_gray=ref_gray,
                ref_template=ref_template,
                anchor_pts=anchor_pts, cur_gray=cur_gray,
                prev_H=prev_H, pattern_ana=pattern_ana,
            )

        result_status = (
            TrackStatus.VALID
            if confidence >= cfg.valid_confidence
            else TrackStatus.WEAK
        )
        transform = TrackTransform.from_matrix(
            frame, H_ref_cur, confidence, result_status
        )

        residuals = self._point_residuals(H_anchor_cur, anchor_subset, cur_subset)
        observations = [
            FeatureObservation(
                frame=frame,
                feature_id=int(i),
                x=float(cur_subset[i][0]),
                y=float(cur_subset[i][1]),
                error=float(residuals[i]),
                confidence=float(confidence),
                status=result_status if bool(inlier_mask[i]) else TrackStatus.WEAK,
            )
            for i in range(len(cur_subset))
        ]

        # ---- 重锚决策：内点过少时在当前位置重新检测，抑制长期漂移 ----
        redetect_floor = max(mode.min_points, self.detector.config.min_features)
        need_redetect = (
            inlier_ratio < cfg.reanchor_inlier_ratio
            or inlier_count < max(
                mode.min_points,
                int(mode.recommended_points * cfg.redetect_min_points_ratio),
            )
        )
        if need_redetect:
            predicted = self._transform_rect(pattern_ana, H_ref_cur)
            fresh = self.detector.detect(cur_gray, predicted)
            if len(fresh) >= redetect_floor:
                return _FrameStep(
                    transform=transform,
                    point_count=len(anchor_subset),
                    inlier_count=inlier_count,
                    inlier_ratio=inlier_ratio,
                    fb_pass_ratio=fb_pass_ratio,
                    spread=spread,
                    reproj_error=error,
                    confidence=confidence,
                    observations=observations,
                    cur_pts=fresh.astype(np.float32),
                    valid_mask=np.ones(len(fresh), dtype=bool),
                    tracked_count=int(tracked.sum()),
                    out_fraction=out_fraction,
                    H_anchor_cur=None,
                    anchor_pts=fresh,
                    anchor_H=H_ref_cur,
                    condition_number=condition_number,
                    appearance_similarity=appearance,
                    local_appearance_similarity=local_appearance,
                )

        return _FrameStep(
            transform=transform,
            point_count=len(anchor_subset),
            inlier_count=inlier_count,
            inlier_ratio=inlier_ratio,
            fb_pass_ratio=fb_pass_ratio,
            spread=spread,
            reproj_error=error,
            confidence=confidence,
            observations=observations,
            cur_pts=cur_subset.astype(np.float32),
            valid_mask=valid,
            tracked_count=int(tracked.sum()),
            out_fraction=out_fraction,
            H_anchor_cur=H_anchor_cur,
            anchor_pts=anchor_pts,
            anchor_H=None,
            condition_number=condition_number,
            appearance_similarity=appearance,
            local_appearance_similarity=local_appearance,
        )

    def _can_update_appearance(
        self, step: "_FrameStep", sharpness: float, mode: TrackingMode,
    ) -> bool:
        """Accept a rolling template only after all motion checks succeeded."""
        cfg = self.config
        return bool(
            cfg.adaptive_appearance
            and step.transform.status == TrackStatus.VALID
            and step.local_appearance_similarity >= cfg.appearance_update_min_similarity
            and step.fb_pass_ratio >= cfg.appearance_update_min_fb_ratio
            and step.inlier_ratio >= cfg.appearance_update_min_inlier_ratio
            and step.inlier_count >= mode.recommended_points
            and sharpness >= cfg.blur_laplacian_min
        )

    @staticmethod
    def _aligned_appearance_template(
        gray: np.ndarray,
        transform: np.ndarray,
        reference: Optional[Tuple[np.ndarray, np.ndarray, tuple]],
    ) -> Optional[Tuple[np.ndarray, np.ndarray, tuple]]:
        """Sample an accepted frame in the unchanged reference template grid."""
        if reference is None:
            return None
        template, origin, rect = reference
        height, width = template.shape[:2]
        patch = cv2.warpPerspective(
            gray, np.linalg.inv(transform @ origin), (width, height),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
        )
        return patch, origin, rect

    def _fail_step(
        self,
        request: TrackRequest,
        frame: int,
        sharpness: float,
        out_fraction: float,
        fb_weak: bool,
        solve_failed: bool,
        tracked_count: int,
        fb_pass_ratio: float,
        allow_redetect: bool,
        allow_recapture: bool,
        ref_gray: np.ndarray,
        anchor_pts: np.ndarray,
        cur_gray: np.ndarray,
        prev_H: np.ndarray,
        pattern_ana: Tuple[float, float, float, float],
        ref_template: Optional[Tuple[np.ndarray, np.ndarray, tuple]] = None,
        reason_override: Optional[FailureReason] = None,
        condition_number: float = 0.0,
        appearance_similarity: float = 0.0,
    ) -> "_FrameStep":
        """构造失败帧结果；仅在目标应当可见时尝试重检测重捕获。

        重捕获约束（避免锚到背景/陈旧位置）：
        - 距上次成功帧太远（超过 redetect_max_gap）不重检测；
        - 预测区域模糊或大量越界不重检测（目标大概率不可见）。
        重锚成功时 anchor_H 取上次有效估计（新锚帧位置≈预测位置），
        使下一帧的「锚→当前」求解能继续合成出正确的「参考→当前」。

        局部重检测无法覆盖长间隔失败（预测位置已陈旧）时，改用模板匹配
        在整帧范围内重定位目标，并在命中位置重新检测特征点重建锚点。
        """
        cfg = self.config
        mode = request.mode
        reason = reason_override or self._classify_failure(
            sharpness, out_fraction, solve_failed, fb_weak
        )
        transform = TrackTransform.failed(frame, reason)

        can_recapture = (
            allow_redetect
            and sharpness >= cfg.blur_laplacian_min
            and out_fraction <= cfg.out_of_bounds_fraction
        )
        if can_recapture:
            predicted = self._transform_rect(pattern_ana, prev_H)
            fresh = self.detector.detect(cur_gray, predicted)
            floor = max(mode.min_points, self.detector.config.min_features)
            if len(fresh) >= floor:
                return _FrameStep(
                    transform=transform,
                    point_count=tracked_count,
                    inlier_count=0,
                    inlier_ratio=0.0,
                    fb_pass_ratio=fb_pass_ratio,
                    spread=0.0,
                    reproj_error=0.0,
                    confidence=0.0,
                    observations=None,
                    cur_pts=fresh.astype(np.float32),
                    valid_mask=np.ones(len(fresh), dtype=bool),
                    tracked_count=tracked_count,
                    out_fraction=out_fraction,
                    H_anchor_cur=None,
                    anchor_pts=fresh,
                    anchor_H=prev_H,
                    condition_number=condition_number,
                    appearance_similarity=appearance_similarity,
                )

        # ---- 模板重捕获：长遮挡/出画后目标可能已远离陈旧预测位置 ----
        if allow_recapture and ref_gray is not None:
            recaptured = self._template_recapture(
                request, cur_gray, ref_gray, pattern_ana,
                ref_template=ref_template,
            )
            if recaptured is not None:
                fresh, recapture_H = recaptured
                return _FrameStep(
                    transform=transform,
                    point_count=tracked_count,
                    inlier_count=0,
                    inlier_ratio=0.0,
                    fb_pass_ratio=fb_pass_ratio,
                    spread=0.0,
                    reproj_error=0.0,
                    confidence=0.0,
                    observations=None,
                    cur_pts=fresh,
                    valid_mask=np.ones(len(fresh), dtype=bool),
                    tracked_count=tracked_count,
                    out_fraction=out_fraction,
                    H_anchor_cur=None,
                    anchor_pts=fresh,
                    anchor_H=recapture_H,
                    condition_number=condition_number,
                    appearance_similarity=appearance_similarity,
                )

        return _FrameStep(
            transform=transform,
            point_count=tracked_count,
            inlier_count=0,
            inlier_ratio=0.0,
            fb_pass_ratio=fb_pass_ratio,
            spread=0.0,
            reproj_error=0.0,
            confidence=0.0,
            observations=None,
            cur_pts=None,
            valid_mask=np.zeros(len(anchor_pts), dtype=bool),
            tracked_count=tracked_count,
            out_fraction=out_fraction,
            H_anchor_cur=None,
            anchor_pts=anchor_pts,
            anchor_H=None,
            condition_number=condition_number,
            appearance_similarity=appearance_similarity,
        )

    # ================================================================
    # 模板重捕获（阶段 7）
    # ================================================================

    @staticmethod
    def _ref_template(
        ref_gray: np.ndarray,
        pattern_ana: Tuple[float, float, float, float],
    ) -> Optional[Tuple[np.ndarray, np.ndarray, tuple]]:
        """提取参考帧 pattern 模板。

        返回 (模板图, 模板局部→分析坐标单应性, 分析坐标矩形)；
        以下情形返回 None（外观门控显式禁用）：
        - 模板任一边小于 12 像素（比对无统计意义）；
        - 模板纹理能量过低（近常量）：NCC 的 0/0 被 OpenCV 规约为
          满分 1.0，门控形同虚设，宁可显式无门控也不给伪满分。
        """
        ih, iw = ref_gray.shape[:2]
        x, y, w, h = pattern_ana
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1 = min(iw, int(round(x + w)))
        y1 = min(ih, int(round(y + h)))
        if x1 - x0 < 12 or y1 - y0 < 12:
            return None
        template = ref_gray[y0:y1, x0:x1]
        texture_var = float(cv2.Laplacian(template, cv2.CV_64F).var())
        if texture_var < TEMPLATE_MIN_TEXTURE_VAR:
            logger.debug(
                "参考模板纹理能量过低（Laplacian var=%.4f < %.2f），"
                "外观门控禁用", texture_var, TEMPLATE_MIN_TEXTURE_VAR,
            )
            return None
        T = np.eye(3, dtype=np.float64)
        T[0, 2], T[1, 2] = float(x0), float(y0)
        return template, T, (float(x0), float(y0), float(x1 - x0), float(y1 - y0))

    @staticmethod
    def _appearance_similarity(
        cur_gray: np.ndarray,
        H_ref_cur: np.ndarray,
        ref_template: Optional[Tuple[np.ndarray, np.ndarray, tuple]],
    ) -> float:
        """候选位姿的外观校验：当前帧 pattern 区域反 warp 回模板坐标系做 NCC。

        候选矩阵把目标内容正确对齐时分数接近 1；目标被遮挡/覆盖或
        跟踪漂移到背景时分数接近 0。模板不可用（过小或低纹理近常量，
        NCC 无统计意义）时不设门控（返回 1）。
        """
        if ref_template is None:
            return 1.0
        template, T, _rect = ref_template
        th, tw = template.shape[:2]
        ch, cw = cur_gray.shape[:2]
        if tw >= cw or th >= ch:
            return 1.0
        try:
            M_fwd = H_ref_cur.astype(np.float64) @ T      # 模板局部 → 当前帧
            M = np.linalg.inv(M_fwd)                       # warpPerspective 取逆
            patch = cv2.warpPerspective(
                cur_gray, M, (tw, th),
                flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
            )
            score = cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED)
            return float(score[0][0])
        except cv2.error:
            return 0.0

    def _template_recapture(
        self,
        request: TrackRequest,
        cur_gray: np.ndarray,
        ref_gray: np.ndarray,
        pattern_ana: Tuple[float, float, float, float],
        ref_template: Optional[Tuple[np.ndarray, np.ndarray, tuple]] = None,
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """模板匹配重捕获：整帧搜索参考帧的 pattern 内容。

        用于遮挡/出画等长间隔失败（预测位置陈旧，局部重检测够不到目标）。
        匹配得分达标后在命中位置重新检测特征点重建锚点；本帧仍记失败，
        由后续帧的正常管线（LK + FB + 求解 + 外观门控）验证重捕获的一致性。
        返回 (新锚点, 「参考帧→锚帧」变换)，未命中返回 None。
        """
        cfg = self.config
        if ref_template is None:
            ref_template = self._ref_template(ref_gray, pattern_ana)
        if ref_template is None:
            return None
        template, _T, rect = ref_template
        x0, y0 = int(rect[0]), int(rect[1])
        tw, th = template.shape[1], template.shape[0]
        ch, cw = cur_gray.shape[:2]
        if th >= ch or tw >= cw:
            return None
        scores = cv2.matchTemplate(cur_gray, template, cv2.TM_CCOEFF_NORMED)
        _min_val, score, _min_loc, max_loc = cv2.minMaxLoc(scores)
        if score < cfg.recapture_template_threshold:
            return None
        candidate = (float(max_loc[0]), float(max_loc[1]), float(tw), float(th))
        fresh = self.detector.detect(cur_gray, candidate)
        floor = max(request.mode.min_points, self.detector.config.min_features)
        if len(fresh) < floor:
            return None
        # 「参考帧→锚帧」平移估计：裁剪中心 → 匹配中心（分析坐标）
        anchor_H = np.eye(3, dtype=np.float64)
        anchor_H[0, 2] = (max_loc[0] + tw / 2.0) - (x0 + x0 + tw) / 2.0
        anchor_H[1, 2] = (max_loc[1] + th / 2.0) - (y0 + y0 + th) / 2.0
        return fresh.astype(np.float32), anchor_H

    # ================================================================
    # 几何求解（按模式选择）
    # ================================================================

    def _estimate(
        self,
        mode: TrackingMode,
        src_pts: np.ndarray,
        dst_pts: np.ndarray,
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """返回 (3x3 矩阵（分析坐标系）, 内点掩码)；失败返回 (None, None)。"""
        cfg = self.config
        src = src_pts.astype(np.float32).reshape(-1, 1, 2)
        dst = dst_pts.astype(np.float32).reshape(-1, 1, 2)

        if mode == TrackingMode.TRANSLATION:
            return self._estimate_translation(src_pts, dst_pts)

        if mode == TrackingMode.TRANSLATION_ROTATION_SCALE:
            M, mask = cv2.estimateAffinePartial2D(
                src, dst, method=cv2.RANSAC,
                ransacReprojThreshold=cfg.ransac_reproj_threshold,
                confidence=cfg.ransac_confidence,
            )
        elif mode == TrackingMode.AFFINE:
            M, mask = cv2.estimateAffine2D(
                src, dst, method=cv2.RANSAC,
                ransacReprojThreshold=cfg.ransac_reproj_threshold,
                confidence=cfg.ransac_confidence,
            )
        else:  # HOMOGRAPHY（CAMERA_3D 不在本期范围）
            H, mask = cv2.findHomography(
                src, dst, cv2.RANSAC,
                ransacReprojThreshold=cfg.ransac_reproj_threshold,
                confidence=cfg.ransac_confidence,
            )
            if H is None:
                return None, None
            return H.astype(np.float64), mask.reshape(-1)

        if M is None:
            return None, None
        H = np.eye(3, dtype=np.float64)
        H[:2, :] = M.astype(np.float64)
        return H, mask.reshape(-1) if mask is not None else None

    def _estimate_translation(
        self, src_pts: np.ndarray, dst_pts: np.ndarray
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """纯平移：位移中值 + 阈值内点筛选（对离群点稳健）。"""
        cfg = self.config
        disp = dst_pts.astype(np.float64) - src_pts.astype(np.float64)
        median = np.median(disp, axis=0)
        residual = np.linalg.norm(disp - median, axis=1)
        mask = residual <= cfg.ransac_reproj_threshold
        if int(mask.sum()) < 1:
            return None, None
        median = np.median(disp[mask], axis=0)
        H = np.eye(3, dtype=np.float64)
        H[0, 2], H[1, 2] = float(median[0]), float(median[1])
        return H, mask

    # ================================================================
    # 辅助
    # ================================================================

    def _frame_sequence(self, request: TrackRequest) -> list[int]:
        ref = request.reference_frame
        if request.direction == TrackDirection.FORWARD:
            return list(range(ref + 1, request.frame_end + 1))
        return list(range(ref - 1, request.frame_start - 1, -1))

    def _classify_failure(
        self,
        sharpness: float,
        out_fraction: float,
        solve_failed: bool,
        fb_weak: bool = False,
    ) -> FailureReason:
        """失败原因分类（按证据强度排序）。"""
        cfg = self.config
        if sharpness < cfg.blur_laplacian_min:
            return FailureReason.BLUR
        if out_fraction > cfg.out_of_bounds_fraction:
            return FailureReason.OUT_OF_BOUNDS
        if solve_failed:
            return FailureReason.SOLVE_FAILED
        if fb_weak:
            return FailureReason.OCCLUSION
        # 锐利且在界内但点对应崩坏 → 视为遮挡（目标内容被覆盖/替换）
        return FailureReason.OCCLUSION

    @staticmethod
    def _scale_rect(
        rect: Tuple[float, float, float, float], scale: float
    ) -> Tuple[float, float, float, float]:
        x, y, w, h = rect
        return (x * scale, y * scale, w * scale, h * scale)

    @staticmethod
    def _transform_rect(
        rect: Tuple[float, float, float, float], H: np.ndarray
    ) -> Tuple[float, float, float, float]:
        """把矩形四角经单应性变换后取外接正矩形（保守估计）。"""
        x, y, w, h = rect
        corners = np.array(
            [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float64
        )
        pts = cv2.perspectiveTransform(corners.reshape(1, -1, 2), H).reshape(-1, 2)
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        return (float(x0), float(y0), float(x1 - x0), float(y1 - y0))

    @staticmethod
    def _points_in_rect(
        pts: np.ndarray, rect: Tuple[float, float, float, float]
    ) -> np.ndarray:
        x, y, w, h = rect
        return (
            (pts[:, 0] >= x) & (pts[:, 0] <= x + w)
            & (pts[:, 1] >= y) & (pts[:, 1] <= y + h)
        )

    @staticmethod
    def _region_sharpness(
        gray: np.ndarray, rect: Tuple[float, float, float, float]
    ) -> float:
        """区域拉普拉斯方差（模糊检测指标）。"""
        h, w = gray.shape[:2]
        x0 = max(0, min(int(rect[0]), w - 1))
        y0 = max(0, min(int(rect[1]), h - 1))
        x1 = max(0, min(int(rect[0] + rect[2]), w))
        y1 = max(0, min(int(rect[1] + rect[3]), h))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return 0.0
        crop = gray[y0:y1, x0:x1]
        return float(cv2.Laplacian(crop, cv2.CV_64F).var())

    @staticmethod
    def _spread_factor(pts: np.ndarray, pattern_area: float) -> float:
        """内点外接矩形面积 / pattern 面积（点位分布因子）。"""
        if len(pts) < 2:
            return 0.0
        mins = pts.min(axis=0)
        maxs = pts.max(axis=0)
        area = float((maxs[0] - mins[0]) * (maxs[1] - mins[1]))
        return max(0.0, min(1.0, area / max(1.0, pattern_area)))

    @staticmethod
    def _point_residuals(
        H: np.ndarray, src_pts: np.ndarray, dst_pts: np.ndarray
    ) -> np.ndarray:
        """逐点重投影残差（分析像素）。"""
        pts = src_pts.astype(np.float64).reshape(-1, 1, 2)
        proj = cv2.perspectiveTransform(pts, H).reshape(-1, 2)
        return np.linalg.norm(proj - dst_pts.astype(np.float64), axis=1)

    @staticmethod
    def _reprojection_error(
        H: np.ndarray,
        src_pts: np.ndarray,
        dst_pts: np.ndarray,
        inlier_mask: np.ndarray,
    ) -> float:
        if not np.any(inlier_mask):
            return float("inf")
        pts = src_pts[inlier_mask].astype(np.float64).reshape(-1, 1, 2)
        proj = cv2.perspectiveTransform(pts, H).reshape(-1, 2)
        return float(np.linalg.norm(proj - dst_pts[inlier_mask], axis=1).mean())

    @staticmethod
    def _to_source_observation(
        obs: FeatureObservation, scale: float
    ) -> FeatureObservation:
        """分析坐标观测 → 原始分辨率坐标。"""
        inv = 1.0 / scale if scale > 0 else 1.0
        return FeatureObservation(
            frame=obs.frame,
            feature_id=obs.feature_id,
            x=obs.x * inv,
            y=obs.y * inv,
            error=obs.error * inv if obs.error != float("inf") else obs.error,
            confidence=obs.confidence,
            status=obs.status,
        )


@dataclass
class _FrameStep:
    """单帧跟踪的中间结果（内部使用）。"""

    transform: TrackTransform
    point_count: int
    inlier_count: int
    inlier_ratio: float
    fb_pass_ratio: float
    spread: float
    reproj_error: float
    confidence: float
    observations: Optional[list[FeatureObservation]]
    cur_pts: Optional[np.ndarray]          # 成功/重捕获时供下一帧继续跟踪的点（分析坐标）
    valid_mask: np.ndarray                 # 与输入点链等长的有效掩码（点链同步用）
    tracked_count: int                     # LK 成功跟踪的点数（诊断）
    out_fraction: float                    # 越界点比例（诊断）
    H_anchor_cur: Optional[np.ndarray]     # 成功且未重锚时的锚→当前变换（诊断用）
    anchor_pts: np.ndarray                 # 当前锚点（可能已重新检测）
    anchor_H: Optional[np.ndarray]         # 非 None 表示本帧重建了锚点（新锚→参考帧的估计）
    condition_number: float = 0.0          # 单应性模式条件数（阶段 8 诊断）
    appearance_similarity: float = 0.0     # 与参考模板的 NCC 相似度（外观门控）
    local_appearance_similarity: float = 0.0
