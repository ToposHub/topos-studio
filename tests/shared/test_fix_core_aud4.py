"""Regression tests for batch A critical fixes (w4-g4).

Covers:
  - AUD-31200: src.main must import QTimer (module-failure notice path).
  - AUD-31500: AI plugin worker cancellation is cooperative — no
    QThread.terminate(), cancel checkpoints at tile boundaries, stale
    results suppressed via the dedicated ``cancelled`` signal.
  - AUD-32500: to_ocio_name never silently falls back to "lin" for
    unmapped (primaries, transfer) combos — explicit fallback table +
    once-per-combination warning.
  - AUD-32601: ConfigManager.get supports ``get(key, default)`` with
    dict.get semantics.
"""
from __future__ import annotations

import ast
import logging
import time
from pathlib import Path

import numpy as np
import pytest

from PyQt6.QtCore import QObject, QThread, pyqtSignal, QTimer as QtQTimer


# ---------------------------------------------------------------------------
# AUD-31200: QTimer import in src/main.py
# ---------------------------------------------------------------------------


class TestMainQTimerImport:
    def test_main_module_exposes_qtimer(self, monkeypatch, tmp_path):
        # Keep the import side effects away from the real user config dir.
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
        import src.main as main_mod

        assert hasattr(main_mod, "QTimer"), (
            "src.main must expose QTimer: the module-failure status-bar "
            "notice (module_failures path) calls QTimer.singleShot after "
            "window.show(); a missing import makes startup FATAL (AUD-31200)"
        )
        assert main_mod.QTimer is QtQTimer

    def test_qtimer_call_site_resolves(self):
        """AST check: the QTimer.singleShot call site has a matching import."""
        import src.main as main_mod

        source = Path(main_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # An ImportFrom PyQt6.QtCore binding the name QTimer exists...
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "PyQt6.QtCore"
            and any(alias.name == "QTimer" for alias in node.names)
            for node in ast.walk(tree)
        )
        assert imported, "no `from PyQt6.QtCore import ... QTimer` in src/main.py"
        # ...and the module-failure notice actually calls QTimer.singleShot.
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "singleShot"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "QTimer"
        ]
        assert calls, "QTimer.singleShot call site disappeared from src/main.py"


# ---------------------------------------------------------------------------
# AUD-32601: ConfigManager.get(key, default) dict semantics
# ---------------------------------------------------------------------------


class TestConfigManagerGetDefault:
    def _manager(self, tmp_path):
        from src.shared.config.config_manager import ConfigManager

        return ConfigManager(config_path=str(tmp_path / "config.json"))

    def test_missing_key_without_default_returns_none(self, tmp_path):
        assert self._manager(tmp_path).get("no.such.key.xyz") is None

    def test_missing_key_with_default_returns_default(self, tmp_path):
        cm = self._manager(tmp_path)
        assert cm.get("no.such.key.xyz", "fallback") == "fallback"
        assert cm.get("no.such.key.xyz", 100) == 100
        assert cm.get("no.such.key.xyz", None) is None

    def test_existing_key_ignores_default(self, tmp_path):
        cm = self._manager(tmp_path)
        cm.set("test.custom.key", "value", auto_save=False)
        assert cm.get("test.custom.key") == "value"
        assert cm.get("test.custom.key", "other") == "value"

    def test_real_call_site_patterns_no_longer_raise(self, tmp_path):
        """The exact two-arg patterns from the audited call sites."""
        cm = self._manager(tmp_path)
        # page_controller HDR preview prefs
        assert int(cm.get("color.preview.hdr_tonemap", 1) or 0) == 1
        assert float(cm.get("color.preview.hdr_target_peak", 100) or 100) == 100.0
        # color_transform_provider engine selection
        assert (cm.get("composite.color_engine", "ocio") or "ocio") == "ocio"
        assert (cm.get("ocio_config_path", "") or "") == ""
        # color_manager default color space
        assert cm.get("default_color_space", "sRGB") == "sRGB"
        # detachable_panel_host export mode
        assert cm.get("dock_video_batch_export_mode", "embedded") == "embedded"

    def test_descriptor_from_config_with_real_manager(self, tmp_path):
        """descriptor_from_config uses .get(key, default); previously the
        TypeError was swallowed and prefs silently fell back to smart default."""
        from src.shared.config.config_manager import ConfigManager
        from src.shared.color.color_space_descriptor import (
            descriptor_from_config, DISPLAY_P3_SRGB,
        )

        cm = ConfigManager(config_path=str(tmp_path / "config.json"))
        cm.set("color.preview.image_primaries", "smpte432", auto_save=False)
        cm.set("color.preview.image_transfer", "iec61966_2_1", auto_save=False)
        cm.set("color.preview.image_range", "full", auto_save=False)
        d = descriptor_from_config(cm, "preview", "image", is_video=False)
        assert d == DISPLAY_P3_SRGB


# ---------------------------------------------------------------------------
# AUD-32500: to_ocio_name explicit fallback + warn-once
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_ocio_warned_combos():
    from src.shared.color import color_space_descriptor as csd

    csd._OCIO_WARNED_COMBOS.clear()
    yield
    csd._OCIO_WARNED_COMBOS.clear()


CSD_LOGGER = "src.shared.color.color_space_descriptor"


class TestToOcioNameFallback:
    def test_exact_mappings_unchanged(self):
        from src.shared.color.color_space_descriptor import (
            LINEAR_SCENE, SRGB_IMAGE, REC709_VIDEO, HDR10_VIDEO, HLG_VIDEO,
            DISPLAY_P3_SRGB, ACESCG_SCENE,
        )
        from src.shared.color.types import ColorPrimaries, ColorTransfer
        from src.shared.color.color_space_descriptor import ColorSpaceDescriptor

        assert LINEAR_SCENE.to_ocio_name() == "lin"
        assert ACESCG_SCENE.to_ocio_name() == "acescg"
        assert SRGB_IMAGE.to_ocio_name() == "srgb"
        assert REC709_VIDEO.to_ocio_name() == "rec709"
        assert HDR10_VIDEO.to_ocio_name() == "bt2020_pq"
        assert HLG_VIDEO.to_ocio_name() == "bt2020_hlg"
        assert DISPLAY_P3_SRGB.to_ocio_name() == "display_p3"
        dci_pq = ColorSpaceDescriptor(
            primaries=ColorPrimaries.P3_DCI, transfer=ColorTransfer.PQ)
        assert dci_pq.to_ocio_name() == "p3_pq"

    def test_ui_selectable_combos_never_map_to_lin(self):
        """Every primaries×transfer combo offered by the preference UI must
        resolve to a real OCIO space (or a warned fallback) — never silently
        to linear, which makes the OCIO provider skip the ODT encode."""
        from src.shared.color.color_space_descriptor import (
            ColorSpaceDescriptor, available_primaries, available_transfers,
        )
        from src.shared.color.types import ColorTransfer

        for prim in available_primaries():
            for tf in available_transfers():
                d = ColorSpaceDescriptor(primaries=prim, transfer=tf)
                name = d.to_ocio_name()
                if tf != ColorTransfer.LINEAR:
                    assert name != "lin", (
                        f"combo {prim.value}/{tf.value} silently maps to lin")
                assert name in {
                    "lin", "acescg", "srgb", "rec709",
                    "bt2020_pq", "bt2020_hlg", "p3_pq", "display_p3",
                }

    def test_fallback_table_values(self):
        from src.shared.color.color_space_descriptor import ColorSpaceDescriptor
        from src.shared.color.types import ColorPrimaries, ColorTransfer

        cases = [
            (ColorPrimaries.BT2020, ColorTransfer.SRGB, "srgb"),
            (ColorPrimaries.BT2020, ColorTransfer.BT709, "rec709"),
            (ColorPrimaries.BT709, ColorTransfer.PQ, "bt2020_pq"),
            (ColorPrimaries.BT709, ColorTransfer.HLG, "bt2020_hlg"),
            (ColorPrimaries.P3_DISPLAY, ColorTransfer.BT709, "display_p3"),
            (ColorPrimaries.P3_DISPLAY, ColorTransfer.PQ, "p3_pq"),
            (ColorPrimaries.P3_DCI, ColorTransfer.SRGB, "display_p3"),
        ]
        for prim, tf, expected in cases:
            d = ColorSpaceDescriptor(primaries=prim, transfer=tf)
            assert d.to_ocio_name() == expected, f"{prim.value}/{tf.value}"

    def test_unmapped_combo_warns_once_with_details(self, caplog):
        from src.shared.color.color_space_descriptor import ColorSpaceDescriptor
        from src.shared.color.types import ColorPrimaries, ColorTransfer

        d = ColorSpaceDescriptor(
            primaries=ColorPrimaries.BT2020, transfer=ColorTransfer.SRGB)
        with caplog.at_level(logging.WARNING, logger=CSD_LOGGER):
            assert d.to_ocio_name() == "srgb"
            assert d.to_ocio_name() == "srgb"  # second call: no duplicate warn
        warnings = [r for r in caplog.records
                    if r.name == CSD_LOGGER and r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "bt2020" in warnings[0].getMessage()
        assert "iec61966_2_1" in warnings[0].getMessage()
        assert "srgb" in warnings[0].getMessage()

    def test_nonlinear_transfer_never_silent_lin_for_unknown_combo(self, caplog):
        """Combos outside both tables (e.g. TRAW log) still get a warned,
        transfer-preserving conservative space — not a silent 'lin'."""
        from src.shared.color.color_space_descriptor import ColorSpaceDescriptor
        from src.shared.color.types import ColorPrimaries, ColorTransfer

        d = ColorSpaceDescriptor(
            primaries=ColorPrimaries.BT709, transfer=ColorTransfer.TRAW_LOG0)
        with caplog.at_level(logging.WARNING, logger=CSD_LOGGER):
            name = d.to_ocio_name()
        assert name != "lin"
        assert name in ("srgb", "rec709")
        assert any(r.name == CSD_LOGGER and r.levelno == logging.WARNING
                   for r in caplog.records)

    def test_linear_non_bt709_primaries_warn_about_gamut_loss(self, caplog):
        """LINEAR + BT.2020 still maps to 'lin' (transfer correct, gamut
        matrix dropped) but now warns instead of folding silently."""
        from src.shared.color.color_space_descriptor import ColorSpaceDescriptor
        from src.shared.color.types import ColorPrimaries, ColorTransfer

        d = ColorSpaceDescriptor(
            primaries=ColorPrimaries.BT2020, transfer=ColorTransfer.LINEAR)
        with caplog.at_level(logging.WARNING, logger=CSD_LOGGER):
            assert d.to_ocio_name() == "lin"
        assert any(r.name == CSD_LOGGER and r.levelno == logging.WARNING
                   for r in caplog.records)

    def test_canonical_linear_scene_stays_silent(self, caplog):
        from src.shared.color.color_space_descriptor import LINEAR_SCENE

        with caplog.at_level(logging.WARNING, logger=CSD_LOGGER):
            assert LINEAR_SCENE.to_ocio_name() == "lin"
        assert not [r for r in caplog.records
                    if r.name == CSD_LOGGER and r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# AUD-31500: cooperative cancellation in ai_plugin_engine
# ---------------------------------------------------------------------------


@pytest.fixture()
def ai_plugin_engine_mod():
    import src.shared.ai_models.ai_plugin_engine as mod

    return mod


class TestNoTerminateCalls:
    def test_ai_plugin_engine_has_no_terminate_calls(self, ai_plugin_engine_mod):
        """Source-level guarantee: no .terminate() call sites anywhere in
        the module (terminate on a thread inside ONNX native inference can
        corrupt runtime state — AUD-31500)."""
        source = Path(ai_plugin_engine_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        terminate_calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "terminate"
        ]
        assert terminate_calls == []

    def test_worker_has_cancel_api(self, ai_plugin_engine_mod):
        w = ai_plugin_engine_mod.AIPluginWorker(
            "layer", "inst", np.zeros((2, 2, 3), np.float32), {}, {})
        assert hasattr(w, "request_cancel")
        assert not w._is_cancelled()
        w.request_cancel()
        assert w._is_cancelled()
        with pytest.raises(ai_plugin_engine_mod._InferenceCancelled):
            w._check_cancelled()


class _FakeInput:
    name = "input"


class _FakeSession:
    """Records session.run calls; optionally cancels the worker each call."""

    def __init__(self, worker=None, out_hw=(4, 4)):
        self.calls = 0
        self._worker = worker
        self._out_hw = out_hw

    def get_inputs(self):
        return [_FakeInput()]

    def run(self, *_args, **_kwargs):
        self.calls += 1
        if self._worker is not None:
            self._worker.request_cancel()
        h, w = self._out_hw
        # ONNX session.run returns a list of outputs (NCHW here).
        return [np.zeros((1, 3, h, w), dtype=np.float32)]


class TestWorkerCancelCheckpoints:
    def _worker(self, mod):
        return mod.AIPluginWorker(
            "layer", "inst", np.zeros((6, 8, 3), np.float32), {}, {})

    def test_tiled_loop_checks_cancel_each_tile(self, ai_plugin_engine_mod):
        """session.run cancels after the first tile → the second tile's
        checkpoint unwinds without another inference call."""
        mod = ai_plugin_engine_mod
        w = self._worker(mod)
        session = _FakeSession(worker=w)
        with pytest.raises(mod._InferenceCancelled):
            w._process_tiled(np.zeros((6, 8, 3), np.float32), session, 4, 0)
        assert session.calls == 1

    def test_upscale_loop_checks_cancel_each_tile(self, ai_plugin_engine_mod):
        mod = ai_plugin_engine_mod
        w = self._worker(mod)
        # Upscale models emit tiles at tile_size * scale (4 * 2.0 = 8).
        session = _FakeSession(worker=w, out_hw=(8, 8))
        with pytest.raises(mod._InferenceCancelled):
            w._process_upscale(
                np.zeros((6, 8, 3), np.float32), session, 4, 0, 2.0)
        assert session.calls == 1

    def test_precancelled_worker_runs_no_inference(self, ai_plugin_engine_mod):
        mod = ai_plugin_engine_mod
        w = self._worker(mod)
        w.request_cancel()
        session = _FakeSession()
        with pytest.raises(mod._InferenceCancelled):
            w._process_tiled(np.zeros((4, 4, 3), np.float32), session, 4, 0)
        assert session.calls == 0


class TestWorkerRunSignalRouting:
    """run() must route cancellation to the dedicated cancelled signal and
    suppress stale finished/error emissions."""

    def _spies(self, w):
        events = {"finished": [], "error": [], "cancelled": []}
        w.finished.connect(
            lambda *a: events["finished"].append(a))
        w.error.connect(lambda *a: events["error"].append(a))
        w.cancelled.connect(lambda *a: events["cancelled"].append(a))
        return events

    def test_cancelled_run_emits_cancelled_only(self, ai_plugin_engine_mod,
                                                monkeypatch):
        mod = ai_plugin_engine_mod
        w = mod.AIPluginWorker(
            "layer", "inst", np.zeros((2, 2, 3), np.float32), {}, {})
        events = self._spies(w)

        def _raise():
            raise mod._InferenceCancelled()

        monkeypatch.setattr(w, "_process", _raise)
        w.run()  # call run() synchronously in the test thread
        assert events["cancelled"] == [("layer", "inst")]
        assert events["finished"] == []
        assert events["error"] == []
        assert w.image is None, "cancelled worker should release its input"

    def test_late_cancel_suppresses_finished(self, ai_plugin_engine_mod,
                                             monkeypatch):
        mod = ai_plugin_engine_mod
        w = mod.AIPluginWorker(
            "layer", "inst", np.zeros((2, 2, 3), np.float32), {}, {})
        events = self._spies(w)

        def _process():
            # Simulate a cancel arriving while inference is finishing.
            w.request_cancel()
            return np.zeros((2, 2, 3), np.float32)

        monkeypatch.setattr(w, "_process", _process)
        w.run()
        assert events["cancelled"] == [("layer", "inst")]
        assert events["finished"] == []
        assert events["error"] == []

    def test_error_run_emits_error_only(self, ai_plugin_engine_mod, monkeypatch):
        mod = ai_plugin_engine_mod
        w = mod.AIPluginWorker(
            "layer", "inst", np.zeros((2, 2, 3), np.float32), {}, {})
        events = self._spies(w)

        def _boom():
            raise RuntimeError("model exploded")

        monkeypatch.setattr(w, "_process", _boom)
        w.run()
        assert len(events["error"]) == 1
        assert events["error"][0][:2] == ("layer", "inst")
        assert "model exploded" in events["error"][0][2]
        assert events["finished"] == []
        assert events["cancelled"] == []

    def test_successful_run_emits_finished(self, ai_plugin_engine_mod,
                                           monkeypatch):
        mod = ai_plugin_engine_mod
        w = mod.AIPluginWorker(
            "layer", "inst", np.zeros((2, 2, 3), np.float32), {}, {})
        events = self._spies(w)
        out = np.full((2, 2, 3), 0.5, np.float32)
        monkeypatch.setattr(w, "_process", lambda: out)
        w.run()
        assert len(events["finished"]) == 1
        assert events["finished"][0][2] is out
        assert events["cancelled"] == []


class _ScriptedWorker(QObject):
    """Non-thread stub with the engine-facing worker API (no terminate)."""

    finished = pyqtSignal(str, str, np.ndarray)
    progress = pyqtSignal(str, str, int)
    error = pyqtSignal(str, str, str)
    cancelled = pyqtSignal(str, str)

    def __init__(self, layer_id, instance_id, image, plugin_data, plugin_params):
        super().__init__()
        self.layer_id = layer_id
        self.instance_id = instance_id
        self.cancel_requested = False
        self.running = False
        self.started = 0

    def isRunning(self) -> bool:
        return self.running

    def isFinished(self) -> bool:
        return not self.running

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def start(self) -> None:
        self.started += 1


class TestEngineCooperativeReentry:
    def test_reentry_requests_cancel_and_keeps_reference(
            self, ai_plugin_engine_mod, monkeypatch):
        mod = ai_plugin_engine_mod
        monkeypatch.setattr(mod, "AIPluginWorker", _ScriptedWorker)
        engine = mod.AIPluginEngine()

        img = np.zeros((2, 2, 3), np.float32)
        engine.process_plugin("L", "i1", img, {}, {})
        w1 = engine._workers["i1"]
        w1.running = True  # simulate mid-inference

        engine.process_plugin("L", "i1", img, {}, {})
        w2 = engine._workers["i1"]

        assert w1.cancel_requested, "old worker must be cancelled cooperatively"
        assert engine._workers["i1"] is w2
        assert w2.started == 1
        assert w1 in engine._retiring_workers, (
            "reference to the possibly-still-running worker must be kept "
            "(zombie mode) so the QThread is never destroyed mid-run")
        # A stale worker finishing after replacement must not pop w2's entry.
        w1.running = False
        engine._release_worker("i1", w1)
        assert engine._workers.get("i1") is w2

    def test_cancel_requests_cooperative_cancel_and_cleans_up(
            self, ai_plugin_engine_mod, monkeypatch):
        mod = ai_plugin_engine_mod
        monkeypatch.setattr(mod, "AIPluginWorker", _ScriptedWorker)
        engine = mod.AIPluginEngine()

        engine.process_plugin(
            "L", "i1", np.zeros((2, 2, 3), np.float32), {}, {})
        w = engine._workers["i1"]
        w.running = True

        engine.cancel("i1")
        assert w.cancel_requested
        assert engine._workers.get("i1") is w  # cleaned up by cancelled signal

        # Simulate the cancelled-signal cleanup once the thread exited.
        w.running = False
        engine._release_worker("i1", w)
        assert "i1" not in engine._workers
        engine._sweep_retiring_workers()
        assert w not in engine._retiring_workers


class _CancelLoopWorker(QThread):
    """Real QThread that loops until cooperatively cancelled."""

    finished = pyqtSignal(str, str, np.ndarray)  # shadows QThread.finished
    progress = pyqtSignal(str, str, int)
    error = pyqtSignal(str, str, str)
    cancelled = pyqtSignal(str, str)

    def __init__(self, layer_id, instance_id, image, plugin_data, plugin_params):
        super().__init__()
        self.layer_id = layer_id
        self.instance_id = instance_id
        self._abort = False

    def request_cancel(self) -> None:
        self._abort = True

    def run(self):
        while not self._abort:
            QThread.msleep(5)
        self.cancelled.emit(self.layer_id, self.instance_id)


class TestEngineCancelEndToEnd:
    def test_cancel_returns_immediately_and_cleans_bookkeeping(
            self, qapp, ai_plugin_engine_mod, monkeypatch):
        mod = ai_plugin_engine_mod
        monkeypatch.setattr(mod, "AIPluginWorker", _CancelLoopWorker)
        engine = mod.AIPluginEngine()

        engine.process_plugin(
            "L", "i1", np.zeros((2, 2, 3), np.float32), {}, {})
        worker = engine._workers["i1"]

        deadline = time.time() + 5.0
        while not worker.isRunning() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert worker.isRunning()

        t0 = time.perf_counter()
        engine.cancel("i1")  # previously terminate() + wait() → UI freeze
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"cancel() blocked for {elapsed:.2f}s"

        deadline = time.time() + 5.0
        while "i1" in engine._workers and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert "i1" not in engine._workers, (
            "cancelled worker did not clean up its bookkeeping")
        assert worker.isFinished()

        engine._sweep_retiring_workers()
        assert worker not in engine._retiring_workers
