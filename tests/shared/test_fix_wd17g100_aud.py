"""wd17-g100：计划表批 E（颜色工具 widgets + vault 桥）8 条 Major 修复验证。

覆盖（docs/audit/2026-09-28/batches/）：
- AUD-38302 / 38303 / 38304：batch-383-shared-ui-widgets.md（HSLCurvesWidget）
- AUD-38308：batch-383-shared-ui-widgets.md（LutGallery）
- AUD-38405 / 38406：batch-384-shared-ui-widgets.md（VectorscopeWidget）
- AUD-38600 / 38601：batch-386-shared-vault.md（vault bridge）

QPainter/QWidget 用例依赖 tests/conftest.py 的 qapp fixture
（QT_QPA_PLATFORM=offscreen）。
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

# 必须在创建 QApplication 之前设置 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from PyQt6.QtCore import QPointF, Qt, QEvent
from PyQt6.QtGui import QMouseEvent

from src.shared.ui.widgets import hsl_curves_widget as hsl_module
from src.shared.ui.widgets.hsl_curves_widget import (
    HSLCurvesWidget, MODE_ORDER,
)
from src.shared.ui.widgets.vectorscope_widget import (
    VectorscopeWidget, REC709_CB_COEFF, REC709_CR_COEFF,
)


# =============================================================================
# 公共构件
# =============================================================================

def _make_hsl_widget(qapp) -> HSLCurvesWidget:
    return HSLCurvesWidget()


def _press_left(widget, wx: float, wy: float) -> None:
    """构造左键按下事件并直接送入 mousePressEvent。"""
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(wx, wy),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.mousePressEvent(event)


def _identity_lut() -> np.ndarray:
    """中性 LUT（6 行：mult 行全 1.0，HueHue shift 行全 0.0）。"""
    lut = np.ones((6, 256), dtype=np.float32)
    hue_idx = MODE_ORDER.index('HueHue')
    lut[hue_idx] = 0.0
    return lut


class _SignalSpy:
    """同步记录信号参数的轻量 spy。"""

    def __init__(self, signal) -> None:
        self.calls: list = []
        signal.connect(self._slot)

    def _slot(self, *args) -> None:
        self.calls.append(args)

    @property
    def count(self) -> int:
        return len(self.calls)


# =============================================================================
# AUD-38302：set_lut(emit_changed=True) 的 changed 信号必须送达
# =============================================================================

class TestAud38302SetLutSignalDelivered:
    def test_emit_changed_true_delivers_changed(self, qapp) -> None:
        """正例：emit_changed=True 时 changed 信号同步送达接收方。"""
        w = _make_hsl_widget(qapp)
        spy = _SignalSpy(w.changed)

        lut = _identity_lut()
        lut[0, :] = 1.5  # LumSat 行给一个非中性值
        w.set_lut(lut, emit_changed=True)

        assert spy.count == 1, "set_lut(emit_changed=True) 必须发射 changed"
        emitted_lut, emitted_data = spy.calls[0]
        assert emitted_lut.shape == (6, 256)
        assert emitted_lut[0, 0] == pytest.approx(1.5)
        assert isinstance(emitted_data, dict) and 'LumSat' in emitted_data

    def test_emit_changed_true_not_blocked_by_internal_blocksignals(self, qapp) -> None:
        """反例（回归锚点）：修复前 blockSignals 会吞掉发射且清空 _pending_lut，
        16ms 定时器重放也无事可做——信号计数恒为 0。"""
        w = _make_hsl_widget(qapp)
        spy = _SignalSpy(w.changed)
        w.set_lut(_identity_lut(), emit_changed=True)
        # 同步路径已发射；不再有待处理项
        assert spy.count == 1
        assert w._pending_lut is None
        assert not w._update_timer.isActive()

    def test_emit_changed_false_silent(self, qapp) -> None:
        """反例：emit_changed=False（默认）不发射信号，但曲线仍被恢复。"""
        w = _make_hsl_widget(qapp)
        spy = _SignalSpy(w.changed)

        lut = _identity_lut()
        lut[1, :] = 0.25  # HueHue shift
        w.set_lut(lut, emit_changed=False)

        assert spy.count == 0
        assert w._pending_lut is None
        assert not w._update_timer.isActive()
        # 数据路径未受影响：控制点由 LUT 采样恢复
        assert len(w.curves['HueHue']) == 9

    def test_invalid_lut_shape_ignored(self, qapp) -> None:
        """边界：形状非法的 LUT 直接忽略，不发射不崩溃。"""
        w = _make_hsl_widget(qapp)
        spy = _SignalSpy(w.changed)
        w.set_lut(np.ones((3, 256), dtype=np.float32), emit_changed=True)
        w.set_lut(None, emit_changed=True)
        assert spy.count == 0


# =============================================================================
# AUD-38303：hue 端点 wrap-around 同步必须置脏并重建 LUT
# =============================================================================

class TestAud38303HueEndpointSyncRebuild:
    def _setup_desynced_hue_curve(self, qapp) -> HSLCurvesWidget:
        """HueHue 曲线两个端点 Y 不一致（旧工程恢复态）。"""
        w = _make_hsl_widget(qapp)
        w._current_mode = 'HueHue'
        data = {m: [(0.0, 127.5), (255.0, 127.5)] for m in MODE_ORDER}
        data['HueHue'] = [(0.0, 60.0), (255.0, 200.0)]
        w.set_curves_data(data, emit_changed=False)
        # set_curves_data 已重建缓存并清脏——模拟"渲染侧与数据一致"
        assert w._lut_dirty is False
        return w

    def test_click_endpoint_marks_dirty_and_queues_changed(self, qapp) -> None:
        """正例：点击端点触发同步后置脏、重建 LUT、changed 进入待发。"""
        w = self._setup_desynced_hue_curve(qapp)
        spy = _SignalSpy(w.changed)

        wx, wy = w.to_widget(0.0, 60.0)  # 点击左端点
        _press_left(w, wx, wy)

        # 端点已同步
        assert w.curves['HueHue'][0].y() == pytest.approx(60.0)
        assert w.curves['HueHue'][-1].y() == pytest.approx(60.0)
        # 置脏并重建（update_lut 清脏），changed 已挂到节流待发
        assert w._pending_lut is not None
        w.flush()
        assert spy.count == 1
        # LUT 与 UI 一致：HueHue 行在 x=0/255 处的 shift = (127.5-60)/255
        hue_idx = MODE_ORDER.index('HueHue')
        expected_shift = (127.5 - 60.0) / 255.0
        assert spy.calls[0][0][hue_idx, 0] == pytest.approx(expected_shift, abs=1e-4)
        assert spy.calls[0][0][hue_idx, 255] == pytest.approx(expected_shift, abs=1e-4)

    def test_click_synced_endpoint_no_spurious_emit(self, qapp) -> None:
        """反例：端点已同步时点击不应触发置脏/信号（避免无意义重渲染）。"""
        w = _make_hsl_widget(qapp)
        w._current_mode = 'HueHue'
        data = {m: [(0.0, 127.5), (255.0, 127.5)] for m in MODE_ORDER}
        w.set_curves_data(data, emit_changed=False)
        assert w._lut_dirty is False

        spy = _SignalSpy(w.changed)
        wx, wy = w.to_widget(0.0, 127.5)
        _press_left(w, wx, wy)

        assert w._pending_lut is None
        assert spy.count == 0
        assert not w._update_timer.isActive()

    def test_non_hue_curve_click_never_syncs(self, qapp) -> None:
        """反例：非 hue 曲线（LumSat）端点 Y 不一致时点击不同步。"""
        w = _make_hsl_widget(qapp)
        w._current_mode = 'LumSat'
        data = {m: [(0.0, 127.5), (255.0, 127.5)] for m in MODE_ORDER}
        data['LumSat'] = [(0.0, 60.0), (255.0, 200.0)]
        w.set_curves_data(data, emit_changed=False)

        wx, wy = w.to_widget(0.0, 60.0)
        _press_left(w, wx, wy)

        assert w.curves['LumSat'][-1].y() == pytest.approx(200.0)


# =============================================================================
# AUD-38304：calculate_curve 裸 except → 具体异常
# =============================================================================

class TestAud38304CalculateCurveExceptions:
    def test_normal_two_point_linear(self, qapp) -> None:
        """正例：两点线性路径正常返回 256 值。"""
        w = _make_hsl_widget(qapp)
        vals = w.calculate_curve([QPointF(0, 100.0), QPointF(255, 200.0)])
        assert vals.shape == (256,)
        assert vals[0] == pytest.approx(100.0)
        assert vals[255] == pytest.approx(200.0)

    def test_degenerate_points_fallback_linear_interp(self, qapp) -> None:
        """正例：重复 X 触发 Pchip ValueError → 降级 np.interp 不崩溃。"""
        w = _make_hsl_widget(qapp)
        pts = [QPointF(0, 10.0), QPointF(0, 10.0), QPointF(255, 200.0)]
        vals = w.calculate_curve(pts)
        assert vals.shape == (256,)
        assert np.all(np.isfinite(vals))
        assert vals[0] == pytest.approx(10.0)
        assert vals[255] == pytest.approx(200.0)

    def test_non_domain_error_propagates(self, qapp) -> None:
        """反例：插值域之外的编程错误（如 RuntimeError）必须穿透，
        不允许被兜底吞掉（裸 except 会掩盖 bug）。"""
        w = _make_hsl_widget(qapp)
        pts = [QPointF(0, 10.0), QPointF(128, 60.0), QPointF(255, 200.0)]

        def _boom(*args, **kwargs):
            raise RuntimeError("programmer error")

        with patch.object(hsl_module, "PchipInterpolator", _boom), \
             patch.object(hsl_module, "HAS_SCIPY", True):
            with pytest.raises(RuntimeError):
                w.calculate_curve(pts)


# =============================================================================
# AUD-38308：LUT 复制失败不得把幽灵条目加入图库
# =============================================================================

class TestAud38308LutImportCopyFailure:
    @pytest.fixture
    def gallery(self, qapp, tmp_path, monkeypatch) -> "object":
        """lut_dir 指向 tmp 的 LutGallery（初始为空）。"""
        lut_dir = tmp_path / "luts"
        lut_dir.mkdir()
        monkeypatch.setattr(
            "src.shared.ui.widgets.lut_gallery.get_lut_dir", lambda: str(lut_dir)
        )
        from src.shared.ui.widgets.lut_gallery import LutGallery
        g = LutGallery()
        assert g.list_widget.count() == 0
        return g

    def test_copy_failure_adds_no_entry(self, qapp, tmp_path, monkeypatch, gallery) -> None:
        """正例：shutil.copy2 抛 OSError 时不添加任何条目。"""
        src = tmp_path / "outside.cube"
        src.write_text("LUT_3D_SIZE 2\n")

        def _fail(src_, dst_):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(
            "src.shared.ui.widgets.lut_gallery.shutil.copy2", _fail
        )
        gallery.add_lut_item(str(src), copy_to_local=True)

        assert gallery.list_widget.count() == 0, "复制失败不得添加幽灵条目"
        # 图库里没有任何条目指向不存在的路径
        for i in range(gallery.list_widget.count()):
            path = gallery.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            assert os.path.exists(path)

    def test_copy_success_adds_entry_with_real_file(self, qapp, tmp_path, gallery) -> None:
        """反例（对照）：复制成功正常入图库且路径存在。"""
        src = tmp_path / "outside2.cube"
        src.write_text("LUT_3D_SIZE 2\n")
        gallery.add_lut_item(str(src), copy_to_local=True)

        assert gallery.list_widget.count() == 1
        item = gallery.list_widget.item(0)
        path = item.data(Qt.ItemDataRole.UserRole)
        assert os.path.exists(path), "成功路径必须指向真实文件"
        assert path != str(src)  # 已复制进 lut_dir
        assert item.text() == "outside2.cube"

    def test_local_path_no_copy_still_added(self, qapp, tmp_path, gallery) -> None:
        """边界：源已在 lut_dir 内（无需复制）仍正常添加。"""
        local = tmp_path / "luts" / "inplace.cube"
        local.write_text("LUT_3D_SIZE 2\n")
        gallery.add_lut_item(str(local), copy_to_local=True)
        assert gallery.list_widget.count() == 1

    def test_copy_failure_no_signals_fired(self, qapp, tmp_path, monkeypatch, gallery) -> None:
        """边界：复制失败时不触发 lut_selected 等信号。"""
        from src.shared.ui.widgets.lut_gallery import LutGallery  # noqa: F401
        src = tmp_path / "gone.cube"
        src.write_text("LUT_3D_SIZE 2\n")

        def _fail(src_, dst_):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(
            "src.shared.ui.widgets.lut_gallery.shutil.copy2", _fail
        )
        fired: list = []
        gallery.lut_selected.connect(lambda p: fired.append(p))
        gallery.add_lut_item(str(src), copy_to_local=True)
        assert fired == []


# =============================================================================
# AUD-38405：Vectorscope 不得 clip(0,1) 扭曲 HDR 色度
# =============================================================================

def _run_vectorscope(w: VectorscopeWidget, img: np.ndarray) -> tuple:
    """直接驱动 _process_image（绕过节流定时器），返回 (cb, cr)。"""
    w._pending_image = img
    w._pending_mask = None
    w._process_image()
    return w._cached_cb, w._cached_cr


class TestAud38405HdrChromaPreserved:
    def test_superwhite_red_keeps_chroma(self, qapp) -> None:
        """正例：HDR 红 (2.4,1.2,1.2) 投影到 (1,0.5,0.5)——色度不再塌缩。
        旧实现 clip → (1,1,1) → cb=cr=0（中心）。"""
        w = VectorscopeWidget()
        img = np.full((4, 4, 3), (2.4, 1.2, 1.2), dtype=np.float32)
        cb, cr = _run_vectorscope(w, img)

        expected_rgb = np.array([1.0, 0.5, 0.5], dtype=np.float32)
        exp_cb = float(np.dot(expected_rgb, REC709_CB_COEFF))
        exp_cr = float(np.dot(expected_rgb, REC709_CR_COEFF))
        assert np.allclose(cb, exp_cb, atol=1e-5)
        assert np.allclose(cr, exp_cr, atol=1e-5)
        # 与旧实现（塌缩到中心）明确区分
        assert np.max(np.abs(cr)) > 0.1

    def test_hdr_not_treated_as_uint8_codes(self, qapp) -> None:
        """正例：分数 HDR 值（2.4）不得被 /255 启发式压到近黑
        （旧实现 max>1.5 一律 /255 → cb/cr 缩小 255 倍塌缩到中心）。"""
        w = VectorscopeWidget()
        img = np.full((4, 4, 3), (2.4, 1.2, 1.2), dtype=np.float32)
        cb, _ = _run_vectorscope(w, img)
        expected_rgb = np.array([1.0, 0.5, 0.5], dtype=np.float32)
        assert np.allclose(cb, float(np.dot(expected_rgb, REC709_CB_COEFF)), atol=1e-5)
        assert np.max(np.abs(cb)) > 0.05  # 若被 /255 会只剩 ~-0.00066

    def test_sdr_unchanged(self, qapp) -> None:
        """反例（回归）：SDR 值（≤1.0）不被归一化，cb/cr 与直接点积一致。"""
        w = VectorscopeWidget()
        color = np.array([0.5, 0.25, 0.1], dtype=np.float32)
        img = np.full((4, 4, 3), color, dtype=np.float32)
        cb, cr = _run_vectorscope(w, img)
        assert np.allclose(cb, float(np.dot(color, REC709_CB_COEFF)), atol=1e-6)
        assert np.allclose(cr, float(np.dot(color, REC709_CR_COEFF)), atol=1e-6)

    def test_superwhite_neutral_stays_centered(self, qapp) -> None:
        """边界：超白中性 (4.2,4.2,4.2) → (1,1,1)，仍位于中心。"""
        w = VectorscopeWidget()
        img = np.full((4, 4, 3), (4.2, 4.2, 4.2), dtype=np.float32)
        cb, cr = _run_vectorscope(w, img)
        assert np.allclose(cb, 0.0, atol=1e-6)
        assert np.allclose(cr, 0.0, atol=1e-6)

    def test_mixed_hdr_sdr(self, qapp) -> None:
        """正例：同一帧内 HDR 与 SDR 像素各自正确（>1 归一，≤1 原样）。"""
        w = VectorscopeWidget()
        img = np.tile(
            np.array([[[2.4, 1.2, 1.2], [0.5, 0.25, 0.1]]], dtype=np.float32),
            (4, 4, 1),
        )
        cb, cr = _run_vectorscope(w, img)
        hdr_rgb = np.array([1.0, 0.5, 0.5], dtype=np.float32)
        sdr_rgb = np.array([0.5, 0.25, 0.1], dtype=np.float32)
        assert np.allclose(cb[0::2], float(np.dot(hdr_rgb, REC709_CB_COEFF)), atol=1e-5)
        assert np.allclose(cb[1::2], float(np.dot(sdr_rgb, REC709_CB_COEFF)), atol=1e-5)

    def test_negative_still_floored_at_zero(self, qapp) -> None:
        """边界：负值仍截到 0（与旧 clip 下界一致，负 RGB 无示波语义）。"""
        w = VectorscopeWidget()
        img = np.full((4, 4, 3), (-0.5, 0.2, 0.2), dtype=np.float32)
        cb, cr = _run_vectorscope(w, img)
        floored = np.array([0.0, 0.2, 0.2], dtype=np.float32)
        assert np.allclose(cb, float(np.dot(floored, REC709_CB_COEFF)), atol=1e-6)

    def test_legacy_uint8_range_heuristic_kept(self, qapp) -> None:
        """回归：整数码值的 0-255 域输入仍除以 255（遗留 uint8 路径）。"""
        w = VectorscopeWidget()
        img = np.full((4, 4, 3), (255.0, 128.0, 128.0), dtype=np.float32)
        cb, cr = _run_vectorscope(w, img)
        norm = np.array([1.0, 128.0 / 255.0, 128.0 / 255.0], dtype=np.float32)
        assert np.allclose(cb, float(np.dot(norm, REC709_CB_COEFF)), atol=1e-5)

    def test_input_buffer_not_mutated(self, qapp) -> None:
        """防护：处理过程不得改动上游渲染缓冲（旧 clip 产生新数组，
        新实现必须保持这一性质）。"""
        w = VectorscopeWidget()
        img = np.full((4, 4, 3), (2.4, -1.0, 1.2), dtype=np.float32)
        before = img.copy()
        _run_vectorscope(w, img)
        assert np.array_equal(img, before)


# =============================================================================
# AUD-38406：稳定缓存采样（去掉逐帧全量置换）
# =============================================================================

def _heterogeneous_big_image() -> np.ndarray:
    """> 200k 像素的异质图像（任何两个不同随机子集的直方图必不同）。"""
    h, w = 500, 450  # 225000 > 200000 → 走随机采样分支
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.stack([
        xx / w, yy / h, ((xx + yy) % 64) / 64.0
    ], axis=-1).astype(np.float32)
    return img


class TestAud38406StableSampling:
    def test_frame_to_frame_trace_stable(self, qapp) -> None:
        """正例：同一图像两帧的 cb/cr 完全一致（采样点固定，迹线不闪烁）。
        旧实现逐帧 np.random.choice(replace=False) 重随机，异质图像下
        两帧结果必然不同。"""
        w = VectorscopeWidget()
        img = _heterogeneous_big_image()

        cb1, cr1 = _run_vectorscope(w, img)
        cb2, cr2 = _run_vectorscope(w, img)

        assert np.array_equal(cb1, cb2)
        assert np.array_equal(cr1, cr2)

    def test_indices_cached_and_bounded(self, qapp) -> None:
        """正例：索引按 (N, k) 缓存复用同一对象，且全部落在 [0, N)。"""
        w = VectorscopeWidget()
        idx1 = w._stable_sample_indices(1000, 200)
        idx2 = w._stable_sample_indices(1000, 200)
        assert idx1 is idx2, "同 key 必须命中缓存（不再每次置换）"
        assert idx1.shape == (200,)
        assert idx1.min() >= 0 and idx1.max() < 1000

    def test_indices_regenerated_on_size_change(self, qapp) -> None:
        """反例：像素总数变化时重新生成并更新缓存 key。"""
        w = VectorscopeWidget()
        idx_small = w._stable_sample_indices(1000, 500)
        idx_big = w._stable_sample_indices(2000, 500)
        assert idx_small is not idx_big
        assert idx_big.max() < 2000
        # 缓存指向最新 key
        assert w._stable_sample_indices(2000, 500) is idx_big

    def test_seed_reproducible_across_instances(self, qapp) -> None:
        """正例：固定种子 → 不同 widget 实例对同尺寸生成相同索引集。"""
        w1, w2 = VectorscopeWidget(), VectorscopeWidget()
        a = w1._stable_sample_indices(4096, 1000)
        b = w2._stable_sample_indices(4096, 1000)
        assert np.array_equal(a, b)

    def test_small_image_uses_all_pixels(self, qapp) -> None:
        """回归：total ≤ target 时不抽样（全像素、原顺序）。"""
        w = VectorscopeWidget()
        img = np.tile(
            np.array([[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]], dtype=np.float32),
            (6, 4, 1),  # → (6, 8, 3) = 48 像素（≥10 采样下限）
        )
        cb, cr = _run_vectorscope(w, img)
        assert cb.shape[0] == 48  # 全像素、保持行主序
        red = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        green = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        assert np.allclose(cb[0::2], float(np.dot(red, REC709_CB_COEFF)), atol=1e-6)
        assert np.allclose(cb[1::2], float(np.dot(green, REC709_CB_COEFF)), atol=1e-6)


# =============================================================================
# AUD-38600：segment_id 显式入参不得被 clip_id 优先级缺陷丢弃
# =============================================================================

class TestAud38600SegmentIdPrecedence:
    def test_explicit_segment_id_kept_when_clip_id_empty(self, qapp) -> None:
        """正例（审计主诉）：clip_id 为空 + 显式 segment_id → 用 segment_id。
        旧实现解析为 `(segment_id or ...) if clip_id else ...`，
        clip_id 为空时 segment_id 被静默丢弃。"""
        from src.shared.vault.bridge import clip_to_semantic_segment
        seg = clip_to_semantic_segment(
            {"clip_id": "", "start_sec": 1.0, "end_sec": 2.0},
            asset_id="a1", fps=24, segment_id="explicit-sid",
        )
        assert seg.segment_id == "explicit-sid"

    def test_clip_id_used_when_no_segment_id(self, qapp) -> None:
        """正例：无 segment_id + 有 clip_id → vault_clip_{clip_id}。"""
        from src.shared.vault.bridge import clip_to_semantic_segment
        seg = clip_to_semantic_segment(
            {"clip_id": "c42", "start_sec": 0.0, "end_sec": 1.0},
            asset_id="a1", fps=24,
        )
        assert seg.segment_id == "vault_clip_c42"

    def test_fallback_when_both_missing(self, qapp) -> None:
        """正例：两者皆无 → vault_shot_{start_frame}。"""
        from src.shared.vault.bridge import clip_to_semantic_segment
        seg = clip_to_semantic_segment(
            {"start_sec": 2.0, "end_sec": 3.0}, asset_id="a1", fps=24,
        )
        assert seg.segment_id == "vault_shot_48"

    def test_segment_id_wins_over_clip_id(self, qapp) -> None:
        """反例：两者都有时 segment_id 优先。"""
        from src.shared.vault.bridge import clip_to_semantic_segment
        seg = clip_to_semantic_segment(
            {"clip_id": "c42", "start_sec": 0.0, "end_sec": 1.0},
            asset_id="a1", fps=24, segment_id="override",
        )
        assert seg.segment_id == "override"


# =============================================================================
# AUD-38601：relevance → confidence 必须验证外部值
# =============================================================================

class TestAud38601RelevanceValidation:
    def test_zero_relevance_preserved(self, qapp) -> None:
        """正例（审计主诉）：合法 0.0 不得被 `or` 误判为缺失置 1.0。"""
        from src.shared.vault.bridge import clip_to_semantic_segment
        seg = clip_to_semantic_segment(
            {"clip_id": "c1", "start_sec": 0.0, "end_sec": 1.0,
             "relevance": 0.0},
            asset_id="a1", fps=24,
        )
        assert seg.confidence == 0.0

    def test_valid_fraction_preserved(self, qapp) -> None:
        from src.shared.vault.bridge import clip_to_semantic_segment
        seg = clip_to_semantic_segment(
            {"clip_id": "c1", "start_sec": 0.0, "end_sec": 1.0,
             "relevance": 0.87},
            asset_id="a1", fps=24,
        )
        assert seg.confidence == pytest.approx(0.87)

    def test_missing_or_none_defaults_to_one(self, qapp) -> None:
        from src.shared.vault.bridge import clip_to_semantic_segment
        for clip in (
            {"clip_id": "c1", "start_sec": 0.0, "end_sec": 1.0},
            {"clip_id": "c1", "start_sec": 0.0, "end_sec": 1.0,
             "relevance": None},
        ):
            seg = clip_to_semantic_segment(clip, asset_id="a1", fps=24)
            assert seg.confidence == 1.0

    def test_out_of_range_clamped_not_raising(self, qapp) -> None:
        """正例：越界值（2.5 / -0.3）钳制到 [0,1]，不再抛 ValueError 穿透。"""
        from src.shared.vault.bridge import clip_to_semantic_segment
        hi = clip_to_semantic_segment(
            {"clip_id": "c1", "start_sec": 0.0, "end_sec": 1.0,
             "relevance": 2.5}, asset_id="a1", fps=24,
        )
        lo = clip_to_semantic_segment(
            {"clip_id": "c1", "start_sec": 0.0, "end_sec": 1.0,
             "relevance": -0.3}, asset_id="a1", fps=24,
        )
        assert hi.confidence == 1.0
        assert lo.confidence == 0.0

    def test_non_numeric_relevance_defaults_not_raising(self, qapp) -> None:
        """正例：非数值（"high"）回退默认值，不再 ValueError 穿透。"""
        from src.shared.vault.bridge import clip_to_semantic_segment
        seg = clip_to_semantic_segment(
            {"clip_id": "c1", "start_sec": 0.0, "end_sec": 1.0,
             "relevance": "high"}, asset_id="a1", fps=24,
        )
        assert seg.confidence == 1.0

    def test_nan_relevance_defaults(self, qapp) -> None:
        from src.shared.vault.bridge import clip_to_semantic_segment
        seg = clip_to_semantic_segment(
            {"clip_id": "c1", "start_sec": 0.0, "end_sec": 1.0,
             "relevance": float("nan")}, asset_id="a1", fps=24,
        )
        assert seg.confidence == 1.0

    def test_search_hit_image_and_audio_confidence(self, qapp) -> None:
        """search_hit_to_segments 两处（VISUAL/AUDIO）同样走校验。"""
        from src.shared.vault.bridge import search_hit_to_segments
        segs = search_hit_to_segments(
            {"vault_media_type": "image", "vault_relevance": 0.0},
            asset_id="a1",
        )
        assert len(segs) == 1 and segs[0].confidence == 0.0

        segs = search_hit_to_segments(
            {"vault_media_type": "audio", "vault_relevance": 2.5},
            asset_id="a1",
        )
        assert len(segs) == 1 and segs[0].confidence == 1.0

    def test_search_hit_invalid_relevance_not_raising(self, qapp) -> None:
        from src.shared.vault.bridge import search_hit_to_segments
        segs = search_hit_to_segments(
            {"vault_media_type": "image", "vault_relevance": "garbage"},
            asset_id="a1",
        )
        assert len(segs) == 1 and segs[0].confidence == 1.0
