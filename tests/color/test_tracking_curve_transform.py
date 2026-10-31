"""阶段 6 测试：曲线蒙版整体变换与 rect 旋转/缩放映射。"""

import math

import numpy as np
import pytest

from src.features.color.tracking.tracking_evaluator import (
    apply_transform_to_geometry,
    capture_base_geometry,
)

FRAME = (1280, 720)


def translation_matrix(dx_px: float, dy_px: float) -> np.ndarray:
    return np.array([[1, 0, dx_px], [0, 1, dy_px], [0, 0, 1]], dtype=np.float64)


class TestCurveTransform:
    def _curve_base(self):
        return capture_base_geometry({
            "type": "curve", "center": [0.5, 0.5], "size": [0, 0],
            "rotation": 0.0,
            "points": [
                {"pos": [0.3, 0.3], "c1": [0.25, 0.3], "c2": [0.35, 0.3]},
                {"pos": [0.7, 0.3], "c1": [0.65, 0.3], "c2": [0.75, 0.3]},
                {"pos": [0.5, 0.7], "c1": [0.45, 0.7], "c2": [0.55, 0.7]},
            ],
        })

    def test_translation_moves_all_points_same_delta(self):
        base = self._curve_base()
        m = translation_matrix(64, 72)  # 归一化 (0.05, 0.1)
        result = apply_transform_to_geometry(base, m, FRAME)
        assert len(result["points"]) == 3
        for before, after in zip(base["points"], result["points"]):
            for key in ("pos", "c1", "c2"):
                assert after[key][0] == pytest.approx(before[key][0] + 0.05, abs=1e-9)
                assert after[key][1] == pytest.approx(before[key][1] + 0.1, abs=1e-9)

    def test_base_points_not_mutated(self):
        base = self._curve_base()
        apply_transform_to_geometry(base, translation_matrix(64, 72), FRAME)
        assert base["points"][0]["pos"] == [0.3, 0.3]

    def test_scale_transform_scales_coordinates(self):
        base = self._curve_base()
        # 以画面左上角为原点整体放大 2 倍
        m = np.diag([2.0, 2.0, 1.0])
        result = apply_transform_to_geometry(base, m, FRAME)
        assert result["points"][0]["pos"][0] == pytest.approx(0.6, abs=1e-9)
        assert result["points"][2]["c2"][1] == pytest.approx(1.4, abs=1e-9)


class TestRectTransform:
    def test_scale_doubles_size_keeps_rotation(self):
        base = capture_base_geometry({
            "type": "rect", "center": [0.25, 0.25], "size": [0.2, 0.1],
            "rotation": 0.0,
        })
        m = np.diag([2.0, 2.0, 1.0])
        result = apply_transform_to_geometry(base, m, FRAME)
        # 中心 (320,180) → (640,360) = 归一化 (0.5, 0.5)
        assert result["center"][0] == pytest.approx(0.5, abs=1e-9)
        assert result["center"][1] == pytest.approx(0.5, abs=1e-9)
        # 像素尺寸翻倍后归一化尺寸同样翻倍
        assert result["size"][0] == pytest.approx(0.4, abs=1e-9)
        assert result["size"][1] == pytest.approx(0.2, abs=1e-9)
        assert result["rotation"] == pytest.approx(0.0, abs=1e-6)

    def test_rotation_around_image_center(self):
        base = capture_base_geometry({
            "type": "rect", "center": [0.5, 0.5], "size": [0.2, 0.1],
            "rotation": 0.0,
        })
        # 绕画面中心旋转 90 度
        cx, cy = FRAME[0] / 2, FRAME[1] / 2
        r = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=np.float64)
        t = np.array([[1, 0, cx], [0, 1, cy], [0, 0, 1]], dtype=np.float64)
        t_inv = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]], dtype=np.float64)
        m = t @ r @ t_inv
        result = apply_transform_to_geometry(base, m, FRAME)
        # 中心不变，旋转 +90 度
        assert result["center"][0] == pytest.approx(0.5, abs=1e-9)
        assert result["center"][1] == pytest.approx(0.5, abs=1e-9)
        assert result["rotation"] == pytest.approx(90.0, abs=1e-6)
        # 旋转由 rotation 字段表达：局部宽高（顶边/左边长度）不变
        assert result["size"][0] == pytest.approx(0.2, abs=1e-9)
        assert result["size"][1] == pytest.approx(0.1, abs=1e-9)

    def test_preexisting_rotation_accumulates(self):
        base = capture_base_geometry({
            "type": "rect", "center": [0.5, 0.5], "size": [0.2, 0.1],
            "rotation": 30.0,
        })
        cx, cy = FRAME[0] / 2, FRAME[1] / 2
        angle = math.radians(15.0)
        rot = np.array([
            [math.cos(angle), -math.sin(angle), 0],
            [math.sin(angle), math.cos(angle), 0],
            [0, 0, 1],
        ])
        t = np.array([[1, 0, cx], [0, 1, cy], [0, 0, 1]])
        t_inv = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]])
        m = t @ rot @ t_inv
        result = apply_transform_to_geometry(base, m, FRAME)
        assert result["rotation"] == pytest.approx(45.0, abs=1e-6)
