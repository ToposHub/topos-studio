"""Regression tests for remediation batch wd9-g44 (plan batch D, deliver UI).

Covers the 8 Major entries fixed in this batch:

- AUD-12214: float_panel already-floating branch honoured ``floating``
  when an area was given (boolean precedence bug)
- AUD-12215: closing the dock actually returns the panel to the splitter
  (the visibilityChanged wiring the code always claimed now exists)
- AUD-12300: the item-view-era ``_on_context_menu``/``_on_double_clicked``
  orphans (QScrollArea.indexAt crash hazard) are gone
- AUD-12400: aspect-lock W/H linkage handlers are wired
- AUD-12401: encoder profile is back-filled by combo data, not by the
  capitalised display label (h264 "high"/"baseline"/"main10" no longer
  silently lost)
- AUD-12402: custom (user) presets mirror into the draft from the card
- AUD-12404: editing job_id only stamps single-job builds and the edit
  session ends on Add / project switch
- AUD-12405: draft-build domain errors surface in the preflight preview
  and the Add slots instead of being swallowed / aborting the process
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QWidget,
)

from src.features.deliver.core.custom_export_presets import (
    list_user_presets as _real_list_user_presets,
)
from src.features.deliver.core.video_export_job import VideoConfig, VideoExportJob
from src.features.deliver.core.video_export_presets import VideoPreset
from src.features.deliver.ui.detachable_panel_host import DetachablePanelHost
from src.features.deliver.ui.video_batch_export_panel import VideoBatchExportPanel
from src.features.deliver.ui.video_export_settings_panel import VideoExportSettingsPanel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ═══════════════════════════ AUD-12214 / AUD-12215 ═══════════════════════════


class TestDetachablePanelHost:
    def _make_host(self, qapp) -> tuple[DetachablePanelHost, QMainWindow, QSplitter, QWidget]:
        win = QMainWindow()
        splitter = QSplitter()
        panel = QWidget()
        splitter.addWidget(panel)
        win.setCentralWidget(splitter)
        win.show()
        host = DetachablePanelHost()
        host.attach(panel, splitter, win)
        return host, win, splitter, panel

    def test_floating_argument_honoured_with_area(self, qapp):
        """AUD-12214: with ``area`` given, floating=True keeps the window
        floating; floating=False docks it. The old expression parsed as
        ``(floating and area is None) or (area is None)`` so any area meant
        "always docked"."""
        host, _win, _splitter, _panel = self._make_host(qapp)
        assert host.float_panel() is True
        dock = host.dock
        assert dock is not None and dock.isFloating()

        assert host.float_panel(Qt.DockWidgetArea.LeftDockWidgetArea, floating=True) is True
        assert dock.isFloating(), "floating=True with an area must stay floating"

        assert host.float_panel(Qt.DockWidgetArea.LeftDockWidgetArea, floating=False) is True
        assert not dock.isFloating(), "floating=False with an area must dock"

    def test_floating_without_area_is_always_floating(self, qapp):
        """AUD-12214 companion: no area → always floating, even when the
        caller passes floating=False."""
        host, _win, _splitter, _panel = self._make_host(qapp)
        assert host.float_panel() is True
        assert host.float_panel(None, floating=False) is True
        assert host.dock is not None and host.dock.isFloating()

    def test_dock_close_returns_panel_to_splitter(self, qapp):
        """AUD-12215: hiding/closing the dock must return the panel to its
        splitter slot — previously the promised visibilityChanged handling
        did not exist and the panel stayed trapped in the closed dock."""
        host, _win, splitter, panel = self._make_host(qapp)
        assert host.float_panel() is True
        dock = host.dock
        assert dock is not None
        # close() is the real user gesture (title-bar close button); the
        # host defers the return by one event-loop tick.
        dock.close()
        qapp.processEvents()

        assert host.mode == "embedded"
        assert host.dock is None
        assert panel.parent() is splitter
        assert splitter.indexOf(panel) >= 0

    def test_manual_return_to_splitter_is_not_disturbed_by_signal(self, qapp):
        """AUD-12215 companion: return_to_splitter's own teardown hides the
        dock; the state lock must break the visibilityChanged recursion and
        still end up embedded."""
        host, _win, splitter, panel = self._make_host(qapp)
        assert host.float_panel() is True
        assert host.return_to_splitter() is True
        assert host.mode == "embedded"
        assert panel.parent() is splitter
        assert splitter.indexOf(panel) >= 0


# ═══════════════════════════════ AUD-12300 ═══════════════════════════════


class TestBatchPanelOrphanSlots:
    def test_indexat_not_on_qscrollarea(self):
        """Sanity: the API the old slot relied on does not exist on the
        card-stack view — any wiring would have crashed with AttributeError."""
        assert not hasattr(QScrollArea, "indexAt")

    def test_orphan_slots_removed(self, qapp):
        panel = VideoBatchExportPanel()
        assert not hasattr(panel, "_on_context_menu")
        assert not hasattr(panel, "_on_double_clicked")

    def test_card_actions_still_available(self, qapp):
        """The removed context menu's capabilities remain covered by the
        queue signals the per-card buttons emit."""
        panel = VideoBatchExportPanel()
        for signal_name in (
            "retry_requested",
            "remove_requested",
            "open_output_requested",
            "edit_job_requested",
            "cancel_current_requested",
        ):
            assert hasattr(panel, signal_name)


# ═══════════════════════════════ AUD-12400 ═══════════════════════════════


class TestAspectLockLinkage:
    def test_width_change_updates_height(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._width_spin.setValue(1920.0)
        panel._height_spin.setValue(1080.0)
        panel._aspect_lock_btn.setChecked(True)  # captures 16:9
        assert panel._aspect_locked is True

        panel._width_spin.setValue(3840.0)
        assert panel._height_spin.value() == pytest.approx(2160.0)

    def test_height_change_updates_width(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._width_spin.setValue(1920.0)
        panel._height_spin.setValue(1080.0)
        panel._aspect_lock_btn.setChecked(True)

        panel._height_spin.setValue(540.0)
        assert panel._width_spin.value() == pytest.approx(960.0)

    def test_no_linkage_when_unlocked(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._width_spin.setValue(1920.0)
        panel._height_spin.setValue(1080.0)
        assert panel._aspect_locked is False

        panel._width_spin.setValue(3840.0)
        assert panel._height_spin.value() == pytest.approx(1080.0)

    def test_linked_values_converge_without_recursion(self, qapp):
        """The _syncing_resolution_values state lock must stop the W→H→W
        echo; the final pair matches the locked ratio."""
        panel = VideoExportSettingsPanel()
        panel._width_spin.setValue(1920.0)
        panel._height_spin.setValue(1080.0)
        panel._aspect_lock_btn.setChecked(True)

        panel._height_spin.setValue(2160.0)
        assert panel._width_spin.value() == pytest.approx(3840.0)
        assert panel._height_spin.value() == pytest.approx(2160.0)


# ═══════════════════════════════ AUD-12401 ═══════════════════════════════


class TestProfileBackfill:
    def test_preset_mirror_selects_profile_by_data(self, qapp):
        """Selecting the Web H.264 preset (profile="high") must actually
        select the "High" item whose *data* is "high" — the label is
        capitalised so setCurrentText silently missed."""
        panel = VideoExportSettingsPanel()
        panel._preset_cards["web_h264"].set_checked(True)
        assert panel._codec_combo.currentData() == "h264"
        assert panel._profile_combo.currentData() == "high"

    def test_built_job_keeps_preset_profile(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._preset_cards["web_h264"].set_checked(True)
        jobs, _ = panel.build_jobs(preflight=False)
        assert len(jobs) == 1
        assert jobs[0].video_config.profile == "high"

    def test_load_job_backfills_profile_by_data(self, qapp):
        panel = VideoExportSettingsPanel()
        job = VideoExportJob(
            video_config=VideoConfig(
                container="mp4",
                codec="h264",
                profile="baseline",
                resolution=(1280, 720),
                quality_mode="vbr",
                quality_value=8_000_000,
            )
        )
        panel.load_job(job)
        assert panel._profile_combo.currentData() == "baseline"


# ═══════════════════════════════ AUD-12402 ═══════════════════════════════


def _user_preset() -> VideoPreset:
    return VideoPreset(
        preset_id="user_my_master",
        title_key="deliver.preset.custom_group",
        fallback_title="My Master",
        subtitle_key="deliver.preset.custom_group",
        fallback_subtitle="MOV · ProRes · UHD",
        video_config=VideoConfig(
            container="mov",
            codec="prores_422",
            profile=None,
            resolution=(3840, 2160),
            fps=None,
            quality_mode="vbr",
            quality_value=20_000_000,
        ),
    )


class TestCustomPresetMirror:
    def test_user_preset_mirrors_into_draft(self, qapp, monkeypatch):
        """Checking a user (custom) preset card must mirror its settings
        into the draft — the old DEFAULT_VIDEO_PRESETS lookup never matched
        "user_…" ids and left a stale draft in place."""
        import src.features.deliver.core.custom_export_presets as cep

        monkeypatch.setattr(cep, "list_user_presets", lambda: [_user_preset()])
        panel = VideoExportSettingsPanel()
        assert "user_my_master" in panel._preset_cards

        panel._preset_cards["user_my_master"].set_checked(True)
        assert panel._container_combo.currentData() == "mov"
        assert panel._codec_combo.currentData() == "prores_422"
        assert panel._width_spin.value() == pytest.approx(3840.0)
        assert panel._height_spin.value() == pytest.approx(2160.0)

    def test_user_preset_settings_reach_built_job(self, qapp, monkeypatch):
        """With the mirror in place the built job carries the custom
        preset's settings instead of being reverse-overridden by the stale
        draft."""
        import src.features.deliver.core.custom_export_presets as cep

        monkeypatch.setattr(cep, "list_user_presets", lambda: [_user_preset()])
        panel = VideoExportSettingsPanel()
        panel._preset_cards["user_my_master"].set_checked(True)
        jobs, _ = panel.build_jobs(preflight=False)
        assert len(jobs) == 1
        assert jobs[0].video_config.codec == "prores_422"
        assert jobs[0].video_config.container == "mov"
        assert jobs[0].video_config.resolution == (3840, 2160)


# ═══════════════════════════════ AUD-12404 ═══════════════════════════════


class TestEditingJobId:
    def test_single_selection_carries_editing_id(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._preset_cards["web_h264"].set_checked(True)
        panel._editing_job_id = "editing-1"
        jobs, _ = panel.build_jobs(preflight=False)
        assert len(jobs) == 1
        assert jobs[0].job_id == "editing-1"

    def test_multi_selection_does_not_duplicate_editing_id(self, qapp):
        """Editing with several presets selected must not stamp the same id
        onto every job — duplicate ids clobber each other in the queue."""
        panel = VideoExportSettingsPanel()
        panel._preset_cards["web_h264"].set_checked(True)
        panel._preset_cards["mobile_h264"].set_checked(True)
        panel._editing_job_id = "editing-1"
        jobs, _ = panel.build_jobs(preflight=False)
        assert len(jobs) == 2
        ids = [j.job_id for j in jobs]
        assert len(set(ids)) == 2, "job ids must be unique"
        assert "editing-1" not in ids

    def test_add_clicked_ends_editing_session(self, qapp, monkeypatch):
        panel = VideoExportSettingsPanel()
        panel._preset_cards["web_h264"].set_checked(True)
        panel._editing_job_id = "editing-1"
        real_jobs, _ = panel.build_jobs(preflight=False)
        monkeypatch.setattr(
            panel, "build_jobs", lambda preflight=True: (real_jobs, None)
        )
        emitted: list = []
        panel.add_to_queue_requested.connect(emitted.append)

        panel._on_add_clicked()

        assert len(emitted) == 1
        assert panel._editing_job_id is None, "edit session must end on Add"

    def test_add_and_start_clicked_ends_editing_session(self, qapp, monkeypatch):
        panel = VideoExportSettingsPanel()
        panel._preset_cards["web_h264"].set_checked(True)
        panel._editing_job_id = "editing-1"
        real_jobs, _ = panel.build_jobs(preflight=False)
        monkeypatch.setattr(
            panel, "build_jobs", lambda preflight=True: (real_jobs, None)
        )
        emitted: list = []
        panel.add_and_start_requested.connect(emitted.append)

        panel._on_add_and_start_clicked()

        assert len(emitted) == 1
        assert panel._editing_job_id is None

    def test_set_project_resets_editing_id(self, qapp):
        panel = VideoExportSettingsPanel()
        panel._editing_job_id = "editing-1"
        panel.set_project(None, project_id="other-project")
        assert panel._editing_job_id is None


# ═══════════════════════════════ AUD-12405 ═══════════════════════════════


class TestDraftBuildErrorHandling:
    @staticmethod
    def _boom(preflight: bool = True):
        raise ValueError("bad draft payload")

    def test_preflight_preview_shows_domain_error(self, qapp, monkeypatch):
        """The live preview must surface the domain error in the status
        line instead of silently clearing it."""
        panel = VideoExportSettingsPanel()
        monkeypatch.setattr(panel, "build_jobs", self._boom)
        panel._refresh_preflight()  # must not raise
        assert "bad draft payload" in panel._preflight_label.text()
        assert panel._preflight_label.text().startswith("⚠")

    def test_add_clicked_reports_domain_error(self, qapp, monkeypatch):
        """Add must never let a draft exception escape the Qt slot (PyQt6
        qFatal) — it shows a dialog and emits nothing."""
        shown: list = []
        monkeypatch.setattr(
            QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a))
        )
        panel = VideoExportSettingsPanel()
        monkeypatch.setattr(panel, "build_jobs", self._boom)
        emitted: list = []
        panel.add_to_queue_requested.connect(emitted.append)

        panel._on_add_clicked()  # must not raise

        assert len(shown) == 1
        assert not emitted

    def test_add_and_start_clicked_reports_domain_error(self, qapp, monkeypatch):
        shown: list = []
        monkeypatch.setattr(
            QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a))
        )
        panel = VideoExportSettingsPanel()
        monkeypatch.setattr(panel, "build_jobs", self._boom)
        emitted: list = []
        panel.add_and_start_requested.connect(emitted.append)

        panel._on_add_and_start_clicked()  # must not raise

        assert len(shown) == 1
        assert not emitted

    def test_error_tuple_is_explicit_and_not_broad(self):
        """The caught set is the explicit domain tuple, not bare/broad
        ``Exception``."""
        from src.features.deliver.ui.video_export_settings_panel import (
            _DRAFT_BUILD_ERRORS,
        )

        assert ValueError in _DRAFT_BUILD_ERRORS
        assert TypeError in _DRAFT_BUILD_ERRORS
        assert KeyError in _DRAFT_BUILD_ERRORS
        assert Exception not in _DRAFT_BUILD_ERRORS
        assert BaseException not in _DRAFT_BUILD_ERRORS


# Silence the "imported but unused" guard for the round-trip import above
# (kept for tests that want the real implementation to monkeypatch around).
assert callable(_real_list_user_presets)


# ── AUD-12216：restore_state 浮窗调度绑定宿主生命周期 ───────────────────


def test_restore_state_floating_schedule_dropped_after_host_deletion(qapp):
    """宿主在 tick 前销毁 → Qt 丢弃回调，不对已删 wrapper 调用。

    回归锁：若退回无上下文的 ``singleShot(0, lambda: self.float_panel())``，
    销毁后回调仍会执行（calls 非空或抛 RuntimeError）。
    """
    from PyQt6 import sip

    from src.features.deliver.ui.detachable_panel_host import DetachablePanelHost

    host = DetachablePanelHost()
    calls: list[int] = []
    host.float_panel = lambda: calls.append(1)  # type: ignore[method-assign]

    class _FloatingCfg:
        def get(self, key, default=None):
            return "floating" if key == "dock_video_batch_export_mode" else default

    host.restore_state(_FloatingCfg())
    sip.delete(host)      # C++ 对象先于 0ms tick 消亡
    qapp.processEvents()  # 交付 timer 事件

    assert calls == []
