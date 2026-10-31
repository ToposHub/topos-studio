"""EF-18 regression tests — 21 Suggestion-level audit findings.

Covers (see docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md):

teaching_animation/render:
- AUD-29804 — AssetResolver.data_uri degrades a vanished/unreadable asset to
  ``None`` (regression guard: the OSError guard already present must stay).
- AUD-29809 — probe_video_metrics never raises when the file disappears
  between ``is_file()`` and the late ``stat()`` (size degrades to 0).
- AUD-30133 — HtmlRendererService.backend_build is served from the build
  cached at construction; it never blocks on a busy pool.
- AUD-30134 — a mid-loop factory failure at construction closes the workers
  already created before propagating.
- AUD-30217 — QtWebEngineGrabBackend.capture fails closed (UNSUPPORTED) when
  called off the main thread; main-thread calls pass through untouched.
- AUD-30302 — the ``allow_uniform`` signature probe runs once per service in
  ``HtmlSceneRendererV2.__init__``, not per rendered frame.
- AUD-30307 — PerfBudget.limits is wrapped in a read-only mapping proxy;
  caller-side dict mutation cannot change a constructed budget.
- AUD-30310 — RenderWorker.render_sync's callback annotation matches submit()
  (truthy return cancels) and ``_handles`` is lock-guarded.
- AUD-30407 — the SVG paint loops look the filter id up from the map built by
  ``_effect_defs`` instead of re-running ``_effect_filter`` per element.

shared/codec:
- AUD-32106 — DecodeWorkerBudget.acquire adopts a caller capacity only in the
  increasing direction (a transient export-window low value cannot pin the
  process singleton).
- AUD-32205 — ParallelSequentialDecoder restores the global slice-threads
  value it changed, on context exit; dead PREFETCH/_owns_codec removed.
- AUD-32208 — encode() docstring references the existing encode_frame API;
  decode() carries no dead ``widths`` variable.
- AUD-32302 — write_packet(durable=True, atomic=True) also fsyncs the parent
  directory; a directory-fsync failure is logged, not raised.
- AUD-32303 — a construction failure between os.open and the first native
  call closes the fd and unlinks the temp file.
- AUD-32304 — the HALF/code LUT lazy builders use a double-checked lock and
  stay bit-exact against the frozen spec mapping.

shared/export:
- AUD-33115 — save_batch_queue(settings=None) preserves previously persisted
  settings (docstring semantics).
- AUD-33119 — ExportConfig is frozen, framerate is typed as media Rational,
  get_total_frames never returns a negative count.
- AUD-33208 — GpuEncodeSurface.submit auto-releases the lease on a falsy
  callback return (same as the raise path) and returns the value unchanged.
- AUD-33212 — get_dnxhr_profile_for_resolution actually uses width/height
  (UHD+ upgrades LB/SQ/HQ to HQX; explicit HQX/444 requests kept).
- AUD-33403 — _pipeline_discard tears down shared queue/cond objects only
  after every convert worker really exited.
- AUD-33405 — the domain-check counter and one-shot warning flags are guarded
  by a lock (exact counts under concurrency).

Codec/export tests live in this file by design (option A of the batch
instruction): they are pure unit tests over ``src/shared`` modules that need
no native library, no real video encode and no modal dialogs.

Run: .venv/bin/python -m pytest tests/teaching_animation/test_ef18_aud.py -q
"""

from __future__ import annotations

import ctypes
import inspect
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from src.features.teaching_animation.core.contracts import (
    AnimationFrame,
    RenderContext,
)
from src.features.teaching_animation.core.model import SceneAssetRef
from src.features.teaching_animation.presets import builders
from src.features.teaching_animation.render.asset_resolver import AssetResolver
from src.features.teaching_animation.render.bitrate_probe import probe_video_metrics
from src.features.teaching_animation.render.html_renderer_service import (
    HtmlRendererService,
)
from src.features.teaching_animation.render.html_renderer_spike import (
    CaptureFailureCode,
    CaptureSurface,
    QtWebEngineGrabBackend,
)
from src.features.teaching_animation.render.html_renderer_spike.capture_backend import (
    CaptureFailure,
)
from src.features.teaching_animation.render.html_scene_renderer_v2 import (
    HtmlSceneRendererV2,
)
from src.features.teaching_animation.render.perf_slo import (
    BenchmarkEnvironment,
    BenchmarkSample,
    PerfBudget,
    PerfGate,
    PerfMetric,
)
from src.features.teaching_animation.render.render_worker import RenderWorker
from src.features.teaching_animation.render import svg_renderer as svg_module
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer
from src.shared.codec.decode_worker_budget import DecodeWorkerBudget
from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import ParallelSequentialDecoder, ToposCodec
from src.shared.codec import topos_image_binding
from src.shared.codec.topos_image_binding import ToposImageCodec
from src.shared.export import batch_queue_store
from src.shared.export.batch_queue_store import (
    BatchQueueItem,
    load_batch_queue,
    save_batch_queue,
)
from src.shared.export.encoder import ExportConfig, VideoEncoderConfig
from src.shared.export.gpu_encode_surface import (
    GpuEncodeSurface,
    GpuEncodeSurfaceDescriptor,
)
from src.shared.export.presets import DNxHRProfile, get_dnxhr_profile_for_resolution
from src.shared.export import topos_encoder as te_module
from src.shared.export.topos_encoder import ToposVideoEncoder


# --------------------------------------------------------------------------- #
# Shared fakes
# --------------------------------------------------------------------------- #

class _FakeBackend:
    """Duck-typed FrameCaptureBackend for pool tests (no Qt/browser)."""

    def __init__(self, build: str = "build-x") -> None:
        self.backend_id = "fake"
        self.backend_build = build
        self.closed = False

    def capture(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("capture not expected in these tests")

    def close(self) -> None:
        self.closed = True


class _FakeLease:
    """Duck-typed GpuFrameRelease recording exactly-once release."""

    def __init__(self) -> None:
        self.release_count = 0
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            self.release_count += 1


class _FakeImageLib:
    """Minimal ``tc_image_*`` stub: derive_idsc OK, write OK (no bytes)."""

    TC_OK = 0

    def tc_image_derive_idsc(self, packet: Any, size: int, profile: Any,
                             idsc: Any) -> int:
        return 0

    def tc_image_write(self, params: Any, io: Any, out_size: Any) -> int:
        return 0


def _image_codec_without_native() -> ToposImageCodec:
    """A ToposImageCodec instance wired to a stub library (no CDLL load)."""
    codec = object.__new__(ToposImageCodec)
    codec._lib = _FakeImageLib()
    codec._detail = lambda status: "fake-detail"  # type: ignore[method-assign]
    return codec


# --------------------------------------------------------------------------- #
# AUD-29804 — data_uri OSError guard (regression guard, already fixed)
# --------------------------------------------------------------------------- #

def _resolver_with_asset(tmp_path: Path, filename: str, payload: bytes) -> tuple[
        AssetResolver, SceneAssetRef]:
    asset_root = tmp_path / "assets"
    asset_root.mkdir(exist_ok=True)
    (asset_root / filename).write_bytes(payload)
    asset = SceneAssetRef(asset_id="a1", kind="image", path=filename)
    scene = builders.scene_document(
        [builders.shape_element("bg", 0, 0, 64, 64, kind="rect", fill="#123456")],
        name="Assets", duration_frames=6)
    scene = replace(scene, asset_refs=(asset,))
    context = RenderContext(
        fps=24.0, width=64, height=64, backend="svg",
        asset_root=str(asset_root), strict_assets=False, render_profile="preview")
    resolver = AssetResolver(context)
    resolver.preflight(scene)
    return resolver, asset


class TestAud29804DataUriOSErrorGuard:
    def test_vanished_asset_degrades_to_none(self, tmp_path: Path) -> None:
        """After preflight the file is deleted; data_uri must return None, not
        raise (the guard is load-bearing for the placeholder path)."""
        resolver, asset = _resolver_with_asset(tmp_path, "dot.png", b"tiny")
        (tmp_path / "assets" / "dot.png").unlink()
        assert resolver.data_uri(asset) is None

    def test_readable_asset_still_inlines(self, tmp_path: Path) -> None:
        resolver, asset = _resolver_with_asset(tmp_path, "dot.png", b"tiny")
        uri = resolver.data_uri(asset)
        assert uri is not None
        assert uri.startswith("data:image/png;base64,")


# --------------------------------------------------------------------------- #
# AUD-29809 — probe_video_metrics TOCTOU on the late stat
# --------------------------------------------------------------------------- #

_PROBE_JSON = json.dumps({
    "streams": [{
        "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920,
        "height": 1080, "r_frame_rate": "24/1", "nb_frames": "24",
    }],
    "format": {"duration": "1.0"},
})


class TestAud29809ProbeStatRace:
    def test_stat_failure_degrades_to_zero_not_raise(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.features.teaching_animation.render.bitrate_probe as bp

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"0" * 128)
        monkeypatch.setattr(bp, "_ffprobe_binary", lambda: "ffprobe-fake")
        monkeypatch.setattr(
            bp.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, _PROBE_JSON, ""))
        # Simulate the file vanishing exactly at the late stat call: the
        # top-of-function is_file() stays permissive, stat() fails.
        monkeypatch.setattr(Path, "is_file", lambda self: True)

        def _boom(self: Path, **kw: Any) -> os.stat_result:
            raise FileNotFoundError(f"gone: {self}")

        monkeypatch.setattr(Path, "stat", _boom)
        metrics = probe_video_metrics(video)
        assert metrics is not None
        assert metrics.size_bytes == 0  # degraded, contract kept

    def test_fallback_size_reads_real_file_when_format_size_missing(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.features.teaching_animation.render.bitrate_probe as bp

        video = tmp_path / "clip.mp4"
        payload = b"0" * 4096
        video.write_bytes(payload)
        monkeypatch.setattr(bp, "_ffprobe_binary", lambda: "ffprobe-fake")
        monkeypatch.setattr(
            bp.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, _PROBE_JSON, ""))
        metrics = probe_video_metrics(video)
        assert metrics is not None
        assert metrics.size_bytes == len(payload)


# --------------------------------------------------------------------------- #
# AUD-30133 / AUD-30134 — HtmlRendererService pool lifecycle
# --------------------------------------------------------------------------- #

class _BusyQueue:
    """Stub idle queue: get() immediately reports empty (no blocking)."""

    def get(self, timeout: Any = None) -> Any:
        raise queue.Empty()


class TestAud30133BackendBuildNonBlocking:
    def test_backend_build_served_from_cache_while_pool_busy(self) -> None:
        backend = _FakeBackend("chromium-1234")
        service = HtmlRendererService(lambda: backend, pool_size=1)
        assert service.backend_build == "chromium-1234"
        # Drain the pool (every worker busy) and make any acquire fail fast.
        assert service.acquire() is backend
        service._idle = _BusyQueue()  # type: ignore[assignment]
        assert service.backend_build == "chromium-1234"  # cached, no acquire

    def test_backend_build_matches_worker(self) -> None:
        service = HtmlRendererService(lambda: _FakeBackend("b42"), pool_size=2)
        assert service.backend_build == "b42"
        service.close()


class TestAud30134ConstructorLeak:
    def test_factory_failure_closes_already_created_workers(self) -> None:
        first = _FakeBackend("b1")
        calls = {"n": 0}

        def factory() -> _FakeBackend:
            calls["n"] += 1
            if calls["n"] == 1:
                return first
            raise RuntimeError("browser launch failed")

        with pytest.raises(RuntimeError, match="browser launch failed"):
            HtmlRendererService(factory, pool_size=2)
        assert first.closed, "workers created before the failure must be closed"

    def test_all_workers_created_on_success(self) -> None:
        backends = [_FakeBackend(f"b{i}") for i in range(3)]
        iterator = iter(backends)
        service = HtmlRendererService(lambda: next(iterator), pool_size=3)
        assert service.acquire() is backends[0]
        service.release(backends[0])
        service.close()
        assert all(b.closed for b in backends)


# --------------------------------------------------------------------------- #
# AUD-30217 — Qt backend main-thread constraint
# --------------------------------------------------------------------------- #

class TestAud30217MainThreadConstraint:
    def test_capture_off_main_thread_fails_closed(self) -> None:
        backend = QtWebEngineGrabBackend()
        results: list[Any] = []

        def run() -> None:
            results.append(backend.capture(
                "<html><body>x</body></html>", CaptureSurface(32, 32, 1.0),
                frame=0, fps_num=24, fps_den=1, csp="",
                url_policy=type("U", (), {})(), timeout_ms=100))

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        result = results[0]
        assert isinstance(result, CaptureFailure)
        assert result.code == CaptureFailureCode.UNSUPPORTED
        assert "main thread" in result.message

    def test_main_thread_still_reaches_input_validation(self) -> None:
        """Positive: on the main thread the guard passes through and the empty
        html is rejected by the ordinary INVALID_INPUT path (no Qt import)."""
        backend = QtWebEngineGrabBackend()
        result = backend.capture(
            "", CaptureSurface(32, 32, 1.0),
            frame=0, fps_num=24, fps_den=1, csp="",
            url_policy=type("U", (), {})(), timeout_ms=100)
        assert isinstance(result, CaptureFailure)
        assert result.code == CaptureFailureCode.INVALID_INPUT


# --------------------------------------------------------------------------- #
# AUD-30302 — signature probe cached per service
# --------------------------------------------------------------------------- #

class _ExplicitAllowUniformService:
    def render_frame(self, html: str, surface: Any, *, allow_uniform: bool = False) -> None:
        raise AssertionError("not called in this test")


class _LegacyService:
    def render_frame(self, html: str, surface: Any) -> None:  # no allow_uniform
        raise AssertionError("not called in this test")


class TestAud30302SignatureProbeCaching:
    def test_probed_once_at_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.features.teaching_animation.render.html_scene_renderer_v2 as mod

        calls = {"n": 0}
        real_signature = mod.inspect.signature

        def counting(fn: Any) -> Any:
            calls["n"] += 1
            return real_signature(fn)

        monkeypatch.setattr(mod.inspect, "signature", counting)
        renderer = HtmlSceneRendererV2(_ExplicitAllowUniformService())  # type: ignore[arg-type]
        assert calls["n"] == 1
        assert renderer._service_accepts_allow_uniform is True

    def test_legacy_service_flag_false(self) -> None:
        renderer = HtmlSceneRendererV2(_LegacyService())  # type: ignore[arg-type]
        assert renderer._service_accepts_allow_uniform is False

    def test_render_path_has_no_per_frame_reflection(self) -> None:
        source = inspect.getsource(HtmlSceneRendererV2.render)
        assert "inspect.signature" not in source
        assert "self._service_accepts_allow_uniform" in source


# --------------------------------------------------------------------------- #
# AUD-30307 — PerfBudget limits immutability
# --------------------------------------------------------------------------- #

class TestAud30307PerfBudgetImmutable:
    def test_caller_mutation_does_not_change_budget(self) -> None:
        limits = {PerfMetric.FIRST_FRAME_MS: 100.0}
        budget = PerfBudget("preview", limits)
        limits[PerfMetric.FIRST_FRAME_MS] = 1.0  # post-construction mutation
        assert budget.limit(PerfMetric.FIRST_FRAME_MS) == 100.0

    def test_direct_write_rejected(self) -> None:
        budget = PerfBudget("preview", {PerfMetric.EXPORT_FPS: 12.0})
        with pytest.raises(TypeError):
            budget.limits[PerfMetric.EXPORT_FPS] = 999.0  # type: ignore[index]

    def test_budget_is_read_only_mapping_and_gate_works(self) -> None:
        budget = PerfBudget("preview", {PerfMetric.FIRST_FRAME_MS: 10.0})
        assert isinstance(budget.limits, MappingProxyType)
        gate = PerfGate({"preview": budget})
        sample = BenchmarkSample(
            metric=PerfMetric.FIRST_FRAME_MS, value=50.0,
            environment=BenchmarkEnvironment(profile="preview", fixture_id="f"))
        result = gate.evaluate((sample,))
        assert not result.passed
        assert result.violations[0].budget == 10.0


# --------------------------------------------------------------------------- #
# AUD-30310 — RenderWorker annotation + lock
# --------------------------------------------------------------------------- #

class TestAud30310RenderWorkerContract:
    def test_render_sync_annotation_matches_submit(self) -> None:
        assert (RenderWorker.render_sync.__annotations__["on_progress"]
                == RenderWorker.submit.__annotations__["on_progress"])
        assert "bool | None" in RenderWorker.render_sync.__annotations__["on_progress"]

    def test_handles_registry_is_lock_guarded(self) -> None:
        worker = RenderWorker()
        assert isinstance(worker._handles_lock, type(threading.Lock()))
        worker.shutdown()

    def test_concurrent_submit_and_shutdown_smoke(self) -> None:
        from src.features.teaching_animation.core.contracts import RenderContext

        worker = RenderWorker(max_workers=4)
        context = RenderContext(fps=24.0, width=8, height=8, backend="svg")
        scene = type("S", (), {"fps": 24.0, "width": 8, "height": 8})()
        errors: list[BaseException] = []

        def submit_many() -> None:
            try:
                for _ in range(20):
                    worker.submit(scene, [0], context)
            except BaseException as exc:  # pragma: no cover - diagnostics only
                errors.append(exc)

        threads = [threading.Thread(target=submit_many) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        worker.shutdown()
        assert errors == []
        assert worker._handles == {}


# --------------------------------------------------------------------------- #
# AUD-30407 — filter id lookup table replaces per-element re-resolution
# --------------------------------------------------------------------------- #

class TestAud30407FilterFragmentReuse:
    def _scene(self) -> Any:
        element = builders.shape_element(
            "e1", 4, 4, 32, 32, kind="rect", fill="#ff8800",
            effects=[{"effect_type": "blur", "params": {"radius": 3.0}}])
        return builders.scene_document([element], name="Fx", duration_frames=2)

    def test_filter_rendered_exactly_once_per_element(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_effect_filter must run once per element per frame (inside
        _effect_defs); the old paint loop called it a second time."""
        calls = {"n": 0}
        real = svg_module._effect_filter

        def counting(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(svg_module, "_effect_filter", counting)
        renderer = SvgSceneRenderer()
        renderer.prepare(self._scene(), RenderContext(
            fps=24.0, width=64, height=64, backend="svg",
            render_profile="preview"))
        data = renderer.render_frame(AnimationFrame(0, 24.0)).data.decode()
        renderer.cleanup()
        assert calls["n"] == 1
        assert "feGaussianBlur" in data
        assert 'filter="url(#effect_e1)"' in data

    def test_effect_defs_returns_filter_id_map(self) -> None:
        document = self._scene()
        # Reuse the renderer's evaluate path through the module under test.
        renderer = SvgSceneRenderer()
        renderer.prepare(document, RenderContext(
            fps=24.0, width=64, height=64, backend="svg",
            render_profile="preview"))
        evaluated = svg_module.evaluate(document, AnimationFrame(0, 24.0))
        defs, chains, filter_ids = svg_module._effect_defs(
            evaluated.elements, svg_module.RenderProfile.preview())
        renderer.cleanup()
        assert defs.startswith("<defs>")
        assert chains["e1"].applied, "blur must resolve on preview profile"
        assert filter_ids["e1"] == "effect_e1"


# --------------------------------------------------------------------------- #
# AUD-32106 — capacity adopted only in the increasing direction
# --------------------------------------------------------------------------- #

class TestAud32106CapacityMonotonic:
    def test_transient_low_capacity_does_not_shrink(self) -> None:
        budget = DecodeWorkerBudget(capacity=8)
        lease = budget.acquire("playback", capacity=1)
        try:
            assert budget.describe()["capacity"] == 8
        finally:
            lease.release()

    def test_higher_capacity_is_adopted_and_clamped(self) -> None:
        budget = DecodeWorkerBudget(capacity=4)
        lease = budget.acquire("playback", capacity=99)
        try:
            assert budget.describe()["capacity"] == 16  # _MAX_WORKERS clamp
        finally:
            lease.release()
        assert budget.describe()["capacity"] == 16  # stays after release

    def test_capacity_none_leaves_capacity_untouched(self) -> None:
        budget = DecodeWorkerBudget(capacity=8)
        lease = budget.acquire("playback")
        try:
            assert budget.describe()["capacity"] == 8
        finally:
            lease.release()


# --------------------------------------------------------------------------- #
# AUD-32205 — ParallelSequentialDecoder global knob restore + dead code
# --------------------------------------------------------------------------- #

class _FakeSliceCodec:
    def __init__(self) -> None:
        self._threads = 8
        self.set_calls: list[int] = []

    def slice_threads(self) -> int:
        return self._threads

    def set_slice_threads(self, n: int) -> int:
        self.set_calls.append(int(n))
        self._threads = int(n)
        return self._threads


class TestAud32205ParallelSequentialDecoder:
    def test_exit_restores_slice_threads_when_instance_set_it(self) -> None:
        codec = _FakeSliceCodec()
        dec = ParallelSequentialDecoder.__new__(ParallelSequentialDecoder)
        dec.__init__("/dev/null", workers=1, threads=1, codec=codec)  # type: ignore[arg-type]
        assert codec.set_calls == [1]
        dec.__exit__(None, None, None)
        assert codec.set_calls == [1, 8], "exit must restore the original value"

    def test_no_threads_argument_never_touches_global_knob(self) -> None:
        codec = _FakeSliceCodec()
        dec = ParallelSequentialDecoder.__new__(ParallelSequentialDecoder)
        dec.__init__("/dev/null", workers=1, threads=None, codec=codec)  # type: ignore[arg-type]
        assert codec.set_calls == []
        dec.__exit__(None, None, None)
        assert codec.set_calls == []

    def test_dead_code_removed(self) -> None:
        source = inspect.getsource(ParallelSequentialDecoder)
        assert "PREFETCH" not in source
        assert "_owns_codec" not in source


# --------------------------------------------------------------------------- #
# AUD-32208 — encode docstring + dead widths
# --------------------------------------------------------------------------- #

class TestAud32208DocsAndDeadCode:
    def test_encode_docstring_references_existing_api(self) -> None:
        doc = ToposCodec.encode.__doc__ or ""
        assert "encode_with_stats" not in doc
        assert "encode_frame" in doc

    def test_decode_has_no_dead_widths_variable(self) -> None:
        source = inspect.getsource(ToposCodec.decode)
        assert "widths" not in source


# --------------------------------------------------------------------------- #
# AUD-32302 / AUD-32303 — write_packet durability + construction gap
# --------------------------------------------------------------------------- #

class TestAud32302DurableDirFsync:
    def test_durable_atomic_write_fsyncs_parent_dir(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        codec = _image_codec_without_native()
        opened: dict[int, str] = {}
        dir_fsyncs: list[int] = []
        real_open, real_fsync = os.open, os.fsync

        def spy_open(path: Any, flags: int, *a: Any, **k: Any) -> int:
            fd = real_open(path, flags, *a, **k)
            opened[fd] = str(path)
            return fd

        def spy_fsync(fd: int) -> None:
            real_fsync(fd)
            dir_fsyncs.append(fd)

        monkeypatch.setattr(os, "open", spy_open)
        monkeypatch.setattr(os, "fsync", spy_fsync)

        target = tmp_path / "frame.toos"
        size = codec.write_packet(str(target), b"PKT", 0, atomic=True, durable=True)
        assert size == 0
        assert target.exists()
        assert not (tmp_path / "frame.toos.toos-tmp").exists()
        parent = str(tmp_path)
        assert any(opened.get(fd) == parent for fd in dir_fsyncs), (
            "durable write must fsync the parent directory entry")

    def test_non_durable_write_skips_dir_fsync(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        codec = _image_codec_without_native()
        dir_fsyncs: list[int] = []
        real_open, real_fsync = os.open, os.fsync
        opened: dict[int, str] = {}

        def spy_open(path: Any, flags: int, *a: Any, **k: Any) -> int:
            fd = real_open(path, flags, *a, **k)
            opened[fd] = str(path)
            return fd

        def spy_fsync(fd: int) -> None:
            real_fsync(fd)
            dir_fsyncs.append(fd)

        monkeypatch.setattr(os, "open", spy_open)
        monkeypatch.setattr(os, "fsync", spy_fsync)

        target = tmp_path / "frame.toos"
        codec.write_packet(str(target), b"PKT", 0, atomic=True, durable=False)
        assert target.exists()
        assert not any(opened.get(fd) == str(tmp_path) for fd in dir_fsyncs)

    def test_dir_fsync_failure_does_not_fail_the_write(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        codec = _image_codec_without_native()
        real_fsync = os.fsync
        # First fsync (the file fd) passes; the second (parent directory)
        # raises EINVAL-style OSError — the write must still succeed.
        counter = {"n": 0}

        def fsync_two_phase(fd: int) -> None:
            counter["n"] += 1
            if counter["n"] == 1:
                real_fsync(fd)
            else:
                raise OSError(22, "Invalid argument")

        monkeypatch.setattr(os, "fsync", fsync_two_phase)
        target = tmp_path / "frame.toos"
        size = codec.write_packet(str(target), b"PKT", 0, atomic=True, durable=True)
        assert size == 0
        assert counter["n"] == 2, "both file and directory fsync attempted"
        assert target.exists(), "a failed dir-fsync must not fail the write"


class TestAud32303ConstructionGapCleanup:
    def test_construction_failure_closes_fd_and_unlinks_tmp(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        codec = _image_codec_without_native()
        opened: list[int] = []
        closed: list[int] = []
        real_open, real_close = os.open, os.close

        def spy_open(path: Any, flags: int, *a: Any, **k: Any) -> int:
            fd = real_open(path, flags, *a, **k)
            opened.append(fd)
            return fd

        def spy_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(os, "open", spy_open)
        monkeypatch.setattr(os, "close", spy_close)

        # create_string_buffer is the unguarded-gap allocation (huge packet →
        # MemoryError). Failure must not leak the fd nor leave the tmp file.
        def boom(*a: Any, **k: Any) -> Any:
            raise MemoryError("packet too large")

        monkeypatch.setattr(ctypes, "create_string_buffer", boom)
        target = tmp_path / "frame.toos"
        with pytest.raises(MemoryError):
            codec.write_packet(str(target), b"PKT", 0, atomic=True, durable=False)
        assert opened, "fd was opened before the failure"
        assert all(fd in closed for fd in opened), "fd leaked"
        assert not (tmp_path / "frame.toos.toos-tmp").exists(), "tmp file leaked"
        assert not target.exists()


# --------------------------------------------------------------------------- #
# AUD-32304 — LUT lazy init under a lock
# --------------------------------------------------------------------------- #

class TestAud32304HalfLutLocking:
    def test_lock_attribute_exists(self) -> None:
        assert isinstance(
            topos_image_binding._LUT_LOCK, type(threading.Lock()))

    def test_frozen_mapping_spot_checks(self) -> None:
        topos_image_binding._HALF_TO_CODE_LUT = None
        codes = topos_image_binding.half_bits_to_codes(
            __import__("numpy").array([0x0000, 0x8000, 0x3C00, 0xBC00],
                                      dtype="<u2"))
        assert codes.tolist() == [0x8000, 0x8000, 0xBC00, 0x4400]

    def test_concurrent_first_build_is_consistent(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        import numpy as np
        monkeypatch.setattr(topos_image_binding, "_HALF_TO_CODE_LUT", None)
        barrier = threading.Barrier(8)
        results: list[Any] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def build() -> None:
            try:
                barrier.wait(timeout=5)
                lut = topos_image_binding._half_to_code_lut()
                with lock:
                    results.append(lut)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=build) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert errors == []
        assert len(results) == 8
        first = results[0]
        assert all(r is first or np.array_equal(r, first) for r in results)
        assert topos_image_binding._HALF_TO_CODE_LUT is not None


# --------------------------------------------------------------------------- #
# AUD-33115 — save_batch_queue(None) preserves persisted settings
# --------------------------------------------------------------------------- #

class TestAud33115SettingsPreservation:
    def test_none_settings_preserve_existing(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
        assert save_batch_queue(
            [BatchQueueItem(file_path="/a.mov")],
            settings={"format": "mov", "quality": "hq"})
        # Second save without settings (items only) must keep the old settings.
        assert save_batch_queue([BatchQueueItem(file_path="/b.mov")], settings=None)
        items, settings = load_batch_queue()
        assert [i.file_path for i in items] == ["/b.mov"]
        assert settings == {"format": "mov", "quality": "hq"}

    def test_explicit_settings_overwrite(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
        save_batch_queue([], settings={"format": "mov"})
        save_batch_queue([], settings={"format": "mp4"})
        _, settings = load_batch_queue()
        assert settings == {"format": "mp4"}

    def test_none_settings_with_no_prior_file_yields_none(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_CONFIG_PATH", str(tmp_path / "config.json"))
        save_batch_queue([BatchQueueItem(file_path="/a.mov")], settings=None)
        items, settings = load_batch_queue()
        assert len(items) == 1
        assert settings is None


# --------------------------------------------------------------------------- #
# AUD-33119 — ExportConfig frozen / typed / non-negative frame count
# --------------------------------------------------------------------------- #

def _video_config() -> VideoEncoderConfig:
    return VideoEncoderConfig(codec="prores", profile="hq")


class TestAud33119ExportConfig:
    def test_frozen(self) -> None:
        config = ExportConfig(video=_video_config())
        with pytest.raises(Exception):
            config.start_frame = 5  # type: ignore[misc]

    def test_framerate_annotated_as_media_rational(self) -> None:
        annotation = ExportConfig.__annotations__["framerate"]
        assert "Any" not in annotation
        assert "Rational" in annotation

    def test_total_frames_normal(self) -> None:
        config = ExportConfig(video=_video_config(), start_frame=0, end_frame=9)
        assert config.get_total_frames(100) == 10

    def test_total_frames_default_total(self) -> None:
        config = ExportConfig(video=_video_config(), start_frame=0, end_frame=-1)
        assert config.get_total_frames(100) == 100

    def test_total_frames_inverted_range_is_zero_not_negative(self) -> None:
        config = ExportConfig(video=_video_config(), start_frame=5, end_frame=2)
        assert config.get_total_frames(100) == 0

    def test_total_frames_start_beyond_default_is_zero(self) -> None:
        config = ExportConfig(video=_video_config(), start_frame=150, end_frame=-1)
        assert config.get_total_frames(100) == 0


# --------------------------------------------------------------------------- #
# AUD-33208 — submit() auto-release on falsy callback return
# --------------------------------------------------------------------------- #

def _surface(lease: _FakeLease, callback: Any) -> GpuEncodeSurface:
    plane = type("P", (), {"api": "metal"})()
    descriptor = GpuEncodeSurfaceDescriptor(
        backend="metal", pixel_format="nv12", width=64, height=64,
        bit_depth=8, device_identity="dev-1", planes=(plane,))
    return GpuEncodeSurface(descriptor, lease, callback)  # type: ignore[arg-type]


class TestAud33208SubmitAutoRelease:
    def test_falsy_return_releases_and_returns_value(self) -> None:
        lease = _FakeLease()
        surface = _surface(lease, lambda s: False)
        assert surface.submit() is False
        assert surface.released
        assert lease.release_count == 1

    def test_truthy_return_keeps_lease(self) -> None:
        lease = _FakeLease()
        surface = _surface(lease, lambda s: "encoder-handle")
        assert surface.submit() == "encoder-handle"
        assert not surface.released
        assert lease.release_count == 0

    def test_raising_callback_still_releases(self) -> None:
        lease = _FakeLease()

        def boom(s: Any) -> Any:
            raise RuntimeError("not accepted")

        surface = _surface(lease, boom)
        with pytest.raises(RuntimeError):
            surface.submit()
        assert surface.released
        assert lease.release_count == 1

    def test_release_is_idempotent_when_caller_also_releases(self) -> None:
        lease = _FakeLease()
        surface = _surface(lease, lambda s: False)
        surface.submit()
        surface.release()
        assert lease.release_count == 1


# --------------------------------------------------------------------------- #
# AUD-33212 — resolution-aware DNxHR recommendation
# --------------------------------------------------------------------------- #

class TestAud33212DnxhrResolution:
    def test_hd_keeps_requested_hq(self) -> None:
        assert (get_dnxhr_profile_for_resolution(1920, 1080, "hq")
                is DNxHRProfile.HQ)

    def test_uhd_upgrades_lower_tiers_to_hqx(self) -> None:
        for level in ("lb", "sq", "hq"):
            assert (get_dnxhr_profile_for_resolution(3840, 2160, level)
                    is DNxHRProfile.HQX)

    def test_uhd_keeps_explicit_hqx_and_444(self) -> None:
        assert (get_dnxhr_profile_for_resolution(3840, 2160, "hqx")
                is DNxHRProfile.HQX)
        assert (get_dnxhr_profile_for_resolution(3840, 2160, "444")
                is DNxHRProfile.RGBA)

    def test_sub_hd_keeps_requested_lb(self) -> None:
        assert (get_dnxhr_profile_for_resolution(640, 360, "lb")
                is DNxHRProfile.LB)

    def test_unknown_quality_defaults(self) -> None:
        assert (get_dnxhr_profile_for_resolution(1920, 1080, "bogus")
                is DNxHRProfile.HQ)
        assert (get_dnxhr_profile_for_resolution(3840, 2160, "bogus")
                is DNxHRProfile.HQX)

    def test_portrait_uhd_also_upgrades(self) -> None:
        assert (get_dnxhr_profile_for_resolution(2160, 3840, "sq")
                is DNxHRProfile.HQX)


# --------------------------------------------------------------------------- #
# AUD-33403 — discard waits for convert workers before teardown
# --------------------------------------------------------------------------- #

class TestAud33403PipelineDiscard:
    def test_discard_waits_for_slow_worker_without_attributeerror(self) -> None:
        enc = object.__new__(ToposVideoEncoder)
        enc._convert_workers = 1
        enc._convert_queue = queue.Queue(maxsize=2)
        enc._converted_queue = queue.Queue()
        enc._converted_map = None
        enc._convert_cond = None
        worker_errors: list[BaseException] = []
        entered = threading.Event()

        def slow_worker() -> None:
            try:
                enc._convert_queue.get()
                entered.set()
                time.sleep(2.5)  # outlives a single join(timeout=2.0) round
                # Old code: _converted_queue was already None here.
                enc._converted_queue.put((0, None, None))
            except BaseException as exc:  # noqa: BLE001 - observed by test
                worker_errors.append(exc)

        enc._convert_queue.put("frame")  # the in-flight conversion
        worker = threading.Thread(target=slow_worker, daemon=True)
        enc._convert_threads = [worker]
        worker.start()
        assert entered.wait(timeout=5.0)

        start = time.monotonic()
        enc._pipeline_discard()
        elapsed = time.monotonic() - start

        worker.join(timeout=10.0)
        assert not worker.is_alive()
        assert worker_errors == [], (
            f"worker crashed after discard: {worker_errors!r}")
        assert elapsed >= 2.0, "discard must wait for the running conversion"
        assert enc._converted_queue is None  # torn down only after exit
        assert enc._convert_threads == []

    def test_discard_with_no_workers_clears_shared_objects(self) -> None:
        enc = object.__new__(ToposVideoEncoder)
        enc._convert_threads = []
        enc._convert_queue = queue.Queue()
        enc._converted_queue = queue.Queue()
        enc._converted_map = None
        enc._convert_cond = None
        enc._pipeline_discard()
        assert enc._converted_queue is None
        assert enc._convert_cond is None


# --------------------------------------------------------------------------- #
# AUD-33405 — locked domain-check counter + one-shot warn flags
# --------------------------------------------------------------------------- #

class TestAud33405ConcurrentCounters:
    def test_domain_check_counter_exact_under_concurrency(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOPOS_ENCODER_FULL_CHECK", raising=False)
        enc = object.__new__(ToposVideoEncoder)
        enc._frames_domain_checked = 0
        enc._domain_stats_lock = threading.Lock()

        per_thread, threads_n = 500, 8
        due_count = {"n": 0}
        due_lock = threading.Lock()
        errors: list[BaseException] = []
        barrier = threading.Barrier(threads_n)

        def run() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(per_thread):
                    if enc._should_full_domain_check():
                        with due_lock:
                            due_count["n"] += 1
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        total = per_thread * threads_n
        assert errors == []
        assert enc._frames_domain_checked == total, "lost update under race"
        assert due_count["n"] == (total + 255) // 256, "sampling schedule drifted"

    def test_claim_warn_has_exactly_one_winner(self) -> None:
        enc = object.__new__(ToposVideoEncoder)
        enc._domain_stats_lock = threading.Lock()
        enc._opaque_alpha_warned = False

        winners = {"n": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def run() -> None:
            barrier.wait(timeout=5)
            if enc._claim_warn("_opaque_alpha_warned"):
                with lock:
                    winners["n"] += 1

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert winners["n"] == 1
        assert enc._opaque_alpha_warned is True

    def test_claim_warn_resets_between_flags(self) -> None:
        enc = object.__new__(ToposVideoEncoder)
        enc._domain_stats_lock = threading.Lock()
        enc._drop_alpha_warned = False
        assert enc._claim_warn("_drop_alpha_warned") is True
        assert enc._claim_warn("_drop_alpha_warned") is False
