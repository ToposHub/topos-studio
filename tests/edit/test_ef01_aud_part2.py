"""Batch EF-01 part 2 — Suggestion-level fixes in ``src/features/edit/core``.

Covers AUD-16505, AUD-16506, AUD-16715, AUD-16716, AUD-16718, AUD-16801,
AUD-16805, AUD-16806, AUD-16807, AUD-17004, AUD-17301, AUD-17305, AUD-17404,
AUD-17405, AUD-17407, AUD-17408, AUD-17413, AUD-17414, AUD-17418, AUD-17420,
AUD-17423, AUD-17505, AUD-17606, AUD-17812.

Rules honoured here:
- No modal dialogs (nothing calls QMessageBox).
- No real GL context (GpuFramePool is exercised with a fake upload_fn).
- Each fix has a positive case; a deterministic revert-detection assertion
  where a behavioural one is impractical. AUD-16801 and AUD-17407 double as
  the two rollback-sensitivity probes (reverting them makes tests fail).
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, List

import pytest

from PyQt6.QtCore import QObject

from src.features.edit.core.realtime_playback.types import (
    PlaybackMode,
    SourceKind,
)
from src.features.edit.core.realtime_playback.types import (
    FrameHandle,
    FrameIdentity,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _run_aligned(num_threads: int, fn) -> List[Any]:
    """Run fn(thread_index) on num_threads aligned threads; return results."""
    barrier = threading.Barrier(num_threads, timeout=10.0)
    results: List[Any] = [None] * num_threads

    def _worker(idx: int) -> None:
        barrier.wait()
        results[idx] = fn(idx)

    threads = [
        threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(num_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)
    return results


def _make_frame(frame_index: int = 0) -> FrameHandle:
    return FrameHandle(
        identity=FrameIdentity(
            timeline_id="tl",
            clip_id="clip1",
            asset_id="a1",
            source_path="/tmp/x.mp4",
            source_frame=frame_index,
            source_kind=SourceKind.MASTER,
        ),
        frame_index=frame_index,
        width=4,
        height=4,
        pixel_format="rgb24",
        bit_depth=8,
        color_space=None,
        pts_seconds=0.0,
        source_kind=SourceKind.MASTER,
        payload=None,
        payload_type="cpu_ndarray",
    )


# ---------------------------------------------------------------------------
# AUD-16505: playback prefetch must not swallow exceptions silently
# ---------------------------------------------------------------------------

class TestAud16505PrefetchErrorLogged:
    def test_prefetch_exception_is_logged_not_swallowed(self, caplog) -> None:
        from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

        mgr = EditGpuDecodeManager()
        source = SimpleNamespace(prefetch_sequential_frame=lambda i: (_ for _ in ()).throw(
            RuntimeError("vt decode exploded")))
        frame = SimpleNamespace(frame_index=41)

        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.edit_gpu_decode_manager"):
            mgr._kick_playback_prefetch(source, ("k1",), 41, (frame, "vt_pixel_buffer"))
            # Wait for the background prefetch thread to finish.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with mgr._playback_prefetch_lock:
                    threads = list(mgr._playback_prefetch_threads)
                if not threads and ("k1",) not in mgr._playback_prefetch_pending:
                    break
                for t in threads:
                    t.join(timeout=0.1)
            else:
                pytest.fail("prefetch thread did not terminate")

        warnings = [r for r in caplog.records if "playback prefetch" in r.getMessage()]
        assert warnings, "AUD-16505: prefetch failure must leave a logger trace"

    def test_prefetch_still_dedups_and_releases(self) -> None:
        from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

        mgr = EditGpuDecodeManager()
        calls = []
        source = SimpleNamespace(
            prefetch_sequential_frame=lambda i: calls.append(i) or False,
        )
        frame = SimpleNamespace(frame_index=7)
        mgr._kick_playback_prefetch(source, ("k2",), 7, (frame, "vt_pixel_buffer"))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with mgr._playback_prefetch_lock:
                threads = list(mgr._playback_prefetch_threads)
            if not threads and ("k2",) not in mgr._playback_prefetch_pending:
                break
            for t in threads:
                t.join(timeout=0.1)
        assert calls == [8], "prefetch should have attempted frame N+1"
        assert ("k2",) not in mgr._playback_prefetch_pending
        assert mgr._source_refs.get(("k2",), 0) == 0, "lease must be released"


# ---------------------------------------------------------------------------
# AUD-16506: preposition reservation/event leak when thread start fails
# ---------------------------------------------------------------------------

class _StartFailsThread:
    """Thread stand-in whose start() always fails (system thread exhaustion)."""

    def __init__(self, target=None, daemon=None, name=None) -> None:
        self.target = target
        self.daemon = daemon
        self.name = name

    def start(self) -> None:
        raise RuntimeError("cannot start new thread")


class TestAud16506PrepositionStartFailureCleanup:
    def _prepare_manager(self, monkeypatch):
        from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

        mgr = EditGpuDecodeManager()
        spec = SimpleNamespace(
            original_path="/x.mp4",
            decode_path="/x.mp4",
            target_size=None,
            source_kind="master",
            vt_policy="auto",
            no_deblock=False,  # T01-a：_make_source_key 参与键计算的配置项
        )
        monkeypatch.setattr(mgr, "_resolve_preview_decode_source", lambda p, s, **k: spec)
        dsk = mgr._normalize_stream_key("/x.mp4", None)
        key = mgr._make_source_key(dsk, spec)
        src = SimpleNamespace(current_frame=0)
        mgr._source_cache[key] = (src, "vt_pixel_buffer")
        mgr._interactive_source_pool[key] = [(src, "vt_pixel_buffer")]
        return mgr, src

    def test_start_failure_releases_reservation_and_event(self, monkeypatch) -> None:
        mgr, src = self._prepare_manager(monkeypatch)
        monkeypatch.setattr(threading, "Thread", _StartFailsThread)

        ok = mgr.preposition_interactive_frame("/x.mp4", 10)
        assert ok is False
        assert mgr._interactive_busy_sources == {}, "busy reservation leaked"
        assert mgr._interactive_prefetch_events == set(), "done_event leaked"
        assert mgr._interactive_preposition_threads == [], "thread registry leaked"

    def test_closing_path_releases_reservation_and_event(self, monkeypatch) -> None:
        mgr, src = self._prepare_manager(monkeypatch)
        mgr._closing = True

        ok = mgr.preposition_interactive_frame("/x.mp4", 10)
        assert ok is False
        assert mgr._interactive_busy_sources == {}
        assert mgr._interactive_prefetch_events == set()

    def test_healthy_start_still_registers(self, monkeypatch) -> None:
        mgr, src = self._prepare_manager(monkeypatch)
        # Hold the worker inside the decode step so the registration window
        # is observable (the stub source would otherwise finish instantly).
        gate = threading.Event()
        monkeypatch.setattr(
            mgr,
            "_decode_frame_interactive",
            lambda *a, **k: (gate.wait(2.0), ""),
        )
        ok = mgr.preposition_interactive_frame("/x.mp4", 10)
        assert ok is True
        assert len(mgr._interactive_busy_sources) == 1
        assert len(mgr._interactive_prefetch_events) == 1
        # Cleanup: release the gate so the worker exits cleanly.
        gate.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and mgr._interactive_busy_sources:
            with mgr._playback_prefetch_lock:
                threads = [t for _, t in mgr._interactive_preposition_threads]
            for t in threads:
                t.join(timeout=0.1)
        assert mgr._interactive_busy_sources == {}
        assert mgr._interactive_prefetch_events == set()


# ---------------------------------------------------------------------------
# AUD-16715: manual save/save-as must not ZIP-write on the calling thread
# ---------------------------------------------------------------------------

def _make_min_project():
    from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin

    asset = MediaAsset(
        asset_id="a-1", path="/tmp/media.mp4",
        media_type="video", display_name="media.mp4", metadata={},
    )
    root = ProjectBin(
        bin_id="root", name="Root", parent_id=None, asset_ids=("a-1",),
    )
    return EditProject(
        project_id="p1", name="Ef01Part2",
        assets={"a-1": asset}, bins={"root": root}, root_bin_id="root",
    )


class TestAud16715ManualSaveAsync:
    def test_save_project_as_writes_off_thread(self, qapp, tmp_path) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        project = _make_min_project()
        out = tmp_path / "proj.topos"

        saved_signal = []
        failed_signal = []
        io.project_saved.connect(saved_signal.append)
        io.manual_save_failed.connect(lambda p, e: failed_signal.append((p, e)))

        assert io.save_project_as(str(out), project) is True

        app = qapp
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            app.processEvents()
            if not io._manual_save_workers and out.exists():
                break
            time.sleep(0.01)
        app.processEvents()

        assert out.exists(), "background save never produced the package"
        assert failed_signal == []
        for _ in range(20):
            app.processEvents()
            if saved_signal:
                break
            time.sleep(0.01)
        assert saved_signal, "project_saved must be emitted (queued) on completion"
        io.cleanup()

    def test_quick_save_without_filepath_fails_fast(self, qapp) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        assert io.quick_save() is False

    def test_shutdown_blocks_new_manual_save(self, qapp, tmp_path) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        io._shutting_down = True
        assert io.save_project_as(str(tmp_path / "x.topos"), _make_min_project()) is False


# ---------------------------------------------------------------------------
# AUD-16716: shared generator-preflight diagnostic state must be lock-guarded
# ---------------------------------------------------------------------------

class TestAud16716PreflightStateLock:
    def test_lock_exists_and_accessor_thread_safe(self, qapp, tmp_path) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        assert hasattr(io, "_generator_preflight_lock"), (
            "AUD-16716: preflight state must be guarded by a lock"
        )
        assert io.get_last_generator_preflight() is None

        project = _make_min_project()
        out = tmp_path / "preflight.topos"
        # Run save_project on a background thread exactly like the save worker.
        errors: List[BaseException] = []

        def _bg() -> None:
            try:
                io.save_project(str(out), project, timeline_sequences={})
            except BaseException as exc:  # noqa: BLE001 — test collects
                errors.append(exc)

        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        t.join(timeout=20.0)
        assert not errors, errors
        preflight = io.get_last_generator_preflight()
        assert preflight is not None, (
            "save with timeline_sequences must record generator preflight diagnostics"
        )
        io.cleanup()


# ---------------------------------------------------------------------------
# AUD-16718: cleanup() must not wait forever on the save pool
# ---------------------------------------------------------------------------

class TestAud16718BoundedCleanupWait:
    def test_cleanup_uses_bounded_waitfordeone_timeout(self) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO

        src = inspect.getsource(EditProjectIO.cleanup)
        # Reverting to waitForDone() (no timeout) must fail this assertion.
        assert "waitForDone(5000)" in src, (
            "AUD-16718: cleanup must bound the pool wait"
        )

    def test_cleanup_returns_promptly_when_idle(self, qapp) -> None:
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        t0 = time.monotonic()
        io.cleanup()
        assert time.monotonic() - t0 < 2.0


# ---------------------------------------------------------------------------
# AUD-16801: AnimatableParam must be frozen (rollback-sensitive)
# ---------------------------------------------------------------------------

class TestAud16801AnimatableParamFrozen:
    def test_direct_field_assignment_raises(self) -> None:
        from dataclasses import FrozenInstanceError

        from src.features.edit.core.effect_instance import AnimatableParam

        param = AnimatableParam(base_value=0.5)
        with pytest.raises(FrozenInstanceError):
            # Reverting @dataclass(frozen=True) -> @dataclass makes this
            # assignment succeed and this test fail.
            param.base_value = 1.0

    def test_copy_on_write_api_still_works(self) -> None:
        from src.features.edit.core.effect_instance import AnimatableParam
        from src.shared.keyframe import InterpolationType

        param = AnimatableParam(base_value=0.5)
        updated = param.add_keyframe(10, 0.9, InterpolationType.LINEAR)
        assert updated is not param
        assert updated.is_animated()
        assert not param.is_animated()
        assert updated.get_value_at_frame(10) == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# AUD-16805: one malformed catalog spec must not abort the registration loop
# ---------------------------------------------------------------------------

class _BadSpec:
    @property
    def effect_type(self) -> str:
        return "bad_spec"

    name = "Bad"
    category = "filter"
    description = "explodes on schema build"
    icon = ""

    @property
    def param_defs(self):
        raise RuntimeError("malformed spec")


class TestAud16805SpecIsolation:
    def test_good_specs_registered_around_bad_one(self, monkeypatch, caplog) -> None:
        import src.features.shared.effects as effects_mod
        from src.features.edit.core.effect_registry import EffectRegistry

        good1 = SimpleNamespace(
            effect_type="ef01_good1", name="Good1", category="filter",
            description="g1", param_defs=(), icon="",
        )
        good2 = SimpleNamespace(
            effect_type="ef01_good2", name="Good2", category="stylize",
            description="g2", param_defs=(), icon="",
        )
        catalog = SimpleNamespace(get_all_specs=lambda: [good1, _BadSpec(), good2])
        monkeypatch.setattr(effects_mod, "get_effect_catalog", lambda: catalog)

        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.effect_registry"):
            registry = EffectRegistry()

        # The spec AFTER the malformed one must still be registered.
        assert registry.get_definition("ef01_good2") is not None, (
            "AUD-16805: a malformed spec aborted the whole catalog loop"
        )
        assert registry.get_definition("ef01_good1") is not None
        # Builtin audio entries survive too.
        assert registry.get_definition("strip_eq") is not None
        warnings = [r for r in caplog.records if "malformed effect spec" in r.getMessage()]
        assert warnings, "skipped spec must be logged"

    def test_catalog_import_failure_still_degrades(self, monkeypatch) -> None:
        import src.features.shared.effects as effects_mod
        from src.features.edit.core.effect_registry import EffectRegistry

        def _boom():
            raise ImportError("catalog gone")

        monkeypatch.setattr(effects_mod, "get_effect_catalog", _boom)
        registry = EffectRegistry()
        assert registry.get_definition("strip_eq") is not None


# ---------------------------------------------------------------------------
# AUD-16806: class docstring must match the actual registry content
# ---------------------------------------------------------------------------

class TestAud16806RegistryDocstring:
    def test_docstring_lists_actual_categories(self) -> None:
        from src.features.edit.core.effect_registry import EffectRegistry

        doc = EffectRegistry.__doc__ or ""
        # Reverting the doc update (stale transform/blur/sharpen list) fails.
        assert "audio" in doc.lower(), "missing audio category docs"
        assert "generator" in doc.lower(), "missing generator category docs"
        assert "EffectCatalog" in doc, "catalog-driven categories must be documented"
        assert "Crop, Opacity" not in doc, "stale category list still present"


# ---------------------------------------------------------------------------
# AUD-16807: singleton/lazy caches need locks; no module-bottom lazy import
# ---------------------------------------------------------------------------

class TestAud16807RegistrySingletonLock:
    def test_no_module_bottom_lazy_import(self) -> None:
        import src.features.edit.core.effect_registry as er

        src = inspect.getsource(er)
        assert "noqa: E402" not in src, (
            "AUD-16807: module-bottom lazy import still present"
        )
        from src.features.edit.core.effect_instance import EffectInstance
        assert er.EffectInstance is EffectInstance

    def test_concurrent_first_call_builds_one_registry(self, monkeypatch) -> None:
        import src.features.edit.core.effect_registry as er

        constructions = []

        class _SlowRegistry:
            def __init__(self) -> None:
                constructions.append(threading.get_ident())
                time.sleep(0.1)

        monkeypatch.setattr(er, "_registry_instance", None)
        monkeypatch.setattr(er, "EffectRegistry", _SlowRegistry)

        results = _run_aligned(8, lambda _i: er.get_effect_registry())
        assert len(constructions) == 1, (
            f"AUD-16807: singleton built {len(constructions)} times concurrently"
        )
        assert all(r is results[0] for r in results)

    def test_audio_default_params_cache_is_stable(self, monkeypatch) -> None:
        import src.features.edit.core.effect_registry as er

        monkeypatch.setattr(er, "_AUDIO_DEFAULT_PARAMS", None)
        first = er._get_audio_default_params()
        second = er._get_audio_default_params()
        assert first is second, "lazy cache must be reused"
        assert len(first) == 8


# ---------------------------------------------------------------------------
# AUD-17004: trace print must hold _LOCK (no torn lines / seq inversion)
# ---------------------------------------------------------------------------

class TestAud17004TracePrintUnderLock:
    def test_concurrent_trace_lines_are_well_formed_and_ordered(self, monkeypatch, capfd) -> None:
        import src.features.edit.core.playback_debug as pd

        monkeypatch.setattr(pd, "_ENABLED", True)
        monkeypatch.setattr(pd, "_FULL_TRACE", True)

        def _emit(i: int) -> None:
            pd.playback_trace("ef01.trace", worker=i)

        _run_aligned(4, lambda i: [_emit(i) for _ in range(50)])

        out = capfd.readouterr().out
        lines = [ln for ln in out.splitlines() if "EDIT_PLAYBACK_TRACE" in ln]
        assert len(lines) == 200, f"expected 200 lines, got {len(lines)}"
        for ln in lines:
            assert ln.startswith("[EDIT_PLAYBACK_TRACE"), f"torn line head: {ln!r}"
            assert ln.rstrip().endswith("]"), f"torn line tail: {ln!r}"
            assert ln.count("seq=") == 1, f"interleaved line: {ln!r}"
        seqs = [int(ln.split("seq=")[1].split(" ")[0]) for ln in lines]
        assert seqs == sorted(seqs), "seq order does not match print order"


# ---------------------------------------------------------------------------
# AUD-17301: set_mode must not emit state_changed while holding _lock
# ---------------------------------------------------------------------------

class TestAud17301SetModeSignalOutsideLock:
    def test_slot_runs_without_engine_lock(self, qapp) -> None:
        from src.features.edit.core.realtime_playback.engine import (
            EngineState,
            RealtimePlaybackEngine,
        )

        engine = RealtimePlaybackEngine()
        engine._state = EngineState.READY

        lock_free_results: List[bool] = []

        def _slot(state: str) -> None:
            def _probe() -> None:
                lock_free_results.append(engine._lock.acquire(timeout=1.0))
                if lock_free_results and lock_free_results[-1]:
                    engine._lock.release()

            probe = threading.Thread(target=_probe, daemon=True)
            probe.start()
            probe.join(timeout=3.0)

        engine.state_changed.connect(_slot)
        # AUD-17301 revert regression: set_mode holding the lock during the
        # emit makes the probe time out -> lock_free_results == [False].
        engine.set_mode(PlaybackMode.REALTIME)
        assert engine.state is EngineState.PLAYING
        assert lock_free_results == [True], (
            "AUD-17301: state_changed emitted while holding engine lock"
        )


# ---------------------------------------------------------------------------
# AUD-17305: get_default_engine singleton must be lock-guarded
# ---------------------------------------------------------------------------

class TestAud17305EngineSingletonLock:
    def test_concurrent_first_call_builds_one_engine(self, monkeypatch) -> None:
        import src.features.edit.core.realtime_playback.engine as engine_mod

        constructions = []

        class _SlowEngine:
            def __init__(self) -> None:
                constructions.append(threading.get_ident())
                time.sleep(0.1)

        monkeypatch.setattr(engine_mod, "_default_engine", None)
        monkeypatch.setattr(engine_mod, "RealtimePlaybackEngine", _SlowEngine)

        results = _run_aligned(8, lambda _i: engine_mod.get_default_engine())
        assert len(constructions) == 1, (
            f"AUD-17305: default engine built {len(constructions)} times concurrently"
        )
        assert all(r is results[0] for r in results)


# ---------------------------------------------------------------------------
# AUD-17404: GL release must be deferred to the GL thread
# ---------------------------------------------------------------------------

class TestAud17404DeferredGlRelease:
    def _new_pool_with_upload(self, release_log: List[int]):
        from src.features.edit.core.realtime_playback.gpu_frame_pool import GpuFramePool
        from src.features.edit.core.realtime_playback.timeline_gpu_compositor import (
            GpuFrameHandle,
        )

        pool = GpuFramePool()

        def _upload(frame: FrameHandle):
            handle = GpuFrameHandle(
                identity=frame.identity,
                texture_ids=(1,),
                texture_type="rgb",
                width=frame.width,
                height=frame.height,
                bit_depth=frame.bit_depth,
                color_space=None,
                generation_id=frame.identity.source_frame,
            )
            handle.release = lambda: release_log.append(threading.get_ident())
            return handle

        pool.set_upload_function(_upload)
        return pool

    def test_release_from_foreign_thread_is_deferred(self) -> None:
        release_log: List[int] = []
        pool = self._new_pool_with_upload(release_log)

        handle = pool.ensure_gpu_frame(_make_frame(1), generation_id=1)
        assert handle is not None
        gl_thread = threading.get_ident()
        assert pool._gl_thread_id == gl_thread

        t = threading.Thread(
            target=lambda: pool.release_generation(1), daemon=True
        )
        t.start()
        t.join(timeout=2.0)

        assert release_log == [], "AUD-17404: GL release ran on a non-GL thread"
        stats = pool.stats()
        assert stats["pending_releases"] == 1
        assert stats["resident"] == 0

        # GL thread drains the queue.
        pool.process_pending_releases()
        assert len(release_log) == 1
        assert pool.stats()["pending_releases"] == 0

    def test_ensure_gpu_frame_drains_pending(self) -> None:
        release_log: List[int] = []
        pool = self._new_pool_with_upload(release_log)

        h1 = pool.ensure_gpu_frame(_make_frame(1), generation_id=1)
        assert h1 is not None
        t = threading.Thread(
            target=lambda: pool.release_generation(1), daemon=True
        )
        t.start()
        t.join(timeout=2.0)
        assert pool.stats()["pending_releases"] == 1

        h2 = pool.ensure_gpu_frame(_make_frame(2), generation_id=2)
        assert h2 is not None
        assert pool.stats()["pending_releases"] == 0, (
            "ensure_gpu_frame must drain deferred releases on the GL thread"
        )
        assert len(release_log) == 1

    def test_unknown_gl_thread_keeps_inline_release(self) -> None:
        release_log: List[int] = []
        pool = self._new_pool_with_upload(release_log)
        # No ensure_gpu_frame ever ran: GL thread unknown -> legacy inline path.
        pool._resident[("x", 5)] = SimpleNamespace(release=lambda: release_log.append(1))
        pool.release_generation(5)
        assert release_log == [1]


# ---------------------------------------------------------------------------
# AUD-17405: in-flight budget placeholder must self-heal / abort
# ---------------------------------------------------------------------------

class TestAud17405InFlightRecovery:
    def test_stale_in_flight_is_recovered(self) -> None:
        from src.features.edit.core.realtime_playback.playback_budget import (
            PlaybackBudgetController,
        )

        # High target_fps shrinks 4*budget below the requested timeout floor.
        budget = PlaybackBudgetController(
            target_fps=200000.0, in_flight_timeout_s=0.05
        )
        assert budget.before_render(1) is True
        time.sleep(0.09)
        # Without the timeout self-heal this would drop forever.
        assert budget.before_render(2) is True, (
            "AUD-17405: stale in-flight still blocks all frames"
        )

    def test_abort_render_clears_placeholder(self) -> None:
        from src.features.edit.core.realtime_playback.playback_budget import (
            PlaybackBudgetController,
        )

        budget = PlaybackBudgetController(in_flight_timeout_s=5.0)
        assert budget.before_render(7) is True
        assert budget.abort_render(7) is True
        assert budget.before_render(8) is True

    def test_abort_render_frame_mismatch_is_rejected(self) -> None:
        from src.features.edit.core.realtime_playback.playback_budget import (
            PlaybackBudgetController,
        )

        budget = PlaybackBudgetController(in_flight_timeout_s=5.0)
        assert budget.before_render(9) is True
        assert budget.abort_render(99) is False
        assert budget.before_render(10) is False, "in-flight must stay held"
        budget.abort_render(None)
        assert budget.before_render(11) is True


# ---------------------------------------------------------------------------
# AUD-17407: after_render validation (rollback-sensitive)
# ---------------------------------------------------------------------------

class TestAud17407AfterRenderValidation:
    def test_frame_mismatch_is_not_recorded(self) -> None:
        from src.features.edit.core.realtime_playback.playback_budget import (
            PlaybackBudgetController,
        )

        budget = PlaybackBudgetController()
        assert budget.before_render(1) is True
        budget.after_render(2, time.time_ns(), presented=True)
        # Reverting the validation makes the bogus frame land in history.
        assert len(budget._history) == 0, (
            "AUD-17407: mismatched after_render polluted the timing history"
        )
        assert budget.before_render(3) is True, "in-flight must still be cleared"

    def test_future_start_ns_is_clamped(self) -> None:
        from src.features.edit.core.realtime_playback.playback_budget import (
            PlaybackBudgetController,
        )

        budget = PlaybackBudgetController()
        assert budget.before_render(5) is True
        bogus_future = time.time_ns() + 1_000_000_000
        budget.after_render(5, bogus_future, presented=True)
        assert len(budget._history) == 1
        # Reverting the sanity check yields a negative elapsed time here.
        assert budget._history[0].elapsed_ns >= 0, (
            "AUD-17407: caller-provided future start_ns was trusted"
        )


# ---------------------------------------------------------------------------
# AUD-17408: get_default_policy singleton lock
# ---------------------------------------------------------------------------

class TestAud17408PolicySingletonLock:
    def test_concurrent_first_call_builds_one_policy(self, monkeypatch) -> None:
        import src.features.edit.core.realtime_playback.policy as policy_mod

        constructions = []

        class _SlowPolicy:
            def __init__(self) -> None:
                constructions.append(threading.get_ident())
                time.sleep(0.1)

        monkeypatch.setattr(policy_mod, "_default_policy", None)
        monkeypatch.setattr(policy_mod, "RealtimePlaybackPolicy", _SlowPolicy)

        results = _run_aligned(8, lambda _i: policy_mod.get_default_policy())
        assert len(constructions) == 1, (
            f"AUD-17408: default policy built {len(constructions)} times concurrently"
        )
        assert all(r is results[0] for r in results)


# ---------------------------------------------------------------------------
# AUD-17413: _ensured_proxies check-then-add must be atomic
# ---------------------------------------------------------------------------

class TestAud17413EnsuredProxiesLock:
    def test_concurrent_ensure_triggers_generation_once(self) -> None:
        from src.features.edit.core.realtime_playback.source_resolver import (
            LayerSourceQuery,
            SourceResolver,
        )

        class _FakeProxyManager:
            def __init__(self) -> None:
                self.calls: List[str] = []
                self._lock = threading.Lock()

            def get_proxy_entry(self, path: str):
                return None

            def ensure_proxy_async(self, source_path: str, asset_id: str = "") -> None:
                with self._lock:
                    self.calls.append(source_path)
                time.sleep(0.05)

        manager = _FakeProxyManager()
        resolver = SourceResolver(proxy_manager=manager)

        query = LayerSourceQuery(
            clip_id="c1", asset_id="a1", source_path="/media/x.mp4",
            source_frame=0, timeline_frame=0,
        )
        _run_aligned(4, lambda _i: resolver.ensure_proxy(query))

        assert len(manager.calls) == 1, (
            f"AUD-17413: proxy generation triggered {len(manager.calls)}x concurrently"
        )
        assert resolver.stats()["ensured_proxies"] == 1

    def test_invalidate_and_reensure(self) -> None:
        from src.features.edit.core.realtime_playback.source_resolver import (
            LayerSourceQuery,
            SourceResolver,
        )

        manager = SimpleNamespace(
            get_proxy_entry=lambda p: None,
            ensure_proxy_async=lambda source_path, asset_id="": None,
        )
        resolver = SourceResolver(proxy_manager=manager)
        query = LayerSourceQuery(
            clip_id="c1", asset_id="a1", source_path="/media/y.mp4",
            source_frame=0, timeline_frame=0,
        )
        resolver.ensure_proxy(query)
        assert resolver.stats()["ensured_proxies"] == 1
        resolver.invalidate_proxy_cache("/media/y.mp4")
        assert resolver.stats()["ensured_proxies"] == 0
        resolver.ensure_proxy(query)
        assert resolver.stats()["ensured_proxies"] == 1


# ---------------------------------------------------------------------------
# AUD-17414: service resolution must be lock-guarded and retryable
# ---------------------------------------------------------------------------

class TestAud17414ServiceResolution:
    def test_service_lock_exists(self) -> None:
        from src.features.edit.core.realtime_playback.source_resolver import (
            DiskProxyServiceAdapter,
        )

        adapter = DiskProxyServiceAdapter()
        assert hasattr(adapter, "_service_lock"), (
            "AUD-17414: service resolution must be lock-guarded"
        )

    def test_failure_does_not_persist_and_retry_succeeds(self, monkeypatch) -> None:
        import src.shared.media.disk_proxy_service as dps_mod
        from src.features.edit.core.realtime_playback.source_resolver import (
            DiskProxyServiceAdapter,
        )

        adapter = DiskProxyServiceAdapter()
        state = {"fail": True}

        def _resolve():
            if state["fail"]:
                raise RuntimeError("service starting up")
            return SimpleNamespace(get_disk_proxy_info=lambda p: None)

        monkeypatch.setattr(dps_mod, "get_disk_proxy_service", _resolve)
        assert adapter._resolve_service() is None
        state["fail"] = False
        service = adapter._resolve_service()
        assert service is not None, "AUD-17414: failure must not be permanent"
        assert adapter._resolve_service() is service


# ---------------------------------------------------------------------------
# AUD-17418: telemetry summary snapshots under lock; no unused import
# ---------------------------------------------------------------------------

class TestAud17418TelemetrySummary:
    def test_module_has_no_unused_field_import(self) -> None:
        # W07 订正：AUD-17418 清理时 field 确实未用；后续 N0.x 给
        # telemetry 记录加了 field(default_factory=...) 的合法使用
        # （stage_status 等按记录惰性建表）。不变量从「模块不暴露
        # field」改为 AST 级「import 的名字必须被使用」。
        import ast
        import inspect

        from src.features.edit.core.realtime_playback import (
            telemetry as telemetry_mod,
        )

        source = inspect.getsource(telemetry_mod)
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name.split(".")[-1])
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                pass  # 模块属性访问不计
        unused = {n for n in imported_names
                  if n not in used and n != "annotations"}
        assert not unused, (
            f"AUD-17418: telemetry 模块存在未使用 import: {sorted(unused)}"
        )

    def test_summary_matches_records(self) -> None:
        from src.features.edit.core.realtime_playback.telemetry import (
            PlaybackFrameTelemetry,
            TelemetryAggregator,
        )

        agg = TelemetryAggregator(window=10)
        agg.record(PlaybackFrameTelemetry(timeline_frame=1, total_ms=16.0))
        summary = agg.summary()
        assert summary["frames"] == 1

    def test_summary_concurrent_with_reset_does_not_crash(self) -> None:
        from src.features.edit.core.realtime_playback.telemetry import (
            PlaybackFrameTelemetry,
            TelemetryAggregator,
        )

        agg = TelemetryAggregator(window=32)
        stop = threading.Event()

        def _record() -> None:
            i = 0
            while not stop.is_set():
                agg.record(PlaybackFrameTelemetry(timeline_frame=i, total_ms=8.0))
                i += 1

        def _reset() -> None:
            while not stop.is_set():
                agg.reset()

        def _summarize() -> None:
            while not stop.is_set():
                agg.summary()

        workers = [
            threading.Thread(target=_record, daemon=True),
            threading.Thread(target=_reset, daemon=True),
            threading.Thread(target=_summarize, daemon=True),
        ]
        for w in workers:
            w.start()
        time.sleep(0.3)
        stop.set()
        for w in workers:
            w.join(timeout=2.0)
        assert all(not w.is_alive() for w in workers)


# ---------------------------------------------------------------------------
# AUD-17420: bundle counters + stats/reset lock symmetry
# ---------------------------------------------------------------------------

class TestAud17420BundleCounters:
    def test_concurrent_misses_are_counted_exactly(self) -> None:
        from src.features.edit.core.realtime_playback.timeline_decode_scheduler import (
            TimelineDecodeScheduler,
        )

        scheduler = TimelineDecodeScheduler()

        def _request(_i: int) -> None:
            for _ in range(250):
                bundle = scheduler.request_bundle(
                    timeline_frame=0,
                    generation_id=0,
                    deadline_ns=0,
                    mode=PlaybackMode.REALTIME,
                )
                assert bundle is not None
                assert bundle.complete is False

        _run_aligned(4, _request)
        stats = scheduler.stats()
        assert stats["bundle_misses"] == 1000, (
            f"AUD-17420: lost counter updates ({stats['bundle_misses']}/1000)"
        )

    def test_reset_stats_clears_prefetch_counters(self) -> None:
        from src.features.edit.core.realtime_playback.timeline_decode_scheduler import (
            TimelineDecodeScheduler,
        )

        scheduler = TimelineDecodeScheduler()
        with scheduler._prefetch_lock:
            scheduler._prefetch_submitted = 3
            scheduler._prefetch_completed = 2
            scheduler._prefetch_dropped = 1

        scheduler.reset_stats()

        stats = scheduler.stats()
        assert stats["bundle_misses"] == 0
        assert stats["bundle_hits"] == 0
        # Reverting the prefetch-clearing fix leaves 3/2/1 here.
        assert stats["prefetch_submitted"] == 0, (
            "AUD-17420: reset_stats must clear prefetch counters"
        )
        assert stats["prefetch_completed"] == 0
        assert stats["prefetch_dropped"] == 0


# ---------------------------------------------------------------------------
# AUD-17423: shutdown must bound the prefetch wait
# ---------------------------------------------------------------------------

class _StubExecutor:
    def __init__(self) -> None:
        self.shutdown_wait: bool | None = None

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        self.shutdown_wait = wait


class TestAud17423BoundedShutdown:
    def test_shutdown_does_not_wait_past_timeout(self, caplog) -> None:
        from src.features.edit.core.realtime_playback.timeline_decode_scheduler import (
            TimelineDecodeScheduler,
        )

        scheduler = TimelineDecodeScheduler()
        stub = _StubExecutor()
        scheduler._prefetch_executor = stub

        hung = Future()
        with scheduler._prefetch_lock:
            scheduler._prefetch_active = True
            scheduler._prefetch_active_future = hung
        # The "decode" completes long after the shutdown budget.
        threading.Timer(0.4, lambda: (not hung.done()) and hung.set_result(True)).start()

        with caplog.at_level(logging.WARNING, logger="src.features.edit.core.timeline_decode_scheduler"):
            t0 = time.monotonic()
            scheduler.shutdown(timeout_s=0.15)
            elapsed = time.monotonic() - t0

        # Reverting to executor.shutdown(wait=True, ...) fails this.
        assert stub.shutdown_wait is False, (
            "AUD-17423: shutdown still blocks on the prefetch executor"
        )
        assert elapsed < 0.35, (
            f"AUD-17423: shutdown waited {elapsed:.2f}s for a hung prefetch"
        )
        assert scheduler._configured is False, "shutdown must continue past the wait"
        warnings = [r for r in caplog.records if "bounded wait" in r.getMessage()]
        assert warnings, "giving up the wait must be logged"

    def test_shutdown_returns_when_future_already_done(self) -> None:
        from src.features.edit.core.realtime_playback.timeline_decode_scheduler import (
            TimelineDecodeScheduler,
        )

        scheduler = TimelineDecodeScheduler()
        scheduler._prefetch_executor = _StubExecutor()
        done = Future()
        done.set_result(True)
        with scheduler._prefetch_lock:
            scheduler._prefetch_active_future = done
        scheduler.shutdown(timeout_s=1.0)
        assert scheduler._configured is False


# ---------------------------------------------------------------------------
# AUD-17505: compositor stats counters need a lock
# ---------------------------------------------------------------------------

class TestAud17505StatsLock:
    def test_stats_lock_exists(self) -> None:
        from src.features.edit.core.realtime_playback.timeline_gpu_compositor import (
            TimelineGpuCompositor,
        )

        compositor = TimelineGpuCompositor()
        assert hasattr(compositor, "_stats_lock"), (
            "AUD-17505: stats counters must be lock-guarded"
        )

    def test_concurrent_submits_do_not_lose_updates(self) -> None:
        from src.features.edit.core.realtime_playback.timeline_gpu_compositor import (
            TimelineGpuCompositor,
        )

        compositor = TimelineGpuCompositor()
        compositor._backend = SimpleNamespace(submit=lambda task: True)
        task = SimpleNamespace(layers=[object()], frame=1)

        def _submit(_i: int) -> None:
            for _ in range(4000):
                assert compositor.submit_backend_task(task) is True

        _run_aligned(4, _submit)
        stats = compositor.stats()
        assert stats["submitted"] == 16000, (
            f"AUD-17505: lost counter updates ({stats['submitted']}/16000)"
        )
        assert stats["gpu_composite_frames"] == 16000

        compositor.reset_stats()
        assert compositor.stats()["submitted"] == 0


# ---------------------------------------------------------------------------
# AUD-17606: TakeInfo.status docstring must match actual values
# ---------------------------------------------------------------------------

class TestAud17606TakeStatusDoc:
    def test_docstring_lists_all_status_values(self) -> None:
        from src.features.edit.core.recording_manager import TakeInfo

        doc = TakeInfo.__doc__ or ""
        for expected in (
            "pending", "recording", "completed", "completed_partial",
            "failed", "discarded",
        ):
            assert expected in doc, (
                f"AUD-17606: TakeInfo.status docstring missing {expected!r}"
            )

    def test_default_status_is_pending(self) -> None:
        from src.features.edit.core.recording_manager import TakeInfo

        assert TakeInfo().status == "pending"


# ---------------------------------------------------------------------------
# AUD-17812: ThumbnailCache must be thread-safe as documented
# ---------------------------------------------------------------------------

class TestAud17812ThumbnailCacheLocks:
    def test_lock_exists_and_basic_ops_work(self, tmp_path) -> None:
        from src.features.edit.core.scene_cut.cache import ThumbnailCache

        cache = ThumbnailCache(thumbs_dir=tmp_path / "thumbs")
        assert hasattr(cache, "_lock"), (
            "AUD-17812: ThumbnailCache claims thread safety without a lock"
        )
        cache.save_thumb("k", 1, "before", b"\xff\xd8data")
        assert cache.has_thumb("k", 1, "before")
        assert cache.load_thumb("k", 1, "before") == b"\xff\xd8data"
        assert cache.delete_thumb("k", 1, "before")
        assert cache.entry_count == 0

    def test_concurrent_access_keeps_invariants(self, tmp_path) -> None:
        from src.features.edit.core.scene_cut.cache import ThumbnailCache

        cache = ThumbnailCache(thumbs_dir=tmp_path / "thumbs")
        errors: List[BaseException] = []

        def _worker(worker_idx: int) -> None:
            try:
                for i in range(40):
                    key = f"k{worker_idx}_{i % 10}"
                    cache.save_thumb(key, i, "before", b"x" * (100 + worker_idx))
                    cache.has_thumb(key, i, "before")
                    if i % 4 == 0:
                        cache.delete_thumb(key, i, "before")
            except BaseException as exc:  # noqa: BLE001 — collected for assert
                errors.append(exc)

        _run_aligned(4, _worker)
        assert not errors, errors

        # Invariant: accounting size equals the sum of tracked entries.
        with cache._lock:
            tracked = sum(e.size_bytes for e in cache._entries.values())
        assert cache.current_size == tracked, (
            "AUD-17812: concurrent access corrupted cache accounting"
        )
