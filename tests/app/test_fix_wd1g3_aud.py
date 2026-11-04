"""回归测试：REMEDIATION_PLAN 批 D（wd1-g3）color_controller 六条 Major。

覆盖 AUD-2302 / 2303 / 2304 / 2305 / 2306 / 2307 的修复行为（2026-09-29）：

* AUD-2302 遮罩操作 undo 覆盖：BUG-C4-01 已覆盖增/删/清空/建组/层级；
  本批补上 ``_on_tracker_pattern_committed``（画布拖拽 pattern 提交）
  的撤销快照。
* AUD-2303 视频降噪参数下发：优先走 ``engine.set_video_denoise_state``
  统一入口；渲染器直连路径失败必须记 warning（不再 ``except: pass``）。
* AUD-2304 denoise/deblur 完成回调：去掉冗余的全图 ``astype(float32)``
  拷贝（FileIO.save_baked_layer 自带 dtype 归一）。
* AUD-2305 denoise/deblur 完成回调：发起时记录 (文件路径, 图层索引)
  上下文，完成时校验；运行期间切换文件/图层则丢弃结果。
* AUD-2306 crop 会话：激活前保存工作显示快照，退出（含取消/空裁切框）
  恢复；apply 路径作废快照；会话期间切换文件不恢复。
* AUD-2307 LUT 反编译：不再 ``processEvents()`` 重入；异常路径恢复
  状态栏并记录堆栈。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMainWindow, QProgressBar
from unittest.mock import MagicMock, call

from src.app.controllers import color_controller as cc
from src.app.controllers.color_controller import ColorControllerMixin
from src.features.color.core.layer import Layer, MaskShape
from src.shared.video.denoise.params import VideoDenoiseParams


# ═══════════════════════════════════════════════════════════════════════
# Fakes（与 test_color_controller_coverage.py 同款约定：真 Qt 容器 +
# MagicMock 面板 + 记录型假引擎，无 GL / 无引擎线程 / 无模态框）
# ═══════════════════════════════════════════════════════════════════════


class _FakeHistory:
    def __init__(self) -> None:
        self.pushed: List[Tuple[Any, Any, int, bool]] = []

    def push_state(self, img: Any, layers: Any, active_index: int,
                   copy_image: bool = True) -> None:
        self.pushed.append((img, layers, active_index, copy_image))


class _FakeEngine:
    """记录型 ImageEngine 替身（可注入 set_video_denoise_state / 异常）。"""

    def get_tracked_geometry(self, shape: Any) -> Optional[Dict[str, Any]]:
        """AUD-2302 路径需要的引擎入口：None → 调用方回退 base geometry。"""
        return None

    def __init__(self) -> None:
        self.layers: List[Layer] = [Layer("Main")]
        self.active_layer_index: int = 0
        self.history: _FakeHistory = _FakeHistory()
        self.img_full_original: Optional[np.ndarray] = np.zeros(
            (4, 8, 3), dtype=np.float32)
        self.img_source_full: Optional[np.ndarray] = self.img_full_original
        self.img_high: Optional[np.ndarray] = np.zeros((4, 8, 3), np.float32)
        self.img_low: Optional[np.ndarray] = np.zeros((4, 8, 3), np.float32)
        self.current_file_path: str = "/proj/a.topos"
        self.current_raw_params: Dict[str, Any] = {"enabled": False}
        self.crop_params: Optional[Dict[str, Any]] = None
        self.source_bake_file_name: Optional[str] = None
        self.source_bake_type: Optional[str] = None
        self.video_denoise_params: Optional[VideoDenoiseParams] = None
        self.renderer: Any = MagicMock()
        self.set_image_calls: List[Dict[str, Any]] = []
        self.set_reference_calls: List[Any] = []
        self.crop_calls: List[Dict[str, Any]] = []
        self.decompile_calls: List[str] = []
        self.decompile_result: Any = {}
        self.decompile_exc: Optional[Exception] = None
        self.denoise_state_calls: List[Any] = []
        # 默认屏蔽类方法 set_video_denoise_state（实例属性 None 遮蔽），
        # 模拟旧引擎无公共入口；enable 后删除遮蔽露出真实入口。
        self.set_video_denoise_state = None  # type: ignore[assignment]

    # -- 注入开关 ---------------------------------------------------------
    def enable_video_denoise_state_entry(self) -> None:
        try:
            del self.set_video_denoise_state  # type: ignore[misc]
        except AttributeError:
            pass

    def set_video_denoise_state(self, state: Any) -> None:
        """真实 engine 的公共入口替身：记录参数（渲染器下发由真入口负责）。"""
        self.denoise_state_calls.append(state)
        self.video_denoise_params = (
            VideoDenoiseParams.from_dict(state) if state else None)

    # -- 记录型 API ---------------------------------------------------------
    def get_active_layer(self) -> Optional[Layer]:
        if 0 <= self.active_layer_index < len(self.layers):
            return self.layers[self.active_layer_index]
        return None

    def set_reference_image(self, image: Any) -> None:
        self.set_reference_calls.append(image)

    def set_image(self, full: Any, high: Any, low: Any,
                  is_new_file: bool = True) -> None:
        self.set_image_calls.append(
            {"full": full, "high": high, "low": low,
             "is_new_file": is_new_file})

    def apply_crop(self, params: Dict[str, Any]) -> None:
        self.crop_calls.append(params)

    def decompile_lut(self, path: str) -> Any:
        self.decompile_calls.append(path)
        if self.decompile_exc is not None:
            raise self.decompile_exc
        return self.decompile_result

    def on_raw_params_changed(self, params: Dict[str, Any]) -> None:
        pass


class _FixHost(QMainWindow, ColorControllerMixin):
    """ColorControllerMixin 最小宿主（MainWindow 形状）。"""

    def __init__(self) -> None:
        QMainWindow.__init__(self)
        self.status_bar = self.statusBar()
        self.engine = _FakeEngine()
        self.canvas = MagicMock()
        self.canvas.active_shape_id = None
        self.canvas.get_crop_data.return_value = None
        self.mask_panel = MagicMock()
        self.layer_panel = MagicMock()
        self.qualifier_panel = MagicMock()
        # AUD-2302 提交路径的跟踪器上下文：非交互 params → overlay
        # 走 hide_tracker_overlay 早退（产品合法分支，不依赖 dock 状态）
        self._tracker_params: Dict[str, Any] = {}
        self.tracker_panel = MagicMock()
        self.crop_panel = MagicMock()
        self.crop_panel.crop_active = False
        self.denoise_panel = MagicMock()
        self.denoise_panel.engine.apply_denoise.return_value = True
        self.deblur_panel = MagicMock()
        self.deblur_panel.engine.apply_denoise.return_value = True
        self.progress_bar = QProgressBar()

        self.pending_mask_updates: Dict[str, Dict[str, Any]] = {}
        self.mask_update_timer = QTimer()
        self.mask_update_timer.setSingleShot(True)

        self.current_file_path: Optional[str] = None
        self.render_calls: int = 0
        self.autosave_calls: int = 0
        self.update_canvas_calls: List[bool] = []
        self.update_from_layer_calls: List[Any] = []

    # -- mixin 依赖的 MainWindow 表面 ------------------------------------
    def request_render(self) -> None:
        self.render_calls += 1

    def _trigger_autosave(self) -> None:
        self.autosave_calls += 1

    def update_canvas(self, is_new_source: bool = False) -> None:
        self.update_canvas_calls.append(is_new_source)

    def update_from_layer(self, layer: Any) -> None:
        self.update_from_layer_calls.append(layer)


@pytest.fixture
def host(qapp) -> Iterator[_FixHost]:
    h = _FixHost()
    yield h
    h.mask_update_timer.stop()
    h.close()
    h.deleteLater()


def _neutralize_file_io(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    """FileIO 打桩：resize 恒等 + 记录 bake 保存入参（含数组身份）。"""
    calls: Dict[str, List[Any]] = {"saved": [], "deleted": []}
    monkeypatch.setattr(cc.FileIO, "_resize_to_fit",
                        staticmethod(lambda img, size: img))
    monkeypatch.setattr(
        cc.FileIO, "save_baked_layer",
        staticmethod(lambda name, arr: calls["saved"].append((name, arr)) or True))
    monkeypatch.setattr(
        cc.FileIO, "delete_baked_layer",
        staticmethod(lambda name: calls["deleted"].append(name) or True))
    return calls


# ═══════════════════════════════════════════════════════════════════════
# AUD-2302：tracker pattern 拖拽提交写 undo
# ═══════════════════════════════════════════════════════════════════════


def test_aud2302_tracker_pattern_commit_pushes_undo_snapshot(
        host: _FixHost) -> None:
    layer = host.engine.get_active_layer()
    assert layer is not None
    shape = MaskShape("circle")
    layer.mask_shapes.append(shape)
    host._tracker_shape_id = shape.id

    host._on_tracker_pattern_committed((0.1, 0.2, 0.4, 0.2))

    assert len(host.engine.history.pushed) == 1, (
        "pattern 拖拽提交必须先写撤销快照（AUD-2302）")
    img, layers, idx, copy_image = host.engine.history.pushed[0]
    assert img is host.engine.img_full_original
    assert layers is host.engine.layers
    assert idx == host.engine.active_layer_index
    assert copy_image is False, "纯遮罩几何快照不得复制源图像"
    assert shape.center == pytest.approx([0.3, 0.3])


def test_aud2302_tracker_pattern_commit_guards_push_no_snapshot(
        host: _FixHost) -> None:
    layer = host.engine.get_active_layer()
    assert layer is not None
    shape = MaskShape("circle")
    layer.mask_shapes.append(shape)
    host._tracker_shape_id = shape.id

    # 区域非法 / 目标缺失：不得产生空快照
    host._on_tracker_pattern_committed((None, None, None, None))
    host._tracker_shape_id = "missing-id"
    host._on_tracker_pattern_committed((0.1, 0.2, 0.4, 0.2))

    assert host.engine.history.pushed == []


# ═══════════════════════════════════════════════════════════════════════
# AUD-2303：视频降噪参数下发（engine 入口 / 失败留日志）
# ═══════════════════════════════════════════════════════════════════════


def test_aud2303_changed_routes_through_engine_entry(
        host: _FixHost) -> None:
    host.engine.enable_video_denoise_state_entry()

    host.on_video_denoise_changed({"temporal": {"enabled": True, "luma": 5.0}})

    assert len(host.engine.denoise_state_calls) == 1, (
        "参数变化必须经 engine.set_video_denoise_state 统一入口（AUD-2303）")
    state = host.engine.denoise_state_calls[0]
    assert isinstance(state, dict) and "temporal" in state
    assert isinstance(host.engine.video_denoise_params, VideoDenoiseParams)
    # engine 入口负责渲染器下发，app 层不再越层直连
    host.engine.renderer.set_video_denoise_params.assert_not_called()
    assert host.render_calls == 1


def test_aud2303_changed_fallback_records_params_and_pushes_renderer(
        host: _FixHost) -> None:
    host.on_video_denoise_changed({"temporal": {"enabled": True, "luma": 4.0}})

    params = host.engine.video_denoise_params
    assert isinstance(params, VideoDenoiseParams)
    host.engine.renderer.set_video_denoise_params.assert_called_once_with(params)
    assert host.render_calls == 1


def test_aud2303_renderer_failure_is_logged_not_swallowed(
        host: _FixHost, caplog: pytest.LogCaptureFixture) -> None:
    host.engine.renderer.set_video_denoise_params.side_effect = RuntimeError(
        "GL context lost")
    host.engine.renderer.reset_video_denoise_history.side_effect = RuntimeError(
        "history boom")

    with caplog.at_level(logging.WARNING, logger=cc.__name__):
        host.on_video_denoise_changed({"temporal": {"enabled": True}})
        host.on_video_denoise_history_reset()
        host.on_video_denoise_debug_overlay_toggled(True)

    # 管线不得被失败打断（渲染照常请求）
    assert host.render_calls == 3
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and r.exc_info]
    assert len(warnings) >= 3, (
        "三处下发失败都必须带 exc_info 记 warning（不再 except: pass）")


def test_aud2303_debug_overlay_kwarg_passthrough(host: _FixHost) -> None:
    host.on_video_denoise_changed({"spatial": {"enabled": True}})
    params = host.engine.video_denoise_params
    assert params is not None

    host.on_video_denoise_debug_overlay_toggled(True)
    host.engine.renderer.set_video_denoise_params.assert_called_with(
        params, debug_overlay=True)
    assert host.render_calls == 2


# ═══════════════════════════════════════════════════════════════════════
# AUD-2304：完成回调去掉冗余全图 astype 拷贝
# ═══════════════════════════════════════════════════════════════════════


def test_aud2304_denoise_finished_saves_original_array_without_copy(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    denoised = np.ones((4, 4, 3), dtype=np.float32)

    host.on_denoise_finished(denoised)

    assert len(calls["saved"]) == 1
    saved_name, saved_arr = calls["saved"][0]
    assert saved_name.startswith("source_bake_")
    assert saved_arr is denoised, (
        "denoised_img 已是 float32，不得再 astype 产生全图拷贝（AUD-2304）")
    assert host.engine.source_bake_file_name == saved_name


def test_aud2304_deblur_finished_saves_original_array_without_copy(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    deblurred = np.full((2, 2, 3), 0.5, dtype=np.float32)

    host.on_deblur_finished(deblurred)

    assert len(calls["saved"]) == 1
    assert calls["saved"][0][1] is deblurred
    assert host.engine.source_bake_type == "deblur"


# ═══════════════════════════════════════════════════════════════════════
# AUD-2305：denoise/deblur 完成回调校验任务上下文
# ═══════════════════════════════════════════════════════════════════════


def test_aud2305_denoise_discarded_after_file_switch(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.engine.img_full_original = np.zeros((4, 4, 3), np.float32)
    host.on_denoise_requested("bm3d", {})
    host.denoise_panel.engine.apply_denoise.assert_called_once()
    assert host.engine.history.pushed == []

    # 任务运行期间用户打开了新文件
    host.engine.current_file_path = "/proj/other.topos"

    host.on_denoise_finished(np.ones((4, 4, 3), np.float32))

    assert host.engine.history.pushed == [], "上下文已变更：不得污染新文件 undo 栈"
    assert host.engine.set_image_calls == [], "结果不得写入切换后的上下文"
    assert calls["saved"] == [] and calls["deleted"] == []
    assert host.engine.source_bake_file_name is None
    # requested 先置 False，丢弃路径必须把按钮恢复为 True
    assert call(True) in host.denoise_panel.apply_btn.setEnabled.call_args_list
    assert host.progress_bar.isHidden()
    assert "discarded" in host.status_bar.currentMessage().lower()


def test_aud2305_denoise_discarded_after_layer_switch(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.engine.layers.append(Layer("Second"))
    host.on_denoise_requested("bm3d", {})

    host.engine.active_layer_index = 1  # 任务运行期间切换图层

    host.on_denoise_finished(np.ones((4, 4, 3), np.float32))
    assert host.engine.set_image_calls == []
    assert calls["saved"] == []


def test_aud2305_denoise_applies_when_context_unchanged(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.on_denoise_requested("bm3d", {})

    host.on_denoise_finished(np.ones((4, 4, 3), np.float32))

    assert len(host.engine.set_image_calls) == 1
    assert len(calls["saved"]) == 1
    assert len(host.engine.history.pushed) == 1
    assert host._denoise_task_ctx is None, "应用后上下文必须清理"


def test_aud2305_finish_without_request_keeps_legacy_apply(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.on_denoise_finished(np.ones((4, 4, 3), np.float32))
    assert len(calls["saved"]) == 1, "无发起上下文的直调按兼容语义应用"


def test_aud2305_error_clears_context_and_busy_reject_keeps_old(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    monkeypatch.setattr(cc, "QMessageBox", MagicMock())

    host.on_denoise_requested("bm3d", {})
    host.on_denoise_error("boom")
    assert getattr(host, "_denoise_task_ctx", None) is None

    # busy 拒绝（apply_denoise 返回 False）不得覆盖/新建上下文
    host.denoise_panel.engine.apply_denoise.return_value = False
    host.on_denoise_requested("bm3d", {})
    assert getattr(host, "_denoise_task_ctx", None) is None


def test_aud2305_deblur_discarded_after_file_switch(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.on_deblur_requested("sharp", {})

    host.engine.current_file_path = "/proj/other.topos"

    host.on_deblur_finished(np.ones((2, 2, 3), np.float32))
    assert host.engine.set_image_calls == []
    assert calls["saved"] == []
    assert call(True) in host.deblur_panel.apply_btn.setEnabled.call_args_list
    assert host.progress_bar.isHidden()


# ═══════════════════════════════════════════════════════════════════════
# AUD-2306：crop 会话退出恢复工作显示
# ═══════════════════════════════════════════════════════════════════════


def test_aud2306_crop_exit_restores_baked_display(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _neutralize_file_io(monkeypatch)
    host.engine.source_bake_file_name = "source_bake_prev.exr"
    host.engine.source_bake_type = "denoise"
    working_high = np.full((4, 8, 3), 0.7, np.float32)
    host.engine.img_high = working_high

    host.on_crop_toggled(True)
    # 激活：换源图预览
    assert len(host.engine.set_image_calls) == 1
    assert host.engine.set_image_calls[0]["full"] is host.engine.img_source_full
    assert host.engine.img_high is working_high, "stash 不得改动引擎显示"

    host.on_crop_toggled(False)
    # 退出（取消）：恢复进入前的工作显示
    assert len(host.engine.set_image_calls) == 2
    restore = host.engine.set_image_calls[1]
    assert restore["high"] is working_high, "恢复必须回传原显示金字塔（零 resize）"
    assert restore["is_new_file"] is False
    assert host.update_canvas_calls == [False, False]
    # bake 引用与面板标记保持分裂前的状态
    assert host.engine.source_bake_file_name == "source_bake_prev.exr"
    assert host.engine.source_bake_type == "denoise"
    assert host.status_bar.currentMessage() == "Crop tool deactivated"
    assert host._crop_preview_restore is None


def test_aud2306_crop_apply_consumes_stash_no_restore(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _neutralize_file_io(monkeypatch)
    host.engine.source_bake_file_name = "source_bake_prev.exr"
    host.canvas.get_crop_data.return_value = {
        "rect": (0, 0, 100, 100), "rotation": 0.0,
        "center": (0.5, 0.5), "size": (1.0, 1.0)}

    host.on_crop_toggled(True)
    host.on_crop_applied(None)  # crop_requested 先于 crop_toggled(False)
    host.on_crop_toggled(False)

    # 裁切已应用（engine.apply_crop 记录到），退出不得恢复旧快照
    assert len(host.engine.crop_calls) == 1
    assert len(host.engine.set_image_calls) == 1, (
        "apply 路径已消费新工作态，退出时不得再 set_image 恢复")


def test_aud2306_no_bake_no_crop_no_restore(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _neutralize_file_io(monkeypatch)
    host.on_crop_toggled(True)
    host.on_crop_toggled(False)
    assert len(host.engine.set_image_calls) == 1, (
        "无可丢失工作态（未 bake、未裁切）时退出不重载")


def test_aud2306_prior_crop_also_restores(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _neutralize_file_io(monkeypatch)
    # 此前已应用过裁切：img_full_original ≠ 源图，退出同样需要恢复
    host.engine.crop_params = {"rect": (0.0, 0.0, 1.0, 1.0), "rotation": 0.0,
                               "center": (0.4, 0.4), "size": (0.8, 0.8)}

    host.on_crop_toggled(True)
    host.on_crop_toggled(False)

    assert len(host.engine.set_image_calls) == 2


def test_aud2306_file_switch_during_session_skips_restore(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _neutralize_file_io(monkeypatch)
    host.engine.source_bake_file_name = "source_bake_prev.exr"

    host.on_crop_toggled(True)
    host.engine.current_file_path = "/proj/other.topos"
    host.on_crop_toggled(False)

    assert len(host.engine.set_image_calls) == 1, (
        "会话期间切换文件：旧快照不再适用，不得恢复")


def test_aud2306_denoise_finish_invalidates_crop_stash(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _neutralize_file_io(monkeypatch)
    host.engine.source_bake_file_name = "source_bake_old.exr"

    host.on_crop_toggled(True)
    denoised = np.ones((4, 4, 3), np.float32)
    host.on_denoise_finished(denoised)

    host.on_crop_toggled(False)
    # 降噪结果已成为新工作显示：crop 退出不得恢复降噪前的旧快照
    # （set_image 共 2 次：crop 激活换源图 1 次 + 降噪结果应用 1 次）
    assert len(host.engine.set_image_calls) == 2
    assert host.engine.set_image_calls[-1]["full"] is denoised
    assert calls["saved"], "降噪结果本身必须正常落盘"


# ═══════════════════════════════════════════════════════════════════════
# AUD-2307：LUT 反编译（去 processEvents / 异常路径恢复状态栏）
# ═══════════════════════════════════════════════════════════════════════


def test_aud2307_decompile_exception_restores_status_bar(
        host: _FixHost, caplog: pytest.LogCaptureFixture) -> None:
    host.engine.decompile_exc = RuntimeError("cube parse blew up")

    with caplog.at_level(logging.ERROR, logger=cc.__name__):
        host.on_decompile_lut_requested("/luts/bad.cube")

    assert host.status_bar.currentMessage() == "LUT Decompile Failed."
    assert any(r.levelno == logging.ERROR and r.exc_info
               for r in caplog.records), "异常必须记录完整堆栈"
    assert host.update_from_layer_calls == []
    assert host.render_calls == 0
    assert host.autosave_calls == 0


def test_aud2307_decompile_no_process_events_reentrancy(
        host: _FixHost, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QApplication
    counter = {"count": 0}
    monkeypatch.setattr(
        QApplication, "processEvents",
        staticmethod(lambda *a, **k: counter.__setitem__("count",
                                                         counter["count"] + 1)),
        raising=True)

    host.engine.decompile_result = {"lift": [0.1, 0, 0]}
    host.on_decompile_lut_requested("/luts/ok.cube")
    assert counter["count"] == 0, (
        "反编译期间不得 processEvents() 重入事件循环（AUD-2307）")
    assert host.status_bar.currentMessage() == "LUT Decompiled Successfully!"
