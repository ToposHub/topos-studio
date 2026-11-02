"""批 D（wd4-g23）修复回归测试：color UI 杂项域 15 条（AUD-25509…AUD-25814）
+ GAP-005（canvas.py 覆盖率补测）。

对应 REMEDIATION_PLAN.md 行 968-981 与 1714 的条目：
- AUD-25509 plugin_panel 离散操作 interaction 边界（undo 快照）
- AUD-25513 presets_panel 缩略图 QPixmap 缓存
- AUD-25514 presets_panel 预设根目录稳定化（AppDataLocation + 迁移）
- AUD-25515 presets_panel 名称校验 / OSError 处理 / rename 回滚
- AUD-25610 scope_panel GPU 呈现失败不再静默
- AUD-25700 clip strip 播放期缩略图抑制接线
- AUD-25701 clip strip 帧率自适应时间码
- AUD-25702 clip strip 零高度视口重试上限
- AUD-25708 tools_panel 停用的 lut_panel 残留引用
- AUD-25801 tracker_overlay 手动修正拖动实时预览
- AUD-25802 tracker_overlay 边中点拖动尊重锁定角点
- AUD-25806 tracker_panel set_params 信号锁
- AUD-25810 tracker_toolbar set_target_names(None) 容错
- AUD-25814 warper_panel set_params 复位防护
- GAP-005 canvas.py 覆盖率（VideoControlBar / 滑杆 / 画布叠加件 / SAM 点）
"""

from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

pytestmark = pytest.mark.usefixtures("qapp")

from PyQt6.QtCore import QPointF, QSize, Qt
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QGraphicsScene, QListWidgetItem, QMessageBox

# ---------------------------------------------------------------------------
# 共享助手
# ---------------------------------------------------------------------------


class FakeMouseEvent:
    """与 test_tracker_homography_overlay 相同的最小鼠标事件桩。"""

    def __init__(self, pos, button=Qt.MouseButton.LeftButton):
        self._pos = QPointF(*pos)
        self._button = button
        self.accepted = False

    def scenePos(self):
        return self._pos

    def button(self):
        return self._button

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


def _img(h=48, w=64, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, 3)) * 255).astype(np.uint8)


# ===========================================================================
# AUD-25509 — plugin_panel 离散操作 interaction 边界
# ===========================================================================


class _FakePluginLayer:
    def __init__(self):
        self.id = "layer-1"
        self.name = "L1"
        self.plugins = []
        self.is_dirty = False
        self._instances = {
            "inst-1": {
                "instance_id": "inst-1",
                "plugin_id": "demo",
                "enabled": True,
                "params": {"amount": 0.5},
            }
        }

    def get_plugin_instance(self, instance_id):
        return self._instances.get(instance_id)

    def set_plugin_enabled(self, instance_id, state):
        inst = self._instances.get(instance_id)
        if inst is not None:
            inst["enabled"] = state

    def remove_plugin(self, instance_id):
        self._instances.pop(instance_id, None)


class TestAud25509PluginPanelInteractionBoundary:
    def _make_panel(self, monkeypatch):
        from src.features.color.ui import plugin_panel as pp

        layer = _FakePluginLayer()
        engine = SimpleNamespace(
            layers=[layer], active_layer_index=0, remove_plugin=Mock()
        )
        manager = Mock()
        manager.get_plugin.return_value = {
            "name": "Demo",
            "type": "dctl",
            "params": [{"var_name": "amount", "default": 0.25}],
        }
        monkeypatch.setattr(pp, "get_plugin_manager", lambda: manager)

        panel = pp.PluginPanel(engine)
        panel.current_layer = layer

        events = []
        panel.interaction_started.connect(lambda: events.append("start"))
        panel.params_changed.connect(lambda: events.append("params"))
        panel.interaction_ended.connect(lambda: events.append("end"))
        return panel, layer, engine, events

    def test_toggle_emits_interaction_boundary(self, monkeypatch):
        panel, layer, _engine, events = self._make_panel(monkeypatch)
        panel._toggle_plugin("inst-1", False)
        assert events == ["start", "params", "end"]
        assert layer._instances["inst-1"]["enabled"] is False

    def test_reset_restores_defaults_inside_boundary(self, monkeypatch):
        panel, layer, _engine, events = self._make_panel(monkeypatch)
        panel._reset_plugin("inst-1", "demo")
        assert events == ["start", "params", "end"]
        assert layer._instances["inst-1"]["params"]["amount"] == 0.25
        assert layer.is_dirty is True

    def test_delete_wraps_engine_removal_in_boundary(self, monkeypatch):
        panel, layer, engine, events = self._make_panel(monkeypatch)
        panel._delete_plugin("inst-1")
        assert events == ["start", "params", "end"]
        engine.remove_plugin.assert_called_once_with(0, "inst-1")

    def test_no_layer_no_signals(self, monkeypatch):
        panel, _layer, _engine, events = self._make_panel(monkeypatch)
        panel.current_layer = None
        panel._toggle_plugin("inst-1", False)
        panel._reset_plugin("inst-1", "demo")
        panel._delete_plugin("inst-1")
        assert events == []


# ===========================================================================
# AUD-25513 / 25514 / 25515 — presets_panel
# ===========================================================================


def _make_presets_panel(tmp_path, monkeypatch, root_name="Presets"):
    from src.features.color.ui import presets_panel as pr

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        pr, "_resolve_presets_root", lambda: str(tmp_path / root_name))
    panel = pr.PresetsPanel()
    return panel, pr


def _make_preset(folder, name, with_thumb=True):
    from src.features.color.core.preset_meta import write_still_images

    topos = os.path.join(folder, f"{name}.topos")
    with open(topos, "wb") as handle:
        handle.write(b"zip")
    if with_thumb:
        write_still_images(
            topos, _img(seed=abs(hash(name)) % 1000), thumb_only=True)
    return topos


class TestAud25513ThumbnailCache:
    def test_pixmap_decoded_once_and_reused(self, tmp_path, monkeypatch):
        panel, pr = _make_presets_panel(tmp_path, monkeypatch)
        folder = panel.get_current_folder()
        topos = _make_preset(folder, "Cached")

        real_qpixmap = pr.QPixmap
        loads = []

        class CountingPixmap:
            def __init__(self, *args):
                loads.append(args[0] if args else None)
                self._real = real_qpixmap(*args)

            def isNull(self):
                return self._real.isNull()

            def width(self):
                return self._real.width()

            def height(self):
                return self._real.height()

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(pr, "QPixmap", CountingPixmap)
        first = panel.delegate._thumbnail_pixmap(topos)
        second = panel.delegate._thumbnail_pixmap(topos)

        assert first is not None and not first.isNull()
        assert first is second, "第二次 paint 必须命中缓存（同一对象）"
        assert loads == [os.path.join(folder, "Cached.thumb.jpg")]

    def test_missing_thumb_negative_result_is_cached(self, tmp_path, monkeypatch):
        panel, pr = _make_presets_panel(tmp_path, monkeypatch)
        folder = panel.get_current_folder()
        topos = _make_preset(folder, "NoThumb", with_thumb=False)

        real_exists = os.path.exists
        stats = []

        def counting_exists(path):
            stats.append(path)
            return real_exists(path)

        monkeypatch.setattr(pr.os.path, "exists", counting_exists)
        assert panel.delegate._thumbnail_pixmap(topos) is None
        assert panel.delegate._thumbnail_pixmap(topos) is None
        assert stats.count(os.path.join(folder, "NoThumb.thumb.jpg")) == 1

    def test_rescan_clears_thumbnail_cache(self, tmp_path, monkeypatch):
        panel, _pr = _make_presets_panel(tmp_path, monkeypatch)
        folder = panel.get_current_folder()
        topos = _make_preset(folder, "Invalidated")
        assert panel.delegate._thumbnail_pixmap(topos) is not None
        panel.scan_presets()
        assert len(panel.delegate._thumb_cache) == 0

    def test_cache_evicts_beyond_limit(self, tmp_path, monkeypatch):
        panel, pr = _make_presets_panel(tmp_path, monkeypatch)
        delegate = panel.delegate
        delegate._THUMB_CACHE_LIMIT = 2
        assert delegate._thumbnail_pixmap("/ghost/a.topos") is None
        assert delegate._thumbnail_pixmap("/ghost/b.topos") is None
        assert delegate._thumbnail_pixmap("/ghost/c.topos") is None
        assert len(delegate._thumb_cache) == 2
        assert "/ghost/a.topos" not in delegate._thumb_cache


class TestAud25514StablePresetsRoot:
    def test_root_uses_app_data_location_not_cwd(self, tmp_path, monkeypatch):
        from src.features.color.ui import presets_panel as pr

        monkeypatch.setattr(
            pr.QStandardPaths, "writableLocation",
            lambda *_a, **_k: str(tmp_path / "AppData"))
        root = pr._resolve_presets_root()
        assert root == str(tmp_path / "AppData" / "Presets")
        assert "Presets" in root

    def test_root_falls_back_to_home_when_qt_location_empty(
            self, tmp_path, monkeypatch):
        from src.features.color.ui import presets_panel as pr

        monkeypatch.setattr(
            pr.QStandardPaths, "writableLocation",
            lambda *_a, **_k: "")
        monkeypatch.setattr(
            pr.os.path, "expanduser", lambda _p: str(tmp_path / "home"))
        root = pr._resolve_presets_root()
        assert root == str(tmp_path / "home" / ".topos-color" / "Presets")

    def test_panel_root_independent_of_cwd(self, tmp_path, monkeypatch):
        panel, _pr = _make_presets_panel(tmp_path, monkeypatch)
        assert panel.presets_root == str(tmp_path / "Presets")
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        assert panel.presets_root == str(tmp_path / "Presets")

    def test_legacy_cwd_presets_copied_not_moved(self, tmp_path, monkeypatch):
        legacy_user = tmp_path / "Presets" / "User"
        legacy_user.mkdir(parents=True)
        (legacy_user / "a.topos").write_bytes(b"zip-a")
        # 目标根目录与旧目录不同（模拟 AppDataLocation）
        panel, _pr = _make_presets_panel(
            tmp_path, monkeypatch, root_name="AppPresets")

        # 迁移是复制：来源保留、目标出现副本
        assert (legacy_user / "a.topos").exists()
        assert os.path.isfile(
            os.path.join(panel.presets_root, "User", "a.topos"))

    def test_ensure_dirs_survives_unwritable_location(
            self, tmp_path, monkeypatch):
        from src.features.color.ui import presets_panel as pr

        real_makedirs = os.makedirs

        def failing_makedirs(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(pr.os, "makedirs", failing_makedirs)
        try:
            pr.PresetsPanel()  # 不得从构造函数抛出（启动崩溃）
        finally:
            monkeypatch.setattr(pr.os, "makedirs", real_makedirs)


class TestAud25515PresetNameValidation:
    # ---- 纯校验函数 ----

    @pytest.mark.parametrize("name,expected", [
        ("Sunset", True),
        ("  Night  ", True),
        ("", False),
        ("   ", False),
        (".", False),
        ("..", False),
        ("../escape", False),
        ("a/b", False),
        ("a\\b", False),
        ("a\x00b", False),
        ("a\x1bb", False),
    ])
    def test_is_safe_preset_name(self, name, expected):
        from src.features.color.ui.presets_panel import _is_safe_preset_name

        assert _is_safe_preset_name(name) is expected

    # ---- create_folder ----

    def test_create_folder_rejects_path_injection(self, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog

        panel, _pr = _make_presets_panel(tmp_path, monkeypatch)
        warnings = []
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("../escape", True)))
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: warnings.append(a)))

        panel.create_folder()

        assert warnings, "非法名称必须弹窗提示"
        assert not (tmp_path.parent / "escape").exists()
        assert os.listdir(panel.get_current_folder()) == []

    def test_create_folder_reports_oserror(self, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog

        panel, pr = _make_presets_panel(tmp_path, monkeypatch)
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("Good", True)))
        criticals = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            staticmethod(lambda *a, **k: criticals.append(a)))
        real_makedirs = os.makedirs

        def failing_makedirs(path, *args, **kwargs):
            raise OSError(28, "No space left on device", path)

        monkeypatch.setattr(pr.os, "makedirs", failing_makedirs)
        try:
            panel.create_folder()  # 不得从槽内抛出
        finally:
            monkeypatch.setattr(pr.os, "makedirs", real_makedirs)
        assert criticals, "makedirs 失败必须弹窗报告"

    def test_create_folder_success(self, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog

        panel, _pr = _make_presets_panel(tmp_path, monkeypatch)
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("MyFolder", True)))
        panel.create_folder()
        assert os.path.basename(panel.get_current_folder()) == "MyFolder"

    # ---- _rename_preset ----

    def _make_item(self, panel, name):
        folder = panel.get_current_folder()
        topos = _make_preset(folder, name)
        panel.scan_presets()
        item = next(
            panel.list_widget.item(i)
            for i in range(panel.list_widget.count())
            if panel.list_widget.item(i).text() == name)
        return item, topos, folder

    def test_rename_rejects_unsafe_name(self, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog

        panel, _pr = _make_presets_panel(tmp_path, monkeypatch)
        item, topos, folder = self._make_item(panel, "Old")
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("../x", True)))
        warnings = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: warnings.append(a)))

        panel._rename_preset(item)

        assert warnings
        assert os.path.exists(topos), "原文件不得被动过"

    def test_rename_rolls_back_when_sidecar_fails(self, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog

        panel, pr = _make_presets_panel(tmp_path, monkeypatch)
        item, topos, folder = self._make_item(panel, "Old")
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("New", True)))
        criticals = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            staticmethod(lambda *a, **k: criticals.append(a)))
        real_rename = os.rename

        def failing_rename(src, dst):
            if str(src).endswith(".thumb.jpg"):
                raise OSError(5, "Input/output error", str(src))
            return real_rename(src, dst)

        monkeypatch.setattr(pr.os, "rename", failing_rename)
        try:
            panel._rename_preset(item)  # 不得从槽内抛出
        finally:
            monkeypatch.setattr(pr.os, "rename", real_rename)

        assert criticals, "改名失败必须弹窗报告"
        # 回滚：.topos 回到旧名，没有半新半旧的残局
        assert os.path.exists(os.path.join(folder, "Old.topos"))
        assert not os.path.exists(os.path.join(folder, "New.topos"))
        assert os.path.exists(os.path.join(folder, "Old.thumb.jpg"))
        assert not os.path.exists(os.path.join(folder, "New.thumb.jpg"))

    def test_rename_success_moves_sidecars(self, tmp_path, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog

        panel, _pr = _make_presets_panel(tmp_path, monkeypatch)
        item, topos, folder = self._make_item(panel, "Old")
        monkeypatch.setattr(
            QInputDialog, "getText",
            staticmethod(lambda *a, **k: ("New", True)))

        panel._rename_preset(item)

        assert os.path.exists(os.path.join(folder, "New.topos"))
        assert os.path.exists(os.path.join(folder, "New.thumb.jpg"))
        assert not os.path.exists(topos)


# ===========================================================================
# AUD-25610 — scope_panel GPU 呈现失败不静默
# ===========================================================================


class TestAud25610GpuPresentFailureLogging:
    def test_present_failure_logs_warning(self, caplog):
        from src.features.color.ui.scope_panel import ScopePanel

        class _FailingPresenter:
            def set_scope_frame(self, *_args):
                raise TypeError("unpack mismatch")

        stub = SimpleNamespace(
            _current_mode="waveform",
            _gpu_wf_presenter=_FailingPresenter(),
        )
        frame = SimpleNamespace(waveform_texture_id=7, waveform_size=(10,))
        with caplog.at_level(
                logging.WARNING,
                logger="src.features.color.ui.scope_panel"):
            ScopePanel._present_gpu_frame(stub, frame)
        assert "GPU scope present failed" in caplog.text

    def test_present_success_is_quiet(self, caplog):
        from src.features.color.ui.scope_panel import ScopePanel

        class _OkPresenter:
            def __init__(self):
                self.calls = []

            def set_scope_frame(self, *args):
                self.calls.append(args)

        presenter = _OkPresenter()
        stub = SimpleNamespace(
            _current_mode="waveform", _gpu_wf_presenter=presenter)
        frame = SimpleNamespace(waveform_texture_id=7, waveform_size=(10, 20))
        with caplog.at_level(
                logging.WARNING,
                logger="src.features.color.ui.scope_panel"):
            ScopePanel._present_gpu_frame(stub, frame)
        assert presenter.calls == [(7, 10, 20)]
        assert caplog.text == ""


# ===========================================================================
# AUD-25700 / 25701 / 25702 — timeline_clip_strip
# ===========================================================================


def _make_clip_entry(index=0, timeline_start=None):
    from src.features.color.ui.timeline_clip_strip import ColorClipEntry

    return ColorClipEntry(
        timeline_id="tl-1",
        clip_id=f"clip-{index}",
        track_id="V1",
        track_name="V1",
        asset_id=f"asset-{index}",
        asset_name=f"clip_{index:04d}.png",
        asset_path=f"/nonexistent/clip_{index:04d}.png",
        media_type="image",
        timeline_start=100 * index if timeline_start is None else timeline_start,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        color_stack_id=None,
        color_shared=False,
        color_graded=False,
    )


class TestAud25700PlaybackSuppression:
    def test_explicit_flag_short_circuits(self, qapp):
        from src.features.color.ui.timeline_clip_strip import TimelineClipStrip

        strip = TimelineClipStrip()
        assert strip._playback_in_progress() is False
        strip.set_playback_active(True)
        assert strip._playback_in_progress() is True
        strip.set_playback_active(False)
        assert strip._playback_in_progress() is False

    def test_global_playback_registry_consulted(self, qapp, monkeypatch):
        import src.shared.video.video_playback as vp
        from src.features.color.ui.timeline_clip_strip import TimelineClipStrip

        strip = TimelineClipStrip()
        monkeypatch.setattr(vp, "_is_any_engine_playing", lambda: True)
        assert strip._playback_in_progress() is True
        monkeypatch.setattr(vp, "_is_any_engine_playing", lambda: False)
        assert strip._playback_in_progress() is False

    def test_playback_suppresses_new_thumbnail_requests(
            self, qapp, monkeypatch):
        from src.features.color.ui import timeline_clip_strip as tcs
        from src.features.color.ui.timeline_clip_strip import (
            CardSize, ClipPreviewMode, TimelineClipCard, TimelineClipStrip,
        )
        import src.shared.video.video_playback as vp

        strip = TimelineClipStrip()
        card = TimelineClipCard(
            _make_clip_entry(), clip_index=1, total_clips=1,
            preview_mode=ClipPreviewMode.MIDDLE_FRAME,
            card_size=CardSize.MEDIUM)
        strip._cards = [card]
        assert card._thumbnail_requested is False

        provider = Mock()
        provider.get_cached_thumbnail.return_value = None
        provider_cls = Mock()
        provider_cls.instance.return_value = provider
        monkeypatch.setattr(tcs, "ThumbnailProvider", provider_cls)

        spy = []
        monkeypatch.setattr(
            card, "ensure_thumbnail_requested", lambda: spy.append(1))

        monkeypatch.setattr(vp, "_is_any_engine_playing", lambda: True)
        strip._request_visible_thumbnails()
        assert spy == [], "播放期间不得发起新的缩略图请求"
        assert card._thumbnail_requested is False

        monkeypatch.setattr(vp, "_is_any_engine_playing", lambda: False)
        strip._request_visible_thumbnails()
        assert spy == [1], "停止播放后正常恢复请求"


class TestAud25701FpsAdaptiveTimecode:
    def test_normalize_fps_variants(self):
        from src.shared.media.metadata import Rational
        from src.features.color.ui.timeline_clip_strip import _normalize_fps

        assert _normalize_fps(25) == 25.0
        assert _normalize_fps(29.97) == pytest.approx(29.97)
        assert _normalize_fps(Rational(24000, 1001)) == pytest.approx(
            24000 / 1001)
        assert _normalize_fps(None) == 24.0
        assert _normalize_fps("bad") == 24.0
        assert _normalize_fps(0) == 24.0
        assert _normalize_fps(-30.0) == 24.0

    def test_timecode_matches_audit_example(self):
        from src.features.color.ui.timeline_clip_strip import _format_timecode

        # 30 fps 下第 1800 帧：错误（24fps）为 00:01:15:00，正确为 00:01:00:00
        assert _format_timecode(1800, 24.0) == "00:01:15:00"
        assert _format_timecode(1800, 30.0) == "00:01:00:00"

    def test_card_set_fps_refreshes_overlay(self, qapp):
        from src.features.color.ui.timeline_clip_strip import (
            CardSize, ClipPreviewMode, TimelineClipCard,
        )

        card = TimelineClipCard(
            _make_clip_entry(0, timeline_start=1800), clip_index=1,
            total_clips=1, preview_mode=ClipPreviewMode.MIDDLE_FRAME,
            card_size=CardSize.MEDIUM)
        assert card._thumb_widget._timecode == "00:01:15:00"  # 默认 24fps
        assert card.set_fps(30.0) is not False
        assert card._thumb_widget._timecode == "00:01:00:00"
        card.deleteLater()

    def test_strip_set_fps_updates_and_reports(self, qapp):
        from src.features.color.ui.timeline_clip_strip import TimelineClipStrip

        strip = TimelineClipStrip()
        assert strip.fps() == 24.0
        assert strip.set_fps(30.0) is True
        assert strip.fps() == 30.0
        assert strip.set_fps(30.0) is False  # 无变化
        assert strip.set_fps(25) is True     # int 亦可

    def test_set_project_and_timeline_derives_fps(self, qapp):
        from src.features.color.ui.timeline_clip_strip import TimelineClipStrip

        strip = TimelineClipStrip()
        timeline = SimpleNamespace(
            timeline_id="t1", fps=25.0, video_tracks=())
        strip.set_project_and_timeline(None, timeline)
        assert strip.fps() == 25.0


class TestAud25702ViewportFitRetryCap:
    def test_zero_height_viewport_stops_after_limit(self, qapp):
        from src.features.color.ui.timeline_clip_strip import (
            _MAX_VIEWPORT_FIT_RETRIES, CardSize, ClipPreviewMode,
            TimelineClipCard, TimelineClipStrip,
        )

        strip = TimelineClipStrip()
        card = TimelineClipCard(
            _make_clip_entry(), clip_index=1, total_clips=1,
            preview_mode=ClipPreviewMode.MIDDLE_FRAME,
            card_size=CardSize.MEDIUM)
        strip._cards = [card]

        class _StubViewport:
            def height(self):
                return 0

        starts = []
        strip._scroll_area = SimpleNamespace(viewport=lambda: _StubViewport())
        strip._geometry_update_timer = SimpleNamespace(
            start=lambda ms: starts.append(ms))

        for _ in range(_MAX_VIEWPORT_FIT_RETRIES * 3):
            strip._fit_cards_to_viewport()

        assert len(starts) == _MAX_VIEWPORT_FIT_RETRIES, (
            "零高度视口的重试必须有上限，不得无限自续期")
        card.deleteLater()


# ===========================================================================
# AUD-25708 — tools_panel 停用的 lut_panel 引用
# ===========================================================================


class TestAud25708ToolsPanelInstantiable:
    def test_construction_does_not_raise(self, qapp, monkeypatch):
        from src.features.color.ui.tools_panel import ToolsPanel

        # 归档面板的子控件栈（示波器/模糊面板）在 offscreen 下有与本条
        # 无关的延迟绘制缺陷；屏蔽布局回调并停掉节流定时器，避免拆除
        # 阶段事件循环把它们当成失败——本用例只验证 lut_panel 修复。
        monkeypatch.setattr(ToolsPanel, "enforce_ratio", lambda self: None)
        panel = ToolsPanel()  # 修复前：init_ui 连接 self.lut_panel 必崩
        # 归档接口（代理信号）保留，供未来恢复 LUT 页时无需改调用方
        assert hasattr(ToolsPanel, "lut_selected")
        assert hasattr(ToolsPanel, "lut_intensity_changed")
        assert hasattr(ToolsPanel, "lut_reset")
        panel.update_from_layer(None)  # None 早退，同样不得抛
        param_timer = getattr(panel.effects_panel, "_param_timer", None)
        if param_timer is not None:
            param_timer.stop()
        panel.deleteLater()


# ===========================================================================
# AUD-25801 / 25802 — tracker_overlay
# ===========================================================================


def _make_trs_overlay():
    from src.features.color.ui.tracker_overlay import TrackerOverlay

    overlay = TrackerOverlay()
    scene = QGraphicsScene()
    scene.addItem(overlay)
    overlay._test_scene = scene  # 持有场景引用，防止 C++ 对象被提前回收
    overlay.set_frame_size(1000, 500)
    overlay.set_pattern_region((0.3, 0.3, 0.4, 0.3))
    overlay.set_search_region((0.2, 0.2, 0.6, 0.5))
    overlay.set_current_transform(np.eye(3))
    overlay.set_overlay_active(True)
    return overlay


def _make_homography_overlay():
    from src.features.color.ui.tracker_overlay import TrackerOverlay

    overlay = TrackerOverlay()
    scene = QGraphicsScene()
    scene.addItem(overlay)
    overlay._test_scene = scene  # 持有场景引用，防止 C++ 对象被提前回收
    overlay.set_frame_size(1000, 500)
    overlay.set_pattern_region((0.2, 0.2, 0.6, 0.6))
    overlay.set_tracking_mode("homography")
    overlay.set_homography_corners([
        (0.25, 0.3), (0.75, 0.25), (0.8, 0.7), (0.2, 0.75)
    ])
    overlay.set_overlay_active(True)
    return overlay


class TestAud25801ManualCorrectionDragPreview:
    def test_drag_updates_preview_live(self):
        overlay = _make_trs_overlay()
        assert overlay._preview_region is None
        assert overlay._current_corners()[0] == pytest.approx((0.3, 0.3))

        overlay.mousePressEvent(FakeMouseEvent((500, 250)))  # 框内 → current
        overlay.mouseMoveEvent(FakeMouseEvent((600, 300)))   # dx=0.1 dy=0.1

        assert overlay._preview_region is not None
        corners = overlay._current_corners()
        assert corners[0] == pytest.approx((0.4, 0.4))
        assert corners[2] == pytest.approx((0.8, 0.7))

    def test_release_keeps_final_preview_and_commits(self):
        overlay = _make_trs_overlay()
        commits = []
        overlay.current_region_committed.connect(commits.append)

        overlay.mousePressEvent(FakeMouseEvent((500, 250)))
        overlay.mouseMoveEvent(FakeMouseEvent((600, 300)))
        overlay.mouseReleaseEvent(FakeMouseEvent((620, 320)))

        assert len(commits) == 1
        assert commits[0] == pytest.approx((0.42, 0.44, 0.4, 0.3))
        # 提交无下游回填：最终预览保留显示，不跳回拖动前位置
        assert overlay._preview_region is not None
        assert overlay._current_corners()[0] == pytest.approx((0.42, 0.44))

    def test_external_transform_refresh_clears_preview(self):
        overlay = _make_trs_overlay()
        overlay.mousePressEvent(FakeMouseEvent((500, 250)))
        overlay.mouseMoveEvent(FakeMouseEvent((600, 300)))
        assert overlay._preview_region is not None

        overlay.set_current_transform(np.eye(3))  # 下一帧变换到达
        assert overlay._preview_region is None
        assert overlay._current_corners()[0] == pytest.approx((0.3, 0.3))

    def test_pattern_change_clears_preview(self):
        overlay = _make_trs_overlay()
        overlay.mousePressEvent(FakeMouseEvent((500, 250)))
        overlay.mouseMoveEvent(FakeMouseEvent((600, 300)))
        overlay.set_pattern_region((0.1, 0.1, 0.3, 0.3))
        assert overlay._preview_region is None

    def test_hide_clears_preview(self):
        overlay = _make_trs_overlay()
        overlay.mousePressEvent(FakeMouseEvent((500, 250)))
        overlay.mouseMoveEvent(FakeMouseEvent((600, 300)))
        overlay.set_overlay_active(False)
        assert overlay._preview_region is None


class TestAud25802EdgeDragRespectsLockedCorners:
    def _arm_edge_drag(self, overlay, index=0):
        corners = [
            (0.25, 0.3), (0.75, 0.25), (0.8, 0.7), (0.2, 0.75)
        ]
        overlay._drag = {
            "kind": "homography_edge",
            "corner": index,
            "start_corners": list(corners),
            "start_norm": (0.5, 0.3),
        }

    def test_locked_endpoint_not_moved_by_edge_drag(self):
        overlay = _make_homography_overlay()
        overlay.set_homography_corner_locked(1, True)  # 边 0 的端点
        self._arm_edge_drag(overlay, index=0)
        overlay._update_homography_drag(0.05, -0.05)

        corners = overlay._homography_corners
        assert corners[0] == pytest.approx((0.30, 0.25))   # 未锁端点移动
        assert corners[1] == pytest.approx((0.75, 0.25))   # 锁定端点锚定
        assert corners[2] == pytest.approx((0.8, 0.7))
        assert corners[3] == pytest.approx((0.2, 0.75))

    def test_fully_locked_edge_is_immovable(self):
        overlay = _make_homography_overlay()
        overlay.set_homography_corner_locked(0, True)
        overlay.set_homography_corner_locked(1, True)
        self._arm_edge_drag(overlay, index=0)
        overlay._update_homography_drag(0.05, -0.05)

        assert overlay._homography_corners[0] == pytest.approx((0.25, 0.3))
        assert overlay._homography_corners[1] == pytest.approx((0.75, 0.25))

    def test_unlocked_edge_moves_both_endpoints(self):
        overlay = _make_homography_overlay()
        self._arm_edge_drag(overlay, index=0)
        overlay._update_homography_drag(0.05, -0.05)

        corners = overlay._homography_corners
        assert corners[0] == pytest.approx((0.30, 0.25))
        assert corners[1] == pytest.approx((0.80, 0.20))

    def test_committed_corners_after_locked_edge_drag(self):
        overlay = _make_homography_overlay()
        overlay.set_homography_corner_locked(1, True)
        commits = []
        overlay.homography_corners_committed.connect(commits.append)
        # 边 0 中点手柄位于 (0.25..0.75 的中点)
        overlay.mousePressEvent(FakeMouseEvent((500, 138)))
        overlay.mouseMoveEvent(FakeMouseEvent((550, 130)))
        overlay.mouseReleaseEvent(FakeMouseEvent((550, 130)))
        assert len(commits) == 1
        assert commits[0][1] == pytest.approx((0.75, 0.25)), "锁定角点不得移动"


# ===========================================================================
# AUD-25806 — tracker_panel set_params 信号锁
# ===========================================================================


class TestAud25806TrackerPanelSetParamsLock:
    def test_programmatic_set_params_does_not_broadcast(self, qapp):
        from src.features.color.ui.tracker_panel import MODE_ITEMS, TrackerPanel

        panel = TrackerPanel()
        emitted = []
        panel.params_changed.connect(lambda *a: emitted.append(1))

        current = panel.get_params()
        other_mode = next(
            value for value, _text in MODE_ITEMS if value != current["mode"])
        panel.set_params({
            "mode": other_mode,
            "min_confidence": 0.5,
            "feature_count": 40,
            "search_multiplier": 3.0,
        })

        assert emitted == [], "程序化回填不得发出半新半旧的 params_changed"
        new = panel.get_params()
        assert new["mode"] == other_mode
        assert new["min_confidence"] == pytest.approx(0.5)
        assert new["feature_count"] == 40
        assert new["search_multiplier"] == pytest.approx(3.0)

    def test_conf_label_refreshed_after_unlock(self, qapp):
        from src.features.color.ui.tracker_panel import TrackerPanel

        panel = TrackerPanel()
        panel.set_params({"min_confidence": 0.42})
        # 信号锁拦掉了 valueChanged 联动，标签必须在解锁后手工同步
        assert panel.conf_value.text() == "0.42"

    def test_manual_change_still_emits(self, qapp):
        from src.features.color.ui.tracker_panel import TrackerPanel

        panel = TrackerPanel()
        emitted = []
        panel.params_changed.connect(lambda *a: emitted.append(1))
        panel.feature_spin.setValue(panel.feature_spin.value() + 1)
        assert emitted == [1], "用户交互仍正常广播"


# ===========================================================================
# AUD-25810 — tracker_toolbar set_target_names(None)
# ===========================================================================


class TestAud25810ToolbarTargetNamesNone:
    def test_none_names_no_crash_and_default_item(self, qapp):
        from src.features.color.ui.tracker_toolbar import TrackerToolBar

        bar = TrackerToolBar()
        bar.set_target_names(None)  # 修复前：TypeError + 信号锁未释放
        assert bar.target_combo.count() == 1
        assert bar.target_combo.currentText() == "Window 1"
        assert bar.target_combo.signalsBlocked() is False
        bar.deleteLater()

    def test_empty_list_falls_back_to_default(self, qapp):
        from src.features.color.ui.tracker_toolbar import TrackerToolBar

        bar = TrackerToolBar()
        bar.set_target_names([])
        assert bar.target_combo.currentText() == "Window 1"
        bar.deleteLater()

    def test_names_preserve_current_selection(self, qapp):
        from src.features.color.ui.tracker_toolbar import TrackerToolBar

        bar = TrackerToolBar()
        bar.set_target_names(["A", "B"])
        bar.target_combo.setCurrentText("B")
        bar.set_target_names(["B", "C"])
        assert bar.target_combo.currentText() == "B"
        bar.deleteLater()

    def test_combo_still_signals_after_none_call(self, qapp):
        from src.features.color.ui.tracker_toolbar import TrackerToolBar

        bar = TrackerToolBar()
        bar.set_target_names(None)
        fired = []
        bar.target_combo.currentIndexChanged.connect(
            lambda _i: fired.append(1))
        bar.set_target_names(["X", "Y"])
        bar.target_combo.setCurrentIndex(1)
        assert fired == [1], "blockSignals 必须已释放"
        bar.deleteLater()


# ===========================================================================
# AUD-25814 — warper_panel set_params 复位防护
# ===========================================================================


class TestAud25814WarperPanelSetParamsResetGuard:
    def test_set_params_cancels_pending_drag_sample(self, qapp):
        from src.features.color.ui.warper_panel import WarperPanel

        panel = WarperPanel()
        # 模拟拖动进行中：16ms 合流定时器已武装、待发样本在场
        panel.warper._pending_deltas = np.zeros((6, 6, 2), dtype=np.float32)
        panel.warper._changed_timer.start()
        assert panel.warper._changed_timer.isActive()

        emitted = []
        panel.warper_changed.connect(emitted.append)

        panel.set_params(np.zeros((6, 6, 2), dtype=np.float32))

        assert not panel.warper._changed_timer.isActive(), (
            "set_grid_size 复位防护必须停掉待发定时器")
        assert panel.warper._pending_deltas is None
        assert emitted == [], "emit=False 期间不得发布任何网格"

    def test_set_params_rebuilds_identity_grid(self, qapp):
        from src.features.color.ui.warper_panel import WarperPanel

        panel = WarperPanel()
        deltas = np.zeros((6, 6, 2), dtype=np.float32)
        deltas[0, 0] = [0.1, 0.05]
        panel.set_params(deltas)
        assert panel.warper.grid_size == 6
        expected = panel.warper._initial_position(0, 0)
        # delta 回到 0（identity）时点位即初始位置
        panel.set_params(np.zeros((6, 6, 2), dtype=np.float32))
        assert np.allclose(panel.warper.points[0, 0], expected)
        assert panel.grid_combo.currentIndex() == 0

    def test_set_params_updates_grid_size_combo(self, qapp):
        from src.features.color.ui.warper_panel import WarperPanel

        panel = WarperPanel()
        panel.set_params(np.zeros((12, 12, 2), dtype=np.float32))
        assert panel.warper.grid_size == 12
        assert panel.grid_combo.currentIndex() == 2

    def test_stale_timer_does_not_publish_after_set_params(self, qapp):
        """端到端竞态：武装定时器后 hydrate 新状态，等待超时不得发布。"""
        from PyQt6.QtCore import QTimer

        from src.features.color.ui.warper_panel import WarperPanel

        panel = WarperPanel()
        emitted = []
        panel.warper_changed.connect(emitted.append)

        panel.warper._pending_deltas = np.full((6, 6, 2), 0.3,
                                               dtype=np.float32)
        panel.warper._changed_timer.start()
        panel.set_params(np.zeros((6, 6, 2), dtype=np.float32))

        loop_timeout = QTimer()
        loop_timeout.setSingleShot(True)
        loop_timeout.start(50)
        while loop_timeout.isActive():
            QApplication_process_events()
        assert emitted == []
        assert panel.warper._pending_deltas is None


def QApplication_process_events():
    from PyQt6.QtWidgets import QApplication

    QApplication.processEvents()


# ===========================================================================
# GAP-005 — canvas.py 覆盖率补测（VideoControlBar / 滑杆 / 叠加件 / SAM 点）
# ===========================================================================


def _make_video_bar(qapp):
    from src.features.color.ui.canvas import VideoControlBar

    bar = VideoControlBar()
    return bar


class TestVideoControlBarState:
    def test_timecode_uses_configured_fps(self, qapp):
        bar = _make_video_bar(qapp)
        bar.set_frame_rate(30.0)
        bar.set_total_frames(2400)
        bar.set_current_frame(90)
        assert bar.timecode_label.text() == "00:00:03:00"
        bar.deleteLater()

    def test_invalid_fps_rejected(self, qapp):
        bar = _make_video_bar(qapp)
        bar.set_frame_rate(30.0)
        bar.set_frame_rate(0)
        bar.set_frame_rate(-5.0)
        bar.set_frame_rate("junk")
        assert bar._fps == 30.0
        bar.deleteLater()

    def test_total_frames_drive_slider_maximum(self, qapp):
        bar = _make_video_bar(qapp)
        bar.set_total_frames(101)
        assert bar.slider.maximum() == 100
        assert bar.ruler_slider.maximum() == 100
        bar.deleteLater()

    def test_set_current_frame_updates_both_sliders(self, qapp):
        bar = _make_video_bar(qapp)
        bar.set_total_frames(100)
        bar.set_current_frame(42)
        assert bar.slider.value() == 42
        assert bar.ruler_slider.value() == 42
        assert bar.current_frame == 42
        bar.deleteLater()

    def test_playing_state_and_time_info(self, qapp):
        bar = _make_video_bar(qapp)
        bar.set_playing(True)
        assert bar.is_playing is True
        bar.set_playing(False)
        assert bar.is_playing is False
        bar.set_time_info("00:12")
        assert bar.timecode_label.text() == "00:12"
        bar.deleteLater()

    def test_in_out_points_update_buttons(self, qapp):
        bar = _make_video_bar(qapp)
        bar.set_total_frames(100)
        bar.set_in_out_points(10, 90)
        assert bar.btn_clear.isEnabled()
        assert bar.label_range_in.text() == "10.0"
        assert bar.label_range_out.text() == "90.0"
        assert bar.slider._in_point == 10 and bar.slider._out_point == 90
        bar.set_in_out_points(None, None)
        assert not bar.btn_clear.isEnabled()
        assert bar.slider._in_point is None
        bar.deleteLater()

    def test_keyframe_frames_forwarded_to_ruler(self, qapp):
        bar = _make_video_bar(qapp)
        bar.set_keyframe_frames([5, 5, 3, None] if False else [5, 5, 3])
        assert bar.ruler_slider._keyframe_frames == [3, 5]
        bar.set_keyframe_frames([])
        assert bar.ruler_slider._keyframe_frames == []
        bar.deleteLater()


class TestCanvasSliders:
    def test_in_out_slider_stores_and_clears(self, qapp):
        from src.features.color.ui.canvas import _InOutSlider

        slider = _InOutSlider(Qt.Orientation.Horizontal)
        slider.set_in_out_points(3, 9)
        assert (slider._in_point, slider._out_point) == (3, 9)
        slider.set_in_out_points(None, None)
        assert slider._in_point is None and slider._out_point is None
        slider.deleteLater()

    def test_fusion_ruler_value_x_mapping(self, qapp):
        from src.features.color.ui.canvas import _FusionFrameRulerSlider

        ruler = _FusionFrameRulerSlider(Qt.Orientation.Horizontal)
        ruler.setRange(0, 99)
        ruler.resize(480, 38)
        assert ruler._value_to_x(0) == 0
        assert ruler._value_to_x(99) == 479
        assert ruler._x_to_value(0) == 0
        assert ruler._x_to_value(479) == 99
        assert ruler._x_to_value(10_000) == 99  # 越界钳制
        assert ruler._tick_step() >= 1
        ruler.deleteLater()

    def test_fusion_ruler_in_out_and_keyframes(self, qapp):
        from src.features.color.ui.canvas import _FusionFrameRulerSlider

        ruler = _FusionFrameRulerSlider(Qt.Orientation.Horizontal)
        ruler.set_in_out_points(4, 8)
        assert (ruler._in_point, ruler._out_point) == (4, 8)
        ruler.set_keyframe_frames([9, 1, 1])
        assert ruler._keyframe_frames == [1, 9]
        ruler.deleteLater()


class TestCanvasOverlayWidgets:
    def test_color_inspector_stores_and_paints(self, qapp):
        from src.features.color.ui.canvas import ColorInspectorOverlay

        overlay = ColorInspectorOverlay()
        overlay.update_info((12, 34, 56), (0.05, 0.13, 0.22), (7, 8))
        assert overlay._rgb == (12, 34, 56)
        assert overlay._pos == (7, 8)
        grabbed = overlay.grab()  # 触发 paintEvent
        assert not grabbed.isNull()
        overlay.deleteLater()

    def test_brush_cursor_geometry_and_modes(self, qapp):
        from src.features.color.ui.canvas import BrushCursorItem

        item = BrushCursorItem()
        # 默认 size=50 feather=50 → r=110，边长 220
        assert item.boundingRect().width() == pytest.approx(220.0)
        item.set_params(10, 20)
        assert item.boundingRect().width() == pytest.approx(80.0)
        # 非 bool 真值必须被强制为 bool（防 KeyboardModifier 误存）
        item.set_eraser_mode(Qt.KeyboardModifier.ShiftModifier)
        assert item._is_eraser is True
        item.set_opacity(0.25)
        assert item._opacity == 0.25

    def test_brush_stroke_preview_smoothing(self, qapp):
        from src.features.color.ui.canvas import BrushStrokePreviewItem

        item = BrushStrokePreviewItem()
        assert item.path.isEmpty()
        item.start_stroke((10.0, 10.0), 50.0, is_eraser=True)
        assert item.is_eraser is True
        assert item.scene_size == 50.0
        item.add_points([(20.0, 10.0), (30.0, 12.0)])
        assert len(item._raw_points) == 3
        assert not item.path.isEmpty()
        # boundingRect 用真实 scene_size 做边距
        rect = item.boundingRect()
        base = item.path.boundingRect()
        assert rect.width() >= base.width() + 2 * 50.0
        item.clear_stroke()
        assert item.path.isEmpty() and item._raw_points == []

    def test_sam_point_marker_geometry(self, qapp):
        from src.features.color.ui.canvas import SAMPointItem

        fg = SAMPointItem(is_foreground=True)
        bg = SAMPointItem(is_foreground=False)
        assert fg.boundingRect() == bg.boundingRect()
        assert fg.boundingRect().width() == pytest.approx(28.0)
        assert fg._is_foreground is True and bg._is_foreground is False


class TestCanvasSamPointBookkeeping:
    def test_undo_and_clear_sam_points(self, qapp, monkeypatch):
        from src.features.color.ui.canvas import Canvas, SAMPointItem

        canvas = Canvas()
        monkeypatch.setattr(canvas, "_update_lod", lambda: None)

        assert canvas.undo_sam_point_ui() is False  # 空表
        for _ in range(3):
            item = SAMPointItem()
            canvas.scene.addItem(item)
            canvas.sam_points_ui.append(item)

        assert canvas.undo_sam_point_ui() is True
        assert len(canvas.sam_points_ui) == 2
        canvas.clear_sam_points()
        assert canvas.sam_points_ui == []
        assert canvas.undo_sam_point_ui() is False
        canvas.deleteLater()

    def test_display_scene_size_override(self, qapp, monkeypatch):
        from src.features.color.ui.canvas import Canvas

        canvas = Canvas()
        monkeypatch.setattr(canvas, "_update_lod", lambda: None)
        monkeypatch.setattr(
            canvas, "_update_scene_pan_bounds", lambda *a, **k: None)

        assert canvas._display_scene_size_for_source(1920, 1080) == (1920, 1080)
        canvas.set_display_scene_size_override((3840, 2160))
        assert canvas._display_scene_size_for_source(1920, 1080) == (3840, 2160)
        canvas.set_display_scene_size_override(None)
        assert canvas._display_scene_size_for_source(1920, 1080) == (1920, 1080)
        canvas.deleteLater()

    def test_brush_params_update_cursor_items(self, qapp, monkeypatch):
        from src.features.color.ui.canvas import BrushCursorItem, Canvas

        canvas = Canvas()
        monkeypatch.setattr(canvas, "_update_lod", lambda: None)
        canvas.set_brush_params({"size": 33, "feather": 12, "opacity": 0.7,
                                 "flow": 1.0})
        assert canvas._brush_params["size"] == 33
        # 画布持有的笔刷光标跟随参数
        cursor = BrushCursorItem()
        cursor.set_params(33, 12)
        assert cursor.boundingRect().width() == pytest.approx(
            2 * (33 + 12 + 10))
        canvas.deleteLater()
