"""WD17-G97 修复验证：batch-373 / batch-374 shared-tracking 审计发现。

覆盖（每条含正例与反例）：
- AUD-37300  ClipFrameMapper.timeline_to_source_frame 边界开区间
- AUD-37302  build_corner_pin_export 矩阵有限性防护
- AUD-37306  分桶均匀化截断按桶轮转（不整桶丢弃）
- AUD-37312  redetect_reference_points 特征点 ID 跨检测语义
- AUD-37403  低纹理参考模板显式禁用外观门控
- AUD-37410  缓存键包含 shape_geometry
"""

from __future__ import annotations

import dataclasses

import cv2
import numpy as np
import pytest

from src.shared.tracking import point_manager
from src.shared.tracking.coordinate_map import ClipFrameMapper
from src.shared.tracking.corner_pin import build_corner_pin_export
from src.shared.tracking.feature_detector import DetectorConfig, FeatureDetector
from src.shared.tracking.frame_sampler import AnalysisQuality
from src.shared.tracking.point_manager import (
    active_observations,
    redetect_reference_points,
)
from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.tracker_2d import (
    TEMPLATE_MIN_TEXTURE_VAR,
    TrackDirection,
    TrackRequest,
    Tracker2D,
)
from src.shared.tracking.tracking_cache import TrackingCache
from src.shared.tracking.types import (
    FeatureObservation,
    TrackStatus,
    TrackTransform,
    TrackingMode,
)


# ================================================================
# AUD-37300：timeline_to_source_frame 边界闭开区间
# ================================================================


class TestAud37300TimelineBoundary:
    MAPPER = ClipFrameMapper(source_start=100, source_end=199, timeline_start=40)

    def test_last_valid_frame_maps_to_source_end(self):
        """正例：timeline_duration-1（最后一个有效帧）仍可映射。"""
        assert self.MAPPER.timeline_duration == 100
        assert self.MAPPER.timeline_to_source_frame(139) == 199
        assert self.MAPPER.timeline_to_source_frame(40) == 100

    def test_timeline_end_rejected(self):
        """反例：timeline_start + duration（旧代码放行并返回 source_end+1）。"""
        with pytest.raises(ValueError):
            self.MAPPER.timeline_to_source_frame(140)

    def test_before_start_rejected(self):
        with pytest.raises(ValueError):
            self.MAPPER.timeline_to_source_frame(39)

    def test_far_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            self.MAPPER.timeline_to_source_frame(200)

    def test_clip_local_end_rejected(self):
        """clip_local == timeline_duration 同样越界（委托同一校验）。"""
        with pytest.raises(ValueError):
            self.MAPPER.clip_local_to_source_frame(100)
        assert self.MAPPER.clip_local_to_source_frame(99) == 199

    def test_double_speed_boundary(self):
        mapper = ClipFrameMapper(
            source_start=0, source_end=199, timeline_start=0, speed=2.0
        )
        assert mapper.timeline_duration == 100
        assert mapper.timeline_to_source_frame(99) == 198
        with pytest.raises(ValueError):
            mapper.timeline_to_source_frame(100)

    def test_parity_with_shared_timeline_clip(self):
        """与 shared TimelineClip 的开区间语义逐帧一致。"""
        from src.shared.media.timeline import TimelineClip

        clip = TimelineClip(
            source_start=100, source_end=199,
            timeline_start=40, timeline_end=140, speed=1.0,
        )
        for tf in range(40, 140):
            assert self.MAPPER.timeline_to_source_frame(tf) == (
                clip.timeline_to_source_frame(tf)
            )
        # 上界帧：两者都必须拒绝（修复前本类放行）
        with pytest.raises(ValueError):
            self.MAPPER.timeline_to_source_frame(140)
        with pytest.raises(ValueError):
            clip.timeline_to_source_frame(140)


# ================================================================
# AUD-37302：build_corner_pin_export 坏帧不炸整次导出
# ================================================================


def _export_binding() -> TrackingBinding:
    return TrackingBinding(
        binding_id="b1", clip_id="c1", shape_id="s1",
        source_frame_start=8, source_frame_end=10,
        reference_frame=10,
        pattern_region=(0.25, 0.25, 0.5, 0.5),
    )


def _transform(frame: int, matrix, status: TrackStatus = TrackStatus.VALID) -> TrackTransform:
    return TrackTransform.from_matrix(frame, np.asarray(matrix), 0.9, status)


def _nan_transform(frame: int) -> TrackTransform:
    nan = float("nan")
    return TrackTransform(
        frame=frame,
        matrix=(nan,) * 9,
        translation=(0.0, 0.0),
        rotation=0.0,
        scale=(1.0, 1.0),
        shear=(0.0, 0.0),
        confidence=0.5,
        status=TrackStatus.WEAK,
    )


class TestAud37302FiniteGuard:
    def test_nan_matrix_frame_marked_not_raised(self):
        """反例：单个 NaN 矩阵帧只标记自身，导出整体不抛异常。"""
        binding = _export_binding()
        binding.apply_result(_transform(8, np.eye(3)))
        binding.apply_result(_nan_transform(9))
        binding.apply_result(_transform(10, np.eye(3)))
        data = build_corner_pin_export(binding, 100, 100)
        entries = {e["frame"]: e for e in data["frames"]}
        assert set(entries) == {8, 9, 10}  # 坏帧不吞掉好帧
        bad = entries[9]
        assert bad["corners"] is None
        assert bad["valid_geometry"] is False
        assert "非有限" in bad["geometry_reason"]
        # 好帧不受影响
        assert entries[8]["corners"] is not None
        assert entries[8]["valid_geometry"] is True

    def test_inf_matrix_rejected(self):
        binding = _export_binding()
        inf = float("inf")
        binding.apply_result(
            _transform(9, np.array([[inf, 0, 0], [0, 1, 0], [0, 0, 1]]))
        )
        data = build_corner_pin_export(binding, 100, 100)
        entry = next(e for e in data["frames"] if e["frame"] == 9)
        assert entry["corners"] is None
        assert entry["valid_geometry"] is False

    def test_homogeneous_divzero_marked_not_raised(self):
        """有限矩阵但齐次除零：同样按坏帧标记（transform_corners 原抛 ValueError）。"""
        binding = _export_binding()
        H = np.array([[0, 0, 1], [0, 0, 1], [0, 0, 0]], dtype=np.float64)
        binding.apply_result(_transform(9, H))
        data = build_corner_pin_export(binding, 100, 100)
        entry = next(e for e in data["frames"] if e["frame"] == 9)
        assert entry["corners"] is None
        assert entry["valid_geometry"] is False
        assert entry["geometry_reason"]

    def test_valid_matrices_unchanged(self):
        """正例：正常矩阵仍输出四角与几何诊断（回归）。"""
        binding = _export_binding()
        binding.apply_result(_transform(10, np.eye(3)))
        binding.apply_result(
            _transform(11, np.array([[1, 0, 5], [0, 1, 0], [0, 0, 1]], float))
        )
        data = build_corner_pin_export(binding, 100, 100)
        entries = {e["frame"]: e for e in data["frames"]}
        assert entries[10]["corners"][0] == {"x": 25.0, "y": 25.0}
        assert entries[11]["corners"][0] == {"x": 30.0, "y": 25.0}
        assert entries[10]["valid_geometry"] is True

    def test_failed_status_entry_untouched(self):
        """FAILED 帧不走角点计算（既有 include_missing 语义保留）。"""
        from src.shared.tracking.types import FailureReason

        binding = _export_binding()
        binding.apply_result(
            TrackTransform.failed(9, FailureReason.OCCLUSION)
        )
        data = build_corner_pin_export(binding, 100, 100, include_missing=True)
        entry = next(e for e in data["frames"] if e["frame"] == 9)
        assert entry["status"] == "failed"
        assert entry["corners"] is None


# ================================================================
# AUD-37306：分桶截断按桶轮转
# ================================================================


def _detector(**overrides) -> FeatureDetector:
    return FeatureDetector(DetectorConfig(**overrides))


def _grid_select(detector: FeatureDetector, points, region=(0.0, 0.0, 100.0, 100.0)):
    x, y, w, h = region
    return detector._balance_by_grid(
        np.asarray(points, dtype=np.float32), x, y, w, h
    )


class TestAud37306RoundRobinTruncation:
    def test_weak_texture_bucket_not_dropped(self):
        """反例：强纹理桶独大时不再整桶丢弃弱纹理桶（旧代码 [A1..A4] 全来自 A）。"""
        detector = _detector(
            max_features=4, per_bucket_limit=12, grid_cols=8, grid_rows=6
        )
        # A 桶（(0,0) 格）：5 个强响应点；B 桶（右下格）：2 个弱响应点
        a_pts = [(1.0 + i * 0.5, 1.0) for i in range(5)]
        b_pts = [(95.0, 95.0), (96.0, 95.5)]
        out = _grid_select(detector, a_pts + b_pts)
        assert len(out) == 4
        xs = out[:, 0].tolist()
        assert any(x < 10 for x in xs), "A（强纹理）桶应有代表"
        assert any(x > 90 for x in xs), "B（弱纹理）桶不得被整桶丢弃"
        assert xs[0] < 10, "全局最强点仍最先入选（响应优先）"

    def test_every_nonempty_bucket_contributes(self):
        """limit >= 非空桶数时，每个非空桶至少贡献 1 个点。"""
        detector = _detector(
            max_features=5, per_bucket_limit=12, grid_cols=8, grid_rows=6
        )
        p1 = [(1.0, 1.0), (2.0, 1.5), (1.5, 2.0)]           # 左上格
        p2 = [(95.0, 1.0), (96.0, 1.5), (95.5, 2.0)]        # 右上格
        p3 = [(1.0, 95.0), (2.0, 95.5), (1.5, 96.0)]        # 左下格
        out = _grid_select(detector, p1 + p2 + p3)
        assert len(out) == 5
        xs, ys = out[:, 0], out[:, 1]
        assert ((xs < 10) & (ys < 10)).any()   # 左上格有代表
        assert ((xs > 90) & (ys < 10)).any()   # 右上格有代表
        assert ((xs < 10) & (ys > 90)).any()   # 左下格有代表

    def test_no_truncation_keeps_all(self):
        """正例：总量低于上限时不截断、不丢点。"""
        detector = _detector(max_features=240)
        out = _grid_select(detector, [(1.0, 1.0), (95.0, 1.0), (1.0, 95.0)])
        assert len(out) == 3

    def test_per_bucket_limit_still_enforced(self):
        """桶容量上限语义保留（桶内响应最强者优先）。"""
        detector = _detector(
            max_features=240, per_bucket_limit=2, grid_cols=8, grid_rows=6
        )
        pts = [(1.0 + i * 0.1, 1.0) for i in range(5)]
        out = _grid_select(detector, pts)
        assert len(out) == 2  # 单桶最多 2 个点
        assert out[0][0] == pytest.approx(1.0)  # 响应最强者优先

    def test_zero_limit_returns_empty_shaped(self):
        detector = _detector(max_features=0)
        out = _grid_select(detector, [(1.0, 1.0), (95.0, 95.0)])
        assert out.shape == (0, 2)

    def test_detect_end_to_end_spread(self):
        """端到端：真实角点图上 max_features 截断后各象限仍有分布。"""
        rng = np.random.default_rng(11)
        gray = np.zeros((200, 200), dtype=np.uint8)
        gray[:100, :100] = rng.integers(0, 255, (100, 100)).astype(np.uint8)
        gray[100:, 100:] = rng.integers(0, 255, (100, 100)).astype(np.uint8)
        # 其余区域保持平坦（无角点）
        detector = _detector(
            max_features=6, quality_level=0.05, min_distance=3.0
        )
        pts = detector.detect(gray, (0.0, 0.0, 200.0, 200.0))
        assert 0 < len(pts) <= 6
        if len(pts) >= 2:
            quadrants = {(int(p[1] >= 100), int(p[0] >= 100)) for p in pts}
            assert len(quadrants) >= 2, "两个纹理象限都应有点（均匀分布目标）"


# ================================================================
# AUD-37312：redetect 特征点 ID 跨检测语义
# ================================================================


class _StubSampler:
    """替代 FrameSampler 的桩：固定 scale=0.5（fresh 坐标 ×2 回源分辨率）。"""

    def __init__(self, media_path: str) -> None:
        self.media_path = media_path

    def read_gray(self, frame: int, quality: AnalysisQuality):
        import types

        return types.SimpleNamespace(
            gray=np.zeros((50, 50), dtype=np.uint8),
            scale=0.5,
            source_width=200,
            source_height=100,
        )

    def close(self) -> None:
        pass


class _StubDetector:
    """固定返回 3 个检测点（分析坐标，×2 后为源坐标）。"""

    def detect(self, gray, region, mask=None):
        return np.array(
            [[10.0, 12.0], [30.0, 40.0], [50.0, 60.0]], dtype=np.float32
        )


def _obs_at_ref(binding: TrackingBinding, ids_coords) -> None:
    binding.observations[binding.reference_frame] = tuple(
        FeatureObservation(
            frame=binding.reference_frame,
            feature_id=fid,
            x=float(x),
            y=float(y),
            error=0.2,
            confidence=0.8,
            status=TrackStatus.VALID,
        )
        for fid, x, y in ids_coords
    )


class TestAud37312RedetectIdSemantics:
    @pytest.fixture()
    def binding(self) -> TrackingBinding:
        b = TrackingBinding(
            binding_id="b1", clip_id="c1", shape_id="s1",
            reference_frame=62, pattern_region=(0.2, 0.2, 0.6, 0.6),
        )
        _obs_at_ref(
            b,
            [(0, 60.0, 30.0), (1, 70.0, 30.0), (2, 80.0, 40.0),
             (3, 60.0, 50.0), (4, 70.0, 50.0)],
        )
        b.lock_feature(0)
        b.lock_feature(3)
        b.exclude_feature(1)
        return b

    @pytest.fixture()
    def stub_sampler(self, monkeypatch):
        monkeypatch.setattr(point_manager, "FrameSampler", _StubSampler)

    def test_locked_observations_preserved(self, binding, stub_sampler):
        """正例：锁定点的旧观测原样保留（ID 与坐标不变，永不被删）。"""
        redetect_reference_points(
            binding, "media.mp4", detector=_StubDetector()
        )
        obs = binding.observations[binding.reference_frame]
        by_id = {o.feature_id: o for o in obs}
        assert 0 in by_id and by_id[0].x == 60.0 and by_id[0].y == 30.0
        assert 3 in by_id and by_id[3].x == 60.0 and by_id[3].y == 50.0

    def test_new_ids_do_not_collide_with_old(self, binding, stub_sampler):
        """反例：新点不得重新从 0 编号（旧排除 ID 会错位命中新点）。"""
        redetect_reference_points(
            binding, "media.mp4", detector=_StubDetector()
        )
        obs = binding.observations[binding.reference_frame]
        new_ids = [o.feature_id for o in obs if o.feature_id > 4]
        assert new_ids == [5, 6, 7]
        # 排除集合中的旧 ID 1 不会命中任何新点
        assert all(o.feature_id != 1 for o in obs)
        fresh = [o for o in obs if o.feature_id >= 5]
        assert [o.x for o in fresh] == [20.0, 60.0, 100.0]  # p * 1/scale
        assert [o.y for o in fresh] == [24.0, 80.0, 120.0]

    def test_return_count_and_lock_sets_unchanged(self, binding, stub_sampler):
        """锁定/排除集合本身不修改；返回值为保留 + 新检测总数。"""
        count = redetect_reference_points(
            binding, "media.mp4", detector=_StubDetector()
        )
        assert count == 2 + 3
        assert binding.locked_feature_ids == {0, 3}
        assert binding.excluded_feature_ids == {1}
        assert binding.result_version >= 1

    def test_active_observations_consistent_after_redetect(
        self, binding, stub_sampler
    ):
        """过滤语义：排除的旧 ID 不误伤任何新点。"""
        redetect_reference_points(
            binding, "media.mp4", detector=_StubDetector()
        )
        obs = binding.observations[binding.reference_frame]
        active = active_observations(obs, binding)
        assert len(active) == len(obs)  # 被排除的 ID 1 已不存在于观测集

    def test_no_prior_observations_starts_from_zero(self, stub_sampler):
        """兼容：无旧观测时仍从 0 编号（既有验收行为）。"""
        b = TrackingBinding(
            binding_id="b2", clip_id="c1", shape_id="s1",
            reference_frame=62, pattern_region=(0.2, 0.2, 0.6, 0.6),
        )
        count = redetect_reference_points(
            b, "media.mp4", detector=_StubDetector()
        )
        obs = b.observations[b.reference_frame]
        assert count == 3
        assert sorted(o.feature_id for o in obs) == [0, 1, 2]


# ================================================================
# AUD-37403：低纹理模板显式禁用外观门控
# ================================================================


class TestAud37403LowTextureTemplate:
    def test_flat_template_rejected(self):
        """正例：近常量模板返回 None（显式无门控路径）。"""
        flat = np.full((60, 80), 128, dtype=np.uint8)
        assert Tracker2D._ref_template(flat, (10.0, 10.0, 40.0, 40.0)) is None

    def test_nearly_flat_template_rejected(self):
        low_texture = np.full((60, 80), 128, dtype=np.uint8)
        low_texture[0, 0] = 130  # 单像素微扰：拉普拉斯方差仍趋近 0
        assert Tracker2D._ref_template(low_texture, (5.0, 5.0, 50.0, 50.0)) is None

    def test_textured_template_accepted(self):
        rng = np.random.default_rng(7)
        noisy = rng.integers(0, 255, (60, 80)).astype(np.uint8)
        tpl = Tracker2D._ref_template(noisy, (10.0, 10.0, 40.0, 40.0))
        assert tpl is not None
        template, T, rect = tpl
        assert template.shape == (40, 40)
        assert T[0, 2] == 10.0 and T[1, 2] == 10.0
        assert rect == (10.0, 10.0, 40.0, 40.0)

    def test_small_template_still_rejected(self):
        """既有尺寸防线保留（反例对照）。"""
        rng = np.random.default_rng(7)
        noisy = rng.integers(0, 255, (60, 80)).astype(np.uint8)
        assert Tracker2D._ref_template(noisy, (0.0, 0.0, 8.0, 8.0)) is None

    def test_flat_template_ncc_is_fake_perfect(self):
        """根因文档化：常量模板 vs 常量 patch 的 NCC 被规约为满分 1.0。"""
        flat = np.zeros((30, 30), np.uint8)
        score = cv2.matchTemplate(flat, flat, cv2.TM_CCOEFF_NORMED)
        assert float(score[0][0]) == 1.0

    def test_threshold_constant_reasonable(self):
        assert 0.0 < TEMPLATE_MIN_TEXTURE_VAR <= 5.0

    def test_gate_disabled_returns_one(self):
        """模板为 None 时显式无门控（返回 1），不产生伪满分诊断。"""
        cur = np.zeros((50, 50), np.uint8)
        assert Tracker2D._appearance_similarity(cur, np.eye(3), None) == 1.0

    def test_textured_gate_still_active(self):
        """正例：纹理模板的门控依旧有效——错误内容得分低于阈值。"""
        rng = np.random.default_rng(3)
        ref = rng.integers(0, 255, (60, 80)).astype(np.uint8)
        other = rng.integers(0, 255, (60, 80)).astype(np.uint8)
        tpl = Tracker2D._ref_template(ref, (10.0, 10.0, 40.0, 40.0))
        score = Tracker2D._appearance_similarity(other, np.eye(3), tpl)
        assert score < 0.45  # < appearance_min_similarity，遮挡/漂移被拦截

    def test_textured_gate_matches_reference(self):
        rng = np.random.default_rng(3)
        ref = rng.integers(0, 255, (60, 80)).astype(np.uint8)
        tpl = Tracker2D._ref_template(ref, (10.0, 10.0, 40.0, 40.0))
        score = Tracker2D._appearance_similarity(ref, np.eye(3), tpl)
        assert score > 0.99


# ================================================================
# AUD-37410：缓存键包含 shape_geometry
# ================================================================


CACHE_BASE = dict(
    fingerprint="fp-xyz",
    frame_start=0,
    frame_end=100,
    reference_frame=50,
    direction="forward",
    mode="trs",
    quality="draft",
    pattern=(0.3, 0.3, 0.4, 0.4),
    search=(0.2, 0.2, 0.6, 0.6),
)

GEO_ELLIPSE_A = {
    "type": "ellipse",
    "center": [0.5, 0.5],
    "radii": [0.2, 0.2],
}
# 同一包围盒（bbox）内改形状：pattern/search 区域不变
GEO_ELLIPSE_B = {
    "type": "ellipse",
    "center": [0.5, 0.5],
    "radii": [0.25, 0.18],   # x/y 半径互换 + 变化，bbox 仍相近
}
GEO_RECT = {"type": "rect", "x": 0.3, "y": 0.3, "w": 0.4, "h": 0.4}


def _request(shape_geometry) -> TrackRequest:
    return TrackRequest(
        media_path="m.mp4",
        pattern_region=CACHE_BASE["pattern"],
        search_region=CACHE_BASE["search"],
        reference_frame=50,
        frame_start=0,
        frame_end=100,
        mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
        direction=TrackDirection.FORWARD,
        quality=AnalysisQuality.DRAFT,
        shape_geometry=shape_geometry,
    )


class TestAud37410CacheKeyShapeGeometry:
    def test_geometry_change_changes_key(self):
        """反例：同包围盒内改形状不得命中旧键（修复前键相同）。"""
        k1 = TrackingCache.make_key(**CACHE_BASE, shape_geometry=GEO_ELLIPSE_A)
        k2 = TrackingCache.make_key(**CACHE_BASE, shape_geometry=GEO_ELLIPSE_B)
        assert k1 != k2
        k3 = TrackingCache.make_key(**CACHE_BASE, shape_geometry=GEO_RECT)
        assert k1 != k3 and k2 != k3

    def test_same_geometry_same_key(self):
        """正例：几何相同（含键序不同、元组/列表等价、浮点抖动）键稳定。"""
        geo_a = {"type": "ellipse", "center": (0.5, 0.5), "radii": (0.2, 0.2)}
        geo_b = {"radii": [0.2, 0.2000000001], "center": [0.5, 0.5], "type": "ellipse"}
        assert (
            TrackingCache.make_key(**CACHE_BASE, shape_geometry=geo_a)
            == TrackingCache.make_key(**CACHE_BASE, shape_geometry=geo_b)
        )

    def test_none_and_empty_distinct(self):
        """None 与空 dict 分别编码。"""
        k_none = TrackingCache.make_key(**CACHE_BASE)
        k_empty = TrackingCache.make_key(**CACHE_BASE, shape_geometry={})
        k_geo = TrackingCache.make_key(**CACHE_BASE, shape_geometry=GEO_RECT)
        assert len({k_none, k_empty, k_geo}) == 3

    def test_non_finite_and_unserializable_values_do_not_break_key(self):
        """键构造必须对任意几何可成功（含 NaN / 不可 JSON 化值）。"""
        weird = {"pts": [[float("nan"), 1.0]], "obj": object()}
        key = TrackingCache.make_key(**CACHE_BASE, shape_geometry=weird)
        assert isinstance(key, str) and key
        assert key != TrackingCache.make_key(**CACHE_BASE)

    def test_key_for_request_includes_geometry(self):
        """请求级入口：仅 shape_geometry 不同的两个请求键不同。"""
        fp = CACHE_BASE["fingerprint"]
        req_plain = _request(None)
        req_geo = _request(GEO_ELLIPSE_A)
        assert (
            TrackingCache.key_for_request(req_plain, fp)
            != TrackingCache.key_for_request(req_geo, fp)
        )
        # 与 make_key 的显式参数一致
        assert TrackingCache.key_for_request(req_geo, fp) == (
            TrackingCache.make_key(**CACHE_BASE, shape_geometry=GEO_ELLIPSE_A)
        )

    def test_stale_result_not_served_after_shape_edit(self):
        """端到端反例：改形状后 get 不得命中按旧形状缓存的旧结果。"""
        cache = TrackingCache()
        old_key = TrackingCache.key_for_request(_request(GEO_ELLIPSE_A), "fp")
        cache.put(old_key, {"stale": True})
        new_key = TrackingCache.key_for_request(_request(GEO_ELLIPSE_B), "fp")
        assert cache.get(new_key) is None
        assert cache.get(old_key) == {"stale": True}

    def test_geometry_edit_via_replace_only(self):
        """dataclasses.replace 修改 frozen 请求的几何后键变化。"""
        req = _request(GEO_ELLIPSE_A)
        req2 = dataclasses.replace(req, shape_geometry=GEO_RECT)
        assert (
            TrackingCache.key_for_request(req, "fp")
            != TrackingCache.key_for_request(req2, "fp")
        )
