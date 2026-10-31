# -*- coding: utf-8 -*-
"""EF-03 part2 regression tests — src/features/edit/ui Suggestion-level fixes.

Covers (one test group per AUD id):
- AUD-20509: relink directory scan caches one index per directory
- AUD-20604: scene cut source switch defers provider close + generation guard
- AUD-20605: Backspace rejects current candidate (already-fixed regression)
- AUD-20702: scene cut graph widget _updating lock survives exceptions
- AUD-20714: ThumbnailLoader pending-request cleanup
- AUD-20717: preview widget _updating lock survives exceptions
- AUD-20803: batch model operations hold the lock / defer signals
- AUD-20807: SceneCutResultModel.sort implementation
- AUD-20815: overlay show/hide anti-recursion lock
- AUD-20904: SourceViewer caches uniform locations / static quad VBO
- AUD-21003: locked subtitle track rejects keyboard delete
- AUD-21110: disabled/locked clip pattern keeps translucent opacity
- AUD-21203: resolve_timeline_hit checks the S/V splitter
- AUD-21204: timeline layout rejects pixels_per_frame <= 0
- AUD-21304: timeline panel _zoom_updating lock survives exceptions
- AUD-21306: proxy HUD stats cached per asset set + TTL
- AUD-21408: locked-track trim feedback emits status_message_requested
- AUD-21410: layout hoists all_ordered_tracks / coalesces scroll relayout
- AUD-21412: external file drop probes new files in a worker pool

Every test is revert-sensitive: undoing the corresponding fix must fail it.
No real OpenGL context is created (GL checks are AST-structural).
"""

from __future__ import annotations

import ast
import math
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QGraphicsView

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "src" / "features" / "edit" / "ui"

pytestmark = pytest.mark.usefixtures("qapp")


# ══════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════


def _method_node(
    file_name: str,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef:
    """Return the AST FunctionDef for a class method, or fail the test."""
    source = (UI_DIR / file_name).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    return item  # type: ignore[return-value]
    pytest.fail(f"{class_name}.{method_name} not found in {file_name}")


def _call_attr_names(node: ast.AST) -> List[str]:
    """Attribute names of every call expression under ``node``."""
    names: List[str] = []
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
        ):
            names.append(sub.func.attr)
    return names


class _FakeTable:
    """Minimal stand-in for the relink dialog's QTableWidget."""

    def __init__(self, rows: int) -> None:
        self._rows = rows

    def rowCount(self) -> int:  # noqa: N802 - Qt naming
        return self._rows

    def item(self, row: int, column: int):  # noqa: ANN201
        if column == 0:
            return SimpleNamespace(
                data=lambda role: f"asset-{row}"
            )
        if column == 2:
            return SimpleNamespace(text=lambda: "")
        return None


class _FakeProvider:
    def __init__(self) -> None:
        self.closed = False
        self.is_open = True

    def open(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _FakeWorker:
    def __init__(self, wait_result: bool = False, running: bool = True) -> None:
        self._wait_result = wait_result
        self._running = running

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return self._running

    def wait(self, timeout_ms: int) -> bool:
        return self._wait_result

    def requestInterruption(self) -> None:
        self._running = False


# ══════════════════════════════════════════════════════════════════════════
# AUD-20509: relink directory scan builds one index per directory
# ══════════════════════════════════════════════════════════════════════════


class TestAud20509RelinkDirectoryIndex:
    def _make_dialog(self):
        from src.features.edit.ui.relink_media_dialog import RelinkMediaDialog

        dialog = RelinkMediaDialog.__new__(RelinkMediaDialog)
        dialog._table = _FakeTable(rows=2)
        return dialog

    def test_build_directory_index_maps_direct_and_nested_files(
        self, tmp_path: Path
    ) -> None:
        dialog = self._make_dialog()
        root = tmp_path / "scan"
        root.mkdir(parents=True)
        (root / "top.mp4").write_bytes(b"x")
        sub = root / "sub"
        sub.mkdir()
        (sub / "nested.wav").write_bytes(b"x")

        index = dialog._build_directory_index(str(root))

        assert index["top.mp4"] == str(root / "top.mp4")
        assert index["nested.wav"] == str(sub / "nested.wav")
        assert dialog._build_directory_index(str(tmp_path / "missing")) == {}

    def test_search_file_in_directory_shares_index_semantics(
        self, tmp_path: Path
    ) -> None:
        dialog = self._make_dialog()
        root = tmp_path / "scan"
        sub = root / "sub"
        sub.mkdir(parents=True)
        (root / "a.mp4").write_bytes(b"x")
        (sub / "b.mp4").write_bytes(b"x")

        assert dialog._search_file_in_directory(str(root), "a.mp4") == str(
            root / "a.mp4"
        )
        assert dialog._search_file_in_directory(str(root), "b.mp4") == str(
            sub / "b.mp4"
        )
        assert dialog._search_file_in_directory(str(root), "c.mp4") is None

    def test_auto_search_indexes_each_directory_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.features.edit.ui.relink_media_dialog as relink_mod

        home = tmp_path / "home"
        for name in ("Desktop", "Downloads"):
            (home / name).mkdir(parents=True)
        project = home / "project"
        project.mkdir()
        (project / "clip01.mp4").write_bytes(b"x")
        (home / "Desktop" / "desktop_clip.mp4").write_bytes(b"x")

        # Keep the scan hermetic: "~" resolves into the tmp tree.
        monkeypatch.setattr(
            relink_mod.os.path, "expanduser", lambda p: str(home)
        )

        dialog = self._make_dialog()
        assets = {
            "asset-0": SimpleNamespace(path=str(project / "clip01.mp4")),
            "asset-1": SimpleNamespace(path=str(home / "nowhere" / "desktop_clip.mp4")),
        }
        monkeypatch.setattr(
            dialog, "_get_asset_by_id", lambda asset_id: assets.get(asset_id)
        )
        updates: List[tuple] = []
        monkeypatch.setattr(
            dialog,
            "_update_relink_path",
            lambda row, new_path: updates.append((row, new_path)),
        )
        monkeypatch.setattr(
            relink_mod.QMessageBox, "information", lambda *a, **k: None
        )

        builds: List[str] = []
        original_index = relink_mod.RelinkMediaDialog._build_directory_index

        def _counting(dlg: Any, directory: str):
            builds.append(directory)
            return original_index(dlg, directory)

        monkeypatch.setattr(
            relink_mod.RelinkMediaDialog,
            "_build_directory_index",
            _counting,
        )

        dialog._on_auto_search()

        # Two assets x 6 candidate directories must not rescan directories:
        # every unique directory is indexed exactly once.
        assert len(builds) == len(set(builds)) == 6
        # Both assets were relinked from the pre-built indexes.
        assert sorted(row for row, _ in updates) == [0, 1]


# ══════════════════════════════════════════════════════════════════════════
# AUD-20604: source switch defers provider close + generation guard
# ══════════════════════════════════════════════════════════════════════════


class TestAud20604SourceSwitch:
    def _make_dialog(self):
        from src.features.edit.core.scene_cut.models import SceneCutSettings
        from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

        dialog = SceneCutDialog.__new__(SceneCutDialog)
        dialog._preview_pending_request = None
        dialog._frame_provider = None
        dialog._preview_worker = None
        dialog._providers_pending_close = []
        dialog._preview_generation = 0
        dialog._settings = SceneCutSettings()
        dialog._cancel_active_preview_worker = lambda: None
        return dialog

    def test_wait_timeout_defers_provider_close(self, monkeypatch) -> None:
        import src.features.edit.ui.scene_cut_dialog as dialog_mod

        dialog = self._make_dialog()
        old_provider = _FakeProvider()
        dialog._frame_provider = old_provider
        dialog._preview_worker = _FakeWorker(wait_result=False)

        new_provider = _FakeProvider()
        monkeypatch.setattr(
            dialog_mod, "PreviewFrameProvider", lambda p, s: new_provider
        )

        dialog._init_frame_provider("whatever.mp4")

        # Old provider must NOT be closed on the UI thread while the worker
        # may still hold its decode lock — the close is deferred.
        assert old_provider.closed is False
        assert old_provider in dialog._providers_pending_close
        assert dialog._frame_provider is new_provider
        # Generation bumped so stale results are dropped.
        assert dialog._preview_generation == 1

    def test_wait_success_closes_provider_immediately(self, monkeypatch) -> None:
        import src.features.edit.ui.scene_cut_dialog as dialog_mod

        dialog = self._make_dialog()
        old_provider = _FakeProvider()
        dialog._frame_provider = old_provider
        dialog._preview_worker = _FakeWorker(wait_result=True)

        new_provider = _FakeProvider()
        monkeypatch.setattr(
            dialog_mod, "PreviewFrameProvider", lambda p, s: new_provider
        )

        dialog._init_frame_provider("whatever.mp4")

        assert old_provider.closed is True
        assert dialog._providers_pending_close == []
        assert dialog._frame_provider is new_provider

    def test_stale_generation_frames_are_dropped(self) -> None:
        from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

        dialog = SceneCutDialog.__new__(SceneCutDialog)
        dialog._preview_generation = 5
        dialog._current_index = -1
        dialog._playback_fps = 24.0
        dialog._transport = SimpleNamespace(current_frame=lambda: 10)
        dialog._preview = SimpleNamespace(
            set_frame_preview=lambda *a: pytest.fail("stale frame applied"),
            set_candidate=lambda *a, **k: pytest.fail("stale candidate applied"),
        )

        # Result decoded against generation 4 (previous source) — dropped.
        dialog._on_preview_frames_ready(-1, 10, None, None, None, generation=4)

        # Current generation still renders.
        dialog._preview = SimpleNamespace(
            set_frame_preview=lambda *a: None, set_candidate=lambda *a, **k: None
        )
        dialog._on_preview_frames_ready(-1, 10, None, None, None, generation=5)

    def test_worker_finished_drains_pending_providers(self) -> None:
        from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

        dialog = SceneCutDialog.__new__(SceneCutDialog)
        deferred = _FakeProvider()
        dialog._providers_pending_close = [deferred]
        dialog._preview_workers = []
        dialog._zombie_preview_workers = []
        dialog._preview_worker = None
        dialog._preview_pending_request = None
        dialog._frame_provider = None

        dialog._on_preview_worker_finished(SimpleNamespace(deleteLater=lambda: None))

        assert deferred.closed is True
        assert dialog._providers_pending_close == []

    def test_worker_finished_keeps_providers_while_workers_run(self) -> None:
        from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

        dialog = SceneCutDialog.__new__(SceneCutDialog)
        deferred = _FakeProvider()
        dialog._providers_pending_close = [deferred]
        dialog._preview_workers = [_FakeWorker(running=True)]
        dialog._zombie_preview_workers = []
        dialog._preview_worker = None
        dialog._preview_pending_request = None
        dialog._frame_provider = None

        dialog._on_preview_worker_finished(SimpleNamespace(deleteLater=lambda: None))

        # A still-running (zombie) worker may hold the deferred provider's
        # decode lock — closing must wait.
        assert deferred.closed is False
        assert dialog._providers_pending_close == [deferred]

    def test_set_source_resets_current_index(self, monkeypatch) -> None:
        from src.features.edit.core.scene_cut.models import SceneCutSettings
        from src.features.edit.ui.scene_cut_dialog import SceneCutDialog

        dialog = SceneCutDialog.__new__(SceneCutDialog)
        dialog._source_path = None
        dialog._asset_id = None
        dialog._proxy_path = None
        dialog._settings = SceneCutSettings()
        dialog._current_index = 3
        dialog._model = SimpleNamespace(set_current_index=lambda i: None)
        dialog._stop_playback = lambda: None
        dialog._init_frame_provider = lambda path: None
        dialog._sync_playback_source = lambda: None
        dialog._bottom_bar = SimpleNamespace(set_sensitivity=lambda v: None)
        dialog._load_initial_preview = lambda: None

        dialog.set_source("new.mp4", "asset-1")

        # A stale candidate index would break the initial-preview guard for
        # the new source (candidate_index == -1 vs old index).
        assert dialog._current_index == -1


# ══════════════════════════════════════════════════════════════════════════
# AUD-20605: Backspace is a reject alias (regression for the committed fix)
# ══════════════════════════════════════════════════════════════════════════


class TestAud20605BackspaceReject:
    def _key_event(self, key: Qt.Key) -> QKeyEvent:
        return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)

    def test_backspace_matches_reject_default(self) -> None:
        from src.features.edit.ui import scene_cut_shortcuts

        assert scene_cut_shortcuts.event_matches(
            self._key_event(Qt.Key.Key_Backspace),
            scene_cut_shortcuts.REJECT_CURRENT_ACTION_ID,
            "Delete",
        )

    def test_delete_still_matches_reject_default(self) -> None:
        from src.features.edit.ui import scene_cut_shortcuts

        assert scene_cut_shortcuts.event_matches(
            self._key_event(Qt.Key.Key_Delete),
            scene_cut_shortcuts.REJECT_CURRENT_ACTION_ID,
            "Delete",
        )

    def test_other_keys_do_not_match(self) -> None:
        from src.features.edit.ui import scene_cut_shortcuts

        assert not scene_cut_shortcuts.event_matches(
            self._key_event(Qt.Key.Key_A),
            scene_cut_shortcuts.REJECT_CURRENT_ACTION_ID,
            "Delete",
        )


# ══════════════════════════════════════════════════════════════════════════
# AUD-20702: graph widget _updating lock survives exceptions
# ══════════════════════════════════════════════════════════════════════════


class TestAud20702GraphWidgetLock:
    def _widget(self):
        from src.features.edit.ui.scene_cut_graph_widget import SceneCutGraphWidget

        return SceneCutGraphWidget()

    @staticmethod
    def _raise_runtime(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("underlying C/C++ object has been deleted")

    def test_set_threshold_releases_lock_on_exception(self) -> None:
        widget = self._widget()
        widget._graph_area.set_threshold = self._raise_runtime
        with pytest.raises(RuntimeError):
            widget.set_threshold(0.6)
        assert widget._updating is False

    def test_set_current_frame_releases_lock_on_exception(self) -> None:
        widget = self._widget()
        widget._graph_area.set_current_frame = self._raise_runtime
        with pytest.raises(RuntimeError):
            widget.set_current_frame(12)
        assert widget._updating is False

    def test_scrubber_set_frame_releases_lock_on_exception(self) -> None:
        widget = self._widget()
        scrubber = widget.get_scrubber()
        scrubber.setValue = self._raise_runtime
        with pytest.raises(RuntimeError):
            scrubber.set_frame(10)
        assert scrubber._updating is False

    def test_graph_click_forwarding_releases_lock_on_exception(self) -> None:
        widget = self._widget()
        widget._scrubber.set_frame = self._raise_runtime
        with pytest.raises(RuntimeError):
            widget._on_graph_frame_clicked(10)
        assert widget._updating is False

    def test_scrubber_change_forwarding_releases_lock_on_exception(self) -> None:
        widget = self._widget()
        widget._graph_area.set_current_frame = self._raise_runtime
        with pytest.raises(RuntimeError):
            widget._on_scrubber_frame_changed(10)
        assert widget._updating is False

    def test_normal_updates_still_work_and_unlock(self) -> None:
        widget = self._widget()
        widget.set_threshold(0.62)
        assert widget.get_threshold() == pytest.approx(0.62)
        assert widget._updating is False
        widget.set_current_frame(7)
        assert widget._updating is False


# ══════════════════════════════════════════════════════════════════════════
# AUD-20714: ThumbnailLoader pending-request cleanup
# ══════════════════════════════════════════════════════════════════════════


class TestAud20714ThumbnailLoader:
    def _loader(self):
        from src.features.edit.ui.scene_cut_preview_widget import (
            PreviewPanelType,
            ThumbnailLoader,
        )

        return ThumbnailLoader(), PreviewPanelType.BEFORE

    def test_sync_load_clears_pending_entry(self) -> None:
        loader, panel = self._loader()
        seen: List[int] = []
        loader.thumbnail_loaded.connect(lambda p, f, img: seen.append(f))

        loader.request_thumbnail(panel, 5, lambda frame: None)

        assert seen == [5]
        assert (panel, 5) not in loader._pending_requests
        assert loader._pending_requests == {}

    def test_failing_loader_still_clears_pending_entry(self) -> None:
        loader, panel = self._loader()

        def _boom(frame: int) -> None:
            raise RuntimeError("decode failed")

        with pytest.raises(RuntimeError):
            loader.request_thumbnail(panel, 6, _boom)

        # AUD-20714: the pending marker must not leak on failure either.
        assert loader._pending_requests == {}

    def test_request_without_loader_completes_immediately(self) -> None:
        loader, panel = self._loader()
        # The class has no async worker: registering without a loader_func
        # completes (and cleans up) synchronously like every other request.
        loader.request_thumbnail(panel, 7, None)
        assert loader._pending_requests == {}
        loader.cancel_request(panel, 8)
        loader.clear_pending()
        assert loader._pending_requests == {}


# ══════════════════════════════════════════════════════════════════════════
# AUD-20717: preview widget _updating lock survives exceptions
# ══════════════════════════════════════════════════════════════════════════


class TestAud20717PreviewWidgetLock:
    def _widget(self):
        from src.features.edit.ui.scene_cut_preview_widget import (
            SceneCutPreviewWidget,
        )

        return SceneCutPreviewWidget()

    @staticmethod
    def _raise_runtime(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("underlying C/C++ object has been deleted")

    def test_set_candidate_releases_lock_on_exception(self) -> None:
        widget = self._widget()
        widget._clear_all_panels = self._raise_runtime
        with pytest.raises(RuntimeError):
            widget.set_candidate(None)
        assert widget._updating is False

    def test_set_candidate_with_data_releases_lock_on_exception(self) -> None:
        from src.features.edit.core.scene_cut.models import SceneCutCandidate

        widget = self._widget()
        widget._before_panel.set_image_from_qimage = self._raise_runtime
        candidate = SceneCutCandidate(
            source_frame=10, pts=None, seconds=1.0, confidence=0.9
        )
        with pytest.raises(RuntimeError):
            widget.set_candidate(candidate)
        assert widget._updating is False

    def test_set_frame_preview_releases_lock_on_exception(self) -> None:
        widget = self._widget()
        widget._before_panel.set_image_from_qimage = self._raise_runtime
        with pytest.raises(RuntimeError):
            widget.set_frame_preview(5, 24.0, None, None, None)
        assert widget._updating is False

    def test_clear_releases_lock_on_exception(self) -> None:
        widget = self._widget()
        widget._clear_all_panels = self._raise_runtime
        with pytest.raises(RuntimeError):
            widget.clear()
        assert widget._updating is False

    def test_clear_normal_path_unlocks(self) -> None:
        widget = self._widget()
        widget.clear()
        assert widget._updating is False


# ══════════════════════════════════════════════════════════════════════════
# AUD-20803: batch operations hold the lock and defer decision_changed
# ══════════════════════════════════════════════════════════════════════════


def _pending_model():
    from src.features.edit.core.scene_cut.models import (
        SceneCutCandidate,
        SceneCutKind,
    )
    from src.features.edit.ui.scene_cut_result_model import SceneCutResultModel

    model = SceneCutResultModel()
    model.set_candidates(
        [
            SceneCutCandidate(source_frame=10, pts=None, seconds=0.4, confidence=0.9),
            SceneCutCandidate(
                source_frame=20,
                pts=None,
                seconds=0.8,
                confidence=0.9,
                kind=SceneCutKind.FLASH,
            ),
            SceneCutCandidate(source_frame=30, pts=None, seconds=1.2, confidence=0.4),
        ]
    )
    return model


class TestAud20803BatchLock:
    def test_batch_survives_handler_mutating_the_list(self) -> None:
        from src.features.edit.core.scene_cut.models import SceneCutCandidate
        from src.features.edit.ui.scene_cut_result_model import SceneCutResultModel

        model = SceneCutResultModel()
        model.set_candidates(
            [
                SceneCutCandidate(source_frame=10, pts=None, seconds=0.4, confidence=0.9),
                SceneCutCandidate(source_frame=20, pts=None, seconds=0.8, confidence=0.9),
                SceneCutCandidate(source_frame=30, pts=None, seconds=1.2, confidence=0.9),
            ]
        )
        removed: List[Any] = []

        def mutating_handler(index: int, decision: Any) -> None:
            # A decision_changed handler that mutates the candidate list.
            removed.append(model.remove_candidate(0))

        model.decision_changed.connect(mutating_handler)

        # AUD-20803: the loop holds the lock and defers every emission until
        # after enumeration, so a handler mutating the list can no longer
        # shift indices underneath the batch (the old per-item emission made
        # this loop skip candidates and under-count).
        count = model.accept_high_confidence()

        assert count == 3
        assert len(removed) == 3
        assert model.rowCount() == 0

    def test_batch_runs_with_lock_released_for_handlers(self) -> None:
        model = _pending_model()
        lock_during_notify: List[bool] = []

        def handler(index: int, decision: Any) -> None:
            lock_during_notify.append(model._updating)

        model.decision_changed.connect(handler)
        count = model.accept_high_confidence()

        assert count == 1
        # Handlers always observe the model unlocked (safe to call back into).
        assert lock_during_notify == [False]

    def test_accept_all_emits_deferred_signals_and_batch_completed(self) -> None:
        from src.features.edit.core.scene_cut.models import SceneCutDecision

        model = _pending_model()
        decisions: List[tuple] = []
        batches: List[tuple] = []
        model.decision_changed.connect(lambda i, d: decisions.append((i, d)))
        model.batch_operation_completed.connect(lambda op, n: batches.append((op, n)))

        count = model.accept_all()

        assert count == 3
        assert [d for _, d in decisions] == [SceneCutDecision.ACCEPTED] * 3
        assert [i for i, _ in decisions] == [0, 1, 2]
        assert batches == [("accept_all", 3)]
        assert all(c.is_accepted for c in model.candidates)

    def test_reset_all_restores_pending(self) -> None:
        model = _pending_model()
        model.accept_all()
        count = model.reset_all()
        assert count == 3
        assert all(c.is_pending for c in model.candidates)

    def test_reject_warnings_only_touches_warnings(self) -> None:
        model = _pending_model()
        count = model.reject_warnings()
        assert count == 2  # flash candidate + low-confidence candidate
        assert model.candidates[0].is_pending
        assert model.candidates[1].is_rejected
        assert model.candidates[2].is_rejected

    def test_single_set_decision_still_notifies(self) -> None:
        from src.features.edit.core.scene_cut.models import SceneCutDecision

        model = _pending_model()
        decisions: List[tuple] = []
        model.decision_changed.connect(lambda i, d: decisions.append((i, d)))
        assert model.set_decision(1, SceneCutDecision.ACCEPTED) is True
        assert decisions == [(1, SceneCutDecision.ACCEPTED)]


# ══════════════════════════════════════════════════════════════════════════
# AUD-20807: SceneCutResultModel.sort
# ══════════════════════════════════════════════════════════════════════════


class TestAud20807ModelSort:
    def _model(self):
        from src.features.edit.core.scene_cut.models import SceneCutCandidate
        from src.features.edit.ui.scene_cut_result_model import SceneCutResultModel

        model = SceneCutResultModel()
        model.set_candidates(
            [
                SceneCutCandidate(source_frame=300, pts=None, seconds=12.0, confidence=0.9),
                SceneCutCandidate(source_frame=100, pts=None, seconds=4.0, confidence=0.5),
                SceneCutCandidate(source_frame=200, pts=None, seconds=8.0, confidence=0.7),
            ]
        )
        return model

    def test_sort_by_frame_ascending_and_descending(self) -> None:
        from src.features.edit.ui.scene_cut_result_model import Column

        model = self._model()
        model.sort(Column.FRAME.value, Qt.SortOrder.AscendingOrder)
        assert [c.source_frame for c in model.candidates] == [100, 200, 300]
        model.sort(Column.FRAME.value, Qt.SortOrder.DescendingOrder)
        assert [c.source_frame for c in model.candidates] == [300, 200, 100]

    def test_sort_by_score_ascending(self) -> None:
        from src.features.edit.ui.scene_cut_result_model import Column

        model = self._model()
        model.sort(Column.SCORE.value, Qt.SortOrder.AscendingOrder)
        assert [c.confidence for c in model.candidates] == [0.5, 0.7, 0.9]

    def test_sort_emits_layout_signals(self) -> None:
        from src.features.edit.ui.scene_cut_result_model import Column

        model = self._model()
        about: List[bool] = []
        changed: List[bool] = []
        model.layoutAboutToBeChanged.connect(lambda: about.append(True))
        model.layoutChanged.connect(lambda: changed.append(True))

        model.sort(Column.FRAME.value, Qt.SortOrder.AscendingOrder)

        assert about == [True]
        assert changed == [True]

    def test_sort_remaps_current_index(self) -> None:
        from src.features.edit.ui.scene_cut_result_model import Column

        model = self._model()
        model.set_current_index(0)  # candidate with source_frame == 300
        model.sort(Column.FRAME.value, Qt.SortOrder.AscendingOrder)
        assert model.get_current_index() == 2
        assert model.candidates[2].source_frame == 300

    def test_sort_ignores_unsupported_columns(self) -> None:
        from src.features.edit.ui.scene_cut_result_model import Column

        model = self._model()
        model.sort(Column.DECISION.value, Qt.SortOrder.AscendingOrder)
        assert [c.source_frame for c in model.candidates] == [300, 100, 200]


# ══════════════════════════════════════════════════════════════════════════
# AUD-20815: overlay show/hide anti-recursion lock
# ══════════════════════════════════════════════════════════════════════════


class TestAud20815OverlayLock:
    def _overlay(self):
        from src.features.edit.ui.scene_cut_timeline_overlay import (
            SceneCutTimelineOverlay,
        )
        from src.features.edit.ui.timeline_layout import TimelineLayoutEngine

        return SceneCutTimelineOverlay(TimelineLayoutEngine())

    def test_visibility_update_runs_under_lock(self) -> None:
        overlay = self._overlay()
        observed: List[bool] = []
        original_update = overlay._update_visibility

        def _observing() -> None:
            observed.append(overlay._updating)
            original_update()

        overlay._update_visibility = _observing
        overlay.show_overlay()

        # AUD-20815: the state mutation window is guarded by the same
        # _updating lock as set_candidates/select_candidate.
        assert observed == [True]
        assert overlay._updating is False
        assert overlay._is_visible is True

    def test_reentrant_show_during_update_is_a_noop(self) -> None:
        overlay = self._overlay()
        emissions: List[bool] = []
        overlay.overlay_visibility_changed.connect(emissions.append)

        def _reentrant() -> None:
            # A listener wired back into the overlay during the guarded
            # window must not recurse (old code ran it fully and emitted
            # twice).
            overlay.show_overlay()

        overlay._update_visibility = _reentrant
        overlay.show_overlay()

        assert emissions == [True]
        assert overlay._is_visible is True

    def test_reentrant_hide_during_update_is_a_noop(self) -> None:
        overlay = self._overlay()
        overlay.show_overlay()
        emissions: List[bool] = []
        overlay.overlay_visibility_changed.connect(emissions.append)

        def _reentrant() -> None:
            overlay.hide_overlay()

        overlay._update_visibility = _reentrant
        overlay.hide_overlay()

        assert emissions == [False]
        assert overlay._is_visible is False

    def test_programmatic_show_hide_emits_once_each(self) -> None:
        overlay = self._overlay()
        emissions: List[bool] = []
        overlay.overlay_visibility_changed.connect(emissions.append)
        overlay.show_overlay()
        overlay.hide_overlay()
        overlay.toggle_overlay()
        assert emissions == [True, False, True]
        assert overlay._updating is False


# ══════════════════════════════════════════════════════════════════════════
# AUD-20904: SourceViewer cached uniforms + static unit quad
# ══════════════════════════════════════════════════════════════════════════


class TestAud20904SourceViewerRenderPath:
    def test_render_texture_has_no_per_frame_location_query_or_upload(self) -> None:
        node = _method_node("source_viewer.py", "SourceViewer", "_render_texture")
        calls = set(_call_attr_names(node))

        # Per-frame glGetUniformLocation / glBufferData are pure overhead.
        assert "glGetUniformLocation" not in calls
        assert "glBufferData" not in calls
        # Cached uniforms and the static VAO draw are used instead.
        assert {"glUniform1i", "glUniform4f", "glUniform1f", "glBindVertexArray",
                "glDrawArrays"} <= calls
        unparsed = ast.unparse(node)
        assert "self._u_texture_loc" in unparsed
        assert "self._u_display_rect_loc" in unparsed
        assert "self._u_flip_y_loc" in unparsed

    def test_initialize_gl_caches_uniform_locations(self) -> None:
        node = _method_node("source_viewer.py", "SourceViewer", "initializeGL")
        unparsed = ast.unparse(node)
        assert "glGetUniformLocation" in unparsed
        assert "u_display_rect" in unparsed
        assert "u_flip_y" in unparsed

    def test_quad_vbo_is_created_once_with_static_usage(self) -> None:
        node = _method_node("source_viewer.py", "SourceViewer", "_create_quad_vbo")
        unparsed = ast.unparse(node)
        assert "glBufferData" in unparsed
        assert "GL_STATIC_DRAW" in unparsed

    def test_render_texture_uniform_flip_matches_texture_source(self) -> None:
        node = _method_node("source_viewer.py", "SourceViewer", "_render_texture")
        unparsed = ast.unparse(node)
        # GPU (shared) textures need the y flip; CPU QImages do not.
        assert "1.0 if self._shared_tex_id is not None else 0.0" in unparsed


# ══════════════════════════════════════════════════════════════════════════
# AUD-21003: locked subtitle track rejects keyboard delete
# ══════════════════════════════════════════════════════════════════════════


class TestAud21003SubtitleKeyboardDelete:
    def _item(self, locked: bool):
        from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem

        return SubtitleClipItem(
            clip_id="clip-1",
            track_id="track-1",
            text="hello",
            start_frame=0,
            duration_frames=10,
            pixels_per_frame=5.0,
            track_height=40,
            is_track_locked=locked,
        )

    @staticmethod
    def _delete_event(key: Qt.Key) -> QKeyEvent:
        return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)

    def test_locked_track_ignores_delete(self) -> None:
        item = self._item(locked=True)
        deletions: List[tuple] = []
        item.subtitle_clip_delete.connect(lambda c, t: deletions.append((c, t)))

        item.keyPressEvent(self._delete_event(Qt.Key.Key_Delete))
        item.keyPressEvent(self._delete_event(Qt.Key.Key_Backspace))

        assert deletions == []

    def test_unlocked_track_emits_delete(self) -> None:
        item = self._item(locked=False)
        deletions: List[tuple] = []
        item.subtitle_clip_delete.connect(lambda c, t: deletions.append((c, t)))

        item.keyPressEvent(self._delete_event(Qt.Key.Key_Delete))

        assert deletions == [("clip-1", "track-1")]


# ══════════════════════════════════════════════════════════════════════════
# AUD-21110: disabled/locked clip pattern keeps 0.5 opacity for the rest
# ══════════════════════════════════════════════════════════════════════════


class TestAud21110DisabledPatternOpacity:
    def _pattern_if_nodes(self) -> List[ast.If]:
        node = _method_node("timeline_items.py", "ClipItem", "paint")
        matches: List[ast.If] = []
        for sub in ast.walk(node):
            if not isinstance(sub, ast.If):
                continue
            calls = _call_attr_names(sub)
            if "_draw_disabled_pattern" in calls:
                matches.append(sub)
        return matches

    def test_main_pattern_block_wrapped_in_save_restore(self) -> None:
        nodes = self._pattern_if_nodes()
        assert nodes, "disabled-pattern branch missing from ClipItem.paint"

        def body_attr_sequence(if_node: ast.If) -> List[str]:
            seq: List[str] = []
            for stmt in if_node.body:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                    assert isinstance(stmt.value.func, ast.Attribute)
                    seq.append(stmt.value.func.attr)
            return seq

        sequences = [body_attr_sequence(n) for n in nodes]
        # AUD-21110: the pattern is drawn fully opaque inside save()/restore()
        # so the 0.5 opacity survives for the waveform / label bar after it.
        assert ["save", "setOpacity", "_draw_disabled_pattern", "restore"] in sequences

    def test_waveform_drawn_after_pattern_without_bare_opacity_reset(self) -> None:
        node = _method_node("timeline_items.py", "ClipItem", "paint")
        unparsed = ast.unparse(node)
        pattern_pos = unparsed.rfind("_draw_disabled_pattern")
        assert pattern_pos != -1
        waveform_pos = unparsed.find("_draw_waveform", pattern_pos)
        assert waveform_pos != -1
        between = unparsed[pattern_pos:waveform_pos]
        # The pattern block restores the painter so the following waveform
        # inherits the 0.5 opacity — no second bare opacity reset in between.
        assert "painter.restore()" in between
        assert "painter.setOpacity(1.0)" not in between


# ══════════════════════════════════════════════════════════════════════════
# AUD-21203: resolve_timeline_hit checks the S/V splitter
# ══════════════════════════════════════════════════════════════════════════


def _engine_with_subtitle():
    from src.features.edit.ui.timeline_layout import (
        TimelineLayoutEngine,
        TimelineLayoutState,
        TrackInfo,
    )

    engine = TimelineLayoutEngine()
    tracks = [
        TrackInfo(track_id="s1", track_type="subtitle"),
        TrackInfo(track_id="v1", track_type="video"),
        TrackInfo(track_id="a1", track_type="audio"),
    ]
    engine.configure(tracks, TimelineLayoutState(), 1200.0, 600.0, 5.0)
    return engine


class TestAud21203ResolveHitSplitters:
    def test_subtitle_splitter_hit_resolves_to_splitter(self) -> None:
        engine = _engine_with_subtitle()
        sr = engine.subtitle_splitter_rect()
        assert sr.height() > 0

        hit = engine.resolve_timeline_hit(
            300.0, (sr.top() + sr.bottom()) / 2.0
        )
        assert hit.area == "splitter"

    def test_va_splitter_hit_still_resolves_to_splitter(self) -> None:
        engine = _engine_with_subtitle()
        sr = engine.splitter_rect()
        hit = engine.resolve_timeline_hit(300.0, (sr.top() + sr.bottom()) / 2.0)
        assert hit.area == "splitter"

    def test_ruler_zone_still_wins_above_track_area(self) -> None:
        from src.features.edit.ui.timeline_layout import TRACK_AREA_TOP

        engine = _engine_with_subtitle()
        hit = engine.resolve_timeline_hit(300.0, TRACK_AREA_TOP - 5)
        assert hit.area == "ruler"


# ══════════════════════════════════════════════════════════════════════════
# AUD-21204: pixels_per_frame <= 0 is rejected
# ══════════════════════════════════════════════════════════════════════════


class TestAud21204RejectInvalidZoom:
    def test_set_pixels_per_frame_rejects_zero_and_negative(self) -> None:
        from src.features.edit.ui.timeline_layout import TRACK_HEADER_WIDTH

        engine = _engine_with_subtitle()
        engine.set_pixels_per_frame(5.0)
        engine.set_pixels_per_frame(0.0)
        # 50 px past the header at 5 px/frame → frame 10 (no ZeroDivisionError)
        assert engine.x_to_frame(TRACK_HEADER_WIDTH + 50) == 10
        engine.set_pixels_per_frame(-2.0)
        assert engine.x_to_frame(TRACK_HEADER_WIDTH + 50) == 10

    def test_configure_rejects_zero_pixels_per_frame(self) -> None:
        from src.features.edit.ui.timeline_layout import (
            TimelineLayoutEngine,
            TimelineLayoutState,
            TrackInfo,
            TRACK_HEADER_WIDTH,
        )

        engine = TimelineLayoutEngine()
        tracks = [TrackInfo(track_id="v1", track_type="video")]
        engine.configure(tracks, TimelineLayoutState(), 800.0, 400.0, 4.0)
        engine.configure(tracks, TimelineLayoutState(), 800.0, 400.0, 0.0)
        assert engine.x_to_frame(TRACK_HEADER_WIDTH + 40) == 10

    def test_valid_update_still_applies(self) -> None:
        from src.features.edit.ui.timeline_layout import TRACK_HEADER_WIDTH

        engine = _engine_with_subtitle()
        engine.set_pixels_per_frame(10.0)
        assert engine.x_to_frame(TRACK_HEADER_WIDTH + 100) == 10


# ══════════════════════════════════════════════════════════════════════════
# AUD-21304: timeline panel _zoom_updating lock survives exceptions
# ══════════════════════════════════════════════════════════════════════════


class TestAud21304ZoomLock:
    @staticmethod
    def _raise_runtime(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("underlying C/C++ object has been deleted")

    def _panel(self, slider):
        from src.features.edit.ui.timeline_panel import TimelinePanel

        panel = TimelinePanel.__new__(TimelinePanel)
        panel._zoom_updating = False
        panel._applying_slider_zoom = False
        panel._defer_background_media_work_for_zoom = lambda: None
        panel.zoom_slider = slider
        panel._ppf_to_slider = lambda ppf: 7
        panel.timeline_view = SimpleNamespace(
            _pixels_per_frame=5.0, set_zoom=lambda z: None
        )
        return panel

    def test_set_zoom_level_releases_lock_on_exception(self) -> None:
        class _FailingSlider:
            def setValue(self, value: int) -> None:
                raise RuntimeError("slider gone")

        panel = self._panel(_FailingSlider())
        with pytest.raises(RuntimeError):
            panel.set_zoom_level(5.0)
        assert panel._zoom_updating is False

    def test_on_view_zoom_changed_releases_lock_on_exception(self) -> None:
        class _FailingSlider:
            def setValue(self, value: int) -> None:
                raise RuntimeError("slider gone")

        panel = self._panel(_FailingSlider())
        with pytest.raises(RuntimeError):
            panel._on_view_zoom_changed(5.0)
        assert panel._zoom_updating is False

    def test_normal_zoom_update_unlocks(self) -> None:
        seen: List[int] = []

        class _Slider:
            def setValue(self, value: int) -> None:
                seen.append(value)

        panel = self._panel(_Slider())
        panel.set_zoom_level(5.0)
        assert seen == [7]
        assert panel._zoom_updating is False


# ══════════════════════════════════════════════════════════════════════════
# AUD-21306: proxy HUD stats cached per asset set + TTL
# ══════════════════════════════════════════════════════════════════════════


class TestAud21306ProxyHudCache:
    def _panel(self, assets: dict):
        from src.features.edit.ui.timeline_panel import TimelinePanel

        panel = TimelinePanel.__new__(TimelinePanel)
        panel._proxy_hud_cache = None
        panel._project = SimpleNamespace(assets=assets)
        return panel

    def _install_fake_service(self, monkeypatch: pytest.MonkeyPatch, calls: dict):
        import src.shared.media.disk_proxy_service as proxy_mod

        class _FakeInfo:
            is_ready = True
            status = SimpleNamespace(value="ready")

        class _FakeService:
            def get_disk_proxy_info(self, path: str):
                calls["n"] += 1
                return _FakeInfo()

        monkeypatch.setattr(
            proxy_mod, "get_disk_proxy_service", lambda: _FakeService()
        )

    def test_repeated_hud_ticks_reuse_cached_stats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        self._install_fake_service(monkeypatch, calls)
        video = SimpleNamespace(media_type="video", path="/media/a.mp4")
        panel = self._panel({"k": video})

        stats1 = panel._collect_proxy_hud_stats()
        assert stats1 == {"ready": 1, "generating": 0, "missing": 0}
        assert calls["n"] == 1

        stats2 = panel._collect_proxy_hud_stats()
        assert stats2 == stats1
        # AUD-21306: the second 500ms tick must not re-run disk validation.
        assert calls["n"] == 1

    def test_non_video_assets_are_not_probed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        self._install_fake_service(monkeypatch, calls)
        panel = self._panel(
            {
                "v": SimpleNamespace(media_type="video", path="/a.mp4"),
                "a": SimpleNamespace(media_type="audio", path="/b.wav"),
            }
        )
        panel._collect_proxy_hud_stats()
        assert calls["n"] == 1

    def test_asset_set_change_invalidates_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        self._install_fake_service(monkeypatch, calls)
        video_a = SimpleNamespace(media_type="video", path="/a.mp4")
        panel = self._panel({"k": video_a})
        panel._collect_proxy_hud_stats()

        panel._project = SimpleNamespace(
            assets={
                "k": video_a,
                "j": SimpleNamespace(media_type="video", path="/b.mp4"),
            }
        )
        panel._collect_proxy_hud_stats()
        assert calls["n"] == 3

    def test_ttl_expiry_triggers_recompute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        self._install_fake_service(monkeypatch, calls)
        panel = self._panel({"k": SimpleNamespace(media_type="video", path="/a.mp4")})

        real_monotonic = time.monotonic
        panel._collect_proxy_hud_stats()
        assert calls["n"] == 1

        # Jump past the cache TTL.
        monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 100.0)
        panel._collect_proxy_hud_stats()
        assert calls["n"] == 2


# ══════════════════════════════════════════════════════════════════════════
# AUD-21408: locked-track trim feedback via status_message_requested
# ══════════════════════════════════════════════════════════════════════════


class TestAud21408LockedTrimFeedback:
    def _locked_trim_function(self) -> Optional[ast.FunctionDef]:
        source = (UI_DIR / "timeline_view.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            unparsed = ast.unparse(node)
            if "_is_track_locked(track_id)" in unparsed and (
                "status_message_requested.emit" in unparsed
            ):
                return node  # type: ignore[return-value]
        return None

    def test_locked_trim_path_emits_status_message(self) -> None:
        node = self._locked_trim_function()
        assert node is not None, (
            "no function combines the locked-track guard with "
            "status_message_requested.emit"
        )
        unparsed = ast.unparse(node)
        # AUD-21408: the dead getattr(self, 'status_bar', None) path is gone.
        assert "status_bar" not in unparsed
        assert "I18N.tr(" in unparsed

    def test_no_status_bar_dead_path_left_in_timeline_view(self) -> None:
        source = (UI_DIR / "timeline_view.py").read_text(encoding="utf-8")
        assert "getattr(self, 'status_bar', None)" not in source


# ══════════════════════════════════════════════════════════════════════════
# AUD-21410: layout hoists all_ordered_tracks / coalesces scroll relayout
# ══════════════════════════════════════════════════════════════════════════


class TestAud21410LayoutHotPath:
    def test_layout_track_items_resolves_track_order_once(self) -> None:
        node = _method_node("timeline_view.py", "TimelineView", "_layout_track_items")
        calls = _call_attr_names(node)

        # One hoisted call + the track-count call — not one per track.
        assert calls.count("all_ordered_tracks") == 2
        unparsed = ast.unparse(node)
        assert unparsed.count("ordered_index.get(track_id, 0)") == 2

    def test_scrollbar_handlers_use_coalesced_relayout(self) -> None:
        for method in (
            "_on_video_scrollbar_changed",
            "_on_audio_scrollbar_changed",
            "_on_subtitle_scrollbar_changed",
        ):
            node = _method_node("timeline_view.py", "TimelineView", method)
            unparsed = ast.unparse(node)
            assert "_schedule_scroll_layout_update()" in unparsed
            assert "_layout_track_items()" not in unparsed

    @staticmethod
    def _make_mini_view():
        """TimelineView instance that skips the heavy __init__."""
        from src.features.edit.ui.timeline_view import TimelineView

        class _MiniView(TimelineView):
            def __init__(self) -> None:
                # Real QGraphicsView init only (QObject must exist for the
                # QTimer parent); TimelineView.__init__ stays unexecuted.
                QGraphicsView.__init__(self)

        return _MiniView()

    def test_scroll_ticks_coalesce_into_one_layout(self, qapp) -> None:
        from PyQt6.QtTest import QTest

        mini = self._make_mini_view()
        mini._syncing_split_scrollbars = False
        mini._layout_state = SimpleNamespace(video_scroll_offset=0)
        mini._timeline = SimpleNamespace()  # non-None → layout proceeds
        layout_calls = {"n": 0}

        def _counting_layout() -> None:
            layout_calls["n"] += 1

        mini._layout_track_items = _counting_layout
        mini._scroll_layout_timer = QTimer(mini)
        mini._scroll_layout_timer.setSingleShot(True)
        mini._scroll_layout_timer.setInterval(16)
        mini._scroll_layout_timer.timeout.connect(mini._apply_scroll_layout_update)

        mini._on_video_scrollbar_changed(5)
        mini._on_video_scrollbar_changed(9)

        # No synchronous layout per tick...
        assert layout_calls["n"] == 0
        assert mini._layout_state.video_scroll_offset == 9
        # ...one coalesced pass per display frame instead.
        QTest.qWait(120)
        assert layout_calls["n"] == 1

    def test_syncing_guard_still_skips(self, qapp) -> None:
        mini = self._make_mini_view()
        mini._syncing_split_scrollbars = True
        mini._layout_state = SimpleNamespace(video_scroll_offset=0)
        mini._timeline = None
        layout_calls = {"n": 0}
        mini._layout_track_items = lambda: layout_calls.__setitem__(
            "n", layout_calls["n"] + 1
        )
        mini._scroll_layout_timer = QTimer(mini)
        mini._scroll_layout_timer.setSingleShot(True)
        mini._scroll_layout_timer.setInterval(16)

        mini._on_video_scrollbar_changed(5)

        assert layout_calls["n"] == 0
        assert not mini._scroll_layout_timer.isActive()


# ══════════════════════════════════════════════════════════════════════════
# AUD-21412: external file drop probes in a bounded worker pool
# ══════════════════════════════════════════════════════════════════════════


class TestAud21412DropProbePool:
    def _drop_handler_node(self) -> ast.FunctionDef:
        return _method_node(
            "timeline_view.py", "TimelineView", "_handle_external_file_drop"
        )

    def test_drop_handler_probes_off_thread(self) -> None:
        node = self._drop_handler_node()
        unparsed = ast.unparse(node)

        assert "ThreadPoolExecutor" in unparsed
        assert "drop-probe" in unparsed
        # The synchronous fallback keeps the old per-path error behaviour.
        assert "probe_media_metadata" in unparsed
        assert "get_asset_by_path" in unparsed

    def test_pool_is_bounded(self) -> None:
        node = self._drop_handler_node()
        unparsed = ast.unparse(node)
        assert "max_workers=min(4, len(new_paths))" in unparsed
