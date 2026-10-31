"""Regression tests for audit batch-A fixes (w4-g8) — shared UI/utils.

Covers four Critical findings:

- AUD-38000: ``ShortcutManager.import_preset`` must validate per-entry
  types of external presets.  Non-string values (list/dict are unhashable,
  int/None would poison config) must be skipped and reported, never
  hashed, applied, or persisted.
- AUD-38205: the eyedropper overlay must not be destroyed inside its own
  event-handling stack.  ``_clear_dropper`` defers destruction with
  ``deleteLater()`` so the C++ object survives the synchronous emission
  of ``colorPicked``/``cancelled`` and is only deleted at an event-loop
  safe point.
- AUD-38404: ``HistogramWidget.update_histogram`` must sanitize NaN
  input before ``np.bincount`` (NaN passes ``np.clip``, casts to
  INT_MIN and makes bincount raise ValueError inside a Qt slot →
  qFatal under PyQt6).
- AUD-38501: ``get_largest_inscribed_rect`` 0°/90° branches had the
  width/height constraints swapped, returning "safe" crop rects larger
  than the image (e.g. 2:1 preset on 1000x1000 → (2000, 1000)).

Widget tests run headless (QT_QPA_PLATFORM=offscreen, qapp fixture from
tests/conftest.py).  No config manager is attached to the ShortcutManager
instances, so nothing touches disk.
"""
from __future__ import annotations

import gc
import math
import os
import sys

# 必须在创建 QApplication 之前设置 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import numpy as np

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtGui import QColor

from src.shared.color import EyeDropperOverlay
from src.shared.ui.shortcut_manager import ShortcutManager
from src.shared.ui.widgets.color_picker_control import ColorPickerControl
from src.shared.ui.widgets.vectorscope_widget import HistogramWidget
from src.shared.utils.geometry_math import get_largest_inscribed_rect

pytestmark = pytest.mark.regression


# ============================================================
# AUD-38501 — geometry_math.get_largest_inscribed_rect
# ============================================================
class TestInscribedRectZeroDegreeAUD38501:
    """0°（最常见路径）：安全区域绝不能大于原图。"""

    def test_wider_preset_constrained_by_width(self):
        # 旧代码返回 (2000, 1000) —— 宽度超出原图一倍。
        assert get_largest_inscribed_rect(1000, 1000, 0, aspect_ratio=2.0) == (1000.0, 500.0)

    def test_taller_preset_constrained_by_height(self):
        assert get_largest_inscribed_rect(1000, 1000, 0, aspect_ratio=0.5) == (500.0, 1000.0)

    def test_square_preset_on_landscape(self):
        # 旧代码返回 (1920, 1920) —— 高度超出原图 77%。
        assert get_largest_inscribed_rect(1920, 1080, 0, aspect_ratio=1.0) == (1080.0, 1080.0)

    def test_default_ratio_returns_image(self):
        assert get_largest_inscribed_rect(1920, 1080, 0) == (1920.0, 1080.0)

    @pytest.mark.parametrize("w,h", [(1920, 1080), (1080, 1920), (1000, 1000), (640, 480)])
    @pytest.mark.parametrize("ar", [0.25, 0.5, 1.0, 1.3333, 16 / 9, 2.0, 4.0])
    def test_zero_degree_always_fits_inside_image(self, w, h, ar):
        wc, hc = get_largest_inscribed_rect(w, h, 0, aspect_ratio=ar)
        assert wc <= w + 1e-6, f"宽度 {wc} 超出原图 {w}"
        assert hc <= h + 1e-6, f"高度 {hc} 超出原图 {h}"
        assert wc > 0 and hc > 0
        # 比例必须严格保持
        assert wc / hc == pytest.approx(ar, rel=1e-9)


class TestInscribedRectGeneralAUD38501:
    """任意角度：旋转投影约束成立 + 结果最大化（0°/90° 统一走通用公式）。"""

    @pytest.mark.parametrize(
        "angle",
        [0, 0.0001, 5, 15, 30, 45, 60, 75, 89.9,
         89.999999, 90, 180, 270, 359.9999, -15, -90],
    )
    @pytest.mark.parametrize("ar", [0.25, 1.0, 16 / 9, 4.0])
    @pytest.mark.parametrize("w,h", [(1920, 1080), (1000, 1000)])
    def test_projection_constraints_hold(self, w, h, angle, ar):
        """轴对齐裁切框的两个旋转投影必须落在原图内（数学上正确的
        "fits inside" 判据；非 0° 时 h_c 合法地可以超过 h）。"""
        wc, hc = get_largest_inscribed_rect(w, h, angle, aspect_ratio=ar)
        a = abs(math.radians(angle)) % (math.pi / 2)
        s, c = math.sin(a), math.cos(a)
        assert wc * c + hc * s <= w + 1e-6
        assert wc * s + hc * c <= h + 1e-6
        assert wc / hc == pytest.approx(ar, rel=1e-9)
        # 最大化：至少一个约束取等（留 1e-3 数值容差）
        assert (wc * c + hc * s >= w - 1e-3) or (wc * s + hc * c >= h - 1e-3)

    def test_exact_90_normalized_to_0(self):
        """90° 输入归一到 0°（radians(90) % (pi/2) == 0），与 0° 同解。"""
        assert get_largest_inscribed_rect(
            1920, 1080, 90, aspect_ratio=1.0) == pytest.approx((1080.0, 1080.0))

    def test_near_90_swapped_constraint(self):
        """cos < 1e-6 的近 90° 分支：受交换后的高度约束（w_c <= h）。"""
        angle = math.degrees(math.pi / 2 - 1e-9)
        wc, hc = get_largest_inscribed_rect(1920, 1080, angle, aspect_ratio=1.0)
        assert (wc, hc) == pytest.approx((1080.0, 1080.0), abs=1e-3)


# ============================================================
# AUD-38000 — ShortcutManager.import_preset per-value validation
# ============================================================
@pytest.fixture()
def manager(qapp):
    m = ShortcutManager()
    m.register("file.open", "Open", "File", "Ctrl+O")
    m.register("file.save", "Save", "File", "Ctrl+S")
    m.register("edit.undo", "Undo", "Edit", "Ctrl+Z")
    yield m


def _preset(manager, **overrides):
    """export_preset 骨架 + 任意覆盖项（可注入非法值）。"""
    data = manager.export_preset()
    data["shortcuts"].update(overrides)
    return data


class TestImportPresetValueValidationAUD38000:
    def test_unhashable_list_value_reported_not_crash(self, manager):
        """list 值在旧代码进入 setdefault(seq, ...) 抛 TypeError（槽内
        未处理异常 → qFatal）。现在必须被逐值校验拦下并计入 errors。"""
        preset = _preset(manager, **{"file.open": [1]})

        report = manager.import_preset(preset)  # 不得 raise

        assert not report.ok
        assert "file.open" not in report.applied
        assert any("file.open" in e and "list" in e for e in report.errors)
        # 非法值绝不落盘
        assert "file.open" not in manager._custom_shortcuts
        # 其余合法项仍被应用
        assert "file.save" in report.applied
        assert manager.get_shortcut("file.save") == "Ctrl+S"

    def test_dict_value_reported(self, manager):
        report = manager.import_preset(_preset(manager, **{"file.save": {"k": "v"}}))
        assert any("file.save" in e and "dict" in e for e in report.errors)
        assert "file.save" not in report.applied

    def test_hashable_non_str_value_reported(self, manager):
        """int 值可哈希，旧代码会穿透到 set_shortcut 污染 config，并在
        之后 QKeySequence(seq) 抛 TypeError。"""
        report = manager.import_preset(_preset(manager, **{"edit.undo": 5}))
        assert any("edit.undo" in e and "int" in e for e in report.errors)
        assert "edit.undo" not in report.applied
        assert "edit.undo" not in manager._custom_shortcuts

    def test_none_value_reported(self, manager):
        report = manager.import_preset(_preset(manager, **{"file.open": None}))
        assert any("file.open" in e and "NoneType" in e for e in report.errors)

    def test_non_str_action_id_reported(self, manager):
        """非字符串 key 落进 unknown 后 sorted() 会因混合类型抛
        TypeError；必须改计入 errors。"""
        preset = _preset(manager)
        preset["shortcuts"][42] = "Ctrl+Q"

        report = manager.import_preset(preset)  # 不得 raise

        assert any("42" in e for e in report.errors)
        assert report.to_dict()["errors"] == list(report.errors)

    def test_conflict_detection_still_works_for_valid_values(self, manager):
        """合法字符串值仍参与预设内冲突检测（不因新校验而失效）。"""
        preset = _preset(manager, **{"file.open": "Ctrl+X", "file.save": "Ctrl+X"})
        report = manager.import_preset(preset)
        assert report.conflicts == (("Ctrl+X", ["file.open", "file.save"]),)
        assert manager.get_shortcut("file.open") == "Ctrl+O"

    def test_valid_roundtrip_still_ok(self, manager):
        report = manager.import_preset(manager.export_preset())
        assert report.ok
        assert sorted(report.applied) == ["edit.undo", "file.open", "file.save"]


# ============================================================
# AUD-38404 — HistogramWidget NaN/Inf 防护
# ============================================================
class TestHistogramNaNAUD38404:
    def _widget(self, qapp):
        widget = HistogramWidget()
        return widget

    def test_nan_input_does_not_raise(self, qapp):
        """含 NaN 的 HDR 帧：旧代码 np.bincount 抛 ValueError（QTimer
        槽内未捕获 → qFatal 终止进程）。"""
        widget = self._widget(qapp)
        img = np.zeros((8, 8, 3), dtype=np.float32)
        img[0, 0, 2] = np.nan  # R 通道（BGR 输入）
        img[4, 4, 1] = np.nan  # G 通道

        widget.update_histogram(img)  # 不得 raise

        assert widget._hist_rgb is not None
        assert np.all(np.isfinite(widget._hist_rgb))

    def test_all_nan_image_counts_into_bin_zero(self, qapp):
        widget = self._widget(qapp)
        img = np.full((4, 4, 3), np.nan, dtype=np.float32)

        widget.update_histogram(img)

        r_hist = widget._hist_rgb[0]
        assert int(np.argmax(r_hist)) == 0  # NaN → 0（黑）
        assert float(r_hist.max()) > 0

    def test_posinf_clamped_to_top_bin(self, qapp):
        widget = self._widget(qapp)
        img = np.zeros((4, 4, 3), dtype=np.float32)
        img[:, :, 2] = np.inf  # R = +inf（超白）

        widget.update_histogram(img)

        assert int(np.argmax(widget._hist_rgb[0])) == 255

    def test_neginf_counts_into_bin_zero(self, qapp):
        widget = self._widget(qapp)
        img = np.zeros((4, 4, 3), dtype=np.float32)
        img[:, :, 0] = -np.inf  # B = -inf

        widget.update_histogram(img)

        assert int(np.argmax(widget._hist_rgb[2])) == 0

    def test_clean_input_unchanged_by_sanitizer(self, qapp):
        """合法输入的统计不受清洗影响（回归保护既有行为）。"""
        widget = self._widget(qapp)
        img = np.zeros((8, 8, 3), dtype=np.float32)
        img[:, :, 2] = 1.0  # 纯红（BGR）

        widget.update_histogram(img)

        assert int(np.argmax(widget._hist_rgb[0])) == 255
        assert int(np.argmax(widget._hist_rgb[1])) == 0


# ============================================================
# AUD-38205 — 吸管覆盖层生命周期
# ============================================================
class TestEyedropperLifetimeAUD38205:
    def _make_control(self, qapp) -> ColorPickerControl:
        return ColorPickerControl((0.2, 0.3, 0.4, 1.0))

    def test_overlay_survives_color_picked_dispatch(self, qapp):
        """colorPicked 由覆盖层自身 mousePressEvent 同步发射；槽返回后
        覆盖层不得因引用计数归零被立即析构（use-after-free）。"""
        ctrl = self._make_control(qapp)
        destroyed: list[bool] = []
        overlay = EyeDropperOverlay()
        overlay.destroyed.connect(lambda: destroyed.append(True))
        ctrl._active_dropper = overlay
        # 与 _start_eyedropper 完全相同的接线（start() 会抓屏，测试跳过）
        overlay.colorPicked.connect(ctrl._commit_color)
        overlay.cancelled.connect(ctrl._clear_dropper)

        overlay.colorPicked.emit(QColor(255, 0, 0))  # 模拟 mousePressEvent 的发射

        # 槽已同步执行：颜色提交、成员清空
        assert ctrl._active_dropper is None
        assert ctrl._color == QColor(255, 0, 0)
        # 丢掉测试自身持有的最后一个 Python 引用
        del overlay
        gc.collect()
        # deleteLater 挂起期间，sip 不得因引用归零析构 C++ 对象
        assert not destroyed

        # 事件循环安全点：延迟删除真正执行
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert destroyed

    def test_overlay_survives_cancelled_dispatch(self, qapp):
        """Esc 取消路径：cancelled.emit() → _clear_dropper 同样不得在
        覆盖层自身 keyPressEvent 栈内析构它。"""
        ctrl = self._make_control(qapp)
        destroyed: list[bool] = []
        overlay = EyeDropperOverlay()
        overlay.destroyed.connect(lambda: destroyed.append(True))
        ctrl._active_dropper = overlay
        # 与 _start_eyedropper 完全相同的接线（start() 会抓屏，测试跳过）
        overlay.colorPicked.connect(ctrl._commit_color)
        overlay.cancelled.connect(ctrl._clear_dropper)

        overlay.cancelled.emit()  # 模拟 keyPressEvent(Esc) 的发射

        assert ctrl._active_dropper is None
        del overlay
        gc.collect()
        assert not destroyed
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert destroyed

    def test_clear_without_dropper_is_noop(self, qapp):
        """_commit_color 从拾色器路径进入时 _active_dropper 为 None，
        不得抛 AttributeError。"""
        ctrl = self._make_control(qapp)
        ctrl._clear_dropper()
        assert ctrl._active_dropper is None
