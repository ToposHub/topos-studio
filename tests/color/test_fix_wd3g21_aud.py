"""wd3-g21（计划批 D）color 模块/跟踪/shaders 域 AUD 修复回归测试。

对应 REMEDIATION_PLAN.md 批 D 的 16 条 Major 条目：

- AUD-24501  Tangent Hub 驱动 slot 不再向 Qt 事件循环抛异常
- AUD-24601  color.capture_still / toggle_comparison 独占默认键序
- AUD-24611  缺失 CMS 键按运行时默认预选（image range 不再静默 limited）
- AUD-24612  load() 对外部配置数值做防御式转换
- AUD-24702  F-Gamut 矩阵按官方基色重新生成（不再是 Rec.2020 复制）
- AUD-24703  RAW_VIEW 3D LUT 半 texel 中心修正
- AUD-24905  合并过滤只作用于回发结果，缓存保存完整结果
- AUD-24906  task_started 先于缓存命中的 task_finished
- AUD-24907  缓存键包含 shape_geometry 摘要
- AUD-25105  set_params_dict 恢复后同步堆栈页面
- AUD-25106  reset_all/reset_blur 堆栈页面归位
- AUD-25200  曲线逐点羽化手柄缩放基准用 logical_size
- AUD-25201  _is_valid_crop 用真实图像边界（非平移扩展过的 sceneRect）
- AUD-25202  RAW 取色 Linear 直通 + Gamma 2.6 分支
- AUD-25203  颜色检查器复用取色器坐标映射与 RAW 色彩管理
- AUD-25204  set_crop_params 收窄异常并记录
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from PyQt6.QtCore import QPointF, QRectF, QSizeF
from PyQt6.QtWidgets import QGraphicsScene

from src.features.color.ui.canvas import (
    Canvas,
    CropRectItem,
    MaskShapeItem,
)
from src.shared.control_surfaces.tangent.settings import (
    COLOR_WHEEL_ROTATION_CONFIG_KEY,
    DEFAULT_COLOR_WHEEL_ROTATION_DEGREES,
    DEFAULT_TANGENT_SENSITIVITY,
    DEFAULT_TRACKERBALL_SMOOTHING,
    TRACKERBALL_SMOOTHING_CONFIG_KEY,
)
from src.shared.ui.shortcut_manager import ShortcutManager
from src.shared.tracking.tracker_2d import TrackDirection
from src.shared.tracking.types import (
    FailureReason,
    TrackStatus,
    TrackTransform,
)

from tests.color import tracker_qt_support as tq

pytestmark = [pytest.mark.regression, pytest.mark.color_core]


# ---------------------------------------------------------------------------
# AUD-24501: Tangent Hub 驱动 slot 的异常降级
# ---------------------------------------------------------------------------


class _FakeHubClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, float]] = []

    def send_parameter_value(self, parameter_id, value, at_default):
        self.sent.append((parameter_id, float(value)))
        return True


class _FakeTimer:
    def __init__(self, active: bool = False) -> None:
        self._active = active

    def isActive(self) -> bool:
        return self._active

    def start(self, *args) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False


class _FakeGradingPanel:
    """同步反馈 grading_changed 的假调色面板（12 参数签名）。"""

    def __init__(self, bridge) -> None:
        self._bridge = bridge
        self.params: dict[str, Any] = {
            "lift": (10.0, 0.2, 0.0), "gamma": (0.0, 0.0, 0.0),
            "gain": (0.0, 0.0, 0.0), "exposure": 0.0, "temp": 0.0,
            "tint": 0.0, "contrast": 0.0, "shadow": 0.0, "saturation": 0.0,
            "vibrance": 0.0, "mid_detail": 0.0, "hue_shift": 0.0,
        }
        self.set_params_calls: list[tuple] = []

    def get_current_params(self) -> dict[str, Any]:
        return dict(self.params)

    def set_params(self, lift, gamma, gain, exposure, temp, tint, contrast,
                   shadow, saturation, vibrance, mid_detail, hue_shift,
                   emit=False):
        self.params.update({
            "lift": tuple(lift) if isinstance(lift, (tuple, list)) else lift,
            "gamma": tuple(gamma) if isinstance(gamma, (tuple, list)) else gamma,
            "gain": tuple(gain) if isinstance(gain, (tuple, list)) else gain,
            "exposure": exposure, "temp": temp, "tint": tint,
            "contrast": contrast, "shadow": shadow,
            "saturation": saturation, "vibrance": vibrance,
            "mid_detail": mid_detail, "hue_shift": hue_shift,
        })
        self.set_params_calls.append(lift)
        if emit:
            # 模拟真实面板：解除 _syncing_ui 后同步 emit grading_changed
            self._bridge._on_panel_changed(
                lift, gamma, gain,
                exposure, temp, tint, contrast, shadow, saturation,
                vibrance, mid_detail, hue_shift)


def _make_bridge():
    from src.features.color.integrations.tangent_bridge import (
        TangentColorBridge, _TrackerballPoint,
    )

    bridge = TangentColorBridge.__new__(TangentColorBridge)
    bridge._panel = _FakeGradingPanel(bridge)
    bridge._main_window = SimpleNamespace(
        on_interaction_started=lambda: None, on_interaction_ended=lambda: None)
    bridge._gesture_open = True
    bridge._gesture_timer = _FakeTimer()
    bridge._flushing_trackerball = False
    bridge._sensitivity = {}
    bridge._trackerball_timer = _FakeTimer(active=True)
    bridge._trackerball_smoothing = 0.0
    bridge._trackerball_points = {
        wheel: _TrackerballPoint() for wheel in ("lift", "gamma", "gain")}
    bridge._wheel_rotation_degrees = 0.0
    bridge._hue_direction = 1.0
    bridge._last_sent_display_values = {}
    bridge._active_surface = "auto"
    bridge.client = _FakeHubClient()
    return bridge


BALL_LIFT_X = 0x00310021   # _WheelParameter("lift", 0, ...)
RING_LIFT = 0x00310024     # _WheelParameter("lift", 2, ...)


class TestAUD24501SlotExceptionsDowngraded:
    def test_parameter_reset_invalid_wheel_dropped_not_raised(self, caplog):
        bridge = _make_bridge()
        bridge._panel.params["lift"] = "broken"
        with caplog.at_level(logging.WARNING):
            bridge._on_parameter_reset(BALL_LIFT_X)   # 旧实现 raise ValueError
            bridge._on_parameter_reset(RING_LIFT)     # 旧实现 raise ValueError
        assert bridge._panel.set_params_calls == []
        assert any("Dropping Tangent parameter reset" in r.message
                   for r in caplog.records)

    def test_parameter_reset_valid_wheel_still_resets(self):
        bridge = _make_bridge()
        bridge._panel.params["lift"] = (10.0, 0.5, 1.0)
        bridge._on_parameter_reset(BALL_LIFT_X)
        assert bridge._panel.params["lift"] == (10.0, 0.0, 1.0)
        assert bridge._panel.set_params_calls, "正常路径必须发布"

    def test_parameter_change_invalid_wheel_dropped_not_raised(self, caplog):
        bridge = _make_bridge()
        bridge._panel.params["lift"] = None
        with caplog.at_level(logging.WARNING):
            # 旧实现 float(None[2]) → TypeError 逃逸进事件循环
            bridge._on_parameter_change(RING_LIFT, 5.0)
        assert bridge._panel.set_params_calls == []
        assert bridge.client.sent == []
        assert any("Dropping Tangent parameter change" in r.message
                   for r in caplog.records)

    def test_flush_trackerball_invalid_wheel_skipped_not_raised(self, caplog):
        bridge = _make_bridge()
        bridge._panel.params["lift"] = None
        state = bridge._trackerball_points["gamma"]
        state.pending_x = 0.1
        state.pending_axes = 1
        state.pending_started_ns = 1
        with caplog.at_level(logging.WARNING):
            bridge._flush_trackerball_motion()   # 旧实现 raise ValueError
        assert any("Skipping trackerball frame" in r.message
                   for r in caplog.records)
        # 坏轮被跳过，pending 状态仍被消费（不残留）
        assert state.pending_x == 0.0
        assert state.pending_axes == 0
        # 坏轮值未被写入面板
        assert bridge._panel.params["lift"] is None

    def test_sync_from_panel_invalid_wheels_skip_without_raise(self, caplog):
        bridge = _make_bridge()
        for wheel in ("lift", "gamma", "gain"):
            bridge._panel.params[wheel] = None
        with caplog.at_level(logging.WARNING):
            bridge.sync_from_panel()
        assert bridge.client.sent == []
        assert any("Skipping Hub sync" in r.message for r in caplog.records)

    def test_parameter_value_invalid_returns_none(self):
        bridge = _make_bridge()
        from src.features.color.integrations.tangent_bridge import (
            _WheelParameter,
        )
        spec = _WheelParameter("lift", 2, -1.0, 1.0)
        assert bridge._parameter_value({"lift": None}, spec) is None
        assert bridge._parameter_value({"lift": ("a", "b", "c")}, spec) is None
        assert bridge._parameter_value({"lift": (1.0, 2.0, 3.0)}, spec) == 3.0


# ---------------------------------------------------------------------------
# AUD-24601: color 模块默认快捷键独占
# ---------------------------------------------------------------------------


class TestAUD24601ShortcutDefaults:
    def test_color_defaults_are_exclusive(self, qapp):
        from src.features.color.module import ColorModule

        sm = ShortcutManager()
        # 模拟 menu_toolbar_controller 先注册的 file.save_as（Ctrl+Shift+S）
        sm.register("file.save_as", "Save Project As...", "File",
                    "Ctrl+Shift+S")
        ColorModule().register_shortcuts(sm)

        assert sm.get_shortcut("color.capture_still") == "Ctrl+Alt+S"
        assert sm.get_shortcut("color.toggle_comparison") == "Ctrl+Alt+D"
        # 新默认不与任何其他已注册动作冲突（排除自身）
        assert sm.find_conflicts("Ctrl+Alt+S",
                                 exclude_action_id="color.capture_still") == []
        assert sm.find_conflicts("Ctrl+Alt+D",
                                 exclude_action_id="color.toggle_comparison") == []
        # Ctrl+Shift+S 目录内只剩 file.save_as
        assert [sa.id for sa in sm.find_conflicts("Ctrl+Shift+S")] == [
            "file.save_as"]
        # Ctrl+Shift+D 不再被任何默认占用
        assert sm.find_conflicts("Ctrl+Shift+D") == []

    def test_canvas_qactions_use_same_exclusive_sequences(self):
        import inspect

        from src.features.color.ui import canvas as canvas_mod

        text = inspect.getsource(canvas_mod)
        assert 'capture_still_act.setShortcut("Ctrl+Alt+S")' in text
        assert 'capture_reference_act.setShortcut("Ctrl+Alt+D")' in text
        assert 'setShortcut("Ctrl+Shift+S")' not in text
        assert 'setShortcut("Ctrl+Shift+D")' not in text

    def test_conflicting_default_logs_warning(self, qapp, caplog):
        from src.features.color.module import ColorModule

        sm = ShortcutManager()
        sm.register("other.action", "Other", "Other", "Ctrl+Alt+S")
        with caplog.at_level(logging.WARNING):
            ColorModule().register_shortcuts(sm)
        assert any("conflicts with" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# AUD-24611 / AUD-24612: 偏好页 CMS 缺键预选与防御式数值转换
# ---------------------------------------------------------------------------


class _RecordingConfig:
    """dict 型 ConfigManager 替身（记录 set 调用）。"""

    def __init__(self, store=None):
        self.store: dict = dict(store or {})
        self.set_calls: list[tuple[str, Any]] = []

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value, auto_save=True):
        self.set_calls.append((key, value))
        self.store[key] = value


def _make_pref_page(qapp):
    from src.features.color.preferences import ColorProcessingPreferencePage

    page = ColorProcessingPreferencePage()
    # 生产路径 parent 为对话框；测试无 parent 时必须持有返回的顶层
    # 控件引用，否则 C++ 侧整棵子树被回收，_widgets 变成悬空包装。
    page._top_widget = page.create_widget(None)
    return page


class TestAUD24611MissingCmsKeysPreselectRuntimeDefaults:
    def test_missing_keys_preselect_runtime_defaults(self, qapp):
        page = _make_pref_page(qapp)
        page.load({})   # 完全缺键
        # image 范围运行时默认 FULL（SRGB_IMAGE），不再是组合框首项 limited
        assert page._widgets["color.preview.image_range"].currentData() == "full"
        assert page._widgets["color.output.image_range"].currentData() == "full"
        # timeline 范围运行时默认 LIMITED（REC709_VIDEO）
        assert page._widgets["color.preview.timeline_range"].currentData() == "limited"
        assert page._widgets["color.output.timeline_range"].currentData() == "limited"
        # timeline 传输函数预选 Rec.709（smart-default 语义）
        assert page._widgets["color.preview.timeline_transfer"].currentData() == "bt709"
        assert page._widgets["color.preview.image_transfer"].currentData() == "iec61966_2_1"
        for key in ("color.preview.image_primaries",
                    "color.output.timeline_primaries"):
            assert page._widgets[key].currentData() == "bt709"

    def test_apply_persists_runtime_defaults_not_index_zero(self, qapp):
        page = _make_pref_page(qapp)
        page.load({})
        cfg = _RecordingConfig()
        page.apply(cfg)
        written = dict(cfg.set_calls)
        assert written["color.preview.image_range"] == "full"
        assert written["color.output.image_range"] == "full"
        assert written["color.preview.timeline_range"] == "limited"

    def test_explicit_config_values_preserved(self, qapp):
        page = _make_pref_page(qapp)
        page.load({"color.output.image_range": "limited"})
        assert page._widgets["color.output.image_range"].currentData() == "limited"

    def test_restore_defaults_missing_range_keys_preselect(self, qapp):
        page = _make_pref_page(qapp)
        # DEFAULT_CONFIG 未覆盖的 range 键同样按运行时默认预选
        page.restore_defaults({
            "default_color_space": "sRGB",
            "default_bit_depth": "8-bit",
            "gpu_color_processing": True,
            "show_color_space_warnings": True,
        })
        assert page._widgets["color.preview.image_range"].currentData() == "full"
        assert page._widgets["color.output.image_range"].currentData() == "full"


class TestAUD24612DefensiveNumericConversions:
    def test_bad_numeric_values_fall_back_without_abort(self, qapp, caplog):
        page = _make_pref_page(qapp)
        bad = {
            COLOR_WHEEL_ROTATION_CONFIG_KEY: "not-a-number",
            TRACKERBALL_SMOOTHING_CONFIG_KEY: None,
            "tangent.lift_brightness_sensitivity": "abc",
            "default_denoise_strength": None,
            "default_sharpen_amount": "x",
            # 数值键之后的组仍应被加载（证明 load 未半途中断）
            "ai_gpu_device": "cpu",
        }
        with caplog.at_level(logging.WARNING):
            page.load(bad)   # 旧实现在此 ValueError，其后控件全部停在构建默认
        assert page._widgets[COLOR_WHEEL_ROTATION_CONFIG_KEY].value() == \
            DEFAULT_COLOR_WHEEL_ROTATION_DEGREES
        assert page._widgets[TRACKERBALL_SMOOTHING_CONFIG_KEY].value() == \
            DEFAULT_TRACKERBALL_SMOOTHING
        assert page._widgets["tangent.lift_brightness_sensitivity"].value() == \
            DEFAULT_TANGENT_SENSITIVITY["tangent.lift_brightness_sensitivity"]
        assert page._widgets["default_denoise_strength"].value() == 50
        assert page._widgets["default_sharpen_amount"].value() == 0
        # AI 组（位于 Tangent 数值键之后）也被正确加载
        assert page._widgets["ai_gpu_device"].currentData() == "cpu"
        assert any("Invalid numeric value" in r.message for r in caplog.records)

    def test_valid_numeric_values_still_load(self, qapp):
        page = _make_pref_page(qapp)
        page.load({
            COLOR_WHEEL_ROTATION_CONFIG_KEY: 123.5,
            TRACKERBALL_SMOOTHING_CONFIG_KEY: 40,
            "default_denoise_strength": 77,
        })
        assert page._widgets[COLOR_WHEEL_ROTATION_CONFIG_KEY].value() == 123.5
        assert page._widgets[TRACKERBALL_SMOOTHING_CONFIG_KEY].value() == 40
        assert page._widgets["default_denoise_strength"].value() == 77


# ---------------------------------------------------------------------------
# AUD-24702 / AUD-24703: shaders.py F-Gamut 矩阵与 RAW_VIEW LUT 采样
# ---------------------------------------------------------------------------


def _rgb_to_xyz_matrix(primaries, wp):
    """基色（列向量）+ 白点 → RGB→XYZ 矩阵。"""
    m = np.zeros((3, 3))
    for i, (x, y) in enumerate(primaries):
        m[:, i] = [x / y, 1.0, (1 - x - y) / y]
    wp_xyz = np.array([wp[0] / wp[1], 1.0, (1 - wp[0] - wp[1]) / wp[1]])
    s = np.linalg.solve(m, wp_xyz)
    return m * s


def _bradford(src_wp, dst_wp):
    ma = np.array([[0.8951, 0.2664, -0.1614],
                   [-0.7502, 1.7135, 0.0367],
                   [0.0389, -0.0685, 1.0296]])
    src = np.array([src_wp[0] / src_wp[1], 1.0,
                    (1 - src_wp[0] - src_wp[1]) / src_wp[1]])
    dst = np.array([dst_wp[0] / dst_wp[1], 1.0,
                    (1 - dst_wp[0] - dst_wp[1]) / dst_wp[1]])
    return np.linalg.inv(ma) @ np.diag((ma @ dst) / (ma @ src)) @ ma


_D50 = (0.3457, 0.3585)
_D65 = (0.3127, 0.3290)
_PROPHOTO = [(0.7347, 0.2653), (0.1596, 0.8404), (0.0366, 0.0001)]


def _prophoto_to_target(primaries, wp):
    """ProPhoto(D50) → 目标色域(wp) 的行主序矩阵（Bradford CAT）。"""
    m_target = _rgb_to_xyz_matrix(primaries, wp)
    m_pp = _rgb_to_xyz_matrix(_PROPHOTO, _D50)
    return np.linalg.inv(m_target) @ _bradford(_D50, wp) @ m_pp


def _shader_mat3(shader: str, condition: str) -> np.ndarray:
    """提取 ``u_raw_color_space == <condition>`` 分支里的 mat3 九系数。

    返回按构造器书写顺序 reshape 的 3x3（即真实行主序矩阵的转置）。
    """
    idx = shader.index(condition)
    chunk = shader[idx:idx + 700]
    match = re.search(r"mat3\(\s*([^;]+?)\);", chunk, re.S)
    assert match is not None, f"mat3 not found after {condition!r}"
    nums = [float(x) for x in re.findall(r"-?\d+\.\d+", match.group(1))]
    assert len(nums) == 9
    return np.array(nums).reshape(3, 3)


class TestAUD24702FGamutMatrix:
    def test_methodology_reproduces_known_srgb_matrix(self):
        """方法论自校验：复算 ProPhoto→sRGB 与文件既有（已验证）矩阵一致。"""
        from src.features.color.shaders.shaders import RAW_VIEW_FRAGMENT

        shader_block = _shader_mat3(RAW_VIEW_FRAGMENT, "u_raw_color_space == 0")
        expected = _prophoto_to_target(
            [(0.64, 0.33), (0.30, 0.60), (0.15, 0.06)], _D65)
        np.testing.assert_allclose(shader_block.T, expected, atol=3e-4)

    def test_fgamut_matrix_matches_official_primaries(self):
        """F-Gamut 分支系数 = Fujifilm 公布基色 + Bradford CAT 的独立复算。"""
        from src.features.color.shaders.shaders import RAW_VIEW_FRAGMENT

        shader_block = _shader_mat3(RAW_VIEW_FRAGMENT, "u_raw_color_space == 13")
        expected = _prophoto_to_target(
            [(0.7347, 0.2653), (0.1424, 0.8576), (0.0991, -0.0040)], _D65)
        np.testing.assert_allclose(shader_block.T, expected, atol=3e-4)
        # 参考量级：[0][0] ≈ 1.070（旧复制值为 1.2006，即 Rec.2020）
        assert expected[0][0] == pytest.approx(1.0696, abs=1e-3)

    def test_fgamut_no_longer_duplicates_rec2020(self):
        from src.features.color.shaders.shaders import RAW_VIEW_FRAGMENT

        fgamut = _shader_mat3(RAW_VIEW_FRAGMENT, "u_raw_color_space == 13")
        rec2020 = _shader_mat3(RAW_VIEW_FRAGMENT, "u_raw_color_space == 2)")
        assert not np.allclose(fgamut, rec2020), \
            "F-Gamut 矩阵不得再是 Rec.2020 矩阵的逐字节复制"

    def test_rec2020_matrix_unchanged(self):
        """既有正确的 Rec.2020 矩阵未被本修复波及。"""
        from src.features.color.shaders.shaders import RAW_VIEW_FRAGMENT

        shader_block = _shader_mat3(RAW_VIEW_FRAGMENT, "u_raw_color_space == 2)")
        expected = _prophoto_to_target(
            [(0.708, 0.292), (0.170, 0.797), (0.131, 0.046)], _D65)
        np.testing.assert_allclose(shader_block.T, expected, atol=3e-4)


class TestAUD24703RawViewLutHalfTexel:
    def test_raw_view_lut_uses_texel_center_mapping(self):
        from src.features.color.shaders.shaders import RAW_VIEW_FRAGMENT

        assert "float lut_n = float(textureSize(u_lut_tex, 0).x);" \
            in RAW_VIEW_FRAGMENT
        assert "(lut_input * (lut_n - 1.0) + 0.5) / lut_n" in RAW_VIEW_FRAGMENT
        # 旧的直接 [0,1] 采样不再存在
        assert "texture(u_lut_tex, clamp(color, 0.0, 1.0))" \
            not in RAW_VIEW_FRAGMENT

    def test_mapping_matches_lut3d_reference(self):
        """与本文件 LUT3D（apply_lut_hdr_safe）已修复的映射一致。"""
        from src.features.color.shaders.shaders import (
            LUT3D_FRAGMENT, RAW_VIEW_FRAGMENT,
        )

        assert "(sdr_part * (lut_n - 1.0) + 0.5) / lut_n" in LUT3D_FRAGMENT
        assert "(lut_input * (lut_n - 1.0) + 0.5) / lut_n" in RAW_VIEW_FRAGMENT


# ---------------------------------------------------------------------------
# AUD-24905 / AUD-24906 / AUD-24907: 跟踪服务
# ---------------------------------------------------------------------------


def _binding_transforms():
    """63..70 有效，71..75 失败。"""
    transforms = {}
    for f in range(63, 71):
        transforms[f] = TrackTransform.from_matrix(
            f, np.eye(3), 0.9, TrackStatus.VALID)
    for f in range(71, 76):
        transforms[f] = TrackTransform.failed(f, FailureReason.OCCLUSION)
    return transforms


class TestAUD24905CacheKeepsFullResult:
    def test_retrack_emits_filtered_but_caches_full(self, qapp):
        service, workers, events = tq.make_service()
        gen = service.retrack_failed(
            "clip1", "shape1", _binding_transforms(),
            tq.MEDIA, tq.PATTERN, tq.SEARCH, 62, 0, 124,
            directions=(TrackDirection.FORWARD,),
        )
        assert len(workers) == 1
        full = tq.make_result(gen, frames=tuple(range(63, 81)))
        workers[0].finished_with_result.emit(gen, full)

        # 回发结果只含失败段+衔接帧
        _, _, emitted = events["finished"][0]
        assert 63 not in emitted.transforms, "远离失败段的有效帧不得覆盖"
        assert {71, 72, 73, 74, 75} <= set(emitted.transforms)

        # 缓存必须保存完整结果：同参数完整跟踪命中缓存并拿到全量帧
        workers_before = len(workers)
        gen2 = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        assert len(workers) == workers_before, "完整跟踪应命中缓存"
        finished2 = [e for e in events["finished"] if e[1] == gen2]
        assert len(finished2) == 1
        assert set(finished2[0][2].transforms) == set(range(63, 81)), \
            "缓存中的必须是未过滤的完整结果"


class TestAUD24906StartedBeforeFinished:
    def _recorder(self, service):
        seq: list[tuple[str, str, int]] = []
        service.task_started.connect(
            lambda k, g: seq.append(("started", k, g)))
        service.task_finished.connect(
            lambda k, g, r: seq.append(("finished", k, g)))
        return seq

    def test_all_cache_hit_started_precedes_finished(self, qapp):
        service, workers, _ = tq.make_service()
        seq = self._recorder(service)
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        workers[0].finished_with_result.emit(gen, tq.make_result(gen))
        assert seq and seq[-1][0] == "finished"

        seq.clear()
        gen2 = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        # 全部方向命中缓存：started 必须先于 finished（旧实现反序，
        # 面板永久卡 RUNNING）
        assert seq == [
            ("started", "clip1:shape1", gen2),
            ("finished", "clip1:shape1", gen2),
        ]

    def test_worker_path_started_precedes_finished(self, qapp):
        service, workers, _ = tq.make_service()
        seq = self._recorder(service)
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,))
        workers[0].finished_with_result.emit(gen, tq.make_result(gen))
        assert seq[0] == ("started", "clip1:shape1", gen)
        assert seq[-1] == ("finished", "clip1:shape1", gen)


class TestAUD24907GeometryInCacheKey:
    GEO_A = {"type": "curve", "center": [0.5, 0.5], "size": [0.2, 0.2],
             "rotation": 0.0, "points": [{"pos": [0.4, 0.5]}]}
    GEO_B = {"type": "curve", "center": [0.5, 0.5], "size": [0.2, 0.2],
             "rotation": 0.0, "points": [{"pos": [0.6, 0.5]}]}

    def test_geometry_change_bypasses_cache(self, qapp):
        service, workers, _ = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,),
                               shape_geometry=self.GEO_A)
        workers[0].finished_with_result.emit(gen, tq.make_result(gen))
        before = len(workers)
        tq.start_default(service, directions=(TrackDirection.FORWARD,),
                         shape_geometry=self.GEO_B)
        assert len(workers) == before + 1, \
            "形状几何变化必须使缓存失效（旧键不含 shape_geometry）"

    def test_same_geometry_hits_cache(self, qapp):
        service, workers, _ = tq.make_service()
        gen = tq.start_default(service, directions=(TrackDirection.FORWARD,),
                               shape_geometry=self.GEO_A)
        workers[0].finished_with_result.emit(gen, tq.make_result(gen))
        before = len(workers)
        tq.start_default(service, directions=(TrackDirection.FORWARD,),
                         shape_geometry=dict(self.GEO_A))
        assert len(workers) == before, "同几何应命中缓存"

    def test_geometry_digest(self):
        from src.features.color.tracking.tracking_service import TrackingService

        assert TrackingService._geometry_digest(None) == "nogeo"
        assert TrackingService._geometry_digest({}) == "nogeo"
        d_a = TrackingService._geometry_digest(self.GEO_A)
        d_b = TrackingService._geometry_digest(self.GEO_B)
        assert d_a != d_b
        # 字典键序不影响摘要（稳定序列化）
        reordered = {k: self.GEO_A[k] for k in reversed(list(self.GEO_A))}
        assert TrackingService._geometry_digest(reordered) == d_a


# ---------------------------------------------------------------------------
# AUD-25105 / AUD-25106: BlurSharpenPanel 堆栈页面同步
# ---------------------------------------------------------------------------


class TestAUD25105SetParamsDictSyncsPages:
    def test_restore_syncs_stack_pages(self, qapp):
        from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel

        panel = BlurSharpenPanel()
        try:
            # 用户先切到 高级 + 动感模糊
            panel.mode_combo.setCurrentIndex(1)
            panel.blur_mode_combo.setCurrentIndex(2)
            assert panel.stack.currentIndex() == 1
            assert panel.blur_stack.currentIndex() == 2
            # 恢复 mode=0 / blur_mode=3（屏蔽信号路径）
            panel.set_params_dict({
                "enabled": True, "mode": 0, "sharpen": 10.0,
                "blur_enabled": True, "blur_mode": 3,
            })
            assert panel.mode_combo.currentIndex() == 0
            assert panel.stack.currentIndex() == 0, \
                "堆栈页面必须随 combo 恢复到基础页"
            assert panel.blur_mode_combo.currentIndex() == 3
            assert panel.blur_stack.currentIndex() == 3, \
                "blur 堆栈页面必须随 combo 恢复到快速模糊页"
        finally:
            panel.deleteLater()

    def test_restore_advanced_mode_shows_advanced_page(self, qapp):
        from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel

        panel = BlurSharpenPanel()
        try:
            panel.set_params_dict({"enabled": True, "mode": 1,
                                   "blur_enabled": False, "blur_mode": 1})
            assert panel.stack.currentIndex() == 1
            assert panel.blur_stack.currentIndex() == 1
        finally:
            panel.deleteLater()


class TestAUD25106ResetPathsSyncPages:
    def _panel(self):
        from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel

        return BlurSharpenPanel()

    def test_reset_all_returns_stack_to_basic(self, qapp):
        panel = self._panel()
        try:
            panel.mode_combo.setCurrentIndex(1)
            assert panel.stack.currentIndex() == 1
            panel.reset_all()
            assert panel.mode_combo.currentIndex() == 0
            assert panel.stack.currentIndex() == 0, \
                "重置后堆栈必须归位到基础页（旧实现停留在高级页）"
        finally:
            panel.deleteLater()

    def test_reset_blur_returns_stack_to_first_page(self, qapp):
        panel = self._panel()
        try:
            panel.blur_mode_combo.setCurrentIndex(2)
            assert panel.blur_stack.currentIndex() == 2
            panel.reset_blur()
            assert panel.blur_mode_combo.currentIndex() == 0
            assert panel.blur_stack.currentIndex() == 0, \
                "重置后 blur 堆栈必须归位到质感模糊页"
        finally:
            panel.deleteLater()

    def test_reset_blur_emits_reset_clicked_like_reset_all(self, qapp):
        """两路 reset 的状态通知语义一致（AUD-25106 附注）。"""
        panel = self._panel()
        try:
            clicks: list[int] = []
            panel.reset_clicked.connect(lambda: clicks.append(1))
            panel.reset_blur()
            assert clicks == [1]
            panel.reset_all()
            assert clicks == [1, 1]
        finally:
            panel.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25200: 曲线逐点羽化手柄缩放基准
# ---------------------------------------------------------------------------


def _curve_item(qapp):
    canvas = Canvas()
    canvas.logical_size = QSizeF(2000.0, 1000.0)
    item = MaskShapeItem("curve-1", "curve", canvas)
    canvas.active_shape_id = "curve-1"
    # 退化切线（c2 == pos）→ normal = (0, -1)，距离计算无歧义
    item.bezier_points = [{
        "pos": QPointF(400.0, 500.0),
        "c1": QPointF(400.0, 500.0),
        "c2": QPointF(400.0, 500.0),
        "type": "smooth",
        "fi": 50.0,
        "fo": 25.0,
    }]
    item.selected_node_idx = 0
    item.feather = 50.0
    return canvas, item


class TestAUD25200FeatherScaleUsesLogicalSize:
    def test_handle_distance_matches_engine_scale(self, qapp):
        canvas, item = _curve_item(qapp)
        try:
            item._update_handle_positions()
            node = item.bezier_points[0]["pos"]
            fi_handle = item.handles["fi_0"]
            fo_handle = item.handles["fo_0"]
            # 引擎公式：4.0 * (2000 / 5.0) / 100.0 = 16.0 px/单位
            feather_scale = 4.0 * (2000.0 / 5.0) / 100.0
            assert abs(fi_handle.y() - node.y()) == pytest.approx(
                50.0 * feather_scale, abs=1.0)
            assert abs(fo_handle.y() - node.y()) == pytest.approx(
                25.0 * feather_scale, abs=1.0)
            # 旧实现恒用 512 → 50 * 4.096 ≈ 205，明显偏小
            assert abs(fi_handle.y() - node.y()) > 700.0
        finally:
            canvas.deleteLater()

    def test_feather_drag_converts_with_engine_scale(self, qapp):
        canvas, item = _curve_item(qapp)
        try:
            item._update_handle_positions()
            node = item.bezier_points[0]["pos"]
            # 沿 normal (0,-1) 拖到 160px：160 / 16.0 = 10
            item.on_handle_moved(
                item.handles["fi_0"], QPointF(node.x(), node.y() - 160.0),
                skip_sync=True)
            assert item.bezier_points[0]["fi"] == pytest.approx(10.0, abs=0.01)
        finally:
            canvas.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25201: _is_valid_crop 用真实图像边界
# ---------------------------------------------------------------------------


class TestAUD25201CropValidityUsesImageBounds:
    def test_crop_inside_padded_scenerect_but_outside_image_rejected(self, qapp):
        # 模拟 _update_scene_pan_bounds 扩展过的大 sceneRect（图像四周各 4 倍）
        scene = QGraphicsScene()
        scene.setSceneRect(-8000.0, -8000.0, 16000.0, 16000.0)
        fake_canvas = SimpleNamespace(
            logical_size=QSizeF(100.0, 100.0), scene=scene)
        crop = CropRectItem(fake_canvas)
        # 角点全部在 100x100 图像内 → 合法
        assert crop._is_valid_crop(
            QPointF(50.0, 50.0), QRectF(-10.0, -10.0, 40.0, 40.0)) is True
        # 角点 (120,120) 在扩展 sceneRect 内但在图像外 → 必须拒绝
        # （旧实现用扩展 sceneRect 校验，恒放行）
        assert crop._is_valid_crop(
            QPointF(90.0, 90.0), QRectF(-10.0, -10.0, 40.0, 40.0)) is False

    def test_valid_crop_within_image_accepted_with_tolerance(self, qapp):
        scene = QGraphicsScene()
        scene.setSceneRect(0.0, 0.0, 100.0, 100.0)
        fake_canvas = SimpleNamespace(
            logical_size=QSizeF(100.0, 100.0), scene=scene)
        crop = CropRectItem(fake_canvas)
        # 贴边裁切（含 2px 浮点容差）合法
        assert crop._is_valid_crop(
            QPointF(50.0, 50.0), QRectF(-50.0, -50.0, 100.0, 100.0)) is True


# ---------------------------------------------------------------------------
# AUD-25202 / AUD-25203: RAW 取色 Linear 直通 + 检查器共用处理链
# ---------------------------------------------------------------------------


# canvas.py _apply_raw_color_management 中的 ProPhoto(D50)->sRGB(D65) 行主序矩阵
_PP_TO_SRGB = np.array([
    [2.0342892, -0.7275078, -0.3067813],
    [-0.2288020, 1.2317255, -0.0029236],
    [-0.0085623, -0.1532653, 1.1618276],
])


def _raw_engine(gamma: str):
    """启用 RAW 色彩管理的引擎替身（除 gamma 外全部恒等参数）。"""
    return SimpleNamespace(
        current_file_path="/tmp/photo.CR2",
        RAW_EXTENSIONS=(".cr2", ".nef", ".dng"),
        is_current_image_srgb=False,
        current_raw_params={
            "enabled": True, "exposure": 0.0,
            "user_wb": [1.0, 1.0, 1.0, 1.0],
            "gain": 1.0, "lift": 0.0, "contrast": 1.0,
            "color_space": "sRGB", "gamma": gamma,
        },
        current_raw_metadata={"camera_wb": [1.0, 1.0, 1.0, 1.0]},
        _last_applied_raw_params={"user_wb": [1.0, 1.0, 1.0, 1.0]},
    )


class TestAUD25202LinearGammaPassthrough:
    def _apply(self, gamma):
        c = np.array([0.25, 0.5, 0.75], dtype=np.float32)
        return Canvas._apply_raw_color_management(
            SimpleNamespace(engine=_raw_engine(gamma)), c)

    def test_linear_passes_through_without_oetf(self):
        out = self._apply("Linear")
        post_matrix = np.maximum(_PP_TO_SRGB @ np.array([0.25, 0.5, 0.75]), 0.0)
        np.testing.assert_allclose(out, post_matrix, atol=1e-6)
        # 旧实现 Linear 与 Gamma 2.2 共用 pow(1/2.2)
        assert not np.allclose(out, post_matrix ** (1.0 / 2.2))

    def test_gamma_22_and_24_unchanged(self):
        post_matrix = np.maximum(_PP_TO_SRGB @ np.array([0.25, 0.5, 0.75]), 0.0)
        np.testing.assert_allclose(
            self._apply("Gamma 2.2"), post_matrix ** (1.0 / 2.2), atol=1e-6)
        np.testing.assert_allclose(
            self._apply("Gamma 2.4"), post_matrix ** (1.0 / 2.4), atol=1e-6)

    def test_gamma_26_branch_present(self):
        out = self._apply("Gamma 2.6")
        post_matrix = np.maximum(_PP_TO_SRGB @ np.array([0.25, 0.5, 0.75]), 0.0)
        np.testing.assert_allclose(out, post_matrix ** (1.0 / 2.6), atol=1e-6)


def _pick_engine(raw: bool):
    """4x4 原图 + 2x2 显示图（scale=2）的取色引擎替身。"""
    orig = np.zeros((4, 4, 3), dtype=np.uint8)
    orig[1, 1] = (10, 10, 10)      # 旧检查器在 scene(1,1) 读到的错误像素
    orig[2, 2] = (200, 128, 60)    # BGR：正确映射应读到的像素
    disp = np.zeros((2, 2, 3), dtype=np.uint8)
    if raw:
        engine = _raw_engine("Gamma 2.2")
    else:
        engine = SimpleNamespace(
            current_file_path="/tmp/pic.png",
            RAW_EXTENSIONS=(".cr2", ".nef", ".dng"),
            is_current_image_srgb=True,
            current_raw_params={"enabled": False},
            current_raw_metadata={},
            _last_applied_raw_params={},
        )
    engine.img_full_original = orig
    engine.get_display_image = lambda: disp
    return engine


def _canvas_stub(engine):
    """未初始化 UI 的 Canvas 实例（只挂 engine，供纯 Python 方法调用）。"""
    stub = Canvas.__new__(Canvas)
    stub.engine = engine
    return stub


class TestAUD25203InspectorSharesPickPipeline:
    def test_scene_pixel_rgb_maps_display_to_original(self):
        rgb, img_x, img_y = _canvas_stub(
            _pick_engine(raw=False))._scene_pixel_rgb(QPointF(1.5, 1.5))
        # display(2x2) → original(4x4)：scale=2 → 像素 (3,3)
        assert (img_x, img_y) == (3, 3)
        np.testing.assert_allclose(rgb, np.zeros(3), atol=1e-6)

    def test_scene_pixel_rgb_out_of_bounds_returns_none_with_coords(self):
        rgb, img_x, img_y = _canvas_stub(
            _pick_engine(raw=False))._scene_pixel_rgb(QPointF(3.9, 3.9))
        assert rgb is None
        assert (img_x, img_y) == (7, 7)

    def test_scene_pixel_rgb_applies_raw_color_management(self):
        # 灰像素 128 → 归一化 0.50196 → 矩阵（行和为 1，灰不变）→
        # gamma 2.2 → ≈ 0.7297；旧检查器显示未处理的 0.50196
        engine = _pick_engine(raw=True)
        engine.img_full_original[3, 3] = (128, 128, 128)  # scene(1.5,1.5)→(3,3)
        rgb, img_x, img_y = _canvas_stub(
            engine)._scene_pixel_rgb(QPointF(1.5, 1.5))
        assert (img_x, img_y) == (3, 3)
        expected = (128.0 / 255.0) ** (1.0 / 2.2)
        np.testing.assert_allclose(rgb, [expected] * 3, atol=1e-4)

    def test_inspector_uses_mapping_and_raw_cm(self, qapp):
        canvas = Canvas()
        canvas.engine = _pick_engine(raw=True)
        # scene(1,1) → display(2x2) scale=2 → original(4x4) 像素 (2,2)：灰像素
        canvas.engine.img_full_original[2, 2] = (128, 128, 128)
        recorded: list[tuple] = []
        canvas.color_inspector.update_info = lambda *a: recorded.append(a)
        try:
            # 画布初始变换非恒等：由场景坐标反推视图坐标，保证
            # scene_pos 落在图像内（display 4x4 → pixel (2,2)）
            pos = canvas.mapFromScene(QPointF(1.0, 1.0))
            expected_rgb, ex, ey = canvas._scene_pixel_rgb(
                canvas.mapToScene(pos))
            assert expected_rgb is not None
            canvas._update_color_inspector(pos)
            assert recorded, "检查器必须更新"
            rgb_int, rgb_f, coords = recorded[-1]
            # 坐标为 display→original 映射后的坐标
            assert coords == (ex, ey)
            # 数值与共享管线输出一致（含 RAW 色彩管理）
            np.testing.assert_allclose(
                np.array(rgb_f), expected_rgb, atol=1e-5)
            assert rgb_int == tuple(
                int(round(min(1.0, max(0.0, v)) * 255.0)) for v in expected_rgb)
        finally:
            canvas.deleteLater()

    def test_color_pick_matches_shared_pipeline(self, qapp):
        canvas = Canvas()
        canvas.engine = _pick_engine(raw=True)
        canvas.engine.img_full_original[2, 2] = (128, 128, 128)
        picked: list[tuple] = []
        canvas.color_picked.connect(
            lambda h, s, l, first: picked.append((h, s, l, first)))
        try:
            pos = canvas.mapFromScene(QPointF(1.0, 1.0))
            expected_rgb, _, _ = canvas._scene_pixel_rgb(canvas.mapToScene(pos))
            assert expected_rgb is not None
            canvas._handle_color_pick(pos, first=True)
            assert picked, "取色器必须发射 color_picked"
            h, s, l, first = picked[-1]
            assert first is True
            # 灰像素：HSL 退化为 (0, 0, 期望亮度)
            expected_l = float(np.max(expected_rgb) + np.min(expected_rgb)) / 2.0
            assert l == pytest.approx(expected_l, abs=1e-4)
            assert s == pytest.approx(0.0, abs=1e-6)
        finally:
            canvas.deleteLater()


# ---------------------------------------------------------------------------
# AUD-25204: set_crop_params 收窄异常
# ---------------------------------------------------------------------------


class TestAUD25204CropAspectParsing:
    def _canvas(self, qapp):
        canvas = Canvas()
        canvas.logical_size = QSizeF(1000.0, 1000.0)
        canvas.scene.setSceneRect(0.0, 0.0, 1000.0, 1000.0)
        canvas.set_crop_mode(True)
        return canvas

    def test_valid_ratio_strings_parse(self, qapp):
        canvas = self._canvas(qapp)
        try:
            canvas.set_crop_params({"aspect": "16:9"})
            assert canvas.crop_item.aspect_ratio == pytest.approx(16.0 / 9.0)
            canvas.set_crop_params({"aspect": "2.35"})
            assert canvas.crop_item.aspect_ratio == pytest.approx(2.35)
        finally:
            canvas.set_crop_mode(False)
            canvas.deleteLater()

    def test_invalid_values_fall_back_with_warning(self, qapp, caplog):
        canvas = self._canvas(qapp)
        try:
            with caplog.at_level(logging.WARNING):
                canvas.set_crop_params({"aspect": "garbage"})
            assert canvas.crop_item.aspect_ratio is None
            assert any("Invalid crop aspect" in r.message
                       for r in caplog.records)
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                canvas.set_crop_params({"aspect": "4:0"})   # 除零
            assert canvas.crop_item.aspect_ratio is None
            assert any("Invalid crop aspect" in r.message
                       for r in caplog.records)
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                canvas.set_crop_params({"aspect": 123})     # 非字符串
            assert canvas.crop_item.aspect_ratio is None
            assert any("Invalid crop aspect" in r.message
                       for r in caplog.records)
        finally:
            canvas.set_crop_mode(False)
            canvas.deleteLater()

    def test_free_and_original_unaffected(self, qapp):
        canvas = self._canvas(qapp)
        try:
            canvas.set_crop_params({"aspect": "Free"})
            assert canvas.crop_item.aspect_ratio is None
            canvas.set_crop_params({"aspect": "Original"})
            assert canvas.crop_item.aspect_ratio == pytest.approx(1.0)
        finally:
            canvas.set_crop_mode(False)
            canvas.deleteLater()
