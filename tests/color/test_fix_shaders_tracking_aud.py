"""批 A（AUD-24700 / AUD-24701 / AUD-24904）修复回归测试。

- GLSL 部分做源码级断言（clamp 模式 / 邻域采样预处理是否存在），
  并用 Python 逐行镜像 shader 内的 rgb2hsv/hsv2rgb 公式验证
  「V 通道 = max 分量」的 HSV 往返对 HDR 高光的保幅度性质；
- tracking 部分用假 worker 验证取消超时后引用不被丢弃（不触发真实
  QThread 析构，只验证服务持有的强引用契约）。
"""

from __future__ import annotations

import re

import numpy as np
import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from src.features.color.shaders.shaders import RAW_VIEW_FRAGMENT
from src.features.color.tracking.tracker_2d import TrackDirection
from src.features.color.tracking.tracking_service import TrackingService
from src.features.color.tracking.types import TrackingMode
from tests.color import tracker_qt_support as tq

pytestmark = pytest.mark.usefixtures("qapp")


def _section(source: str, start_marker: str, end_marker: str) -> str:
    """截取两个注释标记之间的 GLSL 源码段。"""
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _strip_comments(glsl: str) -> str:
    """去掉 // 行注释，源码断言只针对可执行代码。"""
    return re.sub(r"//[^\n]*", "", glsl)


# ================================================================
# AUD-24700：饱和度/色彩增强路径不得 clamp(0,1)
# ================================================================


def _glsl_rgb2hsv(c: np.ndarray) -> np.ndarray:
    """逐行镜像 RAW_VIEW_FRAGMENT 中 rgb2hsv 的公式（非 clamp 版）。"""
    r, g, b = float(c[0]), float(c[1]), float(c[2])
    kx, ky, kz, kw = 0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0
    # vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g))
    t = 1.0 if g >= b else 0.0
    p = (1.0 - t) * np.array([b, g, kw, kz]) + t * np.array([g, b, kx, ky])
    # vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r))
    t2 = 1.0 if r >= p[0] else 0.0
    q = (
        (1.0 - t2) * np.array([p[0], p[1], p[3], r])
        + t2 * np.array([r, p[1], p[2], p[0]])
    )
    d = q[0] - min(q[3], q[1])
    e = 1.0e-10
    h = abs(q[2] + (q[3] - q[1]) / (6.0 * d + e))
    s = d / (q[0] + e)
    v = q[0]
    return np.array([h, s, v])


def _glsl_hsv2rgb(c: np.ndarray) -> np.ndarray:
    """逐行镜像 RAW_VIEW_FRAGMENT 中 hsv2rgb 的公式。"""
    h, s, v = float(c[0]), float(c[1]), float(c[2])
    k = np.array([1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0])
    # vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www)
    p = np.abs(((h + k[:3]) % 1.0) * 6.0 - k[3])
    # c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y)
    return v * (1.0 + (np.clip(p - 1.0, 0.0, 1.0) - 1.0) * s)


def _apply_saturation_block(
    color: np.ndarray, saturation: float = 1.0, boost: float = 0.0
) -> np.ndarray:
    """镜像修复后的第 6 步：保幅度 HSV 分离 + 负值加回。"""
    color = np.asarray(color, dtype=np.float64)
    negative_excess = np.minimum(color, 0.0)
    hsv = _glsl_rgb2hsv(color - negative_excess)
    if saturation != 1.0:
        hsv[1] *= saturation
    if boost != 0.0:
        v_mask = 1.0 - hsv[1]
        hsv[1] += hsv[1] * v_mask * (boost / 100.0)
    return _glsl_hsv2rgb(hsv) + negative_excess


class TestAud24700HdrSafeSaturation:
    HDR_COLORS = [
        (2.0, 1.0, 0.5),
        (8.0, 4.0, 4.0),
        (0.2, 1.7, 0.05),
        (12.5, 0.0, 3.25),
    ]
    NEGATIVE_COLORS = [
        (-0.3, -0.1, -0.05),
        (-0.05, 0.1, 0.2),
        (0.5, -0.2, 0.1),
    ]

    def _saturation_block(self) -> str:
        return _section(
            RAW_VIEW_FRAGMENT, "// 6. Saturation", "// 7. Color Space"
        )

    def test_source_no_clamp_in_saturation_block(self) -> None:
        code = _strip_comments(self._saturation_block())
        assert "rgb2hsv(clamp(" not in code
        assert "clamp(" not in code, (
            "饱和度路径不得出现 clamp（AUD-24700 铁律：HDR 禁止无理由 "
            "clamp(0,1)）"
        )

    def test_source_negative_excess_handling(self) -> None:
        block = self._saturation_block()
        assert "min(color, vec3(0.0))" in block
        assert "color = hsv2rgb(hsv) + negative_excess" in block

    def test_neutral_params_is_identity_for_hdr_and_negatives(self) -> None:
        """saturation=1 且 boost=0 时整段是恒等变换（旧实现会 clamp）。"""
        for color in self.HDR_COLORS + self.NEGATIVE_COLORS:
            result = _apply_saturation_block(color)
            np.testing.assert_allclose(
                result, color, atol=1e-9,
                err_msg=f"中性参数下必须是恒等变换: {color}",
            )

    def test_saturation_preserves_hdr_amplitude(self) -> None:
        """降饱和后 max 分量（高光幅度）必须保持 >1，不被截断。"""
        for color in self.HDR_COLORS:
            result = _apply_saturation_block(color, saturation=0.5)
            assert result.max() == pytest.approx(max(color), rel=1e-9), (
                f"饱和度调整丢失高光幅度: {color} -> {result}"
            )
            assert result.max() > 1.0, f"HDR 高光被截断: {color} -> {result}"

    def test_negative_values_survive_saturation(self) -> None:
        """全负像素（lift 可产生）在 HSV 往返中不得被夹成 0。"""
        color = (-0.3, -0.1, -0.05)
        result = _apply_saturation_block(color, saturation=0.5)
        np.testing.assert_allclose(result, color, atol=1e-9)

    def test_saturation_reduces_chroma_keeps_hue_direction(self) -> None:
        color = np.array([2.0, 1.0, 0.5])
        result = _apply_saturation_block(color, saturation=0.5)
        # 通道序（R>G>B 的色调方向）保持，色度收窄但比例方向不变
        assert result[0] > result[1] > result[2]


# ================================================================
# AUD-24701：锐化/中间细节邻域采样必须与中心像素同色彩空间
# ================================================================


class TestAud24701NeighborhoodColorSpace:
    def test_all_neighborhood_samples_use_prepared_fetch(self) -> None:
        assert RAW_VIEW_FRAGMENT.count("get_linear_color(fetch_prepared(") == 8, (
            "锐化(4) + 中间细节(4) 的 8 次邻域采样必须走 fetch_prepared"
        )

    def test_no_raw_texture_sample_bypasses_preparation(self) -> None:
        assert "get_linear_color(texture(" not in RAW_VIEW_FRAGMENT, (
            "邻域采样不得绕过色彩空间预处理直接进 get_linear_color"
        )

    def test_center_pixel_uses_same_prepare_path(self) -> None:
        assert (
            "vec3 color = prepare_color_space(tex_color.rgb);"
            in RAW_VIEW_FRAGMENT
        )

    def test_prepare_helper_applies_swizzle_and_srgb_prophoto(self) -> None:
        match = re.search(
            r"vec3 prepare_color_space\(vec3 c\) \{.*?\n        \}",
            RAW_VIEW_FRAGMENT,
            re.S,
        )
        assert match is not None, "缺少 prepare_color_space 帮助函数"
        body = match.group(0)
        assert "u_bgr_swizzle" in body, "预处理必须包含 BGR swizzle"
        assert "srgb_to_linear" in body, "预处理必须包含 sRGB -> 线性"
        assert "SRGB_TO_PROPHOTO" in body, "预处理必须包含 ProPhoto 矩阵"

    def test_prophoto_matrix_defined_once_no_local_duplicate(self) -> None:
        assert RAW_VIEW_FRAGMENT.count("SRGB_TO_PROPHOTO") == 2, (
            "矩阵应为单一 const 定义 + helper 内一次使用"
        )
        assert "mat3 srgb_to_prophoto" not in RAW_VIEW_FRAGMENT, (
            "main 中不得再保留局部矩阵定义（避免两份矩阵漂移）"
        )


# ================================================================
# AUD-24904：取消超时后不得丢弃运行中 worker 的最后引用
# ================================================================


class StuckFakeWorker(QObject):
    """可控假 worker：wait() 结果与运行状态由测试驱动。"""

    progress = pyqtSignal(int, int, int)
    finished_with_result = pyqtSignal(int, object)
    failed_with_error = pyqtSignal(int, str)

    def __init__(self, request: object) -> None:
        super().__init__()
        self.request = request
        self.started = False
        self.cancel_called = False
        self.wait_result = True
        self.running = False

    def start(self) -> None:
        self.started = True
        self.running = True

    def request_cancel(self) -> None:
        self.cancel_called = True

    def wait(self, ms: int = 0) -> bool:
        return self.wait_result

    def isRunning(self) -> bool:
        return self.running


def _make_service(workers: list) -> TrackingService:
    def factory(request: object) -> StuckFakeWorker:
        worker = StuckFakeWorker(request)
        workers.append(worker)
        return worker

    return TrackingService(worker_factory=factory)


def _start(service: TrackingService) -> int:
    return service.start(
        clip_id="clip1", shape_id="shape1", media_path=tq.MEDIA,
        pattern_region=tq.PATTERN, search_region=tq.SEARCH,
        reference_frame=62, frame_start=0, frame_end=124,
        mode=TrackingMode.TRANSLATION_ROTATION_SCALE,
        directions=(TrackDirection.FORWARD,),
    )


class TestAud24904ZombieWorkerKeepAlive:
    def test_cancel_timeout_retains_worker_reference(self, qapp) -> None:
        workers: list = []
        service = _make_service(workers)
        _start(service)
        workers[0].wait_result = False  # wait() 超时：线程仍在运行
        service.cancel("clip1", "shape1")
        assert workers[0].cancel_called
        assert not service.is_running("clip1", "shape1")
        # AUD-24904 核心：超时未退出的 worker 必须仍被强引用持有，
        # 否则 GC 析构运行中的 QThread 会导致进程 Abort
        assert workers[0] in service._zombie_workers

    def test_zombie_released_once_exited(self, qapp) -> None:
        workers: list = []
        service = _make_service(workers)
        _start(service)
        workers[0].wait_result = False
        service.cancel("clip1", "shape1")
        assert service._zombie_workers, "超时 worker 应进入保活列表"
        # worker 最终自行退出后，下一次取消/收尾时被回收
        workers[0].running = False
        _start(service)  # start -> _cancel_workers -> _prune_zombie_workers
        assert service._zombie_workers == []

    def test_normal_cancel_never_creates_zombie(self, qapp) -> None:
        workers: list = []
        service = _make_service(workers)
        _start(service)
        service.cancel("clip1", "shape1")  # wait() 返回 True：正常退出
        assert service._zombie_workers == []

    def test_shutdown_timeout_also_retains_reference(self, qapp) -> None:
        workers: list = []
        service = _make_service(workers)
        _start(service)
        workers[0].wait_result = False
        service.shutdown()
        assert workers[0] in service._zombie_workers
        assert service.running_keys() == []
