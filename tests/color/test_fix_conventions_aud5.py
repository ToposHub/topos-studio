"""约定统一回归测试（w5-g3：AUD-36200 / AUD-R2-001 / AUD-R2-002）。

锁定三条约定：

1. AUD-36200 —— WindowsHwPlanarSource 逐帧色彩标签必须来自流真实
   元数据（解码帧码流标记 → 容器探测 → bt709/limited 渲染缺省），
   绝不硬编码覆盖（HDR10/full-range 素材不得被按 BT.709/limited 解码）。
2. AUD-R2-001 —— ``build_curves_lut_from_data``（工程恢复路径）与
   ``CurvesWidget._compute_lut`` / ``scripting.api._interpolate_curve_255``
   （AUD-25900 修复后）同用倒置 Y 轴约定：LUT = (255 - y) / 255，
   identity = [(0, 255), (255, 0)]。
3. AUD-R2-002 —— 脚本示例曲线点必须用倒置 Y 约定（静态检查示例源码，
   防止旧语义示例回流）。

硬解路径无法在 macOS 真机运行，测试用与
tests/media/test_windows_hw_planar_source.py 相同的数据契约层手法
（__new__ 构造 + fake av frame + patch 平面提取）。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from src.shared.media.hwaccel import HardwareDecoder
from src.shared.media.windows_hw.hw_planar_source import (
    _DEFAULT_COLOR_TAGS,
    WindowsHwPlanarSource,
)


# =========================================================================
# AUD-R2-001：build_curves_lut_from_data 倒置 Y 约定
# =========================================================================


class TestBuildCurvesLutInvertedY:
    """工程恢复路径的 RGB 曲线 LUT 必须与 UI 约定一致。"""

    def test_ui_identity_curve_is_identity_lut(self):
        """UI 恒等线 [(0,255),(255,0)] → 恒等 LUT（修复前 y/255 会反相）。"""
        from src.features.color.core.layer import build_curves_lut_from_data

        lut = build_curves_lut_from_data({"M": [(0, 255), (255, 0)]})
        expected = np.arange(256, dtype=np.float32) / 255.0
        # M 通道写入 RGB 三份
        for ch in range(3):
            assert lut[:, 0, ch] == pytest.approx(expected, abs=1e-6)

    def test_old_forward_identity_now_inverts(self):
        """旧约定的 [(0,0),(255,255)] 现在产出反相 LUT（方向锁定）。"""
        from src.features.color.core.layer import build_curves_lut_from_data

        lut = build_curves_lut_from_data({"M": [(0, 0), (255, 255)]})
        assert lut[0, 0, 0] == pytest.approx(1.0, abs=1e-6)
        assert lut[255, 0, 0] == pytest.approx(0.0, abs=1e-6)

    def test_two_point_non_identity_follows_inverted_y(self):
        """2 点线性路径：y=127 → 输出 (255-127)/255 = 0.5。"""
        from src.features.color.core.layer import build_curves_lut_from_data

        lut = build_curves_lut_from_data({"M": [(0, 255), (255, 127)]})
        assert lut[0, 0, 0] == pytest.approx(0.0, abs=1e-6)
        assert lut[255, 0, 0] == pytest.approx((255 - 127) / 255.0, abs=1e-6)

    def test_brightening_midtones_above_half(self):
        """提亮中调示例 [(0,255),(128,100),(255,0)]：lut[128] > 0.5。"""
        from src.features.color.core.layer import build_curves_lut_from_data

        lut = build_curves_lut_from_data({"M": [(0, 255), (128, 100), (255, 0)]})
        assert lut[0, 0, 0] == pytest.approx(0.0, abs=1e-6)
        assert lut[255, 0, 0] == pytest.approx(1.0, abs=1e-6)
        assert lut[128, 0, 0] == pytest.approx((255 - 100) / 255.0, abs=0.02)

    def test_per_channel_inverted_y(self):
        """R/G/B 独立通道各自遵循倒置 Y（通道索引 R=0, G=1, B=2）。"""
        from src.features.color.core.layer import build_curves_lut_from_data

        lut = build_curves_lut_from_data({
            "R": [(0, 255), (255, 0)],   # 恒等
            "G": [(0, 0), (255, 255)],   # 反相
            "B": [(0, 255), (255, 0)],   # 恒等
        })
        identity = np.arange(256, dtype=np.float32) / 255.0
        assert lut[:, 0, 0] == pytest.approx(identity, abs=1e-6)
        assert lut[0, 0, 1] == pytest.approx(1.0, abs=1e-6)
        assert lut[255, 0, 1] == pytest.approx(0.0, abs=1e-6)
        assert lut[:, 0, 2] == pytest.approx(identity, abs=1e-6)

    def test_matches_scripting_api_interpolation(self):
        """与 AUD-25900 修复后的 _interpolate_curve_255 数值一致
        （同一组控制点在两条路径产出相同 LUT）。"""
        from src.features.color.core.layer import build_curves_lut_from_data
        from src.features.scripting.api import _interpolate_curve_255

        pts = [(0, 255), (64, 197), (128, 120), (192, 55), (255, 0)]
        lut = build_curves_lut_from_data({"M": pts})
        api_lut = _interpolate_curve_255(pts)
        # 控制点覆盖 [0,255]，两条路径在控制点区间内应逐点一致
        assert lut[:, 0, 0] == pytest.approx(api_lut, abs=1e-4)


# =========================================================================
# AUD-R2-002：示例曲线点必须用倒置 Y 约定
# =========================================================================


class TestExampleCurvePointsConvention:
    _EXAMPLES_DIR = Path(__file__).resolve().parents[2] / \
        "src/features/scripting/examples"

    def _example(self, name: str) -> str:
        path = self._EXAMPLES_DIR / name
        assert path.exists(), f"missing example: {path}"
        return path.read_text(encoding="utf-8")

    def test_advanced_workflow_curve_uses_inverted_y(self):
        """advanced_workflow.py 的曲线点必须是 UI 倒置约定：
        恒等线 [(0,255),(255,0)] 形态（首点 y=255、末点 y=0）。"""
        src = self._example("advanced_workflow.py")
        match = re.search(
            r"topos\.set_curves\(\s*master\s*=\s*\[(.*?)\]", src, re.S)
        assert match, "advanced_workflow.py must keep a set_curves example"
        points = re.findall(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", match.group(1))
        assert len(points) >= 2, "curve example needs >= 2 points"
        assert points[0] == ("0", "255"), (
            "first point must be (0, 255): identity line, y=0 is top/white")
        assert points[-1] == ("255", "0"), (
            "last point must be (255, 0): identity line end")

    def test_advanced_workflow_documents_inverted_axis(self):
        """示例必须注释说明 y 轴是 UI 倒置坐标（防止旧语义回流）。"""
        src = self._example("advanced_workflow.py")
        lowered = src.lower()
        assert "inverted" in lowered and "identity" in lowered, (
            "example should document the inverted Y axis and identity curve")

    def test_no_forward_identity_curve_in_examples(self):
        """examples/ 全目录不得再出现旧正向恒等线 [(0,0),(255,255)]。"""
        offenders: list[str] = []
        for py in sorted(self._EXAMPLES_DIR.glob("*.py")):
            text = py.read_text(encoding="utf-8")
            for bad in re.findall(r"\(\s*0\s*,\s*0\s*\)\s*,[^\]]*?\(\s*255\s*,\s*255\s*\)", text):
                offenders.append(f"{py.name}: {bad}")
        assert not offenders, f"old forward-y curves remain: {offenders}"


# =========================================================================
# AUD-36200：硬解 planar 帧色彩标签来自流真实元数据
# =========================================================================


class _FakeAvFrame:
    """最小 PyAV VideoFrame 替身（含 PyAV 17 唯一暴露 color_* 的表面）。"""

    def __init__(
        self,
        fmt: str = "nv12",
        width: int = 8,
        height: int = 8,
        pts: int = 42,
        color_primaries: int = 2,   # FFmpeg: unspecified
        color_trc: int = 2,         # unspecified
        colorspace: int = 2,        # unspecified
        color_range: int = 0,       # unspecified
    ):
        self.format = SimpleNamespace(name=fmt)
        self.width = width
        self.height = height
        self.pts = pts
        self.planes = [SimpleNamespace(index=i) for i in range(2)]
        self.color_primaries = color_primaries
        self.color_trc = color_trc
        self.colorspace = colorspace
        self.color_range = color_range


def _make_convert_source(metadata: object = None,
                         probe_tags: dict | None = None) -> WindowsHwPlanarSource:
    """与 tests/media/test_windows_hw_planar_source.py 相同的契约层构造。"""
    src = WindowsHwPlanarSource.__new__(WindowsHwPlanarSource)
    src._path = "fake.mp4"
    src._hw_decoder = HardwareDecoder.NVDEC
    src._hw_active = True
    src._hw_output_format = "nv12"
    src._current_frame = 7
    src._metadata = metadata if metadata is not None else object()
    src._video_stream = SimpleNamespace(time_base=None)
    src._is_open = True
    src._container = None
    src._decoder_buffer = []
    src._frame_iterator = None
    src._pending_av_frame = None
    if probe_tags is not None:
        src._probe_color_tags = dict(probe_tags)
    return src


def _convert(src: WindowsHwPlanarSource, frame: _FakeAvFrame):
    y = np.zeros((8, 8), dtype=np.uint8)
    uv = np.zeros((4, 8), dtype=np.uint8)
    with patch.object(
        src, "_extract_visible_plane_data", side_effect=[y, uv],
    ):
        return src._convert_frame_planar(frame)


class TestHwPlanarColorTags:
    """修复前：每帧硬编码 bt709/limited，屏蔽真实流色彩元数据。"""

    def test_frame_hdr10_tags_are_used(self):
        """解码帧携带 HDR10 标记（bt2020/smpte2084/bt2020nc/full）→
        必须原样透出，不得按 bt709/limited 硬编码（修复前的缺陷）。"""
        src = _make_convert_source()
        frame = _FakeAvFrame(
            color_primaries=9,   # bt2020
            color_trc=16,        # smpte2084 (PQ)
            colorspace=9,        # bt2020nc
            color_range=2,       # full
        )
        decoded = _convert(src, frame)
        assert decoded.color_primaries == "bt2020"
        assert decoded.color_transfer == "smpte2084"
        assert decoded.color_matrix == "bt2020nc"
        assert decoded.color_range == "full"

    def test_frame_bt601_full_tags_are_used(self):
        """SD full-range 素材（smpte170m/full）也不得被 limited 覆盖。"""
        src = _make_convert_source()
        frame = _FakeAvFrame(
            color_primaries=6,   # smpte170m
            color_trc=6,         # smpte170m
            colorspace=6,        # smpte170m (BT.601)
            color_range=2,       # full
        )
        decoded = _convert(src, frame)
        assert decoded.color_matrix == "smpte170m"
        assert decoded.color_range == "full"

    def test_unspecified_frame_falls_back_to_probe_metadata(self):
        """帧未标记（unspecified=2/0）→ 回退容器探测 metadata 值。"""
        md = SimpleNamespace(
            color_primaries="bt2020",
            color_transfer="smpte2084",
            color_matrix="bt2020nc",
            color_range=None,
        )
        src = _make_convert_source(metadata=md)
        src._probe_color_tags = src._resolve_probe_color_tags()
        decoded = _convert(src, _FakeAvFrame())
        assert decoded.color_primaries == "bt2020"
        assert decoded.color_transfer == "smpte2084"
        assert decoded.color_matrix == "bt2020nc"
        # range 未标记 → 渲染缺省 limited
        assert decoded.color_range == "limited"

    def test_all_unspecified_falls_back_to_renderer_defaults(self):
        """帧与探测均未标记 → 渲染器缺省 bt709/limited（既有契约保持）。"""
        md = SimpleNamespace(
            color_primaries=None, color_transfer=None,
            color_matrix=None, color_range=None,
        )
        src = _make_convert_source(metadata=md)
        src._probe_color_tags = src._resolve_probe_color_tags()
        decoded = _convert(src, _FakeAvFrame())
        assert decoded.color_range == "limited"
        assert decoded.color_matrix == "bt709"
        assert decoded.color_primaries == "bt709"
        assert decoded.color_transfer == "bt709"

    def test_unknown_normalized_values_treated_as_unspecified(self):
        """探测 metadata 的 'unknown' 值视为未标记 → 渲染缺省。"""
        md = SimpleNamespace(
            color_primaries="unknown", color_transfer="unknown",
            color_matrix="unknown", color_range="unknown",
        )
        src = _make_convert_source(metadata=md)
        tags = src._resolve_probe_color_tags()
        assert tags == _DEFAULT_COLOR_TAGS

    def test_no_probe_tags_attr_still_safe(self):
        """__new__ 等路径未带 _probe_color_tags 时不崩，落渲染缺省
        （与既有 tests/media 契约测试的构造方式兼容）。"""
        src = _make_convert_source()
        assert not hasattr(src, "_probe_color_tags")
        decoded = _convert(src, _FakeAvFrame())
        assert decoded.color_range == "limited"
        assert decoded.color_matrix == "bt709"

    def test_resolve_probe_tags_normalizes_case(self):
        """探测值大小写/空白归一化（'BT709 ' → 'bt709'）。"""
        md = SimpleNamespace(
            color_primaries=" BT709 ", color_transfer="Smpte2084",
            color_matrix="BT2020NC", color_range="FULL",
        )
        src = _make_convert_source(metadata=md)
        tags = src._resolve_probe_color_tags()
        assert tags == {
            "color_primaries": "bt709",
            "color_transfer": "smpte2084",
            "color_matrix": "bt2020nc",
            "color_range": "full",
        }
