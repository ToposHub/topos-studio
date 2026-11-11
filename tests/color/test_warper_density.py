# -*- coding: utf-8 -*-
"""CW-B07:密度云/代际守卫/节流/viewer 取色测试。"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.warper_density import (
    DensityResult,
    DensityThrottle,
    ViewState,
    accept_result,
    pick_color,
    sample_density,
    viewer_to_image,
)


def _patchwork(seed_colors, size: int = 256) -> np.ndarray:
    """确定性色块拼图:每个 seed 色一块。"""
    image = np.zeros((size, size, 3), np.float32)
    blocks = len(seed_colors)
    tile = size // blocks
    for index, color in enumerate(seed_colors):
        image[index * tile:(index + 1) * tile] = np.array(color, np.float32)
    return image


# ---- 采样 ----

def test_known_patches_land_in_expected_hs_cells() -> None:
    # 红(hue 0, sat 1)与青(hue 0.5, sat 1)两块
    image = _patchwork([(1, 0, 0), (0, 1, 1)])
    result = sample_density(image, generation=1, source="test")
    assert result.reason is None and result.points.shape[0] >= 2
    hues = result.points[:, 0]
    sats = result.points[:, 1]
    assert hues.min() == pytest.approx(0.0, abs=0.02)      # 红块落 hue≈0
    assert hues.max() == pytest.approx(0.5, abs=0.02)      # 青块落 hue≈0.5
    assert sats.min() > 0.9                                 # 两块都满饱和
    label = result.label()
    assert label["source"] == "test" and label["generation"] == 1
    assert label["sampled_pixels"] > 0


def test_black_gray_white_produce_no_chroma_noise() -> None:
    image = _patchwork([(0, 0, 0), (0.5, 0.5, 0.5), (1, 1, 1)])
    result = sample_density(image, generation=2, source="test")
    # 零色度政策:黑白灰不冒彩色密度
    assert result.points.shape[0] == 0
    assert result.reason in ("black", "uniform")
    # 全灰(非黑)应报 uniform 而不是 black
    gray = np.full((64, 64, 3), 0.5, np.float32)
    assert sample_density(gray, generation=3, source="t").reason == "uniform"
    black = np.zeros((64, 64, 3), np.float32)
    assert sample_density(black, generation=4, source="t").reason == "black"


def test_none_and_invalid_inputs_honest_empty() -> None:
    result = sample_density(None, generation=1, source="t")
    assert result.reason == "no_data" and result.points.shape[0] == 0
    bad = sample_density(np.zeros((4, 4), np.float32), generation=1, source="t")
    assert bad.reason == "no_data"


def test_sampling_deterministic_and_bounded() -> None:
    rng = np.random.default_rng(7)
    image = rng.random((512, 512, 3)).astype(np.float32)
    first = sample_density(image, generation=1, source="t", max_samples=4096)
    second = sample_density(image, generation=1, source="t", max_samples=4096)
    np.testing.assert_array_equal(first.points, second.points)
    np.testing.assert_array_equal(first.weights, second.weights)
    assert first.sampled_pixels <= 4096 * 2  # 二维 stride 的实际上界
    # 大图极限:百万像素也受采样上限约束
    big = rng.random((1200, 900, 3)).astype(np.float32)
    bounded = sample_density(big, generation=2, source="t", max_samples=8192)
    assert bounded.sampled_pixels <= 8192 * 2


# ---- 代际守卫 ----

def _empty(gen: int) -> DensityResult:
    return DensityResult(generation=gen, points=np.zeros((0, 2), np.float32),
                         weights=np.zeros((0,), np.float32), source="t",
                         frame=None, revision=None, colorspace="working",
                         sampled_pixels=0)


def test_generation_guard_rejects_late_results() -> None:
    current = _empty(5)
    assert accept_result(_empty(4), current) is None   # 旧的晚到 → 拒绝
    assert accept_result(_empty(6), current) is not None
    assert accept_result(_empty(5), current) is not None  # 同代可接受(重试)


# ---- 节流 ----

def test_playback_throttle_and_stop_refresh() -> None:
    throttle = DensityThrottle(interval=0.25)
    throttle.set_playing(True)
    clock = 100.0
    assert throttle.should_request(clock)               # 第一个允许
    assert not throttle.should_request(clock + 0.1)     # 间隔内拒绝
    assert throttle.should_request(clock + 0.3)         # 间隔后允许
    throttle.set_playing(False)                          # 停止 → 立即补一次
    assert throttle.pending_stop_refresh
    assert throttle.should_request(clock + 0.31)
    assert not throttle.pending_stop_refresh
    assert throttle.should_request(clock + 0.32)         # 非播放不节流


# ---- viewer 取色 ----

def _view(**kwargs) -> ViewState:
    base = dict(viewport_width=800, viewport_height=600, image_width=400,
                image_height=300, scale=2.0, offset_x=0.0, offset_y=0.0)
    base.update(kwargs)
    return ViewState(**base)


def test_viewer_mapping_identity_scale_and_zoom() -> None:
    assert viewer_to_image(0, 0, _view(scale=1.0)) == (0, 0)
    assert viewer_to_image(399, 299, _view(scale=1.0)) == (399, 299)
    # scale=2:视口 (400,300) → 图像 (200,150)
    assert viewer_to_image(400, 300, _view()) == (200, 150)
    assert viewer_to_image(799, 599, _view()) == (399, 299)


def test_viewer_mapping_pan_flip_crop_and_dpr() -> None:
    # 平移:图像左上角在视口 (100, 50)
    assert viewer_to_image(100, 50, _view(offset_x=100, offset_y=50)) == (0, 0)
    # 翻转:水平翻转后视口 x=0 对应图像最右列
    assert viewer_to_image(0, 0, _view(flip_h=True)) == (399, 0)
    # 裁剪:显示 crop(10, 20) 起,视口 (0,0) → 图像 (10, 20)
    assert viewer_to_image(0, 0, _view(crop_x=10, crop_y=20)) == (10, 20)
    # DPR=2:物理像素坐标先除 DPR
    assert viewer_to_image(800, 600, _view(dpr=2.0)) == (200, 150)
    # 组合:DPR2 + scale1
    assert viewer_to_image(20, 30, _view(scale=1.0, dpr=2.0)) == (10, 15)


def test_viewer_mapping_out_of_bounds_returns_none() -> None:
    assert viewer_to_image(-1, 0, _view(scale=1.0)) is None
    assert viewer_to_image(0, -1, _view(scale=1.0)) is None
    assert viewer_to_image(400, 0, _view(scale=1.0)) is None
    assert viewer_to_image(0, 300, _view(scale=1.0)) is None


def test_pick_color_nearest_no_transform() -> None:
    image = np.zeros((4, 4, 3), np.float32)
    image[2, 3] = (0.25, 0.5, 0.75)
    picked = pick_color(image, (3, 2))
    np.testing.assert_array_equal(picked, (0.25, 0.5, 0.75))
    assert pick_color(image, None) is None
    assert pick_color(image, (99, 0)) is None
    assert pick_color(None, (0, 0)) is None


def test_picker_round_trip_with_density_coordinates() -> None:
    # 取色 → 密度坐标一致性:同一像素的 (hue, sat) 落在其云图 bin
    image = np.zeros((64, 64, 3), np.float32)
    image[:] = (0.1, 0.9, 0.2)  # 绿色 sat=8/9, hue=1/3
    view = _view(viewport_width=128, viewport_height=128, image_width=64,
                 image_height=64, scale=2.0)
    position = viewer_to_image(64, 64, view)
    color = pick_color(image, position)
    result = sample_density(image, generation=1, source="viewer")
    from src.features.color.core.warper_density import _vectorized_hue, _vectorized_saturation
    hue = float(_vectorized_hue(color[None, :])[0])
    sat = float(_vectorized_saturation(color[None, :])[0])
    assert result.points.shape[0] == 1
    assert result.points[0, 0] == pytest.approx(hue, abs=0.02)
    assert result.points[0, 1] == pytest.approx(sat, abs=0.02)
