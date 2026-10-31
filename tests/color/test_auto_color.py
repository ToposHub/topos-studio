"""Auto Color / Color Match（P1-COLOR-21）测试。

覆盖：
- 数学：灰图零建议、色偏校正（shader 正向模型复核）、曝光收敛到目标、
  对比度限幅、参考匹配恢复统计、predict/difference_metrics
- 控制器：Auto Balance 应用（standalone 单 history push / timeline 单命令）、
  Color Match 从 Gallery 静帧加载并增量应用、绝对/增量语义
- 对话框：复选状态与 selected_keys
- 真实素材：4K ProRes 帧 + 合成色偏 → WB 建议拉平通道均值
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.auto_color import (
    TARGET_LUMA, AutoColorSuggestion, FrameStats, auto_balance,
    difference_metrics, match_to_reference, predict,
)


def _img(h=96, w=128, seed=0, scale=1.0):
    rng = np.random.default_rng(seed)
    img = rng.random((h, w, 3))
    return (np.clip(img * scale, 0.0, 1.0) * 255.0).astype(np.uint8)


def _apply_shader_wb(img_bgr_float, temp, tint, exposure=0.0):
    """shader apply_grading 的 WB + 曝光正向模型（numpy 复刻，BGR 出入一致）。"""
    rgb = img_bgr_float[:, :, ::-1].astype(np.float64)
    stops = np.sign(exposure) * (abs(exposure) / 100.0) ** 2 * 6.0
    rgb *= 2.0 ** stops
    wb = np.array([1.0 + temp / 200.0, 1.0 - tint / 200.0, 1.0 - temp / 200.0])
    rgb *= wb
    rgb = np.clip(rgb, 0.0, 1.0)
    return rgb[:, :, ::-1]  # 回到 BGR，供 FrameStats.from_image 使用


# ─────────────────────────────────────────────────────────────────────
# 数学
# ─────────────────────────────────────────────────────────────────────

class TestAutoBalance:
    def test_neutral_gray_zero_suggestion(self):
        stats = FrameStats.from_image(
            np.full((64, 64, 3), 0.4 * 255, dtype=np.uint8))
        s = auto_balance(stats)
        assert abs(s.temp) < 1.0 and abs(s.tint) < 1.0
        assert abs(s.exposure) < 1.0 and abs(s.contrast) < 1.0

    def test_blue_cast_corrected_by_positive_temp(self):
        # 蓝偏：B 通道均值高 → 建议正 temp（shader: +temp 提 R 压 B）
        img = _img(seed=1).astype(np.float64) / 255.0
        img[:, :, 0] *= 1.4  # BGR: B 通道 ×1.4
        stats = FrameStats.from_image(img)
        s = auto_balance(stats)
        assert s.temp > 5.0

        # shader 正向模型复核：应用建议后通道均值应被拉平
        corrected = _apply_shader_wb(img, s.temp, s.tint)
        before_spread = np.ptp(stats.mean)
        after_spread = np.ptp(FrameStats.from_image(corrected).mean)
        assert after_spread < before_spread * 0.25

    def test_green_cast_corrected_by_tint(self):
        img = _img(seed=2).astype(np.float64) / 255.0
        img[:, :, 1] *= 1.3  # G 偏高 → shader tint 压 G（wb.g = 1 - tint/200）
        stats = FrameStats.from_image(img)
        s = auto_balance(stats)
        assert s.tint > 5.0

    def test_dark_image_gets_positive_exposure(self):
        stats = FrameStats.from_image(_img(seed=3, scale=0.25))
        s = auto_balance(stats)
        assert s.exposure > 5.0
        # 预测应用后中位亮度收敛到目标
        predicted = predict(stats, s.items())
        assert abs(predicted.luma_median - TARGET_LUMA) < 0.08

    def test_bright_image_gets_negative_exposure(self):
        stats = FrameStats.from_image(_img(seed=4, scale=0.95))
        s = auto_balance(stats)
        assert s.exposure < 0

    def test_params_clamped(self):
        # 极端色偏（B ×5）不产生越界参数
        img = _img(seed=5).astype(np.float64) / 255.0
        img[:, :, 0] *= 5.0
        s = auto_balance(FrameStats.from_image(img))
        assert abs(s.temp) <= 100.0 and abs(s.tint) <= 100.0

    def test_absolute_semantics(self):
        s = AutoColorSuggestion(temp=30, absolute=True)
        merged = s.merged_onto({"temp": -20})
        assert merged["temp"] == 30.0  # 绝对：替换

    def test_merged_onto_clamps(self):
        s = AutoColorSuggestion(temp=80, absolute=False)
        merged = s.merged_onto({"temp": 60})
        assert merged["temp"] == 100.0  # 增量限幅


class TestMatchToReference:
    def test_known_gains_recovered(self):
        base = _img(seed=6).astype(np.float64) / 255.0
        # 参考 = 基础图 × 已知通道增益；当前 = 基础图 → 建议应恢复增益
        ref = base.copy()
        ref[:, :, 2] *= 1.15   # R +15%
        ref[:, :, 0] *= 0.85   # B -15%
        s = match_to_reference(FrameStats.from_image(base),
                               FrameStats.from_image(ref))
        assert s.temp > 10.0  # 需要提 R 压 B
        assert not s.absolute  # 增量语义

        # 应用建议后通道均值差收敛
        corrected = _apply_shader_wb(base, s.temp, s.tint)
        before = difference_metrics(
            FrameStats.from_image(base), FrameStats.from_image(ref))
        after = difference_metrics(
            FrameStats.from_image(corrected), FrameStats.from_image(ref))
        assert after["channel_delta"] < before["channel_delta"]

    def test_exposure_matched(self):
        dark = _img(seed=7, scale=0.3)
        bright = _img(seed=7, scale=0.8)
        s = match_to_reference(FrameStats.from_image(dark),
                               FrameStats.from_image(bright))
        assert s.exposure > 5.0

    def test_saturation_matched(self):
        rng = np.random.default_rng(8)
        low = rng.random((64, 64, 3))
        high = np.clip(low * 1.0, 0, 1)
        # high 版本拉大通道差异（更饱和）
        gray = high.mean(axis=2, keepdims=True)
        high = np.clip(gray + (high - gray) * 2.0, 0, 1)
        s = match_to_reference(FrameStats.from_image(low),
                               FrameStats.from_image(high))
        assert s.saturation > 0.0


class TestPredictAndMetrics:
    def test_predict_scales_mean(self):
        stats = FrameStats.from_image(_img(seed=9))
        p = predict(stats, {"temp": 20.0, "exposure": 10.0})
        assert p.mean[0] > stats.mean[0]  # R 增
        assert p.mean[2] < stats.mean[2]  # B 降
        assert p.luma_median > stats.luma_median  # 正曝光

    def test_difference_metrics_zero_for_identical(self):
        stats = FrameStats.from_image(_img(seed=10))
        m = difference_metrics(stats, stats)
        assert m["channel_delta"] == 0.0 and m["luma_delta"] == 0.0

    def test_uint16_input(self):
        img = (np.ones((32, 32, 3)) * 0.5 * 65535).astype(np.uint16)
        stats = FrameStats.from_image(img)
        assert 0.4 < stats.mean[0] < 0.6


# ─────────────────────────────────────────────────────────────────────
# 对话框
# ─────────────────────────────────────────────────────────────────────

class TestAutoColorDialog:
    def test_checkbox_state_and_selection(self, qtbot):
        from src.features.color.ui.auto_color_dialog import AutoColorDialog
        s = AutoColorSuggestion(temp=25.0, tint=-8.0, exposure=12.0,
                                contrast=5.0, saturation=0.0)
        dlg = AutoColorDialog(None, s, {"channel_delta": 0.05},
                              {"channel_delta": 0.01})
        qtbot.addWidget(dlg)
        # 有建议值的默认勾选；饱和度为 0 不勾
        assert dlg.selected_keys() == {"wb", "exposure", "contrast"}
        dlg._checkboxes["saturation"].setChecked(True)
        dlg._toggle_all()  # 全不选
        assert dlg.selected_keys() == set()

    def test_toggle_all_roundtrip(self, qtbot):
        from src.features.color.ui.auto_color_dialog import AutoColorDialog
        s = AutoColorSuggestion(temp=10.0)
        dlg = AutoColorDialog(None, s, {}, {})
        qtbot.addWidget(dlg)
        dlg._toggle_all()
        assert dlg.selected_keys() == {"wb", "exposure", "contrast", "saturation"}


# ─────────────────────────────────────────────────────────────────────
# 控制器
# ─────────────────────────────────────────────────────────────────────

class _EngineStub:
    def __init__(self, layers, source_img, graded_img):
        self.layers = layers
        self.img_high = source_img
        self.img_full_original = source_img
        self.active_layer_index = 0
        self.renderer = None
        self.history = MagicMock()
        self.graded = graded_img

    def get_active_layer(self):
        return self.layers[self.active_layer_index] if self.layers else None

    def grab_viewport_image(self, target_size=None):
        return self.graded


def _make_ctrl(tmp_path, monkeypatch, layers, source_img, graded_img=None):
    from src.app.controllers.layer_controller import LayerOpsMixin
    monkeypatch.chdir(tmp_path)
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    ctrl.engine = _EngineStub(layers, source_img,
                              graded_img if graded_img is not None else source_img)
    ctrl.status_bar = MagicMock()
    for attr in ("grading_panel", "layer_panel", "plugin_panel",
                 "curves_widget", "hsl_curves_panel", "mixer_panel",
                 "effects_panel", "soft_clip_panel", "qualifier_panel",
                 "lut_panel", "warper_panel", "mask_panel", "info_panel"):
        setattr(ctrl, attr, MagicMock())
    ctrl.current_file_is_video = False
    ctrl.request_render = MagicMock()
    ctrl.update_canvas = MagicMock()
    return ctrl


def _force_dialog_keys(monkeypatch, keys):
    """绕过模态对话框，直接返回给定选择（None=取消）。"""
    import src.features.color.ui.auto_color_dialog as acd

    class _Dlg:
        @staticmethod
        def get_application(parent, suggestion, before, after, title=""):
            return set(keys) if keys is not None else None

    monkeypatch.setattr(acd, "AutoColorDialog", _Dlg)


class TestControllerAutoColor:
    def test_auto_balance_applies_absolute_standalone(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        layer = Layer(name="target")
        img = _img(seed=11).astype(np.float64) / 255.0
        img[:, :, 0] *= 1.35  # 蓝偏源
        src = (np.clip(img, 0.0, 1.0) * 255).astype(np.uint8)
        ctrl = _make_ctrl(tmp_path, monkeypatch, [layer], src)
        _force_dialog_keys(monkeypatch, {"wb", "exposure"})

        ctrl.on_auto_color_requested()

        assert layer.grading_params["temp"] > 5.0
        assert layer.grading_params["temperature"] == \
            layer.grading_params["temp"]  # 标准名镜像
        assert layer.grading_params["exposure"] != 0
        assert layer.is_dirty
        assert ctrl.engine.history.push_state.call_count == 1  # 单一 undo

    def test_dialog_cancel_does_nothing(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        layer = Layer(name="target")
        ctrl = _make_ctrl(tmp_path, monkeypatch, [layer], _img(seed=12))
        _force_dialog_keys(monkeypatch, None)
        ctrl.on_auto_color_requested()
        assert layer.grading_params["temp"] == 0
        assert ctrl.engine.history.push_state.call_count == 0

    def test_no_source_shows_message(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer()], _img(seed=13))
        ctrl.engine.img_full_original = None
        ctrl.engine.img_high = None
        ctrl.on_auto_color_requested()
        ctrl.status_bar.showMessage.assert_called_once()

    def test_timeline_routes_to_paste_command(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        layer = Layer(name="target")
        ctrl = _make_ctrl(tmp_path, monkeypatch, [layer], _img(seed=14))
        _force_dialog_keys(monkeypatch, {"wb"})
        ctrl.color_timeline_clip_strip = SimpleNamespace(_active_clip_id="c7")
        ctrl._paste_grade_to_clip = MagicMock(return_value=True)

        ctrl.on_auto_color_requested()

        args = ctrl._paste_grade_to_clip.call_args
        assert args.args[0] == "c7"
        assert args.args[1] == {"primary"}
        # 引擎层未被就地修改（快照层改的）
        assert layer.grading_params["temp"] == 0
        assert ctrl.engine.history.push_state.call_count == 0


class TestControllerColorMatch:
    def test_match_applies_delta_onto_current(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        from src.features.color.core.preset_meta import write_still_images
        layer = Layer(name="target")
        layer.grading_params["temp"] = 20.0  # 已有调色

        base = _img(seed=15).astype(np.float64) / 255.0
        warm = base.copy()
        warm[:, :, 2] *= 1.2  # 参考更暖（R 高）
        topos = os.path.join(os.getcwd(), "ref.topos")
        write_still_images(topos, (np.clip(warm, 0, 1) * 255).astype(np.uint8))

        ctrl = _make_ctrl(tmp_path, monkeypatch, [layer],
                          (np.clip(base, 0, 1) * 255).astype(np.uint8))
        _force_dialog_keys(monkeypatch, {"wb"})
        ctrl.on_match_to_still_requested(topos)

        # 增量语义：在当前 20 之上叠加
        assert layer.grading_params["temp"] > 20.0
        assert ctrl.engine.history.push_state.call_count == 1

    def test_match_without_still_image_shows_message(self, tmp_path, monkeypatch):
        from src.features.color.core.layer import Layer
        ctrl = _make_ctrl(tmp_path, monkeypatch, [Layer()], _img(seed=16))
        _force_dialog_keys(monkeypatch, {"wb"})
        ctrl.on_match_to_still_requested(
            os.path.join(os.getcwd(), "ghost.topos"))
        ctrl.status_bar.showMessage.assert_called_once()


# ─────────────────────────────────────────────────────────────────────
# 真实素材：4K ProRes 帧 + 合成色偏
# ─────────────────────────────────────────────────────────────────────

class TestRealMaterialAutoColor:
    def test_prores_cast_corrected(self, tmp_path):
        import cv2
        video_dir = os.path.join(os.path.dirname(__file__), "..", "test_videos")
        path = os.path.join(video_dir, "4K_prores422HQ.mov")
        if not os.path.isfile(path):
            pytest.skip("real material not available")
        cap = cv2.VideoCapture(path)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            pytest.skip("cv2 cannot decode this ProRes file")

        # 注入蓝偏后，auto_balance 应给出正 temp 并显著拉平通道。
        # AUD-R2-004：素材是 gitignored 本地文件（2026-09-27 被替换过），
        # 当前 4K ProRes 帧自带暖偏（R 均值高出 G/B 约 26%）。直接 B×1.3
        # 并不构成"蓝偏主导"（B≈R），temp>5 的前提不成立——这是素材漂移，
        # 不是 auto_balance 的数值缺陷（src 自特性提交以来数值未变，
        # 对实际像素仍给出正确的灰世界解）。先把各通道均值归一到灰世界
        # 中性再注入 B×1.3，使"蓝偏主导"前提对任意素材成立：
        # 实测 temp≈25.5、通道 spread 校正后比值≈0.12。
        cast = frame.astype(np.float64)
        means = cast.reshape(-1, 3).mean(axis=0)
        cast *= float(means.mean()) / np.maximum(means, 1e-6)
        cast[:, :, 0] *= 1.3  # B 偏
        cast = np.clip(cast, 0, 255).astype(np.uint8)
        stats = FrameStats.from_image(cast)
        s = auto_balance(stats)

        assert s.temp > 5.0
        corrected = _apply_shader_wb(
            cast.astype(np.float64) / 255.0, s.temp, s.tint)
        after = FrameStats.from_image(corrected)
        assert np.ptp(after.mean) < np.ptp(stats.mean) * 0.3
