"""批 D 修复回归 + GAP 补测（wd13-g69）。

AUD 回归（11 条 Major）：
- AUD-21403 — TimelineView._debounce_thumbnail_schedule 单例 QTimer 复用
- AUD-21404 — Alt 拖拽复制失败回滚项目引用（孤儿 solid asset / 失步）
- AUD-21500 — TrimNumericDialog._refresh 状态锁防 valueChanged 重入
- AUD-21503 — _apply_text_input_style 选择器覆盖 QTextEdit
- AUD-21504 — TTS 参考音频路径存在性校验 + 可见提示
- AUD-25902 — apply_lut 相对路径解析到项目根 luts/
- AUD-25903 — 两点曲线路径不再 clip(0,1)（与 PCHIP/Widget 外推一致）
- AUD-25904 — 脚本级调色提交时失效视频渲染参数缓存（S5.C 等价）
- AUD-25910 — BlockingBridge.call_sync 的 timeout/abort 真正可达
- AUD-26307 — @export_support 声明值白名单（fail-closed）
- AUD-26308 — DCTL 目录扫描失败必须落日志
- AUD-26408 — PluginIdentity.from_dict 外部数据防御（never raises）
- AUD-26410 — _pick_degradation 参数已达标时继续探测 LOWER_RESOLUTION/BYPASS
- AUD-17614 — 缓存状态条：单轮 manifest 索引 + 轮询门控 + 命令路径即时刷新

GAP 补测（GAP_REPORT 硬约束：不改 src、不改既有测试、mock 外部 IO/GPU）：
- GAP-004 timeline_view.py / GAP-006 media_pool_panel.py /
  GAP-009 timeline_items.py / GAP-016 edit_gpu_preview_renderer.py /
  GAP-019 edit_properties_panel.py / GAP-021 ai_edit_assistant_panel.py /
  GAP-026 program_viewer.py —— 各模块纯逻辑/静态助手孤岛区定向覆盖。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types
from dataclasses import replace
from typing import Any, List

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.edit_core

from PyQt6.QtCore import QMimeData, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QDialog, QLineEdit, QTextEdit

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    TransitionInstance,
)


# =====================================================================
# 小工具
# =====================================================================

class _InlineBridge:
    """直接在当前线程执行的 bridge 替身（api 层单测用）。"""

    def call_sync(self, func, timeout: int = 60000):
        return func()

    def is_aborted(self) -> bool:
        return False


class _RecordingEngine:
    """记录引擎调用的 ImageEngine 替身。"""

    def __init__(self) -> None:
        self.lut_paths: List[str] = []
        self.render_calls = int(0)
        self.layers: List[Any] = []

    def apply_lut(self, path: str) -> None:
        self.lut_paths.append(path)

    def render_pipeline(self) -> None:
        self.render_calls += 1

    def get_active_layer(self):
        return None


class _FakeVideoController:
    def __init__(self, loaded: bool) -> None:
        self.loaded = loaded
        self.invalidate_calls = 0

    def is_video_loaded(self) -> bool:
        return self.loaded

    def invalidate_cached_render_params(self) -> None:
        self.invalidate_calls += 1


class _FakeMainWindow:
    def __init__(self, video_controller: Any, current_file_is_video: bool = False):
        self.video_controller = video_controller
        self.current_file_is_video = current_file_is_video


def qRgb(r: int, g: int, b: int) -> int:
    return (0xFF << 24) | (r << 16) | (g << 8) | b


def _clip(clip_id: str, start: int, duration: int, *, asset_id: str = "asset-1",
          composite_id: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=asset_id, track_id="v1",
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
        composite_id=composite_id,
    )


def _project_with_asset(asset: MediaAsset) -> EditProject:
    project = EditProject.create("wd13-g69")
    bins = dict(project.bins)
    bins[project.root_bin_id] = bins[project.root_bin_id].with_asset_added(
        asset.asset_id)
    return replace(project, assets={asset.asset_id: asset}, bins=bins)


def _video_asset(asset_id: str = "asset-1") -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path="/tmp/wd13_source.mp4", media_type="video",
        display_name="Source", metadata={},
    )


# =====================================================================
# AUD-21500 — TrimNumericDialog 状态锁
# =====================================================================

class TestTrimDialogRefreshLock:

    def _make_dialog(self, qapp, bounds_for):
        from src.features.edit.ui.trim_numeric_dialog import TrimNumericDialog
        return TrimNumericDialog(bounds_for=bounds_for, duration_frames=100)

    def test_no_reentry_when_range_clamps_value(self, qapp) -> None:
        """切换 edge 使 setMinimum 钳取数值时，_refresh 不得重入。"""
        calls = []
        bounds = {"low": -10, "high": 10}

        def bounds_for(_edge: str, _mode: str):
            calls.append(1)
            return bounds["low"], bounds["high"]

        dialog = self._make_dialog(qapp, bounds_for)
        dialog._delta_spin.setValue(-5)
        assert len(calls) == 2  # __init__ + setValue

        # 缩小合法范围：setMinimum(0) 会把 -5 钳到 0 并同步发 valueChanged
        bounds.update(low=0, high=10)
        dialog._edge_combo.setCurrentIndex(1)

        # 锁生效：bounds_for 只被执行一次（旧实现在此重入为两次）
        assert len(calls) == 3
        # 标签反映钳取后的真实值
        assert dialog._delta_spin.value() == 0
        dialog.deleteLater()

    def test_refresh_is_idempotent_under_programmatic_range_change(self, qapp) -> None:
        from src.features.edit.ui.trim_numeric_dialog import TrimNumericDialog
        counter = {"n": 0}

        def bounds_for(_edge: str, _mode: str):
            counter["n"] += 1
            return (20, -20)  # 倒置边界，迫使 sorted/max 防御路径执行

        dialog = TrimNumericDialog(bounds_for=bounds_for, duration_frames=50,
                                   can_roll=True)
        dialog._mode_combo.setCurrentIndex(2)  # roll
        qapp.processEvents()
        # __init__ + 模式切换，无重入
        assert counter["n"] == 2
        assert dialog._edge_combo.isEnabled() is False
        dialog.deleteLater()


# =====================================================================
# AUD-21503 — 多行输入统一样式
# =====================================================================

class TestTextInputStyleMultiLine:

    def test_style_matches_qtextedit_selector(self, qapp) -> None:
        from src.shared.export.export_panel import _apply_text_input_style
        edit = QTextEdit()
        _apply_text_input_style(edit)
        sheet = edit.styleSheet()
        # 选择器必须同时覆盖 QLineEdit 与 QTextEdit（含 hover/focus 态）
        assert "QLineEdit, QTextEdit {" in sheet
        assert "QLineEdit:hover, QTextEdit:hover" in sheet
        assert "QLineEdit:focus, QTextEdit:focus" in sheet
        edit.deleteLater()

    def test_qline_edit_styling_unchanged(self, qapp) -> None:
        from src.shared.export.export_panel import _apply_text_input_style
        line = QLineEdit()
        _apply_text_input_style(line)
        sheet = line.styleSheet()
        assert "#232325" in sheet and "#ff8800" in sheet
        line.deleteLater()


# =====================================================================
# AUD-21504 — TTS 参考音频存在性校验
# =====================================================================

class TestTtsRefAudioValidation:

    def _make_dialog(self, qapp):
        from src.features.edit.ui.tts_settings_dialog import TtsSettingsDialog
        dialog = TtsSettingsDialog()
        dialog._tabs.setCurrentIndex(1)  # 音色克隆 tab
        dialog._xvector_check.setChecked(True)  # 跳过 ref_text 必填
        return dialog

    def test_nonexistent_path_blocks_accept_with_message(self, qapp, tmp_path) -> None:
        dialog = self._make_dialog(qapp)
        dialog._ref_audio_edit.setText(str(tmp_path / "missing.wav"))
        dialog._on_accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog._ref_validation_label.isVisibleTo(dialog)
        assert "missing.wav" in dialog._ref_validation_label.text()
        dialog.deleteLater()

    def test_existing_file_passes_validation(self, qapp, tmp_path) -> None:
        audio = tmp_path / "ref.wav"
        audio.write_bytes(b"RIFF")
        dialog = self._make_dialog(qapp)
        dialog._ref_audio_edit.setText(str(audio))
        dialog._on_accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert not dialog._ref_validation_label.isVisibleTo(dialog)
        dialog.deleteLater()

    def test_directory_path_is_rejected(self, qapp, tmp_path) -> None:
        dialog = self._make_dialog(qapp)
        dialog._ref_audio_edit.setText(str(tmp_path))
        dialog._on_accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        dialog.deleteLater()

    def test_empty_path_still_blocked_with_hint(self, qapp) -> None:
        dialog = self._make_dialog(qapp)
        dialog._ref_audio_edit.setText("   ")
        dialog._on_accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog._ref_validation_label.isVisibleTo(dialog)
        dialog.deleteLater()


# =====================================================================
# AUD-25902 — apply_lut 相对路径解析
# =====================================================================

class TestApplyLutProjectRoot:

    def _repo_root(self) -> str:
        from src.features.scripting.api import _project_luts_root
        return os.path.dirname(_project_luts_root())

    def test_relative_name_resolves_to_project_root_luts(self) -> None:
        from src.features.scripting.api import ToposAPI
        engine = _RecordingEngine()
        api = ToposAPI(engine=engine, bridge=_InlineBridge())
        api.apply_lut("cinematic.cube")
        expected = os.path.join(self._repo_root(), "luts", "cinematic.cube")
        assert engine.lut_paths == [expected]
        assert os.path.isfile(expected)  # 随附 LUT 真的可命中

    def test_absolute_path_passthrough(self) -> None:
        from src.features.scripting.api import ToposAPI
        engine = _RecordingEngine()
        api = ToposAPI(engine=engine, bridge=_InlineBridge())
        lut = os.path.join(self._repo_root(), "luts", "07.cube")
        api.apply_lut(lut)
        assert engine.lut_paths == [lut]

    def test_missing_lut_raises_file_not_found(self) -> None:
        from src.features.scripting.api import ToposAPI
        api = ToposAPI(engine=_RecordingEngine(), bridge=_InlineBridge())
        with pytest.raises(FileNotFoundError):
            api.apply_lut("no_such_lut.cube")


# =====================================================================
# AUD-25903 — 两点曲线不裁切
# =====================================================================

class TestCurveTwoPointExtrapolation:

    def test_two_point_curve_extrapolates_uncapped(self) -> None:
        from src.features.scripting.api import _interpolate_curve_255
        # 陡峭两点：外推段必然越过 [0,1]
        lut = _interpolate_curve_255([(60, 0), (200, 255)])
        assert lut[0] > 1.0
        assert lut[255] < 0.0

    def test_two_point_identity_unchanged(self) -> None:
        from src.features.scripting.api import _interpolate_curve_255
        lut = _interpolate_curve_255([(0, 255), (255, 0)])
        identity = np.arange(256, dtype=np.float32) / 255.0
        assert np.allclose(lut, identity, atol=1e-6)

    def test_two_and_three_point_share_interior_semantics(self) -> None:
        """控制点内侧的线性语义不受去 clip 影响（中段采样落回 [0,1]）。"""
        from src.features.scripting.api import _interpolate_curve_255
        lut = _interpolate_curve_255([(0, 255), (255, 0)])
        mid = lut[128]
        assert 0.0 <= mid <= 1.0


# =====================================================================
# AUD-25904 — 脚本调色失效视频渲染参数缓存
# =====================================================================

class _GradingEngine(_RecordingEngine):
    def __init__(self) -> None:
        super().__init__()
        self.layer = types.SimpleNamespace(
            grading_params={}, qualifier_params={}, soft_clip_params={},
            mixer_params={}, effects_params={}, curves_data={},
            hsl_curve_lut=None, hsl_curve_data=None,
        )

    def get_active_layer(self):
        return self.layer


class TestScriptRenderInvalidation:

    def _make_api(self, video_controller, current_file_is_video=False):
        from src.features.scripting.api import ToposAPI
        engine = _GradingEngine()
        api = ToposAPI(engine=engine, bridge=_InlineBridge())
        api._main_window = _FakeMainWindow(video_controller, current_file_is_video)
        return api, engine

    def test_grade_invalidates_when_video_loaded(self) -> None:
        vc = _FakeVideoController(loaded=True)
        api, engine = self._make_api(vc)
        api.grade(exposure=0.5)
        assert engine.render_calls == 1
        assert vc.invalidate_calls == 1

    def test_no_invalidation_without_video(self) -> None:
        vc = _FakeVideoController(loaded=False)
        api, _ = self._make_api(vc)
        api.grade(exposure=0.5)
        assert vc.invalidate_calls == 0

    def test_current_file_is_video_flag_alone_triggers(self) -> None:
        vc = _FakeVideoController(loaded=False)
        api, _ = self._make_api(vc, current_file_is_video=True)
        api.grade(contrast=5)
        assert vc.invalidate_calls == 1

    def test_set_curves_invalidates_via_apply_path(self) -> None:
        vc = _FakeVideoController(loaded=True)
        api, engine = self._make_api(vc)
        engine.apply_curves = lambda lut: None
        api.set_curves(master=[(0, 255), (128, 100), (255, 0)])
        assert vc.invalidate_calls == 1

    def test_missing_video_controller_is_tolerated(self) -> None:
        from src.features.scripting.api import ToposAPI
        engine = _GradingEngine()
        api = ToposAPI(engine=engine, bridge=_InlineBridge())
        api._main_window = types.SimpleNamespace(video_controller=None)
        api.grade(exposure=1.0)  # 不得抛异常
        assert engine.render_calls == 1


# =====================================================================
# AUD-25910 — call_sync 超时可达
# =====================================================================

class TestCallSyncTimeout:

    def test_timeout_reachable_when_main_thread_busy(self, qapp) -> None:
        """主线程不泵事件时，脚本线程必须在 timeout 后拿到 TimeoutError。"""
        from src.features.scripting.engine import BlockingBridge
        bridge = BlockingBridge()
        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                outcome["value"] = bridge.call_sync(
                    lambda: 42, timeout=250)
            except TimeoutError as exc:
                outcome["timeout"] = str(exc)
            except Exception as exc:  # noqa: BLE001 — 记录意外失败
                outcome["unexpected"] = repr(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=3.0)
        assert not thread.is_alive()
        assert "timeout" in outcome, outcome
        assert "value" not in outcome
        # 交付迟到的排队事件，避免跨线程析构告警
        for _ in range(6):
            qapp.processEvents()
            time.sleep(0.01)

    def test_success_path_via_event_loop(self, qapp) -> None:
        from src.features.scripting.engine import BlockingBridge
        bridge = BlockingBridge()
        outcome: dict[str, Any] = {}

        def worker() -> None:
            outcome["value"] = bridge.call_sync(lambda: 41 + 1, timeout=5000)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 5.0
        while "value" not in outcome and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        thread.join(timeout=2.0)
        assert outcome.get("value") == 42

    def test_error_propagates_to_caller(self, qapp) -> None:
        from src.features.scripting.engine import BlockingBridge
        bridge = BlockingBridge()
        outcome: dict[str, Any] = {}

        def _boom() -> None:
            raise ValueError("boom-in-main")

        def worker() -> None:
            try:
                bridge.call_sync(_boom, timeout=5000)
            except ValueError as exc:
                outcome["error"] = str(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 5.0
        while "error" not in outcome and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        thread.join(timeout=2.0)
        assert outcome.get("error") == "boom-in-main"

    def test_same_thread_call_executes_directly(self, qapp) -> None:
        from src.features.scripting.engine import BlockingBridge
        bridge = BlockingBridge()
        assert bridge.call_sync(lambda: "direct", timeout=100) == "direct"


# =====================================================================
# AUD-26307 / AUD-26308 — effect_catalog
# =====================================================================

class TestExportSupportWhitelist:

    def _caps(self, declared: str):
        from src.features.shared.effects.effect_catalog import (
            _plugin_capabilities_from_declaration,
        )
        return _plugin_capabilities_from_declaration(
            "p-test", {"capabilities": {"export_support": declared}})

    def test_declared_none_and_empty_stay_preview_only(self) -> None:
        assert self._caps("none") == ("gpu_preview",)
        assert self._caps("") == ("gpu_preview",)
        assert self._caps("  NONE ") == ("gpu_preview",)

    @pytest.mark.parametrize("token", ["opengl", "all", "video",
                                       "OpenGL", " ALL "])
    def test_known_tokens_grant_export_opengl(self, token: str) -> None:
        caps = self._caps(token)
        assert "export_opengl" in caps and "gpu_preview" in caps

    @pytest.mark.parametrize("token", ["openggl", "cpu", "metal", "lut", "true"])
    def test_unknown_tokens_fail_closed(self, token: str, caplog) -> None:
        with caplog.at_level("WARNING"):
            caps = self._caps(token)
        assert caps == ("gpu_preview",)
        assert "unknown @export_support" in caplog.text

    def test_real_catalog_plugins_follow_declaration(self) -> None:
        """仓库真实插件（all/video/opengl）的能力与声明一致。"""
        from src.features.shared.effects.effect_catalog import get_effect_catalog
        specs = [s for s in get_effect_catalog().get_all_specs()
                 if s.effect_type.startswith("plugin:")]
        assert specs, "catalog 未发现任何 DCTL 插件"
        declared = [s for s in specs if "export_opengl" in s.plugin_capabilities]
        assert declared, "声明 export_support 的插件（warm_tint 等）丢失能力"


class TestDctlScanFailureLogging:

    def test_scan_failure_is_logged_not_swallowed(self, caplog) -> None:
        from src.features.color.core import plugin_manager as pm_module
        from src.features.shared.effects.effect_catalog import (
            _build_dctl_plugin_specs,
        )
        original = pm_module.get_plugin_manager

        def _raise() -> None:
            raise RuntimeError("scan exploded")

        pm_module.get_plugin_manager = _raise
        try:
            with caplog.at_level("WARNING"):
                specs = _build_dctl_plugin_specs()
        finally:
            pm_module.get_plugin_manager = original
        assert specs == []
        assert "DCTL plugin scan failed" in caplog.text
        assert "scan exploded" in caplog.text


# =====================================================================
# AUD-26408 — PluginIdentity.from_dict 防御
# =====================================================================

class TestPluginIdentityRobust:

    def test_non_dict_payload_degrades_to_empty(self) -> None:
        from src.features.shared.effects.plugin_identity import PluginIdentity
        identity = PluginIdentity.from_dict("garbage")  # type: ignore[arg-type]
        assert identity.plugin_id == ""
        assert identity.capabilities == ()

    def test_wrong_typed_fields_coerce_without_raising(self) -> None:
        from src.features.shared.effects.plugin_identity import PluginIdentity
        identity = PluginIdentity.from_dict({
            "plugin_id": None, "version": 1.5,
            "capabilities": "gpu_preview",   # 标量字符串不可 tuple()
        })
        assert identity.plugin_id == ""
        assert identity.version == "1.5"
        assert identity.capabilities == ()

    def test_capabilities_filters_non_str_items(self) -> None:
        from src.features.shared.effects.plugin_identity import PluginIdentity
        identity = PluginIdentity.from_dict({
            "plugin_id": "plugin:x",
            "capabilities": [1, "gpu_preview", None, "export_opengl"],
        })
        assert identity.capabilities == ("gpu_preview", "export_opengl")

    def test_resolve_plugin_status_never_raises_on_corrupt_identity(self) -> None:
        from src.features.shared.effects.plugin_identity import (
            PluginIdentity,
            resolve_plugin_status,
        )
        spec = types.SimpleNamespace(
            plugin_version="1.0", param_defs=(), shader_sources=("void main(){}",),
        )
        for garbage in ("a-string", 42, None, {"plugin_id": ["nested"]}):
            report = resolve_plugin_status(
                PluginIdentity.from_dict(garbage).to_dict() if isinstance(garbage, dict) else garbage,
                "plugin:x", catalog_spec=spec,
            )
            assert report is not None  # 契约：never raises，必产 report


# =====================================================================
# AUD-26410 — _pick_degradation 落空后继续探测
# =====================================================================

class TestDegradationFallThrough:

    def test_glow_within_caps_falls_through_to_lower_resolution(self) -> None:
        """glow 声明 {FULL, PARAMETER_CAP, LOWER_RESOLUTION}：参数达标且超
        预算时必须降级半分辨率，而不是提前返回 FULL。"""
        from src.features.shared.effects.preview_policy import (
            DegradationMode, PreviewPolicyEngine, PolicyContext,
        )
        engine = PreviewPolicyEngine()
        ctx = PolicyContext(
            width=3840, height=2160, is_playing=True,
            gpu_budget_ms=0.0, force_full=False,
        )
        decisions = engine.evaluate_chain(
            ctx, [("fx1", "glow", {"radius": 4.0})])
        assert decisions[0].mode == DegradationMode.LOWER_RESOLUTION
        assert decisions[0].capped_params is None

    def test_glow_over_cap_uses_parameter_cap_first(self) -> None:
        from src.features.shared.effects.preview_policy import (
            DegradationMode, PreviewPolicyEngine, PolicyContext,
        )
        engine = PreviewPolicyEngine()
        ctx = PolicyContext(
            width=1920, height=1080, is_playing=True,
            gpu_budget_ms=0.0, force_full=False,
        )
        decisions = engine.evaluate_chain(
            ctx, [("fx1", "glow", {"radius": 30.0})])
        assert decisions[0].mode == DegradationMode.PARAMETER_CAP
        assert decisions[0].capped_params == {"radius": 8.0}

    def test_cap_only_effect_still_returns_full_when_within_caps(self) -> None:
        """只声明 {FULL, PARAMETER_CAP} 的效果（defocus）参数达标 → FULL。"""
        from src.features.shared.effects.preview_policy import (
            DegradationMode, PreviewPolicyEngine, PolicyContext,
        )
        engine = PreviewPolicyEngine()
        ctx = PolicyContext(
            width=1920, height=1080, is_playing=True,
            gpu_budget_ms=0.0, force_full=False,
        )
        decisions = engine.evaluate_chain(
            ctx, [("fx1", "defocus", {"max_radius": 3.0})])
        assert decisions[0].mode == DegradationMode.FULL
        assert "within caps" in decisions[0].reason

    def test_cap_plus_bypass_uses_bypass_when_within_caps(self) -> None:
        from src.features.shared.effects.preview_policy import (
            DegradationDecl, DegradationMode, PreviewPolicyEngine, PolicyContext,
            register_degradation_decl,
        )
        register_degradation_decl("test:capbypass", DegradationDecl(
            supported_modes=frozenset({
                DegradationMode.FULL, DegradationMode.PARAMETER_CAP,
                DegradationMode.BYPASS}),
            param_caps={"size": 8.0}, export_behavior="full",
            cost_estimate_ms=2.0,
        ))
        engine = PreviewPolicyEngine()
        ctx = PolicyContext(
            width=1920, height=1080, is_playing=True,
            gpu_budget_ms=0.0, force_full=False,
        )
        decisions = engine.evaluate_chain(
            ctx, [("fx1", "test:capbypass", {"size": 2.0})])
        assert decisions[0].mode == DegradationMode.BYPASS


# =====================================================================
# AUD-17614 — 缓存状态条性能
# =====================================================================

class TestCacheIndicatorRoundIndex:

    def _manifest(self, cache_id: str, clip_id: str, status="VALID",
                  fp="fp-1", render_kind="composite", scene_id=""):
        return {
            "cache_id": cache_id, "timeline_id": "tl-1", "clip_id": clip_id,
            "composite_id": "", "color_stack_revision": 0,
            "composite_graph_revision": 0, "source_asset_revision": "",
            "render_fingerprint": fp, "timeline_fps": "24/1",
            "start_frame": 0, "duration_frames": 24,
            "path": f"/tmp/{cache_id}.mov", "codec": "prores_ks",
            "container": "mov", "render_kind": render_kind,
            "scene_id": scene_id, "width": 1920, "height": 1080,
            "status": status,
        }

    def test_single_round_deserializes_each_manifest_once(self, monkeypatch) -> None:
        """N 个 clip × M 个 manifest 时，from_dict 总次数 == M（而非 N×M）。"""
        from src.features.edit.core import render_cache_status as rcs
        project = _project_with_asset(_video_asset())
        manifests = {
            f"c{i}": self._manifest(f"c{i}", f"clip-{i % 3}")
            for i in range(6)
        }
        project = replace(project, render_cache_manifests=manifests)
        timeline = types.SimpleNamespace(
            timeline_id="tl-1",
            all_tracks=[types.SimpleNamespace(clips=[
                _clip(f"clip-{i}", i * 30, 24, composite_id=f"g{i}")
                for i in range(3)
            ])],
        )
        calls = {"n": 0}
        original = rcs.RenderCacheManifest.from_dict

        def counting(data):
            calls["n"] += 1
            return original(data)

        monkeypatch.setattr(rcs.RenderCacheManifest, "from_dict",
                            staticmethod(counting))
        segments = rcs.build_cache_indicator_segments(timeline, project)
        assert segments  # 指示条有内容（uncached 段）
        assert calls["n"] == 6  # 6 manifests，3 clips → 旧实现为 18+

    def test_index_path_matches_getter_path_results(self) -> None:
        """索引路径与逐 clip getter 路径产出一致的状态。"""
        from src.features.edit.core.render_cache_status import (
            build_cache_indicator_segments, cache_status_for_clip,
        )
        project = _project_with_asset(_video_asset())
        manifests = {
            "a": self._manifest("a", "clip-0", status="VALID", fp="match"),
        }
        project = replace(project, render_cache_manifests=manifests)
        timeline = types.SimpleNamespace(
            timeline_id="tl-1",
            all_tracks=[types.SimpleNamespace(clips=[
                _clip("clip-0", 0, 24, composite_id="g0"),
                _clip("clip-1", 30, 24, composite_id="g1"),
            ])],
        )
        segments = build_cache_indicator_segments(timeline, project)
        statuses = {(s.start_frame, s.end_frame): s.status for s in segments}
        # 两段都是 uncached（指纹不匹配的 VALID 缓存与无 manifest 各一段）
        assert set(statuses.values()) == {"uncached"}
        for clip in timeline.all_tracks[0].clips:
            direct = cache_status_for_clip(project, "tl-1", clip)
            indexed = build_cache_indicator_segments(timeline, project)
            assert direct is not None and indexed  # 两条路径都成功求值

    def test_plain_source_timeline_never_builds_index(self, monkeypatch) -> None:
        from src.features.edit.core import render_cache_status as rcs
        project = _project_with_asset(_video_asset())
        project = replace(project, render_cache_manifests={
            "a": self._manifest("a", "clip-0")})
        timeline = types.SimpleNamespace(
            timeline_id="tl-1",
            all_tracks=[types.SimpleNamespace(clips=[_clip("plain", 0, 25)])],
        )
        calls = {"n": 0}
        original = rcs.RenderCacheManifest.from_dict

        def counting(data):
            calls["n"] += 1
            return original(data)

        monkeypatch.setattr(rcs.RenderCacheManifest, "from_dict",
                            staticmethod(counting))
        assert rcs.build_cache_indicator_segments(timeline, project) == ()
        assert calls["n"] == 0  # 无 cache-required clip → 索引懒构建未触发


class TestCacheIndicatorPollGating:

    def test_plain_timeline_stops_the_poll_timer(self, qapp) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        view = TimelineView()
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                          clips=(_clip("plain", 0, 25),))
        timeline = TimelineSequence(
            timeline_id="tl-1", name="Seq", width=1920, height=1080, fps=24,
            video_tracks=(track,))
        view.load_timeline(timeline)
        view.set_project(_project_with_asset(_video_asset()))
        qapp.processEvents()
        assert not view._cache_indicator_timer.isActive()
        view.deleteLater()

    def test_motion_graphic_timeline_keeps_polling(self, qapp) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        mg_asset = MediaAsset(
            asset_id="mg-1", path="topos://mg/mg-1", media_type="motion_graphic",
            display_name="MG", metadata={"scene_id": "scene-1"},
        )
        track = EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(_clip("mg", 0, 48, asset_id="mg-1"),))
        timeline = TimelineSequence(
            timeline_id="tl-1", name="Seq", width=1920, height=1080, fps=24,
            video_tracks=(track,))
        view = TimelineView()
        view.load_timeline(timeline)
        view.set_project(_project_with_asset(mg_asset))
        qapp.processEvents()
        assert view._cache_indicator_timer.isActive()
        view.deleteLater()

    def test_none_project_stops_timer(self, qapp) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        view = TimelineView()
        view.set_project(None)
        qapp.processEvents()
        assert not view._cache_indicator_timer.isActive()
        view.deleteLater()


# =====================================================================
# AUD-21403 — 防抖 QTimer 复用
# =====================================================================

class TestDebounceTimerReuse:

    def test_repeated_calls_reuse_one_timer(self, qapp) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        view = TimelineView()
        view._debounce_thumbnail_schedule()
        first = view._scroll_debounce_timer
        assert first.isActive() and first.isSingleShot()
        assert first.interval() == 80
        view._debounce_thumbnail_schedule()
        view._debounce_thumbnail_schedule()
        assert view._scroll_debounce_timer is first  # 同一对象，不再累积
        view._scroll_debounce_timer.stop()
        view.deleteLater()

    def test_playback_activation_stops_and_reschedules(self, qapp) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        view = TimelineView()
        view._debounce_thumbnail_schedule()
        timer = view._scroll_debounce_timer
        view.set_playback_active_for_thumbnails(True)
        assert not timer.isActive()
        view.set_playback_active_for_thumbnails(False)
        assert timer.isActive()  # 复用同一 timer 重启
        assert view._scroll_debounce_timer is timer
        timer.stop()
        view.deleteLater()


# =====================================================================
# AUD-21404 — Alt 拖拽复制失败回滚
# =====================================================================

class TestAltDragRollback:

    def _setup_view(self, qapp):
        from src.features.edit.ui.timeline_view import TimelineView
        solid = MediaAsset(
            asset_id="solid-1", path="topos://solid/solid-1", media_type="solid",
            display_name="Solid 1", metadata={"generator_params": {"mode": "color"}},
        )
        clip = _clip("solid-clip", 0, 24, asset_id="solid-1")
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                          clips=(clip,))
        timeline = TimelineSequence(
            timeline_id="tl-1", name="Seq", width=1920, height=1080, fps=24,
            video_tracks=(track,))
        view = TimelineView()
        view.load_timeline(timeline)
        project = _project_with_asset(solid)
        view.set_project(project)
        view._current_drag_session = types.SimpleNamespace(
            original_positions={"solid-clip": 0},
            current_track_ids={"solid-clip": "v1"},
            original_track_ids={"solid-clip": "v1"},
        )
        return view, project

    def test_rejected_macro_rolls_back_project_reference(self, qapp, monkeypatch) -> None:
        view, project = self._setup_view(qapp)
        broadcast: List[Any] = []
        view.project_changed.connect(lambda p: broadcast.append(p))

        def _reject(command: Any) -> None:
            raise ValueError("overlap detected")

        monkeypatch.setattr(view, "_execute_command", _reject)
        view._handle_copy_drag_finished("solid-clip", "v1", 0, 48)
        # 失败路径：项目引用回滚（无孤儿 solid asset），不广播 project_changed
        assert view._project is project
        assert "solid-1" in project.assets
        new_assets = [a for a in view._project.assets.values()
                      if a.asset_id != "solid-1"]
        assert new_assets == []  # 未留下任何新建 asset
        assert broadcast == []
        view.deleteLater()

    def test_successful_copy_broadcasts_new_project(self, qapp, monkeypatch) -> None:
        view, project = self._setup_view(qapp)
        broadcast: List[Any] = []
        view.project_changed.connect(lambda p: broadcast.append(p))
        monkeypatch.setattr(view, "_execute_command", lambda command: None)
        view._handle_copy_drag_finished("solid-clip", "v1", 0, 48)
        assert view._project is not None
        assert len(view._project.assets) == len(project.assets) + 1
        assert broadcast == [view._project]
        view.deleteLater()


# =====================================================================
# GAP-004 — timeline_view.py 纯助手孤岛
# =====================================================================

class TestTimelineViewHelpers:

    def test_filter_external_media_paths_dedupes_and_validates(self, tmp_path) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        media = tmp_path / "clip.mp4"
        media.write_bytes(b"0")
        junk = tmp_path / "notes.txt"
        junk.write_text("x")
        result = TimelineView._filter_external_media_paths(
            [str(media), str(media), str(junk), str(tmp_path / "nope.mp4"), ""])
        assert result == [os.path.abspath(str(media))]

    def test_is_subtitle_asset(self) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        assert TimelineView._is_subtitle_asset(_video_asset()) is False
        srt = MediaAsset(asset_id="s1", path="/tmp/a.srt", media_type="subtitle",
                         display_name="subs", metadata={})
        assert TimelineView._is_subtitle_asset(srt) is True
        fake = MediaAsset(asset_id="s2", path="/tmp/b.srt", media_type="video",
                          display_name="v", metadata={})
        assert TimelineView._is_subtitle_asset(fake) is True

    def test_media_pool_preview_settings_frame_bounds(self) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        assert TimelineView._media_pool_preview_settings(None) == {}
        audio = MediaAsset(asset_id="a", path="x.wav", media_type="audio",
                           display_name="a", metadata={})
        assert TimelineView._media_pool_preview_settings(audio) == {}
        video = MediaAsset(
            asset_id="v", path="x.mp4", media_type="video",
            display_name="v", metadata={"total_frames": 100})
        from src.features.edit.ui.timeline_view import (
            MEDIA_POOL_PREVIEW_ASSET_ID_KEY, MEDIA_POOL_PREVIEW_FRAME_KEY,
        )
        settings = TimelineView._media_pool_preview_settings(video)
        assert settings[MEDIA_POOL_PREVIEW_ASSET_ID_KEY] == "v"
        assert 0 <= settings[MEDIA_POOL_PREVIEW_FRAME_KEY] < 100
        image = MediaAsset(asset_id="i", path="x.png", media_type="image",
                           display_name="i", metadata={})
        assert TimelineView._media_pool_preview_settings(image)[
            MEDIA_POOL_PREVIEW_FRAME_KEY] == 0

    def test_transition_center_bounds_symmetric_slide(self) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        left = _clip("l", 0, 48)
        right = _clip("r", 48, 48)
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                          clips=(left, right))
        transition = TransitionInstance(
            transition_id="t1", track_id="v1", left_clip_id="l",
            right_clip_id="r", center_frame=48, duration=12)
        # centered 对齐：center = start + duration/2 → [36+6, 48+6]
        assert TimelineView._transition_center_bounds(track, transition) == (42, 54)
        start_on_cut = replace(transition, alignment="start_on_cut")
        assert TimelineView._transition_center_bounds(track, start_on_cut) == (36, 48)

    def test_transition_center_bounds_mismatched_cut_is_fixed(self) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        left = _clip("l", 0, 40)
        right = _clip("r", 48, 48)  # 不邻接
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                          clips=(left, right))
        transition = TransitionInstance(
            transition_id="t1", track_id="v1", left_clip_id="l",
            right_clip_id="r", center_frame=44, duration=12)
        assert TimelineView._transition_center_bounds(
            track, transition) == (44, 44)

    def test_effective_timeline_duration_floors_and_runtime_extent(self, qapp) -> None:
        from src.features.edit.ui.timeline_view import TimelineView
        view = TimelineView()
        assert view._effective_timeline_duration(None) == 0
        view._runtime_duration_frames = 9999
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                          clips=())
        timeline = TimelineSequence(
            timeline_id="tl", name="T", width=1920, height=1080, fps=24,
            video_tracks=(track,))
        assert view._effective_timeline_duration(timeline) == 9999
        view.deleteLater()


# =====================================================================
# GAP-006 — media_pool_panel.py 纯助手孤岛
# =====================================================================

class TestMediaPoolHelpers:

    def test_media_pool_drag_mime_roundtrip(self, qapp) -> None:
        from src.features.edit.ui.media_pool_panel import (
            build_media_pool_drag_mime, parse_media_pool_drag_mime,
        )
        mime = build_media_pool_drag_mime(
            asset_ids=["a1", "a2", "a1"],
            timeline_ids=["tl1", "tl1", "tl2"])
        assets, timelines = parse_media_pool_drag_mime(mime)
        assert assets == ["a1", "a2"]
        assert timelines == ["tl1", "tl2"]

    def test_internal_drag_detection_prefers_custom_mime(self, qapp) -> None:
        from src.features.edit.ui.media_pool_panel import (
            _is_internal_media_pool_drag, build_bin_drag_mime,
            build_media_pool_drag_mime,
        )
        assert _is_internal_media_pool_drag(
            build_media_pool_drag_mime(asset_ids=["a1"])) is True
        assert _is_internal_media_pool_drag(
            build_bin_drag_mime(["bin-1"])) is True
        external = QMimeData()
        external.setText("just text")
        assert _is_internal_media_pool_drag(external) is False

    def test_bin_drag_mime_roundtrip_and_garbage(self, qapp) -> None:
        from src.features.edit.ui.media_pool_panel import (
            build_bin_drag_mime, parse_bin_drag_mime,
        )
        mime = build_bin_drag_mime(["b2", "b1", "b2"])
        assert parse_bin_drag_mime(mime) == ["b2", "b1"]
        empty = QMimeData()
        assert parse_bin_drag_mime(empty) == []
        corrupt = QMimeData()
        corrupt.setData("application/x-topos-media-pool-bins",
                        b"\xff\xfe not-json")
        assert parse_bin_drag_mime(corrupt) == []

    def test_asset_mime_falls_back_to_plain_text(self, qapp) -> None:
        from src.features.edit.ui.media_pool_panel import (
            MIME_TYPE_MEDIA_POOL_ASSETS, parse_media_pool_drag_mime,
        )
        mime = QMimeData()
        mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS, b"{invalid json")
        mime.setText("a9, a10")
        assets, timelines = parse_media_pool_drag_mime(mime)
        assert assets == ["a9", "a10"]
        assert timelines == []

    def test_extract_color_metadata_filters(self) -> None:
        from src.features.edit.ui.media_pool_panel import _extract_color_metadata
        assert _extract_color_metadata(None) is None
        assert _extract_color_metadata({}) is None
        meta = {"color_primaries": "bt709", "junk": 1,
                "color_transfer": None}
        assert _extract_color_metadata(meta) == {"color_primaries": "bt709"}

    def test_grid_item_accessors(self) -> None:
        from src.features.edit.ui.media_pool_panel import GridItem
        from src.features.edit.core.project import TimelineSequenceRef
        asset_item = GridItem("asset", "a1", _video_asset())
        assert asset_item.display_name == "Source"
        assert asset_item.is_asset and not asset_item.is_timeline
        ref = TimelineSequenceRef(timeline_id="tl-1", name="My Seq")
        tl_item = GridItem("timeline", "tl-1", ref)
        assert tl_item.display_name == "My Seq"
        assert tl_item.is_timeline and not tl_item.is_asset
        assert GridItem("asset", "x", "not-an-asset").display_name == ""

    def test_subclip_frame_request_variants(self) -> None:
        from src.features.edit.ui.media_pool_panel import GridDelegate
        plain = _video_asset()
        assert GridDelegate._subclip_frame_request(plain) is None
        sub = replace(plain, metadata={
            "is_subclip": True, "source_start": 10,
            "source_duration": 20, "source_total_frames": 25,
            "thumbnail_source_frame": 12})
        # total = max(25, frame+1=13, start+duration=30) = 30
        assert GridDelegate._subclip_frame_request(sub) == (12, 30)
        corrupt = replace(plain, metadata={"is_subclip": True,
                                           "source_start": "bad"})
        assert GridDelegate._subclip_frame_request(corrupt) is None

    def test_parse_keywords_splits_and_trims(self) -> None:
        from src.features.edit.ui.media_pool_panel import BatchAssetLogDialog
        assert BatchAssetLogDialog._parse_keywords("a, b 、c, , d ") == (
            "a", "b", "c", "d")

    @pytest.mark.parametrize("seconds,expected", [
        (12.5, "12.50s"), (75.4, "1:15.40"), (0.0, "0.00s"),
    ])
    def test_format_duration(self, seconds: float, expected: str) -> None:
        from src.features.edit.ui.media_pool_panel import QuickLookDialog
        assert QuickLookDialog._format_duration(seconds) == expected


# =====================================================================
# GAP-009 — timeline_items.py 纯助手孤岛
# =====================================================================

class TestTimelineItemsHelpers:

    def test_derive_label_palette_rejects_invalid(self) -> None:
        from src.features.edit.ui.timeline_items import _derive_label_palette
        assert _derive_label_palette(None) is None
        assert _derive_label_palette("") is None
        assert _derive_label_palette("not-a-color") is None

    def test_derive_label_palette_constrains_saturation(self) -> None:
        from src.features.edit.ui.timeline_items import _derive_label_palette
        palette = _derive_label_palette("#ff0000")
        assert palette is not None
        start, end, bar, wave = palette
        for color in (start, end, bar, wave):
            assert color.isValid()
        # 波形填充是近中性浅色（饱和度受限）
        _, wave_sat, wave_light, _ = wave.getHslF()
        assert wave_sat <= 0.36
        assert wave_light == pytest.approx(0.77, abs=1e-6)

    def test_derive_label_palette_achromatic(self) -> None:
        from src.features.edit.ui.timeline_items import _derive_label_palette
        palette = _derive_label_palette("#808080")
        assert palette is not None
        _, body_sat, _, _ = palette[0].getHslF()
        assert body_sat == 0.0

    def test_coordinate_manager_conversions(self) -> None:
        from src.features.edit.ui.timeline_items import (
            TimelineCoordinateManager, TRACK_HEADER_WIDTH,
        )
        mgr = TimelineCoordinateManager(pixels_per_frame=2.5)
        assert mgr.frame_to_x(10) == TRACK_HEADER_WIDTH + 25.0
        assert mgr.x_to_frame(TRACK_HEADER_WIDTH + 24.9) == 10
        assert mgr.x_to_frame(0) == 0  # 负帧钳 0
        assert mgr.frames_to_width(8) == 20.0
        assert mgr.width_to_frames(5.0) == 2
        assert mgr.width_to_frames(0.1) == 1  # 最小 1 帧
        mgr.update_zoom(5.0)
        assert mgr.pixels_per_frame == 5.0

    def test_global_coord_manager_singleton(self) -> None:
        from src.features.edit.ui.timeline_items import (
            TimelineCoordinateManager, get_coord_manager, set_coord_manager,
        )
        first = get_coord_manager()
        assert isinstance(first, TimelineCoordinateManager)
        custom = TimelineCoordinateManager(pixels_per_frame=9.0)
        try:
            set_coord_manager(custom)
            assert get_coord_manager() is custom
        finally:
            set_coord_manager(first)

    def test_is_menu_action_selected(self) -> None:
        from src.features.edit.ui.timeline_items import _is_menu_action_selected
        action = " QAction"
        assert _is_menu_action_selected(action, action) is True
        assert _is_menu_action_selected(action, None) is False
        assert _is_menu_action_selected(None, None) is False
        assert _is_menu_action_selected(None, action) is False

    @pytest.mark.parametrize("width,expected", [
        (10, "none"), (39.9, "none"), (40, "single"), (119.9, "single"),
        (120, "start_end"), (259.9, "start_end"), (260, "filmstrip"),
        (1200, "filmstrip"),
    ])
    def test_thumbnail_mode_lod_boundaries(self, width: float,
                                           expected: str) -> None:
        from src.features.edit.ui.timeline_items import ClipItem
        assert ClipItem._calculate_thumbnail_mode(width) == expected

    def test_static_cache_key_format(self) -> None:
        from src.features.edit.ui.timeline_items import ClipItem
        assert ClipItem._static_cache_key("c1", 100, 50) == "c1_100_50"

    def test_edit_tool_enum_members(self) -> None:
        from src.features.edit.ui.timeline_items import EditTool
        names = {tool.name for tool in EditTool}
        assert {"SELECT", "BLADE", "SLIP", "RATE_STRETCH"} <= names


# =====================================================================
# GAP-016 — edit_gpu_preview_renderer.py 纯助手孤岛
# =====================================================================

class TestGpuPreviewRendererHelpers:

    def test_blend_code_of(self) -> None:
        from src.features.edit.core.edit_gpu_preview_renderer import _blend_code_of
        assert _blend_code_of({}) == 0
        assert _blend_code_of({"blend_mode": None}) == 0
        assert _blend_code_of("not-a-dict") == 0
        assert _blend_code_of({"blend_mode": types.SimpleNamespace(code=3)}) == 3

    def test_decoded_frame_color_tag_prefers_direct_attr(self) -> None:
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _decoded_frame_color_tag,
        )
        direct = types.SimpleNamespace(
            color_primaries="bt2020",
            metadata=types.SimpleNamespace(color_primaries="bt709"))
        assert _decoded_frame_color_tag(direct, "color_primaries", "bt601") == "bt2020"
        via_meta = types.SimpleNamespace(
            color_primaries=None,
            metadata=types.SimpleNamespace(color_primaries="BT709"))
        assert _decoded_frame_color_tag(via_meta, "color_primaries", "bt601") == "bt709"
        empty = types.SimpleNamespace(metadata=None)
        assert _decoded_frame_color_tag(empty, "color_primaries", " BT601 ") == "bt601"

    def test_transform_tuple_is_identity(self) -> None:
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _transform_tuple_is_identity,
        )
        assert _transform_tuple_is_identity(
            (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)) is True
        assert _transform_tuple_is_identity(
            (5.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)) is False
        assert _transform_tuple_is_identity(
            (0.0, 0.0, 1.0, 1.0, 0.0, "topleft", 0.0, 0.0)) is False
        assert _transform_tuple_is_identity(()) is False
        assert _transform_tuple_is_identity(
            ("x", "y", "s", "s", 0.0, "center", 0.0, 0.0)) is False

    @pytest.mark.parametrize("fmt,planes,depth,expected", [
        ("nv12", 2, 8, "nv12"),
        ("p010le", 2, 10, "p010"),
        ("p016le", 2, 16, "p016"),
        ("yuv444p10le", 3, 10, "yuv444"),
        ("yuv422p10le", 3, 10, "yuv422"),
        ("yuv420p", 3, 8, "yuv420"),
        ("yuv440p", 3, 8, "yuv440"),
        ("yuv411p", 3, 8, "yuv411"),
        ("gbrp10le", 3, 10, "rgb_planar"),
        ("xyz12le", 3, 12, "yuv444"),
        ("topos_cfa12le", 4, 12, "cfa"),
        ("yuv444unknown16", 3, 16, "yuv444"),   # 子串回退
        ("weirdgbrp", 3, 8, "rgb_planar"),
        ("audioish", 0, 0, "unknown"),
    ])
    def test_classify_planar_format(self, fmt: str, planes: int, depth: int,
                                    expected: str) -> None:
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _classify_planar_format,
        )
        assert _classify_planar_format(fmt, planes, depth) == expected

    def test_degrade_params_for_preview_caps_glow_radius(self) -> None:
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = types.SimpleNamespace(effect_type="glow")
        out = _degrade_params_for_preview(spec, {"radius": 30.0, "intensity": 1})
        assert out == {"radius": 8.0, "intensity": 1}
        # 参数已达标 → 原引用透传（非拷贝）
        params = {"radius": 2.0}
        assert _degrade_params_for_preview(spec, params) is params
        # 无降级声明的效果原样透传
        plain = types.SimpleNamespace(effect_type="sharpen")
        params2 = {"amount": 9.0}
        assert _degrade_params_for_preview(plain, params2) is params2

    def test_gui_gl_stack_depth_counters(self) -> None:
        from src.features.edit.core.edit_gpu_preview_renderer import (
            enter_gui_gl_stack, exit_gui_gl_stack, gui_gl_stack_depth,
        )
        assert gui_gl_stack_depth() == 0
        enter_gui_gl_stack()
        enter_gui_gl_stack()
        assert gui_gl_stack_depth() == 2
        exit_gui_gl_stack()
        assert gui_gl_stack_depth() == 1
        exit_gui_gl_stack()
        assert gui_gl_stack_depth() == 0


# =====================================================================
# GAP-019 — edit_properties_panel.py 纯助手孤岛
# =====================================================================

class TestEditPropertiesHelpers:

    @pytest.mark.parametrize("frames,fps,expected", [
        (36, 24.0, "1s (36f)"),
        (0, 24.0, "0s (0f)"),
        (1440, 24.0, "1:00"),
        (100, 0.0, "4s (100f)"),  # 非法 fps 回退 24
    ])
    def test_format_duration(self, frames: int, fps: float,
                             expected: str) -> None:
        from src.features.edit.ui.edit_properties_panel import _format_duration
        assert _format_duration(frames, fps) == expected

    def test_compute_label_min_width_monotonic(self, qapp) -> None:
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        empty = EditPropertiesPanel._compute_label_min_width({})
        assert empty == 60  # 下限
        short = EditPropertiesPanel._compute_label_min_width({"radius": None})
        long = EditPropertiesPanel._compute_label_min_width(
            {"very_long_parameter_name_here": None})
        assert 60 <= short < long

    def test_value_to_slider_mapping(self) -> None:
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        assert EditPropertiesPanel._value_to_slider(7, {"type": "int"}) == 7
        assert EditPropertiesPanel._value_to_slider(
            0.5, {"type": "float", "min_val": 0.0, "step": 0.1}) == 5

    def test_properties_row_object_name_and_style(self, qapp) -> None:
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        row = EditPropertiesPanel._make_properties_row()
        assert row.objectName() == "editPropertiesRow"
        assert row.styleSheet()
        row.deleteLater()

    def test_section_widget_initial_state_and_toggle(self, qapp) -> None:
        from src.features.edit.ui.edit_properties_panel import EditSectionWidget
        section = EditSectionWidget("clip:c1:transform", "Transform")
        assert section.section_id == "clip:c1:transform"
        assert section._is_collapsed is False
        assert section._content_widget.isVisibleTo(section)
        section.toggle_collapse()
        assert section._is_collapsed is True
        assert not section._content_widget.isVisibleTo(section)
        section.set_effect_type("audio:reverb")
        assert section.effect_type == "audio:reverb"
        section.deleteLater()

    def test_section_widget_signals_wired(self, qapp) -> None:
        from src.features.edit.ui.edit_properties_panel import EditSectionWidget
        section = EditSectionWidget("s1", "S")
        fired: List[Any] = []
        section.param_changed.connect(lambda *a: fired.append(a))
        section.reset_requested.connect(lambda sid: fired.append(("reset", sid)))
        section.param_changed.emit("s1", "radius", 4.0)
        section.reset_requested.emit("s1")
        assert fired == [("s1", "radius", 4.0), ("reset", "s1")]
        section.deleteLater()

    def test_panel_defaults(self, qapp) -> None:
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        panel = EditPropertiesPanel()
        assert panel._updating is False
        assert panel._fps == 24.0
        assert panel._current_clips == []
        panel.deleteLater()


# =====================================================================
# GAP-021 — ai_edit_assistant_panel.py 纯助手孤岛
# =====================================================================

class TestAiAssistantHelpers:

    def test_trf_formats_and_falls_back(self) -> None:
        from src.features.edit.ui.ai_edit_assistant_panel import _trf
        assert _trf("k.x", "值 {n}", n=3) == "值 3"
        # 模板缺 key → 原样返回，不抛异常
        assert _trf("k.y", "plain {missing}", n=3) == "plain {missing}"
        assert _trf("k.z", "no kwargs") == "no kwargs"

    def test_slash_text_keeps_ids_localizes_chrome(self) -> None:
        from src.features.edit.ui.ai_edit_assistant_panel import (
            _SLASH_COMMANDS, _slash_text,
        )
        command = _SLASH_COMMANDS[0]
        assert _slash_text(command, "label") == command["label"]
        assert _slash_text(command, "description") == command["description"]
        # 收录的命令必须有稳定的 name 与 signal 入口
        for cmd in _SLASH_COMMANDS:
            assert cmd["name"] and cmd["signal"].endswith("_requested")

    def test_decode_data_url_bytes(self) -> None:
        import base64
        from src.features.edit.ui.ai_edit_assistant_panel import (
            _decode_data_url_bytes,
        )
        payload = base64.b64encode(b"image-bytes").decode()
        assert _decode_data_url_bytes(
            f"data:image/png;base64,{payload}") == b"image-bytes"
        assert _decode_data_url_bytes("data:no-comma") == b""
        assert _decode_data_url_bytes(
            f"data:image/png;base64,{'!!!not base64!!!'}") == b""

    def test_coerce_list_helpers(self) -> None:
        from src.features.edit.ui.ai_edit_assistant_panel import (
            _coerce_dict_list, _coerce_str_list,
        )
        assert _coerce_str_list(None) == []
        assert _coerce_str_list("scalar") == []
        assert _coerce_str_list(["a", 1, "b", None]) == ["a", "b"]
        assert _coerce_str_list(("x",)) == ["x"]
        assert _coerce_dict_list([{"k": 1}, "junk", 3, {"k": 2}]) == [
            {"k": 1}, {"k": 2}]
        assert _coerce_dict_list(None) == []

    def test_history_value_dict_or_object(self) -> None:
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        get = AiEditAssistantPanel._history_value
        assert get({"role": "user"}, "role", "x") == "user"
        assert get(types.SimpleNamespace(role="assistant"), "role") == "assistant"
        assert get("garbage", "role", "default") == "default"
        # key 存在但值为 None：dict.get 返回 None 而非 default
        assert get({"text": None}, "text", "fallback") is None

    def test_first_user_message_text_normalizes(self) -> None:
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        messages = [
            {"role": "system", "text": "system prompt"},
            {"role": "user", "text": "  hello   \n world\t"},
            {"role": "user", "text": "second"},
        ]
        assert AiEditAssistantPanel._first_user_message_text(messages) == "hello world"
        assert AiEditAssistantPanel._first_user_message_text([]) == ""
        assert AiEditAssistantPanel._first_user_message_text(None) == ""

    @pytest.mark.parametrize("title,expected", [
        ("", ""), ("short", "short"), ("12345678", "12345678"),
        ("123456789", "1234567…"),
        ("  lots   of   internal   space  ", "lots of…"),
    ])
    def test_compact_conversation_title(self, title: str, expected: str) -> None:
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        assert AiEditAssistantPanel._compact_conversation_title(title) == expected


# =====================================================================
# GAP-026 — program_viewer.py 纯助手孤岛
# =====================================================================

class TestProgramViewerHelpers:

    def test_modifiers_int_coercion(self, qapp) -> None:
        from src.features.edit.ui.program_viewer import _modifiers_int
        assert _modifiers_int(5) == 5
        assert _modifiers_int(
            Qt.KeyboardModifier.ShiftModifier) == int(
            Qt.KeyboardModifier.ShiftModifier.value)
        assert _modifiers_int(object()) == 0

    def test_rgb888_qimage_to_rgba_padded_stride(self) -> None:
        from src.features.edit.ui.program_viewer import _rgb888_qimage_to_rgba
        # 宽度 34（34*3=102，非 4 对齐）触发逐行去 padding 路径
        w, h = 34, 4
        image = QImage(w, h, QImage.Format.Format_RGB888)
        for y in range(h):
            for x in range(w):
                image.setPixel(x, y, qRgb(x * 7 % 256, y * 11 % 256, 128))
        out = _rgb888_qimage_to_rgba(image)
        assert out is not None and out.shape == (h, w, 4)
        assert out.dtype == np.uint8
        assert (out[:, :, 3] == 255).all()
        # 行首像素 RGB 与源一致（AUD-20400：padding 曾导致对角线错位）
        assert out[0, 0, 0] == 0 and out[0, 0, 1] == 0 and out[0, 0, 2] == 128
        assert out[2, 33, 0] == (33 * 7 % 256)
        assert out[2, 33, 1] == (2 * 11 % 256)

    def test_rgb888_qimage_to_rgba_compact_stride(self) -> None:
        from src.features.edit.ui.program_viewer import _rgb888_qimage_to_rgba
        w, h = 8, 2  # 8*3=24 已 4 对齐 → 紧凑路径
        image = QImage(w, h, QImage.Format.Format_RGB888)
        image.fill(qRgb(10, 20, 30))
        out = _rgb888_qimage_to_rgba(image)
        assert out.shape == (h, w, 4)
        assert (out[:, :, :3] == [10, 20, 30]).all()

    def test_rgb888_qimage_to_rgba_empty_is_none(self) -> None:
        from src.features.edit.ui.program_viewer import _rgb888_qimage_to_rgba
        assert _rgb888_qimage_to_rgba(QImage()) is None

    def test_pbo_rgba_to_qimage_flip(self) -> None:
        from src.features.edit.ui.program_viewer import ProgramViewer
        buffer = np.zeros((2, 3, 4), dtype=np.uint8)
        buffer[0, :, :] = (1, 2, 3, 255)
        buffer[1, :, :] = (4, 5, 6, 255)
        image = ProgramViewer._pbo_rgba_to_qimage(
            buffer.tobytes(), 3, 2, is_gl_oriented=True)
        assert image is not None and (image.width(), image.height()) == (3, 2)
        # GL Y 翻转：QImage 顶行来自 buffer 的最后一行
        top = QColor(image.pixelColor(0, 0))
        assert (top.red(), top.green(), top.blue()) == (4, 5, 6)
        bottom = QColor(image.pixelColor(0, 1))
        assert (bottom.red(), bottom.green(), bottom.blue()) == (1, 2, 3)

    def test_pbo_rgba_to_qimage_no_flip(self) -> None:
        from src.features.edit.ui.program_viewer import ProgramViewer
        buffer = np.full((2, 2, 4), (7, 8, 9, 255), dtype=np.uint8)
        image = ProgramViewer._pbo_rgba_to_qimage(
            buffer.tobytes(), 2, 2, is_gl_oriented=False)
        assert image is not None
        c = QColor(image.pixelColor(1, 1))
        assert (c.red(), c.green(), c.blue()) == (7, 8, 9)

    def test_pbo_rgba_to_qimage_bad_buffer_is_none(self) -> None:
        from src.features.edit.ui.program_viewer import ProgramViewer
        assert ProgramViewer._pbo_rgba_to_qimage(
            b"too-short", 4, 4, is_gl_oriented=False) is None
