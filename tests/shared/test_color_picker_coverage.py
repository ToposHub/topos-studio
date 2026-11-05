"""color_picker 补测（计划02 / agent-A4）。

覆盖 src/shared/color/color_picker.py 中可 headless 验证的部分：
1. 颜色空间转换数值正确性（sRGB transfer / 亮度 / WCAG / Lab / XYZ / P3）
2. 边界值（0/1、超界 clamp、无效输入回退、空历史点击）
3. picker 组件状态与信号（offscreen Qt：色块/滑块/输入框/历史/面板）
4. 序列化往返（swatch JSON 配置 save/load、ASE 二进制导出）

纯函数部分不依赖 QApplication；widget 部分依赖 tests/conftest.py 的 qapp
（QT_QPA_PLATFORM=offscreen）。所有涉及磁盘的用例均把 CONFIG_FILE_PATH
monkeypatch 到 tmp_path，避免读写真实用户配置。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

# 必须在创建 QApplication 之前设置 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from PyQt6.QtCore import QEvent, QPoint, QPointF, QSize, Qt, QByteArray
from PyQt6.QtGui import QColor, QCursor, QEnterEvent, QKeyEvent
from PyQt6.QtWidgets import QDialog

from src.shared.color import color_picker as cp
from src.shared.color.color_conversion import linear_to_srgb, srgb_to_linear

pytestmark = pytest.mark.color_core


# ============================================================
# fixtures
# ============================================================
@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把模块级 CONFIG_FILE_PATH 指向 tmp，保证配置读写隔离。"""
    path = tmp_path / "color_picker_swatches.json"
    monkeypatch.setattr(cp, "CONFIG_FILE_PATH", path)
    return path


class _PosEvent:
    """duck-typed 鼠标事件：只提供 pos()（mouseMoveEvent 全部用到的接口）。"""

    def __init__(self, x: int, y: int) -> None:
        self._point = QPoint(x, y)

    def pos(self) -> QPoint:
        return self._point


class _StubMimeData:
    """duck-typed QMimeData：dropEvent/dragEnterEvent 用到的子集。"""

    def __init__(self, fmt: str, payload: bytes) -> None:
        self._fmt = fmt
        self._payload = payload

    def hasFormat(self, fmt: str) -> bool:
        return fmt == self._fmt

    def data(self, fmt: str) -> QByteArray:
        return QByteArray(self._payload)


class _StubDropEvent:
    """duck-typed QDropEvent。"""

    def __init__(self, mime: _StubMimeData) -> None:
        self._mime = mime
        self.accepted = False

    def mimeData(self) -> _StubMimeData:
        return self._mime

    def acceptProposedAction(self) -> None:
        self.accepted = True


def _esc_event() -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)


# ============================================================
# 1. sRGB transfer 函数（纯数学）
# ============================================================
class TestTransferFunctions:
    def test_srgb_to_linear_known_values(self) -> None:
        assert cp.srgb_to_linear(0.0) == 0.0
        assert cp.srgb_to_linear(1.0) == 1.0
        # 分段边界：<= 0.04045 走线性段
        assert cp.srgb_to_linear(0.04045) == pytest.approx(0.04045 / 12.92, abs=1e-9)
        assert cp.srgb_to_linear(0.5) == pytest.approx(0.21404114, abs=1e-7)

    def test_srgb_to_linear_clamps_out_of_range(self) -> None:
        assert cp.srgb_to_linear(1.5) == 1.0
        assert cp.srgb_to_linear(-0.25) == 0.0
        assert cp.linearize_srgb(2.0) == 1.0
        assert cp.delinearize_srgb(-1.0) == 0.0

    def test_linear_srgb_roundtrip_grid(self) -> None:
        for i in range(21):
            v = i / 20.0
            assert linear_to_srgb(srgb_to_linear(v)) == pytest.approx(v, abs=1e-9)
            assert srgb_to_linear(linear_to_srgb(v)) == pytest.approx(v, abs=1e-9)

    def test_aliases_match_color_conversion(self) -> None:
        for v in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0):
            assert cp.linearize_srgb(v) == srgb_to_linear(v)
            assert cp.delinearize_srgb(v) == linear_to_srgb(v)

    def test_matrix_multiply_identity(self) -> None:
        ident = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        assert cp.matrix_multiply(ident, [2.0, 3.0, 4.0]) == [2.0, 3.0, 4.0]


# ============================================================
# 2. 相对亮度 / WCAG 对比度
# ============================================================
class TestLuminanceAndContrast:
    def test_relative_luminance_extremes(self) -> None:
        assert cp.get_relative_luminance(255, 255, 255) == pytest.approx(1.0, abs=1e-9)
        assert cp.get_relative_luminance(0, 0, 0) == 0.0

    def test_relative_luminance_gray_128(self) -> None:
        # 消色差颜色：亮度 = 单通道线性值
        assert cp.get_relative_luminance(128, 128, 128) == pytest.approx(0.21586, abs=1e-4)
        assert cp.get_relative_luminance(128, 128, 128) == pytest.approx(
            srgb_to_linear(128 / 255.0), abs=1e-9
        )

    def test_contrast_ratio_black_white(self) -> None:
        black, white = QColor(0, 0, 0), QColor(255, 255, 255)
        assert cp.get_contrast_ratio(black, white) == pytest.approx(21.0, abs=1e-9)
        # 对称性
        assert cp.get_contrast_ratio(white, black) == pytest.approx(21.0, abs=1e-9)

    def test_contrast_ratio_same_color_is_one(self) -> None:
        c = QColor(31, 77, 200)
        assert cp.get_contrast_ratio(c, QColor(c)) == pytest.approx(1.0, abs=1e-9)

    def test_contrast_ratio_aa_boundary_color(self) -> None:
        # #767676 是白底上 AA(4.5) 的经典临界灰
        ratio = cp.get_contrast_ratio(QColor(0x76, 0x76, 0x76), QColor(255, 255, 255))
        assert ratio == pytest.approx(4.54, abs=0.05)

    @pytest.mark.parametrize(
        "ratio,expected_level,expected_color",
        [
            (21.0, "AAA", "#22C55E"),
            (7.0, "AAA", "#22C55E"),       # 边界包含
            (6.99, "AA", "#3B82F6"),
            (4.5, "AA", "#3B82F6"),        # 边界包含
            (4.49, "AA Large", "#F59E0B"),
            (3.0, "AA Large", "#F59E0B"),  # 边界包含
            (2.99, "Fail", "#EF4444"),
            (1.0, "Fail", "#EF4444"),
        ],
    )
    def test_wcag_level_boundaries(
        self, ratio: float, expected_level: str, expected_color: str
    ) -> None:
        level, color = cp.get_wcag_level(ratio)
        assert level == expected_level
        assert color == expected_color


class TestFindMinimumContrast:
    def test_dark_text_against_white(self) -> None:
        fixed, ratio, new_l = cp.find_minimum_l_for_contrast(QColor(120, 120, 120), 4.5, True)
        assert fixed is not None
        assert ratio >= 4.5
        assert 0 <= new_l <= 100
        assert cp.get_contrast_ratio(fixed, QColor(255, 255, 255)) >= 4.5

    def test_light_text_against_black(self) -> None:
        fixed, ratio, new_l = cp.find_minimum_l_for_contrast(QColor(60, 60, 60), 4.5, False)
        assert fixed is not None
        assert ratio >= 4.5
        assert new_l >= 0
        assert cp.get_contrast_ratio(fixed, QColor(0, 0, 0)) >= 4.5

    def test_unreachable_target_returns_none(self) -> None:
        fixed, ratio, new_l = cp.find_minimum_l_for_contrast(QColor(128, 128, 128), 100.0, True)
        assert fixed is None
        assert ratio == 0
        assert new_l == 0


# ============================================================
# 3. Lab / XYZ / 色彩空间转换
# ============================================================
class TestLabConversion:
    @pytest.mark.parametrize(
        "rgb,expected",
        [
            ((255, 0, 0), (53, 80, 67)),
            ((0, 255, 0), (87, -86, 83)),
            ((255, 255, 255), (100, 0, 0)),
            ((0, 0, 0), (0, 0, 0)),
        ],
    )
    def test_rgb_to_lab_known_values(
        self, rgb: tuple[int, int, int], expected: tuple[int, int, int]
    ) -> None:
        assert cp.rgb_to_lab(*rgb) == expected

    def test_lab_to_rgb_extremes(self) -> None:
        assert cp.lab_to_rgb(0, 0, 0) == (0, 0, 0)
        r, g, b = cp.lab_to_rgb(100, 0, 0)
        assert (r, g, b) == (255, 255, 254)  # 量化后 b 差 1

    def test_lab_roundtrip_within_quantization(self) -> None:
        # rgb→Lab 取整 + Lab→rgb 取整，双重量化误差最大 3（绿色实测差 3）
        for rgb in [(255, 0, 0), (0, 255, 0), (128, 128, 128), (200, 100, 50)]:
            out = cp.lab_to_rgb(*cp.rgb_to_lab(*rgb))
            for got, want in zip(out, rgb):
                assert abs(got - want) <= 3, f"{rgb} -> {out}"

    def test_lab_blue_roundtrip_large_error(self) -> None:
        # 现状行为（精度缺陷，见 BUGS-agent-A4.md BUG-A4-05）：
        # 饱和蓝 (0,0,255) 的 Lab 往返误差达 10/255 ≈ 4%。
        # 本测试固化当前数值，修复后应改为小容差断言。
        lab = cp.rgb_to_lab(0, 0, 255)
        assert lab == (32, 79, -107)
        assert cp.lab_to_rgb(*lab) == (10, 0, 252)


class TestXyzAndColorSpace:
    def test_white_srgb_to_xyz(self) -> None:
        x, y, z = cp.rgb_to_xyz_linear(255, 255, 255, "sRGB")
        assert x == pytest.approx(0.95047, abs=1e-4)   # D65 白点
        assert y == pytest.approx(1.0, abs=1e-4)
        assert z == pytest.approx(1.08883, abs=1e-4)

    @pytest.mark.parametrize("channel", [128, 200])
    def test_xyz_roundtrip_gray(self, channel: int) -> None:
        xyz = cp.rgb_to_xyz_linear(channel, channel, channel, "sRGB")
        out = cp.xyz_linear_to_rgb(xyz[0], xyz[1], xyz[2], "sRGB")
        for got in out:
            assert abs(got - channel) <= 1

    def test_xyz_linear_to_rgb_clamps(self) -> None:
        # 超界正向输入被 clamp 到 1.0；因 1.055-0.055 的浮点截断会得到 254
        # 而不是 255（见 BUGS-agent-A4.md BUG-A4-04）
        hi = cp.xyz_linear_to_rgb(10.0, 10.0, 10.0, "sRGB")
        assert all(254 <= c <= 255 for c in hi)
        assert cp.xyz_linear_to_rgb(-1.0, -1.0, -1.0, "sRGB") == (0, 0, 0)

    def test_convert_same_space_is_identity(self) -> None:
        assert cp.convert_color_space(12, 34, 56, "sRGB", "sRGB") == (12, 34, 56)

    def test_convert_red_srgb_to_p3(self) -> None:
        # sRGB 纯红在 Display P3 下的经典表示 ≈ (234, 51, 35)；
        # 本模块矩阵计算（含截断）得到 (233, 51, 35)
        assert cp.convert_color_space(255, 0, 0, "sRGB", "Display P3") == (233, 51, 35)

    @pytest.mark.parametrize(
        "r,g,b,src,tgt,expected",
        [
            (255, 0, 0, "sRGB", "sRGB", False),          # 同空间
            (0, 255, 0, "Display P3", "sRGB", True),     # P3 绿超出 sRGB
            (255, 0, 0, "Display P3", "sRGB", True),     # P3 纯红超出 sRGB
            (128, 128, 128, "Display P3", "sRGB", False),  # 灰在两空间内
        ],
    )
    def test_check_out_of_gamut(
        self, r: int, g: int, b: int, src: str, tgt: str, expected: bool
    ) -> None:
        assert cp.check_out_of_gamut(r, g, b, src, tgt) is expected

    def test_acescg_target_output_in_range(self) -> None:
        # 烟雾测试：见 BUGS-agent-A4.md BUG-A4-01（ACEScg 分支的矩阵/OETF 疑问）
        xyz = cp.rgb_to_xyz_linear(255, 255, 255, "sRGB")
        out = cp.xyz_linear_to_rgb(xyz[0], xyz[1], xyz[2], "ACEScg")
        assert len(out) == 3
        for c in out:
            assert 0 <= c <= 255


# ============================================================
# 4. 配置目录 / 序列化
# ============================================================
class TestConfigPersistence:
    def test_load_missing_returns_none(self, isolated_config: Path) -> None:
        assert cp.load_config() is None

    def test_save_load_roundtrip(self, isolated_config: Path) -> None:
        swatches = [{"name": "行1", "colors": ["#AABBCC"] * 3}]
        ok = cp.save_config(
            swatches, "#00FF00", ["#111111"], ["#222222", "#333333"]
        )
        assert ok is None  # 无返回值（吞异常式保存）
        assert isolated_config.exists()
        data = cp.load_config()
        assert data is not None
        assert data["version"] == "2.0"
        assert data["last_color"] == "#00FF00"
        assert data["swatches"] == swatches
        assert data["picker_custom_swatches"] == ["#111111"]
        assert data["color_history"] == ["#222222", "#333333"]

    def test_save_defaults_for_optional_fields(self, isolated_config: Path) -> None:
        cp.save_config([{"name": "x", "colors": []}])
        data = cp.load_config()
        assert data is not None
        assert data["picker_custom_swatches"] == ["#333333"] * 20
        assert data["color_history"] == []
        assert data["last_color"] == "#FF0000"

    def test_load_corrupt_json_returns_none(self, isolated_config: Path) -> None:
        isolated_config.write_text("{ not json !!", encoding="utf-8")
        assert cp.load_config() is None

    def test_user_config_dir_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "topos" / "config.json"
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(target))
        assert cp._get_user_config_dir() == target.parent


# ============================================================
# 5. SpinLineEdit
# ============================================================
class TestSpinLineEdit:
    def test_set_value_clamps_and_syncs_text(self, qapp) -> None:
        edit = cp.SpinLineEdit(0, 255)
        edit.setValue(999)
        assert edit.value() == 255
        assert edit.text() == "255"
        edit.setValue(-7)
        assert edit.value() == 0
        assert edit.text() == "0"

    def test_editing_finished_valid_emits(self, qapp) -> None:
        edit = cp.SpinLineEdit(0, 255)
        received: list[int] = []
        edit.valueChanged.connect(received.append)
        edit._value = 10
        edit.setText("150")
        edit._on_editing_finished()
        assert edit.value() == 150
        assert received == [150]

    def test_editing_finished_invalid_reverts(self, qapp) -> None:
        edit = cp.SpinLineEdit(0, 255)
        received: list[int] = []
        edit.valueChanged.connect(received.append)
        edit._value = 42
        edit.setText("abc")
        edit._on_editing_finished()
        assert edit.value() == 42
        assert edit.text() == "42"
        assert received == []

    def test_editing_finished_clamps_out_of_range_text(self, qapp) -> None:
        edit = cp.SpinLineEdit(0, 255)
        received: list[int] = []
        edit.valueChanged.connect(received.append)
        edit.setText("400")
        edit._on_editing_finished()
        assert edit.value() == 255
        assert received == [255]

    def test_editing_finished_same_value_no_signal(self, qapp) -> None:
        edit = cp.SpinLineEdit(0, 255)
        received: list[int] = []
        edit.valueChanged.connect(received.append)
        edit.setValue(77)
        edit.setText("77")
        edit._on_editing_finished()
        assert received == []

    def test_hover_state_on_enter_leave(self, qapp) -> None:
        edit = cp.SpinLineEdit(0, 100)
        event = QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1))
        edit.enterEvent(event)
        assert edit._hover is True
        edit.leaveEvent(event)
        assert edit._hover is False


# ============================================================
# 6. 取色子控件：ColorSquare / HueSlider / AlphaSlider
# ============================================================
class TestColorSquare:
    def test_defaults(self, qapp) -> None:
        square = cp.ColorSquare()
        assert (square.hue, square.sat, square.val) == (0, 255, 255)
        assert square.size() == QSize(250, 250)

    def test_mouse_mapping_and_signal(self, qapp) -> None:
        square = cp.ColorSquare()
        received: list[tuple[int, int]] = []
        square.colorChanged.connect(lambda s, v: received.append((s, v)))
        square.mouseMoveEvent(_PosEvent(250, 0))
        assert (square.sat, square.val) == (255, 255)
        assert received[-1] == (255, 255)
        square.mouseMoveEvent(_PosEvent(0, 250))
        assert (square.sat, square.val) == (0, 0)
        assert received[-1] == (0, 0)
        square.mousePressEvent(_PosEvent(125, 125))  # press 转发 move
        assert received[-1] == (127, 127)

    def test_mouse_clamps_out_of_bounds(self, qapp) -> None:
        square = cp.ColorSquare()
        square.mouseMoveEvent(_PosEvent(-99, 999))
        assert (square.sat, square.val) == (0, 0)
        square.mouseMoveEvent(_PosEvent(9999, -99))
        assert (square.sat, square.val) == (255, 255)

    def test_paint_renders_offscreen(self, qapp) -> None:
        square = cp.ColorSquare()
        square.hue = 200
        assert not square.grab().isNull()


class TestHueSlider:
    def test_mapping_and_clamp(self, qapp) -> None:
        slider = cp.HueSlider()
        received: list[int] = []
        slider.hueChanged.connect(received.append)
        slider.mousePressEvent(_PosEvent(0, 250))
        assert slider.hue == 359
        slider.mouseMoveEvent(_PosEvent(0, 0))
        assert slider.hue == 0
        slider.mouseMoveEvent(_PosEvent(0, -50))
        assert slider.hue == 0
        assert received == [359, 0, 0]

    def test_paint_renders_offscreen(self, qapp) -> None:
        slider = cp.HueSlider()
        slider.hue = 120
        assert not slider.grab().isNull()


class TestAlphaSlider:
    def test_defaults_and_set_color(self, qapp) -> None:
        slider = cp.AlphaSlider()
        assert slider.alpha == 255
        slider.set_color(QColor(0, 255, 0))
        assert slider._color.name() == "#00ff00"

    def test_mouse_mapping_and_signal(self, qapp) -> None:
        slider = cp.AlphaSlider()
        received: list[int] = []
        slider.alphaChanged.connect(received.append)
        slider.mouseMoveEvent(_PosEvent(0, 0))
        assert slider.alpha == 0
        slider.mouseMoveEvent(_PosEvent(slider.width() - 1, 0))
        assert slider.alpha == 255
        assert received == [0, 255]

    def test_paint_renders_offscreen(self, qapp) -> None:
        slider = cp.AlphaSlider()
        slider.set_color(QColor("#FF8800"))
        slider.alpha = 128
        assert not slider.grab().isNull()


# ============================================================
# 7. 预览 / 色块按钮 / 历史
# ============================================================
class TestPreviewAndButtons:
    def test_draggable_preview_translucent_css(self, qapp) -> None:
        preview = cp.DraggablePreview()
        preview.set_color(QColor(255, 0, 0, 128))
        assert "rgba(255,0,0,0.502)" in preview.styleSheet()
        assert preview.color.alpha() == 128

    def test_draggable_preview_solid_and_string_css(self, qapp) -> None:
        preview = cp.DraggablePreview()
        preview.set_color(QColor("#00FF00"))
        assert "background-color: #00ff00" in preview.styleSheet()
        preview.set_color("#123456")
        assert "background-color: #123456" in preview.styleSheet()

    def test_draggable_button_set_color_emits(self, qapp) -> None:
        btn = cp.DraggableColorButton("#112233")
        received: list[str] = []
        btn.colorChanged.connect(received.append)
        btn.set_color("#AABBCC")
        assert btn.color == "#AABBCC"
        assert btn.toolTip() == "#AABBCC"
        assert received == ["#AABBCC"]

    def test_swatch_button_defaults(self, qapp) -> None:
        btn = cp.SwatchButton("#ABCDEF")
        assert btn.objectName() == "Swatch"
        assert btn.color == "#ABCDEF"
        assert btn.size() == QSize(18, 18)


class TestColorHistory:
    def test_cap_and_order(self, qapp) -> None:
        history = cp.ColorHistory()
        for i in range(15):
            history.add_color(QColor(i * 16, 0, 0))
        assert len(history.history) == cp.ColorHistory.MAX_HISTORY == 12
        assert history.history[0] == "#E00000"  # 最新在前
        assert history.history[-1] == "#300000"  # 最老被挤出

    def test_duplicate_moves_to_front(self, qapp) -> None:
        history = cp.ColorHistory()
        history.add_color(QColor("#111111"))
        history.add_color(QColor("#222222"))
        history.add_color(QColor("#111111"))
        assert history.history[0] == "#111111"
        assert len(history.history) == 2

    def test_click_emits_selected_color(self, qapp) -> None:
        history = cp.ColorHistory()
        selected: list[QColor] = []
        history.colorSelected.connect(selected.append)
        history.add_color(QColor("#00ABFF"))
        history._on_history_clicked(0)
        assert len(selected) == 1
        assert selected[0].name() == "#00abff"

    def test_click_empty_slot_no_signal(self, qapp) -> None:
        history = cp.ColorHistory()
        selected: list[QColor] = []
        history.colorSelected.connect(selected.append)
        history._on_history_clicked(0)
        assert selected == []


# ============================================================
# 8. ContrastChecker / CodeExportPanel
# ============================================================
class TestContrastChecker:
    def test_initial_black_on_white(self, qapp) -> None:
        checker = cp.ContrastChecker()
        assert checker.ratio_label.text() == "21.0:1"
        assert checker.badge_label.text() == "AAA"
        assert checker.btn_fix.isEnabled() is False  # 已达标，禁用修复

    def test_failing_contrast_enables_fix_and_emits(self, qapp) -> None:
        checker = cp.ContrastChecker()
        fixed: list[QColor] = []
        checker.fixRequested.connect(fixed.append)
        checker.set_foreground_color(QColor(255, 255, 255))  # 白字白底
        assert checker.ratio_label.text() == "1.0:1"
        assert checker.badge_label.text() == "Fail"
        assert checker.btn_fix.isEnabled() is True
        checker._on_fix_clicked()
        assert len(fixed) == 1
        # 修复结果对白底必须满足 AA
        assert cp.get_contrast_ratio(fixed[0], QColor(255, 255, 255)) >= 4.5

    def test_toggle_background_flips(self, qapp) -> None:
        from src.shared.i18n.i18n import I18N
        checker = cp.ContrastChecker()
        assert checker.btn_toggle_bg.text() == I18N.tr("color_picker.white_bg", "白底")
        checker._toggle_background()
        assert checker.btn_toggle_bg.text() == I18N.tr("color_picker.black_bg", "黑底")
        checker._toggle_background()
        assert checker.btn_toggle_bg.text() == I18N.tr("color_picker.white_bg", "白底")


class TestCodeExportPanel:
    def test_default_css_and_set_color(self, qapp) -> None:
        panel = cp.CodeExportPanel()
        assert panel.code_display.text() == "--primary-color: #FF0000;"
        panel.set_color(QColor("#00FF00"))
        assert panel.code_display.text() == "--primary-color: #00FF00;"

    @pytest.mark.parametrize(
        "fmt,expected",
        [
            ("css", "--primary-color: #00FF00;"),
            ("sass", "$primary-color: #00FF00;"),
            ("swift", "UIColor(red: 0/255, green: 255/255, blue: 0/255, alpha: 1.0)"),
            ("kotlin", "Color(0x00FF00)"),
            ("json", '{"color": "#00FF00", "rgb": {"r": 0, "g": 255, "b": 0}}'),
        ],
    )
    def test_generate_code_formats(self, qapp, fmt: str, expected: str) -> None:
        panel = cp.CodeExportPanel()
        panel.set_color(QColor("#00FF00"))
        assert panel._generate_code(fmt) == expected

    def test_format_switch_updates_display(self, qapp) -> None:
        panel = cp.CodeExportPanel()
        panel._on_format_clicked("json")
        assert panel.current_format == "json"
        assert panel.code_display.text().startswith('{"color"')
        # 其它按钮取消勾选
        assert panel.format_buttons["css"].isChecked() is False
        assert panel.format_buttons["json"].isChecked() is True

    def test_copy_to_clipboard(self, qapp) -> None:
        from PyQt6.QtWidgets import QApplication

        panel = cp.CodeExportPanel()
        panel.set_color(QColor("#123456"))
        panel._copy_code()
        assert QApplication.clipboard().text() == "--primary-color: #123456;"


# ============================================================
# 9. 对话框 / 覆盖层 / 图标
# ============================================================
class TestDialogsAndOverlay:
    def test_input_dialog_get_text_strips(self, qapp) -> None:
        dialog = cp.MinimalInputDialog("标题", "提示", "默认")
        dialog.input_edit.setText("  名称  ")
        assert dialog.get_text() == "名称"

    def test_input_dialog_esc_rejects(self, qapp) -> None:
        dialog = cp.MinimalInputDialog("标题", "提示", "默认")
        dialog.keyPressEvent(_esc_event())
        assert dialog.result() == int(QDialog.DialogCode.Rejected)

    def test_confirm_dialog_esc_rejects(self, qapp) -> None:
        dialog = cp.MinimalConfirmDialog("标题", "消息")
        dialog.keyPressEvent(_esc_event())
        assert dialog.result() == int(QDialog.DialogCode.Rejected)

    def test_dropper_esc_emits_cancelled(self, qapp) -> None:
        overlay = cp.EyeDropperOverlay()
        cancelled: list[bool] = []
        overlay.cancelled.connect(lambda: cancelled.append(True))
        overlay.keyPressEvent(_esc_event())
        assert cancelled == [True]

    def test_svg_with_fill_replacement(self) -> None:
        out = cp._svg_with_fill(cp._DROPPER_SVG, "#FF8800")
        assert b'fill="#FF8800"' in out
        assert b'fill="currentColor"' not in out

    def test_create_dropper_cursor(self, qapp) -> None:
        cursor: QCursor = cp.create_dropper_cursor()
        assert cursor is not None
        assert not cursor.pixmap().isNull()
        assert cursor.pixmap().size() == QSize(32, 32)

    def test_icon_button_paint(self, qapp) -> None:
        btn = cp.IconButton(24)
        btn.set_svg_data(cp._svg_with_fill(cp._DROPPER_SVG, "#FFFFFF"))
        btn.resize(60, 60)
        assert not btn.grab().isNull()


# ============================================================
# 10. SettingsDialog
# ============================================================
class TestSettingsDialog:
    def test_default_srgb_no_warning(self, qapp) -> None:
        dialog = cp.SettingsDialog(QColor(255, 0, 0))
        assert dialog.space_combo.currentText() == "sRGB"
        assert dialog.gamut_warning.isHidden()

    def test_p3_out_of_gamut_colors_show_warning(self, qapp) -> None:
        dialog = cp.SettingsDialog(QColor(255, 0, 0))
        dialog.space_combo.setCurrentText("Display P3")
        # P3 纯红/纯绿都超出 sRGB 色域
        assert dialog.gamut_warning.isVisibleTo(dialog)
        dialog.set_color(QColor(0, 255, 0))
        assert dialog.gamut_warning.isVisibleTo(dialog)

    def test_p3_in_gamut_gray_hides_warning(self, qapp) -> None:
        dialog = cp.SettingsDialog(QColor(128, 128, 128))
        dialog.space_combo.setCurrentText("Display P3")
        assert dialog.gamut_warning.isHidden()

    def test_set_color_propagates(self, qapp) -> None:
        dialog = cp.SettingsDialog(QColor(255, 0, 0))
        dialog.set_color(QColor("#00FF00"))
        assert dialog.current_color == QColor("#00FF00")
        assert dialog.contrast_checker.foreground_color == QColor("#00FF00")
        assert dialog.code_export.current_color == QColor("#00FF00")

    def test_color_fixed_applies(self, qapp) -> None:
        dialog = cp.SettingsDialog(QColor(255, 255, 255))
        applied: list[QColor] = []
        dialog.colorApplied.connect(applied.append)
        fixed = QColor("#001122")
        dialog._on_color_fixed(fixed)
        assert applied == [fixed]
        assert dialog.current_color == fixed


# ============================================================
# 11. SwatchPage（含配置序列化 / ASE 导出）
# ============================================================
class TestSwatchPage:
    def test_defaults(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        assert len(page.swatch_rows) == len(cp.SwatchPage.DEFAULT_SWATCHES) == 8
        assert page.current_color == QColor("#FF0000")
        assert page.swatch_rows[-1].is_locked is True  # “自定义”行锁定

    def test_hex_changed_valid(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        selected: list[QColor] = []
        page.colorSelected.connect(selected.append)
        page.hex_edit.setText("#00FF00")
        page._on_hex_changed()
        assert page.current_color == QColor("#00FF00")
        assert selected[-1] == QColor("#00FF00")
        assert page.rgb_edits["G"].value() == 255

    def test_hex_changed_invalid_keeps_color(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        page.hex_edit.setText("ZZZZZZ")  # 6 位但非法
        page._on_hex_changed()
        assert page.current_color == QColor("#FF0000")
        page.hex_edit.setText("#12345")  # 长度不对
        page._on_hex_changed()
        assert page.current_color == QColor("#FF0000")

    def test_select_color_updates_inputs(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        selected: list[QColor] = []
        page.colorSelected.connect(selected.append)
        page._select_color("#0000FF")
        assert page.current_color == QColor("#0000FF")
        assert page.hex_edit.text() == "#0000FF"
        assert page.rgb_edits["B"].value() == 255
        assert page.rgb_edits["R"].value() == 0
        assert selected[-1] == QColor("#0000FF")

    def test_rgb_changed_updates_hex(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        page.rgb_edits["R"].setValue(0)
        page.rgb_edits["G"].setValue(255)
        page.rgb_edits["B"].setValue(0)
        page._on_rgb_changed("G", 255)
        assert page.current_color == QColor("#00FF00")
        assert page.hex_edit.text() == "#00FF00"

    def test_confirm_color_emits_and_saves(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        confirmed: list[QColor] = []
        page.colorConfirmed.connect(confirmed.append)
        page._select_color("#0000FF")
        page._confirm_color()
        assert confirmed == [QColor("#0000FF")]
        assert isolated_config.exists()
        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data["last_color"] == "#0000FF"
        assert data["swatches"][0]["name"] == "基础色板"

    def test_confirm_wipes_picker_fields(self, qapp, isolated_config: Path) -> None:
        # 现状行为（疑似数据丢失，见 BUGS-agent-A4.md BUG-A4-02）：
        # SwatchPage._confirm_color 不传 picker 字段 → 保存时被重置为默认
        cp.save_config(
            cp.SwatchPage.DEFAULT_SWATCHES, "#FFFFFF", ["#ABCDEF"], ["#111111"]
        )
        page = cp.SwatchPage()
        page._confirm_color()
        data = cp.load_config()
        assert data is not None
        assert data["picker_custom_swatches"] == ["#333333"] * 20
        assert data["color_history"] == []

    def test_add_current_to_custom_first_empty_slot(
        self, qapp, isolated_config: Path
    ) -> None:
        page = cp.SwatchPage()
        page._select_color("#0AF0CA")
        page._add_current_to_custom()
        assert page.swatch_data[-1]["colors"][0] == "#0AF0CA"
        assert page.swatch_rows[-1].get_colors()[0] == "#0AF0CA"

    def test_add_current_to_custom_full_shifts(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        filled = [f"#00{i:02X}00" for i in range(20)]
        page.swatch_data[-1]["colors"] = list(filled)
        page._create_swatch_rows()
        page._select_color("#FFFFFF")
        page._add_current_to_custom()
        colors = page.swatch_rows[-1].get_colors()
        assert colors[0] == "#FFFFFF"
        assert colors[1] == filled[0]
        assert colors[-1] == filled[18]  # 最后一个被挤掉

    def test_name_changed_updates_data(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        page._on_name_changed(0, "RENAMED")
        assert page.swatch_data[0]["name"] == "RENAMED"

    def test_row_moved_reorders_and_locked_blocked(
        self, qapp, isolated_config: Path
    ) -> None:
        page = cp.SwatchPage()
        page._on_row_moved(0, 1)
        assert page.swatch_data[0]["name"] == "纯色色板"  # 原 1 号上移
        assert page.swatch_data[1]["name"] == "基础色板"
        before = [row["name"] for row in page.swatch_data]
        page._on_row_moved(0, len(page.swatch_data) - 1)  # 目标是锁定行
        assert [row["name"] for row in page.swatch_data] == before

    def test_set_current_color_syncs(self, qapp, isolated_config: Path) -> None:
        page = cp.SwatchPage()
        page.set_current_color(QColor("#445566"))
        assert page.current_color == QColor("#445566")
        assert page.hex_edit.text() == "#445566"
        assert page.rgb_edits["R"].value() == 0x44

    def test_write_ase_file(self, qapp, isolated_config: Path, tmp_path: Path) -> None:
        page = cp.SwatchPage()
        target = tmp_path / "palette.ase"
        page._write_ase_file(str(target))
        raw = target.read_bytes()
        assert raw[:4] == b"ASEF"
        # AUD-32404：逐条解析 RGB color entry（块结构 = uint16 块类型 +
        # uint32 块长度 + 块数据），统计实际写入的条目数并核对块长度字段
        offset = 12  # 跳过签名 + 版本 + 块计数
        entries = 0
        while offset < len(raw):
            block_type = int.from_bytes(raw[offset:offset + 2], "big")
            if block_type != 0x0001:
                break
            block_len = int.from_bytes(raw[offset + 2:offset + 6], "big")
            payload = raw[offset + 6:offset + 6 + block_len]
            # payload: 名称长度(含终止符) + UTF-16BE 名称 + 终止符 +
            # 'RGB ' + 3×float32 + 颜色类型
            name_len = int.from_bytes(payload[0:2], "big")
            body = 2 + name_len * 2 + 4 + 12 + 2
            assert block_len == body
            assert payload[2 + (name_len - 1) * 2:2 + name_len * 2] == b"\x00\x00"
            assert payload[2 + name_len * 2:2 + name_len * 2 + 4] == b"RGB "
            offset += 6 + block_len
            entries += 1
        expected = sum(
            1
            for row in page.swatch_rows
            for c in row.get_colors()
            if c != "#333333"
        )
        assert entries == expected == 140  # 7 个非自定义行 × 20
        # AUD-32404：头部声明的块数必须等于实际写入块数（此前占位色被
        # 计入头部但循环跳过，声明 160 vs 实际 140）
        header_count = int.from_bytes(raw[8:12], "big")
        assert header_count == entries == 140


# ============================================================
# 12. PickerPage
# ============================================================
class TestPickerPage:
    def test_load_qcolor_syncs_everything(self, qapp) -> None:
        page = cp.PickerPage()
        page.load_qcolor(QColor("#336699"))
        assert (page.hue, page.sat, page.val) == (210, 170, 153)
        assert page.alpha == 255
        assert page.edits["R"].value() == 51
        assert page.edits["G"].value() == 102
        assert page.edits["B"].value() == 153
        assert page.edits["#"].text() == "#336699"
        assert (page.edits["H"].value(), page.edits["S"].value(), page.edits["L"].value()) == (210, 50, 40)
        assert (page.square.hue, page.square.sat, page.square.val) == (210, 170, 153)
        assert page.slider.hue == 210

    def test_get_current_color_roundtrip(self, qapp) -> None:
        page = cp.PickerPage()
        page.load_qcolor(QColor("#336699"))
        got = page.get_current_color()
        assert (got.red(), got.green(), got.blue()) == (51, 102, 153)
        assert got.alpha() == 255

    def test_update_h(self, qapp) -> None:
        page = cp.PickerPage()
        page.update_h(120)
        assert page.hue == 120
        assert page.square.hue == 120
        assert page.edits["H"].value() == 120

    def test_update_sv(self, qapp) -> None:
        page = cp.PickerPage()
        page.update_sv(100, 200)
        assert (page.sat, page.val) == (100, 200)

    def test_on_spin_changed_rgb(self, qapp) -> None:
        page = cp.PickerPage()
        for key in ("R", "G", "B"):
            page.edits[key].setValue(0)
        page.on_spin_changed("R", 0)
        assert page.edits["#"].text() == "#000000"

    def test_on_spin_changed_hsl(self, qapp) -> None:
        page = cp.PickerPage()
        page.edits["S"].setValue(100)
        page.edits["L"].setValue(50)
        page.on_spin_changed("H", 240)
        assert page.edits["H"].value() == 240
        assert page.edits["#"].text() == "#0101FE"

    def test_on_spin_changed_lab(self, qapp) -> None:
        page = cp.PickerPage()
        page.edits["a"].setValue(0)
        page.edits["b"].setValue(0)
        page.on_spin_changed("L_lab", 50)
        assert page.edits["#"].text() == "#767676"

    def test_alpha_edit_updates_preview(self, qapp) -> None:
        page = cp.PickerPage()
        page._on_alpha_changed(128)
        assert page.alpha == 128
        preview_color = page.preview.color
        assert isinstance(preview_color, QColor)
        assert preview_color.alpha() == 128

    def test_confirm_color_emits_with_alpha(self, qapp) -> None:
        page = cp.PickerPage()
        confirmed: list[QColor] = []
        page.colorConfirmed.connect(confirmed.append)
        page.load_qcolor(QColor("#336699"))
        page._on_alpha_changed(30)
        page._confirm_color()
        assert confirmed[-1].alpha() == 30
        assert confirmed[-1].name() == "#336699"

    def test_add_to_custom_swatch_shifts(self, qapp) -> None:
        page = cp.PickerPage()
        page.custom_swatches[0].set_color("#111111")
        page.custom_swatches[1].set_color("#222222")
        page.edits["#"].setText("#ABCDEF")
        page.add_to_custom_swatch()
        assert page.custom_swatches[0].color == "#ABCDEF"
        assert page.custom_swatches[1].color == "#111111"
        assert page.custom_swatches[2].color == "#222222"

    def test_load_color_from_swatch_skips_empty(self, qapp) -> None:
        page = cp.PickerPage()
        page.load_qcolor(QColor("#336699"))
        page.load_color_from_swatch(SimpleNamespace(color="#333333"))
        assert page.edits["#"].text() == "#336699"
        page.load_color_from_swatch(SimpleNamespace(color="#00FF00"))
        assert page.edits["#"].text() == "#00FF00"


# ============================================================
# 13. CompactColorStudio
# ============================================================
class TestCompactColorStudio:
    def test_set_and_get_current_color(self, qapp, isolated_config: Path) -> None:
        studio = cp.CompactColorStudio()
        studio.set_current_color(QColor("#00CCFF"))
        got = studio.get_current_color()
        assert (got.red(), got.green(), got.blue()) == (0, 204, 255)

    def test_switch_tool_syncs_swatch_page(self, qapp, isolated_config: Path) -> None:
        studio = cp.CompactColorStudio()
        studio.set_current_color(QColor("#00CCFF"))
        studio._switch_tool("swatch")
        assert studio.current_tool == "swatch"
        assert studio.page_stack.currentWidget() is studio.swatch_page
        assert studio.swatch_page.current_color.name() == "#00ccff"
        studio._switch_tool("picker")
        assert studio.page_stack.currentWidget() is studio.picker_page

    def test_on_color_picked_updates_picker_and_history(
        self, qapp, isolated_config: Path
    ) -> None:
        studio = cp.CompactColorStudio()
        studio._on_color_picked(QColor("#123456"))
        assert studio.picker_page.get_current_color().name() == "#123456"
        assert studio.picker_page.color_history.history[0] == "#123456"

    def test_confirm_and_close_saves_full_state(
        self, qapp, isolated_config: Path
    ) -> None:
        studio = cp.CompactColorStudio()
        confirmed: list[QColor] = []
        studio.colorConfirmed.connect(confirmed.append)
        studio.picker_page.custom_swatches[0].set_color("#ABCDEF")
        color = QColor("#336699")
        studio._confirm_and_close(color)
        assert confirmed == [color]
        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data["last_color"] == "#336699"
        assert data["picker_custom_swatches"][0] == "#ABCDEF"
        assert "#336699" in data["color_history"]

    def test_event_filter_passthrough_non_mouse(
        self, qapp, isolated_config: Path
    ) -> None:
        studio = cp.CompactColorStudio()
        result = studio.eventFilter(studio, QEvent(QEvent.Type.User))
        assert result is False

    def test_on_reset_settings(self, qapp, isolated_config: Path) -> None:
        studio = cp.CompactColorStudio()
        isolated_config.write_text("junk", encoding="utf-8")
        studio.picker_page.color_history.add_color(QColor("#111111"))
        studio._on_reset_settings()
        assert not isolated_config.exists()
        assert studio.picker_page.color_history.history == []
        assert studio.picker_page.get_current_color().name() == "#ff0000"
        assert all(
            btn.color == "#333333" for btn in studio.picker_page.custom_swatches
        )


# ============================================================
# 14. DraggableSwatchRow（拖拽事件用 duck-typed 事件）
# ============================================================
class TestDraggableSwatchRow:
    def test_drag_drop_emits_row_moved(self, qapp) -> None:
        row0 = cp.DraggableSwatchRow("A", ["#111111"], 0)
        row1 = cp.DraggableSwatchRow("B", ["#222222"], 1)
        moved: list[tuple[int, int]] = []
        row1.rowMoved.connect(lambda f, t: moved.append((f, t)))
        event = _StubDropEvent(_StubMimeData("application/x-swatch-row", b"0"))
        row1.dragEnterEvent(event)
        assert event.accepted is True
        row1.dropEvent(event)
        assert moved == [(0, 1)]
        # 源=目标 不发信号
        row1.dropEvent(_StubDropEvent(_StubMimeData("application/x-swatch-row", b"1")))
        assert moved == [(0, 1)]

    def test_drag_enter_rejects_other_format(self, qapp) -> None:
        row = cp.DraggableSwatchRow("A", ["#111111"], 0)
        event = _StubDropEvent(_StubMimeData("text/plain", b"x"))
        row.dragEnterEvent(event)
        assert event.accepted is False

    def test_get_colors_and_row_index(self, qapp) -> None:
        row = cp.DraggableSwatchRow("A", ["#111111", "#222222"], 3)
        assert row.get_colors() == ["#111111", "#222222"]
        row.update_row_index(7)
        assert row.row_index == 7

    def test_custom_name_row_locked(self, qapp) -> None:
        assert cp.DraggableSwatchRow("自定义", ["#333333"], 0).is_locked is True
        assert cp.DraggableSwatchRow("其它", ["#333333"], 0).is_locked is False
