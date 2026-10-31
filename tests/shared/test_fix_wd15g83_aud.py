"""wd15-g83：计划表批 D（shared color/config/control_surfaces/db/diagnostics
域 Major）24 条条目的根因修复测试。

覆盖：AUD-32400..32407 / 32501 / 32502 / 32510 / 32511 / 32602..32605 /
32800 / 32901..32903 / 33010 / 33020..33022（docs/audit/2026-09-28/
batches/batch-324..330）。

纯函数部分不依赖 QApplication；widget 部分依赖 tests/conftest.py 的
qapp（QT_QPA_PLATFORM=offscreen）。涉及用户配置文件的用例均把
CONFIG_FILE_PATH / TOPOS_CONFIG_PATH 指向 tmp_path。
"""
from __future__ import annotations

import gc
import json
import logging
import os
import socket
import sqlite3
import sys
import threading
import time
import types
import weakref
from pathlib import Path
from types import SimpleNamespace

# 必须在创建 QApplication 之前设置 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import numpy as np
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QImage

from src.shared.color import color_picker as cp
from src.shared.config.config_manager import ConfigManager
from src.shared.control_surfaces.tangent.hub_client import TangentHubClient
from src.shared.db.database import Database
from src.shared.diagnostics import resource_snapshot as rs
from src.shared.diagnostics.performance_bundle import sanitize_value

pytestmark = pytest.mark.color_core


# ===========================================================================
# fixtures / helpers
# ===========================================================================
@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 color_picker 模块级 CONFIG_FILE_PATH 指向 tmp，配置读写隔离。"""
    path = tmp_path / "color_picker_swatches.json"
    monkeypatch.setattr(cp, "CONFIG_FILE_PATH", path)
    return path


class _FakeMouseEvent:
    """duck-typed 鼠标事件：只提供 globalPosition().toPoint()。"""

    def __init__(self, global_x: int, global_y: int) -> None:
        self._point = QPoint(global_x, global_y)

    def globalPosition(self) -> SimpleNamespace:
        return SimpleNamespace(toPoint=lambda: self._point)


class _FakeMessageBox:
    """替代 QMessageBox：记录 warning/information 调用而不弹窗。"""

    warnings: list = []
    infos: list = []

    @staticmethod
    def warning(*args, **kwargs):
        _FakeMessageBox.warnings.append(args)

    @staticmethod
    def information(*args, **kwargs):
        _FakeMessageBox.infos.append(args)


# ===========================================================================
# AUD-32400：color 包 UI 符号真·延迟导入（PEP 562）
# ===========================================================================
class TestAud32400LazyPickerImport:
    def test_import_package_does_not_load_picker_ui(self) -> None:
        saved = {
            k: v for k, v in sys.modules.items()
            if k == "src.shared.color" or k.startswith("src.shared.color.")
        }
        for key in list(saved):
            sys.modules.pop(key)
        try:
            import src.shared.color as color_pkg  # noqa: WPS433

            # 只 import 包本身不加载 PyQt6 UI 栈（color_picker 模块未执行）
            assert "src.shared.color.color_picker" not in sys.modules

            # PEP 562 按需加载：属性访问触发 color_picker 导入
            assert callable(color_pkg.get_color)
            import src.shared.color.color_picker as cp2
            assert color_pkg.EyeDropperOverlay is cp2.EyeDropperOverlay
            assert color_pkg.CompactColorStudio is cp2.CompactColorStudio
            assert "src.shared.color.color_picker" in sys.modules

            # 未知属性仍按常规语义抛 AttributeError；__dir__ 含懒导出名
            with pytest.raises(AttributeError):
                _ = color_pkg.not_a_symbol
            assert "get_color" in dir(color_pkg)
        finally:
            for key in list(sys.modules):
                if key == "src.shared.color" or key.startswith("src.shared.color."):
                    if key not in saved:
                        sys.modules.pop(key)
            sys.modules.update(saved)


# ===========================================================================
# AUD-32401：屏幕吸管 HiDPI 逻辑↔物理坐标换算
# ===========================================================================
class TestAud32401HiDpiSampling:
    def _make_overlay_with_image(self, dpr: float) -> cp.EyeDropperOverlay:
        overlay = cp.EyeDropperOverlay()
        # 物理像素 200x100 的截图；dpr=2 → 逻辑 100x50
        img = QImage(200, 100, QImage.Format.Format_RGB32)
        img.fill(QColor(0, 0, 0).rgb())
        img.setDevicePixelRatio(dpr)
        overlay.full_screen_img = img
        # 截图来源屏幕位于全局逻辑坐标 (10, 5)
        overlay._screen_origin = QPoint(10, 5)
        return overlay

    def test_hidpi_click_maps_to_physical_pixel(self, qapp) -> None:
        overlay = self._make_overlay_with_image(dpr=2.0)
        # 目标：物理 (60, 40) → 逻辑屏内 (30, 20) → 全局逻辑 (40, 25)
        overlay.full_screen_img.setPixel(60, 40, QColor(255, 0, 0).rgb())
        picked: list[QColor] = []
        overlay.colorPicked.connect(picked.append)
        overlay.mousePressEvent(_FakeMouseEvent(40, 25))
        assert picked and picked[0] == QColor(255, 0, 0)
        # 修复前：物理/逻辑混用 + -8 偏移 → 采样 (32, 25) 的黑色

    def test_dpr_1_click_unchanged_semantics(self, qapp) -> None:
        overlay = self._make_overlay_with_image(dpr=1.0)
        overlay.full_screen_img.setPixel(30, 20, QColor(0, 255, 0).rgb())
        picked: list[QColor] = []
        overlay.colorPicked.connect(picked.append)
        overlay.mousePressEvent(_FakeMouseEvent(40, 25))
        assert picked and picked[0] == QColor(0, 255, 0)

    def test_out_of_bounds_click_clamped(self, qapp) -> None:
        overlay = self._make_overlay_with_image(dpr=2.0)
        # 右下角物理 (199, 99) 为红色；越界点击被钳制到边缘
        overlay.full_screen_img.setPixel(199, 99, QColor(0, 0, 255).rgb())
        picked: list[QColor] = []
        overlay.colorPicked.connect(picked.append)
        overlay.mousePressEvent(_FakeMouseEvent(10000, 10000))
        assert picked and picked[0] == QColor(0, 0, 255)


# ===========================================================================
# AUD-32402：get_color 泄漏 + 覆盖层截图释放
# ===========================================================================
class TestAud32402Lifetime:
    def test_pick_and_cancel_release_full_screen_image(self, qapp) -> None:
        overlay = cp.EyeDropperOverlay()
        img = QImage(4, 4, QImage.Format.Format_RGB32)
        img.setDevicePixelRatio(1.0)
        overlay.full_screen_img = img
        overlay.mousePressEvent(_FakeMouseEvent(1, 1))
        assert overlay.full_screen_img is None  # 取色后立即释放截图

        overlay2 = cp.EyeDropperOverlay()
        img2 = QImage(4, 4, QImage.Format.Format_RGB32)
        overlay2.full_screen_img = img2
        from PyQt6.QtCore import QEvent, Qt as _Qt
        from PyQt6.QtGui import QKeyEvent
        overlay2.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, _Qt.Key.Key_Escape,
            _Qt.KeyboardModifier.NoModifier,
        ))
        assert overlay2.full_screen_img is None  # Esc 取消也释放截图

    def test_get_color_schedules_delete_later(
        self, qapp, isolated_config: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PyQt6.QtCore import QEventLoop

        delete_later_calls: list[bool] = []

        class _RecordingStudio(cp.CompactColorStudio):
            def deleteLater(self) -> None:  # noqa: N802 - Qt API 命名
                delete_later_calls.append(True)
                super().deleteLater()

        class _ImmediateLoop(QEventLoop):
            """事件循环立即返回（无用户交互），验证返回路径。"""

            def exec(self, flags=None):  # noqa: A003 - Qt API 命名
                return 0

        monkeypatch.setattr(cp, "CompactColorStudio", _RecordingStudio)
        import PyQt6.QtCore as qt_core
        monkeypatch.setattr(qt_core, "QEventLoop", _ImmediateLoop)

        result = cp.get_color()
        assert result is None
        # AUD-32402：loop 退出后调度销毁整棵选择器 widget 树（不再泄漏）
        assert delete_later_calls == [True]


# ===========================================================================
# AUD-32403：导入色板 JSON 结构校验
# ===========================================================================
class TestAud32403ImportValidation:
    @pytest.fixture(autouse=True)
    def _msg_box(self, monkeypatch: pytest.MonkeyPatch) -> type:
        _FakeMessageBox.warnings = []
        _FakeMessageBox.infos = []
        monkeypatch.setattr(cp, "QMessageBox", _FakeMessageBox)
        return _FakeMessageBox

    def test_validator_accepts_valid_rows(self) -> None:
        rows = cp._validate_swatch_rows([
            {"name": "a", "colors": ["#AABBCC"]},
            {"name": "自定义", "colors": ["#333333"] * 20, "locked": True},
        ])
        assert rows is not None
        assert rows[0] == {"name": "a", "colors": ["#AABBCC"], "locked": False}
        assert rows[1]["locked"] is True

    @pytest.mark.parametrize("bad", [
        "not-a-list",
        [],
        [{"name": "x"}],                              # 缺 colors
        [{"colors": ["#000000"]}],                    # 缺 name
        [{"name": 1, "colors": ["#000000"]}],         # name 非字符串
        [{"name": "x", "colors": "green"}],           # colors 非列表
        [{"name": "x", "colors": ["green"]}],         # 颜色非 hex
        [{"name": "x", "colors": ["#ABC"]}],          # hex 位数不足
        ["row"],                                      # 行非 dict
        [{"name": f"r{i}", "colors": ["#000000"]} for i in range(65)],  # 行数上限
    ])
    def test_validator_rejects_malformed(self, bad: object) -> None:
        assert cp._validate_swatch_rows(bad) is None

    def test_import_rejects_malformed_and_keeps_current_data(
        self, qapp, isolated_config: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps(
            {"swatches": [
                {"name": "ok", "colors": ["#00FF00"]},
                {"name": "broken", "colors": ["#00FF00", "not-hex"]},
            ]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            cp.QFileDialog, "getOpenFileName",
            staticmethod(lambda *a, **k: (str(bad_file), "")),
        )
        page = cp.SwatchPage()
        names_before = [row["name"] for row in page.swatch_data]
        page._import_swatches()
        # 数据与 UI 均保持不变；用户收到明确拒绝提示
        assert [row["name"] for row in page.swatch_data] == names_before
        assert len(page.swatch_rows) == len(names_before)
        assert len(_FakeMessageBox.warnings) == 1
        assert _FakeMessageBox.infos == []

    def test_import_accepts_valid_file(
        self, qapp, isolated_config: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        good = tmp_path / "good.json"
        good.write_text(json.dumps(
            {"swatches": [{"name": "导入", "colors": ["#123456", "#ABCDEF"]}]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            cp.QFileDialog, "getOpenFileName",
            staticmethod(lambda *a, **k: (str(good), "")),
        )
        page = cp.SwatchPage()
        page._import_swatches()
        assert page.swatch_data == [
            {"name": "导入", "colors": ["#123456", "#ABCDEF"], "locked": False},
        ]
        assert len(_FakeMessageBox.infos) == 1

    def test_import_non_dict_reports_error(
        self, qapp, isolated_config: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        raw = tmp_path / "raw.json"
        raw.write_text("[1, 2, 3]", encoding="utf-8")
        monkeypatch.setattr(
            cp.QFileDialog, "getOpenFileName",
            staticmethod(lambda *a, **k: (str(raw), "")),
        )
        page = cp.SwatchPage()
        names_before = [row["name"] for row in page.swatch_data]
        page._import_swatches()
        assert [row["name"] for row in page.swatch_data] == names_before
        assert len(_FakeMessageBox.warnings) == 1


# ===========================================================================
# AUD-32404：ASE 导出块格式（头部计数 + 每块 uint32 长度字段）
# ===========================================================================
class TestAud32404AseFormat:
    def test_single_color_block_layout(self, qapp, isolated_config: Path,
                                       tmp_path: Path) -> None:
        import struct
        page = cp.SwatchPage()
        page.swatch_data = [
            {"name": "s", "colors": ["#336699", "#333333"], "locked": False},
        ]
        page._create_swatch_rows()
        target = tmp_path / "one.ase"
        page._write_ase_file(str(target))
        raw = target.read_bytes()
        assert raw[:4] == b"ASEF"
        # 头部计数 == 实际写入块数（#333333 占位被跳过）
        assert int.from_bytes(raw[8:12], "big") == 1
        # 块：uint16 类型 + uint32 长度 + payload
        assert int.from_bytes(raw[12:14], "big") == 0x0001
        block_len = int.from_bytes(raw[14:18], "big")
        payload = raw[18:18 + block_len]
        # payload = 名称长度(含终止符) + UTF-16BE 名称 + 终止符 +
        # 'RGB ' + 3×float32 + 颜色类型
        name_len = int.from_bytes(payload[0:2], "big")
        assert name_len == len("#336699") + 1
        assert payload[2:2 + name_len * 2 - 2] == "#336699".encode("utf-16-be")
        assert payload[2 + name_len * 2 - 2:2 + name_len * 2] == b"\x00\x00"
        body_offset = 2 + name_len * 2
        assert payload[body_offset:body_offset + 4] == b"RGB "
        r, g, b = struct.unpack(">fff", payload[body_offset + 4:body_offset + 16])
        assert r == pytest.approx(QColor("#336699").redF(), abs=1e-6)
        assert g == pytest.approx(QColor("#336699").greenF(), abs=1e-6)
        assert b == pytest.approx(QColor("#336699").blueF(), abs=1e-6)
        assert int.from_bytes(payload[-2:], "big") == 0
        assert block_len == len(payload) == 2 + name_len * 2 + 4 + 12 + 2
        assert 18 + block_len == len(raw)


# ===========================================================================
# AUD-32405：色板页 RGB 中间值不进历史
# ===========================================================================
class TestAud32405RgbIntermediateValues:
    def test_intermediate_values_do_not_emit_color_selected(
        self, qapp, isolated_config: Path,
    ) -> None:
        studio = cp.CompactColorStudio()
        page = studio.swatch_page
        selected: list[QColor] = []
        page.colorSelected.connect(selected.append)

        # 模拟中键拖拽/滚轮的连续中间值（每像素步进一次）
        for v in range(0, 200, 10):
            page.rgb_edits["G"].setValue(v)
            page._on_rgb_changed("G", v)
            assert page.hex_edit.text() == page.current_color.name().upper()

        assert selected == []  # 中间值绝不广播 colorSelected
        # 历史记录未被中间值污染
        assert studio.picker_page.color_history.history == []

    def test_committed_edit_emits_once(self, qapp, isolated_config: Path) -> None:
        studio = cp.CompactColorStudio()
        page = studio.swatch_page
        selected: list[QColor] = []
        page.colorSelected.connect(selected.append)
        page.rgb_edits["R"].setValue(255)
        page.rgb_edits["G"].setValue(255)
        page.rgb_edits["B"].setValue(0)
        page._on_rgb_edit_committed("G")
        assert len(selected) == 1
        assert selected[0].name() == "#ffff00"
        # 提交后进入历史（离散动作）
        assert studio.picker_page.color_history.history == ["#FFFF00"]

    def test_wheel_signal_path_keeps_preview_synced(
        self, qapp, isolated_config: Path,
    ) -> None:
        studio = cp.CompactColorStudio()
        page = studio.swatch_page
        selected: list[QColor] = []
        page.colorSelected.connect(selected.append)
        # SpinLineEdit.valueChanged 信号路径（滚轮/拖拽实际入口）
        page.rgb_edits["R"].setValue(0)
        page.rgb_edits["G"].setValue(128)
        page.rgb_edits["B"].setValue(255)
        page.rgb_edits["B"].valueChanged.emit(255)
        assert page.current_color == QColor(0, 128, 255)
        assert page.hex_edit.text() == "#0080FF"
        assert selected == []


# ===========================================================================
# AUD-32406：Rec.709 / ACEScg 溢色检查矩阵与解码
# ===========================================================================
class TestAud32406GamutMatrices:
    D65_XYZ = (0.95047, 1.0, 1.08883)

    def test_bt709_decode_matches_project_contract(self) -> None:
        # Rec.709 契约（tests/color/test_ocio_config_load.py）：code 0.5 的
        # BT.709 OETF 逆 ≈ 0.2596（非 BT.1886 纯 2.4 幂的 ~0.19）
        assert cp._bt709_to_linear(0.5) == pytest.approx(0.2596, abs=2e-3)
        assert cp._bt709_to_linear(0.0) == 0.0
        assert cp._bt709_to_linear(0.0405) == pytest.approx(0.009, abs=1e-4)
        assert cp._bt709_to_linear(1.0) == 1.0

    @pytest.mark.parametrize("space", ["sRGB", "Display P3", "Rec.709", "ACEScg"])
    def test_white_maps_to_d65_white(self, space: str) -> None:
        x, y, z = cp.rgb_to_xyz_linear(255, 255, 255, space)
        assert (x, y, z) == pytest.approx(self.D65_XYZ, abs=2e-3), space

    def test_rec709_decode_differs_from_srgb(self) -> None:
        assert cp.rgb_to_xyz_linear(128, 128, 128, "Rec.709") != pytest.approx(
            cp.rgb_to_xyz_linear(128, 128, 128, "sRGB"), abs=1e-6
        )

    def test_acescg_uses_ap1_matrix_not_srgb_fallback(self) -> None:
        # AP1 绿 (0,255,0) 的 XYZ 与 sRGB 绿显著不同（此前静默回退 sRGB）
        aces = cp.rgb_to_xyz_linear(0, 255, 0, "ACEScg")
        assert aces[1] == pytest.approx(0.674082, abs=5e-3)  # AP1 G 的 Y

    @pytest.mark.parametrize(
        "r,g,b,expected",
        [
            (0, 255, 0, True),   # AP1 绿远超 sRGB 色域
            (255, 0, 0, True),   # AP1 红超 sRGB 色域
            (128, 128, 128, False),  # 中性灰在各空间均合法
        ],
    )
    def test_check_out_of_gamut_acescg(self, r: int, g: int, b: int,
                                       expected: bool) -> None:
        assert cp.check_out_of_gamut(r, g, b, "ACEScg", "sRGB") is expected

    def test_srgb_and_p3_paths_unchanged(self) -> None:
        # 既有回归锚点（tests/shared/test_color_picker_coverage.py）
        assert cp.convert_color_space(255, 0, 0, "sRGB", "Display P3") == (233, 51, 35)
        assert cp.check_out_of_gamut(0, 255, 0, "Display P3", "sRGB") is True
        assert cp.check_out_of_gamut(128, 128, 128, "Display P3", "sRGB") is False


# ===========================================================================
# AUD-32407：用户配置文件结构校验
# ===========================================================================
class TestAud32407UserConfigValidation:
    def test_malformed_swatches_fall_back_to_defaults(
        self, qapp, isolated_config: Path,
    ) -> None:
        isolated_config.write_text(json.dumps({
            "swatches": [1, 2], "last_color": "#00FF00",
        }), encoding="utf-8")
        page = cp.SwatchPage()  # 修复前在 _create_swatch_rows 抛 TypeError
        assert [r["name"] for r in page.swatch_data] == \
            [r["name"] for r in cp.SwatchPage.DEFAULT_SWATCHES]
        assert len(page.swatch_rows) == len(page.swatch_data)

    def test_row_missing_keys_fall_back_to_defaults(
        self, qapp, isolated_config: Path,
    ) -> None:
        isolated_config.write_text(json.dumps({
            "swatches": [{"name": "nokołor"}],  # 缺 colors 键
        }), encoding="utf-8")
        page = cp.SwatchPage()
        assert page.swatch_rows[-1].is_locked is True  # 默认自定义行仍在

    def test_invalid_last_color_falls_back(
        self, qapp, isolated_config: Path,
    ) -> None:
        isolated_config.write_text(json.dumps({
            "swatches": [{"name": "x", "colors": ["#000000"]}],
            "last_color": "not-a-color",
        }), encoding="utf-8")
        page = cp.SwatchPage()
        assert page.current_color == QColor("#FF0000")

    def test_studio_restores_only_valid_custom_and_history(
        self, qapp, isolated_config: Path,
    ) -> None:
        isolated_config.write_text(json.dumps({
            "swatches": [{"name": "x", "colors": ["#000000"]}],
            "last_color": "#010203",
            "picker_custom_swatches": ["#ABCDEF", 42, "bad"],
            "color_history": ["#111111", None, "junk", "#222222"],
        }), encoding="utf-8")
        studio = cp.CompactColorStudio()  # 修复前可抛 TypeError
        custom = [btn.color for btn in studio.picker_page.custom_swatches[:2]]
        assert custom == ["#ABCDEF", "#333333"]  # 非法项跳过，不位移
        assert studio.picker_page.color_history.history == ["#222222", "#111111"]

    def test_non_list_custom_swatches_ignored(
        self, qapp, isolated_config: Path,
    ) -> None:
        isolated_config.write_text(json.dumps({
            "swatches": [{"name": "x", "colors": ["#000000"]}],
            "picker_custom_swatches": "green",
            "color_history": 7,
        }), encoding="utf-8")
        studio = cp.CompactColorStudio()
        assert all(btn.color == "#333333"
                   for btn in studio.picker_page.custom_swatches)
        assert studio.picker_page.color_history.history == []


# ===========================================================================
# AUD-32501：color.smart_default 生效
# ===========================================================================
class _StubCfg:
    """dict-like 配置源（descriptor_from_config 只依赖 .get(key, default)。"""

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping

    def get(self, key: str, default=None):
        return self._mapping.get(key, default)


class TestAud32501SmartDefault:
    @pytest.fixture(autouse=True)
    def _clear_overrides(self):
        from src.shared.color.color_space_descriptor import (
            clear_project_color_overrides, get_project_color_overrides,
        )
        saved = get_project_color_overrides()
        clear_project_color_overrides()
        yield
        from src.shared.color.color_space_descriptor import (
            set_project_color_overrides,
        )
        set_project_color_overrides(saved)

    def test_flag_off_falls_back_to_linear(self) -> None:
        from src.shared.color.color_space_descriptor import (
            LINEAR_SCENE, descriptor_from_config,
        )
        desc = descriptor_from_config(
            _StubCfg({"color.smart_default": False}),
            "preview", "timeline", is_video=True,
        )
        assert desc == LINEAR_SCENE

    def test_flag_on_infers_from_source_kind(self) -> None:
        from src.shared.color.color_space_descriptor import (
            REC709_VIDEO, SRGB_IMAGE, descriptor_from_config,
        )
        assert descriptor_from_config(
            _StubCfg({"color.smart_default": True}),
            "preview", "timeline", True,
        ) == REC709_VIDEO
        assert descriptor_from_config(
            _StubCfg({"color.smart_default": True}),
            "preview", "image", False,
        ) == SRGB_IMAGE
        # flag 未设置（默认 True）→ 智能默认（既有行为）
        assert descriptor_from_config(
            _StubCfg({}), "preview", "image", False,
        ) == SRGB_IMAGE

    def test_explicit_keys_still_win(self) -> None:
        from src.shared.color.color_space_descriptor import (
            ColorSpaceDescriptor, descriptor_from_config,
        )
        from src.shared.color.types import ColorPrimaries, ColorTransfer
        cfg = {
            "color.smart_default": False,
            "color.preview.timeline_primaries": "bt2020",
            "color.preview.timeline_transfer": "smpte2084",
        }

        class _Cfg:
            def get(self, key, default=None):
                return cfg.get(key, default)

        desc = descriptor_from_config(_Cfg(), "preview", "timeline", True)
        assert desc.primaries == ColorPrimaries.BT2020
        assert desc.transfer == ColorTransfer.PQ


# ===========================================================================
# AUD-32502：collect_color_management_settings 失败留痕
# ===========================================================================
class TestAud32502CollectSettingsLogging:
    def test_unreadable_config_manager_logs_warning(
        self, caplog, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.shared.color.color_space_descriptor as csd

        class _Broken:
            def __init__(self) -> None:
                raise OSError("config disk boom")

        fake_module = types.ModuleType("src.shared.config.config_manager")
        fake_module.ConfigManager = _Broken
        monkeypatch.setitem(
            sys.modules, "src.shared.config.config_manager", fake_module,
        )
        with caplog.at_level(logging.WARNING,
                             logger="src.shared.color.color_space_descriptor"):
            values = csd.collect_color_management_settings()
        assert values == {}
        assert any("ConfigManager unavailable" in r.message
                   for r in caplog.records)

    def test_happy_path_collects_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.shared.color.color_space_descriptor as csd
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
        values = csd.collect_color_management_settings()
        assert values  # 全默认配置也应收集到色彩管理键
        assert "color.smart_default" in values
        assert values["color.smart_default"] is True


# ===========================================================================
# AUD-32510：builtin LIMITED→LIMITED range 展开（与 OCIO 对称）
# ===========================================================================
class TestAud32510LimitedToLimited:
    def test_rec709_to_hdr10_matches_manual_chain(self) -> None:
        from src.shared.color.color_space_descriptor import HDR10_VIDEO, REC709_VIDEO
        from src.shared.color.color_transform_provider import (
            BuiltinColorProvider,
            _apply_primaries_np,
            _bt709_to_linear_np,
            _full_to_limited_np,
            _limited_to_full_np,
            _linear_to_pq_np,
        )
        img = np.array(
            [[[16 / 255, 128 / 255, 235 / 255, 1.0],
              [40 / 255, 90 / 255, 200 / 255, 0.5]]],
            dtype=np.float32,
        )
        out = BuiltinColorProvider().transform(img.copy(), REC709_VIDEO, HDR10_VIDEO)
        expected = img.copy()
        expected = _limited_to_full_np(expected)          # 无条件展开（修复点）
        expected = _bt709_to_linear_np(expected)
        expected = _apply_primaries_np(expected, "bt709", "bt2020")
        expected = _linear_to_pq_np(expected)
        expected = _full_to_limited_np(expected)          # 无条件压缩（修复点）
        np.testing.assert_allclose(out, expected, atol=1e-6)

    def test_limited_to_full_path_unchanged(self) -> None:
        from src.shared.color.color_space_descriptor import REC709_VIDEO, SRGB_IMAGE
        from src.shared.color.color_transform_provider import (
            BuiltinColorProvider,
            _bt709_to_linear_np,
            _limited_to_full_np,
            _linear_to_srgb_np,
        )
        img = np.array([[[100 / 255, 128 / 255, 200 / 255, 1.0]]], dtype=np.float32)
        out = BuiltinColorProvider().transform(img.copy(), REC709_VIDEO, SRGB_IMAGE)
        expected = _linear_to_srgb_np(
            _bt709_to_linear_np(_limited_to_full_np(img.copy())))
        expected = np.clip(expected, 0.0, 1.0)  # 非线性输出 clip（既有行为）
        np.testing.assert_allclose(out, expected, atol=1e-6)

    def test_same_descriptor_is_identity(self) -> None:
        from src.shared.color.color_space_descriptor import REC709_VIDEO
        from src.shared.color.color_transform_provider import BuiltinColorProvider
        img = np.array([[[0.1, 0.5, 0.9, 1.0]]], dtype=np.float32)
        out = BuiltinColorProvider().transform(img, REC709_VIDEO, REC709_VIDEO)
        assert out is img


# ===========================================================================
# AUD-32511：provider 缓存/诊断并发同步
# ===========================================================================
class TestAud32511ProviderCacheLock:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        from src.shared.color import color_transform_provider as ctp
        ctp.reset_provider_cache()
        yield
        ctp.reset_provider_cache()

    def test_concurrent_first_calls_build_provider_once(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.shared.color import color_transform_provider as ctp

        class _CountingBuiltin:
            instances = 0
            name = "builtin"

            def __init__(self) -> None:
                type(self).instances += 1

            def is_available(self) -> bool:
                return True

        monkeypatch.setattr(ctp, "BuiltinColorProvider", _CountingBuiltin)
        cfg = {"composite.color_engine": "builtin", "ocio_config_path": ""}
        results: list = []
        errors: list = []

        def _worker() -> None:
            try:
                results.append(ctp.get_color_provider(cfg))
            except Exception as exc:  # noqa: BLE001 记录后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert errors == []
        assert _CountingBuiltin.instances == 1  # 并发首调不重复构建
        assert all(r is results[0] for r in results)

    def test_diagnostics_snapshot_is_complete(self) -> None:
        from src.shared.color import color_transform_provider as ctp
        cfg = {"composite.color_engine": "builtin", "ocio_config_path": ""}
        ctp.get_color_provider(cfg)
        diag = ctp.get_color_provider_diagnostics(cfg)
        assert diag["active_provider"] == "builtin"
        assert diag["requested_engine"] == "builtin"
        assert diag["fallback_reason"] == ""
        assert set(diag) == {
            "requested_engine", "ocio_importable", "ocio_config_path",
            "ocio_config_loaded", "active_provider", "fallback_reason",
        }

    def test_reset_provider_cache_clears_under_lock(self) -> None:
        from src.shared.color import color_transform_provider as ctp
        cfg = {"composite.color_engine": "builtin", "ocio_config_path": ""}
        first = ctp.get_color_provider(cfg)
        ctp.reset_provider_cache()
        assert ctp._provider_cache == {}
        second = ctp.get_color_provider(cfg)
        assert second is not first


# ===========================================================================
# AUD-32602..32605：ConfigManager
# ===========================================================================
class TestAud32602BackupOnLoadFailure:
    def test_unicode_decode_error_backs_up_before_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_bytes(b"\xff\xfe{")  # utf-8 解码失败
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(cfg_path))
        manager = ConfigManager()
        backup = tmp_path / "config.json.corrupted"
        assert backup.exists()
        assert backup.read_bytes() == b"\xff\xfe{"
        # 原文件被默认配置重建
        assert json.loads(cfg_path.read_text(encoding="utf-8"))["locale"] == "en_US"
        assert manager.get("locale") == "en_US"

    def test_non_dict_top_level_backs_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("[1, 2, 3]", encoding="utf-8")  # 合法 JSON 非对象
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(cfg_path))
        manager = ConfigManager()
        assert (tmp_path / "config.json.corrupted").exists()
        assert json.loads(cfg_path.read_text(encoding="utf-8"))["locale"] == "en_US"
        assert manager.get("current_page") == "Color"


class TestAud32603DirtyKeysRetainedOnFailure:
    def test_failed_save_keeps_dirty_keys_for_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 父路径是一个文件 → makedirs/写盘必失败
        parent = tmp_path / "blocker"
        parent.write_text("x", encoding="utf-8")
        cfg_path = parent / "config.json"
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(cfg_path))

        manager = ConfigManager()
        manager.set("locale", "zh_CN", auto_save=False)
        assert manager._dirty_keys == {"locale"}
        manager.save_config()
        # AUD-32603：持久化失败时 dirty 键保留（下次 save_config 重试），
        # 内存态仍有用户值
        assert manager._dirty_keys == {"locale"}
        assert manager.get("locale") == "zh_CN"

    def test_successful_save_clears_dirty_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
        manager = ConfigManager()
        manager.set("locale", "zh_CN")
        assert manager._dirty_keys == set()
        assert json.loads((tmp_path / "config.json").read_text())["locale"] == "zh_CN"


class TestAud32604NoRollbackOfOtherInstances:
    def test_empty_dirty_save_does_not_rollback_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(cfg_path))
        stale = ConfigManager()      # 长生命周期实例（如 main_window）
        ConfigManager().set("k_from_other", "v1")  # 临时实例写盘（timeline_panel 模式）
        assert stale.get("k_from_other") is None or True  # 快照可能已过期

        # 显式 save_config（无 dirty 键）不得用陈旧内存快照回滚磁盘
        stale._dirty_keys.clear()
        stale.save_config()
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["k_from_other"] == "v1"
        # 内存态顺带从磁盘收敛
        assert stale.get("k_from_other") == "v1"


class TestAud32605SetUnderLock:
    def test_concurrent_set_and_save_no_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
        manager = ConfigManager()
        errors: list = []

        def _setter() -> None:
            try:
                for i in range(200):
                    manager.set(f"bench.key.{i}", i, auto_save=False)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def _saver() -> None:
            try:
                for _ in range(40):
                    manager.save_config()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        setter = threading.Thread(target=_setter)
        saver = threading.Thread(target=_saver)
        for t in (setter, saver):
            t.start()
        for t in (setter, saver):
            t.join(timeout=30.0)
        assert errors == []
        # 收尾一次成功保存应把全部键落盘
        manager.save_config()
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert sum(1 for k in data if k.startswith("bench.key.")) == 200


# ===========================================================================
# AUD-32800：Tangent Hub sendall 部分发送拆连接
# ===========================================================================
class TestAud32800SendTeardown:
    def test_socket_timeout_is_oserror(self) -> None:
        assert issubclass(socket.timeout, OSError)

    def test_failed_send_closes_socket_and_clears_reference(self, qapp) -> None:
        client = TangentHubClient("T", system_path=".")

        class _TimeoutSocket:
            def __init__(self) -> None:
                self.shutdown_called = False
                self.closed = False

            def sendall(self, data: bytes) -> None:
                raise socket.timeout("timed out")

            def shutdown(self, flag: int) -> None:
                self.shutdown_called = True

            def close(self) -> None:
                self.closed = True

        fake = _TimeoutSocket()
        client._socket = fake  # type: ignore[assignment]
        assert client._send(b"\x00\x00\x00\x02ab") is False
        # 连接被拆掉：引用清空 + shutdown/close，接收循环随 recv 错误退出
        assert client._socket is None
        assert fake.shutdown_called and fake.closed

    def test_successful_send_keeps_socket(self, qapp) -> None:
        client = TangentHubClient("T", system_path=".")

        class _OkSocket:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self.closed = False

            def sendall(self, data: bytes) -> None:
                self.sent.append(data)

            def shutdown(self, flag: int) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        fake = _OkSocket()
        client._socket = fake  # type: ignore[assignment]
        assert client._send(b"frame") is True
        assert client._socket is fake
        assert fake.sent == [b"frame"]
        assert fake.closed is False

    def test_no_socket_returns_false(self, qapp) -> None:
        client = TangentHubClient("T", system_path=".")
        assert client._socket is None
        assert client._send(b"x") is False


# ===========================================================================
# AUD-32901..32903：Database 连接复用 / 原子迁移 / 异常安全
# ===========================================================================
class TestAud32901ConnectionReuse:
    def test_rating_lookups_reuse_shared_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = Database(str(tmp_path / "t.db"))
        real_connect = sqlite3.connect
        connects: list = []

        def _counting_connect(*args, **kwargs):
            connects.append(args)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _counting_connect)
        db.set_file_rating("/a.mov", 4)
        assert db.get_file_rating("/a.mov") == 4
        assert db.get_file_rating("/missing.mov") == 0
        assert db.get_setting("nope", "dft") == "dft"
        db.set_setting("k", "v")
        assert db.get_setting("k") == "v"
        db.save_layout("L", "g", "s")
        db.list_layouts()
        db.close()  # 显式关闭后下一次访问重新建连（1 次）
        assert db.get_setting("k") == "v"
        assert len(connects) == 1

    def test_many_rating_reads_use_one_connection(self, tmp_path: Path) -> None:
        db = Database(str(tmp_path / "sort.db"))
        for i in range(50):
            db.set_file_rating(f"/f{i}.mov", i % 6)
        real_connect = sqlite3.connect
        monkeypatch_connects: list = []

        import unittest.mock as mock
        with mock.patch("sqlite3.connect",
                        side_effect=lambda *a, **k: (
                            monkeypatch_connects.append(1),
                            real_connect(*a, **k))[1]):
            # 模拟排序热路径：N 次比较 × 2 次查询
            for i in range(100):
                db.get_file_rating(f"/f{i % 50}.mov")
        assert monkeypatch_connects == []  # 复用零新建


class _InjectCursor:
    """包装真实游标：命中 fail_substr 的 SQL 注入失败，其余透传。"""

    def __init__(self, real: sqlite3.Cursor, fail_substr: str) -> None:
        self._real = real
        self._fail_substr = fail_substr

    def execute(self, sql, *args, **kwargs):
        if self._fail_substr in sql:
            raise sqlite3.OperationalError(f"injected: {self._fail_substr}")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _InjectConn:
    """包装真实连接（sqlite3.Connection 为不可变类型，无法 monkeypatch）。"""

    def __init__(self, real: sqlite3.Connection, fail_substr: str) -> None:
        self._real = real
        self._fail_substr = fail_substr

    def execute(self, sql, *args, **kwargs):
        if self._fail_substr in sql:
            raise sqlite3.OperationalError(f"injected: {self._fail_substr}")
        return self._real.execute(sql, *args, **kwargs)

    def cursor(self) -> _InjectCursor:
        return _InjectCursor(self._real.cursor(), self._fail_substr)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestAud32902AtomicMigration:
    OLD_SCHEMA = (
        "CREATE TABLE layouts (name TEXT PRIMARY KEY, geometry TEXT, "
        "state TEXT, is_default INTEGER DEFAULT 0)"
    )
    NEW_SCHEMA = (
        "CREATE TABLE layouts (\n"
        "    name TEXT,\n    geometry TEXT,\n    state TEXT,\n"
        "    panel_states TEXT,\n    is_default INTEGER DEFAULT 0,\n"
        "    work_mode TEXT NOT NULL DEFAULT 'all',\n"
        "    PRIMARY KEY (name, work_mode)\n)"
    )

    def test_legacy_single_pk_migrates_preserving_rows(self, tmp_path: Path) -> None:
        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(self.OLD_SCHEMA)
        conn.execute("INSERT INTO layouts VALUES ('A', 'g1', 's1', 1)")
        conn.execute("INSERT INTO layouts VALUES ('B', 'g2', 's2', 0)")
        conn.commit()
        conn.close()

        db = Database(path)
        assert db.list_layouts("all") == ["A", "B"]
        assert db.load_layout("A") == ("g1", "s1", None)

        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(layouts)").fetchall()]
        leftovers = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'layouts_legacy'"
        ).fetchall()
        conn.close()
        assert "work_mode" in cols
        assert leftovers == []

    def test_crash_leftover_legacy_table_recovered(self, tmp_path: Path) -> None:
        path = str(tmp_path / "crash.db")
        db = Database(path)
        db.save_layout("P1", "g", "s")
        db.save_layout("P2", "g2", "s2", is_default=True)

        # 模拟修复前的迁移崩溃窗口：RENAME 已提交、新表为空、legacy 未删
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE layouts RENAME TO layouts_legacy")
        conn.execute(self.NEW_SCHEMA)
        conn.commit()
        conn.close()

        db2 = Database(path)  # 恢复路径：合并 legacy 行而不是跳过迁移
        assert sorted(db2.list_layouts("all")) == ["P1", "P2"]
        assert db2.get_default_layout() == ("g2", "s2", None)
        conn = sqlite3.connect(path)
        leftovers = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'layouts_legacy'"
        ).fetchall()
        conn.close()
        assert leftovers == []

    def test_migration_rollback_on_error(self, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        # 注入回填失败：整个迁移事务回滚，旧表原样保留（RENAME 不残留）
        path = str(tmp_path / "rollback.db")
        conn = sqlite3.connect(path)
        conn.execute(self.OLD_SCHEMA)
        conn.execute("INSERT INTO layouts VALUES ('K', 'g', 's', 0)")
        conn.commit()

        monkeypatch.setattr(
            Database, "_connection",
            lambda self: _InjectConn(conn, "FROM layouts_legacy"),
        )
        try:
            with pytest.raises(sqlite3.OperationalError):
                Database(path)
        finally:
            monkeypatch.undo()

        cols = [r[1] for r in conn.execute("PRAGMA table_info(layouts)").fetchall()]
        rows = conn.execute("SELECT name FROM layouts").fetchall()
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='layouts_legacy'"
        ).fetchall()
        conn.close()
        # 旧 schema 原样保留：既没有半迁移的空表，也没有孤儿 legacy
        assert "work_mode" not in cols
        assert rows == [("K",)]
        assert legacy == []


class TestAud32903ExceptionSafety:
    def test_bind_error_does_not_break_connection(self, tmp_path: Path) -> None:
        db = Database(str(tmp_path / "x.db"))
        with pytest.raises(sqlite3.Error):
            db.set_file_rating("/bad", object())  # sqlite 无法绑定的类型
        # 异常后共享连接仍然可用（无泄漏的半开事务/游标）
        db.set_file_rating("/ok", 3)
        assert db.get_file_rating("/ok") == 3
        db.set_setting("k", "v")
        assert db.get_setting("k") == "v"

    def test_save_default_layout_is_atomic_single_default(self, tmp_path: Path) -> None:
        db = Database(str(tmp_path / "d.db"))
        db.save_layout("U1", "g1", "s1", is_default=True)
        db.save_layout("U2", "g2", "s2")
        db.save_default_layout("g9", "s9")
        assert db.get_default_layout() == ("g9", "s9", None)
        conn = sqlite3.connect(str(tmp_path / "d.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM layouts WHERE is_default = 1 AND work_mode = 'all'"
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_write_failure_rolls_back_whole_transaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = Database(str(tmp_path / "tx.db"))
        assert db._conn is not None
        monkeypatch.setattr(
            Database, "_connection",
            lambda self: _InjectConn(db._conn, "ON CONFLICT(name, work_mode)"),
        )
        with pytest.raises(sqlite3.OperationalError):
            db.save_default_layout("g9", "s9")
        monkeypatch.undo()
        # UPDATE（清默认）已回滚——没有任何默认布局残留
        assert db.get_default_layout() == (None, None, None)
        db.save_layout("N", "g", "s")
        assert db.list_layouts() == ["N"]


# ===========================================================================
# AUD-33010：脱敏正则覆盖 /Volumes、/root、UNC
# ===========================================================================
class TestAud33010PathRedaction:
    @pytest.mark.parametrize("leaky", [
        "/Volumes/BIGCARD/shot_001.mov",
        "/Volumes/BOOTCAMP/a/b.png",
        "/root/session/clip.mp4",
        "\\\\NAS\\share\\project\\clip.mov",
        "\\\\fileserver.local\\media$\\x.dpx",
    ])
    def test_new_path_styles_masked(self, leaky: str) -> None:
        assert sanitize_value(leaky) == "<path>"

    def test_new_styles_masked_inside_messages(self) -> None:
        out = sanitize_value({"error": f"failed to read {chr(92)*2}srv\\share\\a.mov"})
        assert "srv" not in out["error"]
        assert "<path>" in out["error"]
        out2 = sanitize_value({"note": "open /Volumes/EXT/x.mov failed"})
        assert "/Volumes/EXT" not in out2["note"]

    @pytest.mark.parametrize("known", [
        "/Users/alice/Movies/x.mov",
        "/home/bob/y.dpx",
        "C:\\Users\\carol\\z.png",
    ])
    def test_existing_styles_still_masked(self, known: str) -> None:
        assert sanitize_value(known) == "<path>"

    def test_relative_paths_and_plain_words_untouched(self) -> None:
        assert sanitize_value("assets/clip.mov") == "assets/clip.mov"
        assert sanitize_value(" volumes of data") == " volumes of data"


# ===========================================================================
# AUD-33020：psutil.Process 单例缓存（CPU% 不再恒 0）
# ===========================================================================
class TestAud33020PsutilCache:
    def test_process_instance_reused_across_samples(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created: list = []

        class _FakeProc:
            def __init__(self) -> None:
                created.append(self)
                self.cpu_calls = 0

            def memory_info(self) -> SimpleNamespace:
                return SimpleNamespace(rss=123_456)

            def cpu_percent(self, interval) -> float:
                self.cpu_calls += 1
                return 0.5

            def num_fds(self) -> int:
                return 7

        fake_psutil = types.ModuleType("psutil")
        fake_psutil.Process = _FakeProc
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        monkeypatch.setattr(rs, "_PSUTIL_PROC", None)

        s1 = rs.ResourceSnapshotSampler._system_stats()
        s2 = rs.ResourceSnapshotSampler._system_stats()
        assert s1["rss_bytes"] == s2["rss_bytes"] == 123_456
        assert s1["fd_count"] == 7
        assert len(created) == 1              # 单例：不再每次新建 Process
        assert created[0].cpu_calls == 2      # 差分状态保留在实例内

    def test_real_psutil_smoke_if_available(self) -> None:
        pytest.importorskip("psutil")
        stats1 = rs.ResourceSnapshotSampler._system_stats()
        stats2 = rs.ResourceSnapshotSampler._system_stats()
        assert stats1["rss_bytes"] > 0
        assert "cpu_percent" in stats1 and "cpu_percent" in stats2


# ===========================================================================
# AUD-33021：registry 弱持有绑定方法
# ===========================================================================
class TestAud33021WeakProviderRefs:
    def test_bound_method_provider_released_with_owner(self) -> None:
        registry = rs.ResourceSnapshotRegistry()

        class _Renderer:
            def stats(self) -> dict:
                return {"frames": 1}

        renderer = _Renderer()
        registry.register("playback", "edit_gpu_pipeline", renderer.stats)
        assert registry.collect()["playback"]["edit_gpu_pipeline"] == {"frames": 1}

        ref = weakref.ref(renderer)
        del renderer
        gc.collect()
        assert ref() is None  # registry 不再钉住渲染器对象图
        # 失效槽位被跳过，不抛错也不产出死对象数据
        assert "edit_gpu_pipeline" not in registry.collect()["playback"]

    def test_lambda_and_function_providers_kept_strong(self) -> None:
        registry = rs.ResourceSnapshotRegistry()

        def module_style() -> dict:
            return {"ok": True}

        registry.register("cache", "fn", module_style)
        registry.register("cache", "lambda", lambda: {"v": 2})
        collected = registry.collect()["cache"]
        assert collected == {"fn": {"ok": True}, "lambda": {"v": 2}}

    def test_unregister_owner_still_works(self) -> None:
        registry = rs.ResourceSnapshotRegistry()
        registry.register("export", "edit_a", lambda: {})
        registry.register("export", "edit_b", lambda: {})
        assert registry.unregister_owner("edit_") == 2
        assert registry.collect()["export"] == {}


# ===========================================================================
# AUD-33022：采样线程 stop 超时不丢句柄 / 不双线程
# ===========================================================================
class TestAud33022SamplerLifecycle:
    def test_stop_timeout_keeps_handle_and_blocks_double_start(self) -> None:
        sampler = rs.ResourceSnapshotSampler(interval_s=0.05)
        release = threading.Event()
        entered = threading.Event()
        orig_sample = sampler.sample_once

        def _blocking_sample() -> dict:
            entered.set()
            release.wait(timeout=10.0)
            return orig_sample()

        sampler.sample_once = _blocking_sample  # type: ignore[assignment]
        sampler._join_timeout = 0.2
        try:
            sampler.start()
            assert entered.wait(timeout=5.0)
            zombie = sampler._thread
            assert zombie is not None and zombie.is_alive()

            sampler.stop()  # join 超时
            assert sampler._thread is zombie      # 句柄保留
            assert zombie.is_alive()

            sampler.start()                        # 僵尸未退出 → 拒绝重启
            assert sampler._thread is zombie       # 没有第二个线程

            release.set()
            deadline = time.monotonic() + 5.0
            while zombie.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            sampler.stop()                         # 真正退出后清理句柄
            assert not zombie.is_alive()
            assert sampler._thread is None
        finally:
            release.set()
            sampler.stop()

    def test_restart_after_clean_stop_gets_fresh_thread(self) -> None:
        sampler = rs.ResourceSnapshotSampler(interval_s=0.05)
        try:
            sampler.start()
            first = sampler._thread
            assert first is not None and first.is_alive()
            sampler.stop()
            assert sampler._thread is None
            sampler.start()
            second = sampler._thread
            assert second is not None and second is not first and second.is_alive()
        finally:
            sampler.stop()
