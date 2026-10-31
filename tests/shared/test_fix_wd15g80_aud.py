"""wd15-g80 批 D 修复验证（REMEDIATION_PLAN 16 条，shared/ai_models 上半）。

覆盖条目：AUD-31501 / 31502(INVALID 复核) / 31510 / 31511 / 31520 / 31521 /
31522 / 31523 / 31600 / 31601 / 31610 / 31611 / 31614 / 31615 / 31616 / 31620。

ONNX / Keychain / 云端 transport 全部 mock，不发网络、不读真实模型权重、
不触碰用户 Keychain。与 tests/shared/test_fix_core_aud4.py（AUD-31500）
及 tests/edit/ai_editing/* 互补，不重复既有断言。
"""

from __future__ import annotations

import ast
import hashlib
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from PyQt6.QtCore import QMutex

pytestmark = pytest.mark.regression


def _method_of(tree: ast.Module, class_name: str, method: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == method:
                    return fn
    return None


# ---------------------------------------------------------------------------
# AUD-31501：AIPluginWorker 类级会话缓存互斥
# ---------------------------------------------------------------------------


@pytest.fixture()
def plugin_mod():
    import src.shared.ai_models.ai_plugin_engine as mod

    mod.AIPluginWorker._session_cache.clear()
    mod.AIPluginWorker._current_model_path = None
    yield mod
    mod.AIPluginWorker._session_cache.clear()
    mod.AIPluginWorker._current_model_path = None


class _FakeOnnxInput:
    name = "input"


class _FakePluginSession:
    def __init__(self, tag: str = "fake") -> None:
        self.tag = tag

    def get_inputs(self):
        return [_FakeOnnxInput()]

    def get_providers(self):
        return ["CPUExecutionProvider"]


class TestAud31501SessionCacheLock:
    def test_cache_mutex_exists(self, plugin_mod):
        assert isinstance(plugin_mod.AIPluginWorker._cache_mutex, QMutex)

    def test_model_load_runs_outside_the_cache_mutex(self, plugin_mod, monkeypatch):
        """Loading can take minutes — it must NOT hold the cache mutex."""
        observed_locked: list[bool] = []

        class _ProbeSession(_FakePluginSession):
            def __init__(self) -> None:
                super().__init__("probe")
                # tryLock succeeds only when nobody (e.g. _load_session)
                # holds the mutex during the load.
                observed_locked.append(
                    not plugin_mod.AIPluginWorker._cache_mutex.tryLock()
                )
                if not observed_locked[-1]:
                    plugin_mod.AIPluginWorker._cache_mutex.unlock()

        monkeypatch.setattr(
            plugin_mod.ort, "InferenceSession", lambda *a, **k: _ProbeSession()
        )
        worker = plugin_mod.AIPluginWorker(
            "l", "i", np.zeros((2, 2, 3), np.float32), {}, {}
        )
        worker._load_session("/models/a.onnx")
        assert observed_locked == [False]

    def test_concurrent_loads_keep_cache_consistent(self, plugin_mod, monkeypatch):
        """Worker threads for different instances must never leave the
        cache cleared-while-in-use or pointing at the wrong model."""
        loads: list[str] = []
        lock = threading.Lock()

        def fake_inference_session(path, *a, **k):
            with lock:
                loads.append(path)
            time.sleep(0.01)  # widen the historical race window
            return _FakePluginSession(path)

        monkeypatch.setattr(plugin_mod.ort, "InferenceSession", fake_inference_session)

        workers = [
            plugin_mod.AIPluginWorker("l", str(i), np.zeros((2, 2, 3), np.float32), {}, {})
            for i in range(4)
        ]
        barrier = threading.Barrier(4)

        def target(worker, model):
            barrier.wait()
            worker._load_session(model)

        threads = [
            threading.Thread(target=target, args=(w, f"/models/{i % 2}.onnx"))
            for i, w in enumerate(workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        current = plugin_mod.AIPluginWorker._current_model_path
        assert current in {"/models/0.onnx", "/models/1.onnx"}
        # Only the current model stays cached (switch clears the other).
        assert set(plugin_mod.AIPluginWorker._session_cache.keys()) == {current}
        assert len(loads) >= 1

    def test_process_no_longer_touches_class_cache(self, plugin_mod):
        """Model-switch cleanup moved into _load_session's critical
        section: _process must not manipulate the class-level cache."""
        source = Path(plugin_mod.__file__).read_text(encoding="utf-8")
        process_fn = _method_of(ast.parse(source), "AIPluginWorker", "_process")
        assert process_fn is not None
        cache_touches = [
            n for n in ast.walk(process_fn)
            if isinstance(n, ast.Attribute)
            and n.attr in {"_session_cache", "_current_model_path"}
        ]
        assert cache_touches == [], (
            "_process still manipulates the class-level session cache — "
            "model-switch cleanup must live inside _load_session's mutex"
        )


# ---------------------------------------------------------------------------
# AUD-31502（INVALID 复核）：协作取消检查点已随 AUD-31500 修复存在
# ---------------------------------------------------------------------------


class TestAud31502InvalidVerification:
    def test_tiled_loops_have_cancel_checkpoints(self, plugin_mod):
        """Evidence for INVALID: both tile loops already call
        _check_cancelled() per tile (remediated together with AUD-31500)."""
        source = Path(plugin_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for method in ("_process_tiled", "_process_upscale"):
            fn = _method_of(tree, "AIPluginWorker", method)
            assert fn is not None
            checkpoints = [
                n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_check_cancelled"
            ]
            assert len(checkpoints) >= 1, (
                f"{method} has no cooperative-cancel checkpoint (AUD-31502)"
            )


# ---------------------------------------------------------------------------
# AUD-31510 / AUD-31511：AIMaskWorker SAM 状态一致性与增量解码
# ---------------------------------------------------------------------------


class _FakeSamDecoder:
    """Deterministic per-point SAM decoder stub.

    ``run`` returns a sigmoid mask whose selected region encodes the
    prompt point, so results compose like the real brush-accumulation.
    """

    def __init__(self, img_shape=(8, 12), input_size=(1024, 1024)) -> None:
        self.calls: list[tuple[float, float, float]] = []
        self.h, self.w = img_shape
        self.input_size = input_size

    def run(self, _names, inputs):
        pt = inputs["point_coords"][0, 0]
        label = inputs["point_labels"][0, 0]
        self.calls.append((float(pt[0]), float(pt[1]), float(label)))
        # Coordinates arrive in the 1024-space; map back to unit space.
        scale = self.input_size[0] / max(self.h, self.w)
        x = pt[0] / (self.w * scale) if self.w else 0.0
        y = pt[1] / (self.h * scale) if self.h else 0.0
        ys, xs = np.mgrid[0:256, 0:256]
        inside = ((xs / 256.0 - x) ** 2 + (ys / 256.0 - y) ** 2) < 0.02
        logits = np.where(inside, 4.0, -4.0).astype(np.float32)
        masks = 1 / (1 + np.exp(-logits))
        batch = np.repeat(masks[None], 3, axis=0)[None]  # (1, 3, 256, 256)
        ious = np.array([[0.9, 0.1, 0.1]], dtype=np.float32)
        return [batch, ious]


class TestAud31510IncrementalSamDecode:
    def _worker(self, qapp, img_shape=(8, 12)):
        from src.shared.ai_models.ai_mask_engine import AIMaskWorker

        worker = AIMaskWorker()
        worker.sam_img_shape = img_shape
        worker.sam_embedding = np.zeros((1, 4, 64, 64), dtype=np.float32)
        decoder = _FakeSamDecoder(img_shape=img_shape)
        worker.session_sam_dec = decoder
        return worker, decoder

    def test_extended_point_list_only_decodes_new_points(self, qapp):
        worker, decoder = self._worker(qapp)
        emitted: list[np.ndarray] = []
        worker.mask_ready.connect(lambda m: emitted.append(m))

        points1 = [(0.3, 0.5), (0.7, 0.5)]
        labels1 = [1, 1]
        worker.process_sam_decode(list(points1), list(labels1))
        assert len(decoder.calls) == 2

        # Caller re-emits the FULL list with two extra points (real
        # image_engine behaviour): only the 2 new points may be decoded.
        points2 = points1 + [(0.5, 0.2), (0.5, 0.8)]
        labels2 = labels1 + [1, 1]
        worker.process_sam_decode(list(points2), list(labels2))
        assert len(decoder.calls) == 4  # 2 cached + 2 new, not 6

        # Incremental result must equal a from-scratch recomputation.
        fresh_worker, fresh_decoder = self._worker(qapp)
        fresh_worker.sam_embedding = worker.sam_embedding
        fresh_worker.sam_img_shape = worker.sam_img_shape
        fresh_emitted: list[np.ndarray] = []
        fresh_worker.mask_ready.connect(lambda m: fresh_emitted.append(m))
        fresh_worker.process_sam_decode(list(points2), list(labels2))
        assert len(fresh_decoder.calls) == 4
        np.testing.assert_allclose(emitted[-1], fresh_emitted[-1])

    def test_undo_shrinks_prefix_and_forces_full_recompute(self, qapp):
        worker, decoder = self._worker(qapp)
        worker.process_sam_decode([(0.3, 0.5), (0.7, 0.5)], [1, 1])
        assert len(decoder.calls) == 2
        # A list whose tail changed is not an extension of the cached
        # prefix → full recompute.
        worker.process_sam_decode([(0.3, 0.5), (0.4, 0.4)], [1, 1])
        assert len(decoder.calls) == 4

    def test_reencoded_embedding_invalidates_cache(self, qapp):
        worker, decoder = self._worker(qapp)
        worker.process_sam_decode([(0.3, 0.5)], [1])
        assert len(decoder.calls) == 1
        # New encode (new image) → new embedding object → prefix invalid.
        worker.sam_embedding = np.zeros((1, 4, 64, 64), dtype=np.float32)
        worker.process_sam_decode([(0.3, 0.5), (0.6, 0.6)], [1, 1])
        assert len(decoder.calls) == 3  # full recompute

    def test_positive_and_negative_points_via_same_cache(self, qapp):
        worker, decoder = self._worker(qapp)
        emitted: list[np.ndarray] = []
        worker.mask_ready.connect(lambda m: emitted.append(m))
        worker.process_sam_decode([(0.3, 0.5)], [1])
        worker.process_sam_decode([(0.3, 0.5), (0.7, 0.5)], [1, 0])
        assert len(decoder.calls) == 2
        # A negative point subtracts from the union.
        assert emitted[-1].max() <= 1.0 and np.isfinite(emitted[-1]).all()


class TestAud31511EncodeStateConsistency:
    def _worker_with_failing_encoder(self):
        from src.shared.ai_models.ai_mask_engine import AIMaskWorker

        class _FailingEncoder:
            def get_inputs(self):
                return [_FakeOnnxInput()]

            def run(self, _names, feed):
                raise RuntimeError("encoder exploded")

        worker = AIMaskWorker()
        worker.session_sam_enc = _FailingEncoder()
        return worker

    def test_failed_encode_invalidates_stale_embedding(self, qapp):
        worker = self._worker_with_failing_encoder()
        # Previous image state: valid embedding + shape.
        worker.sam_embedding = np.ones((1, 4, 64, 64), dtype=np.float32)
        worker.sam_img_shape = (64, 64)
        errors: list[str] = []
        worker.error.connect(lambda msg: errors.append(msg))

        worker.process_sam_encode(np.zeros((8, 12, 3), dtype=np.uint8))

        assert errors, "encode failure must surface via error signal"
        # AUD-31511: the stale OLD embedding must be gone — a later decode
        # must refuse instead of pairing old embedding with the new shape.
        assert worker.sam_embedding is None
        assert worker._sam_decode_cache is None

    def test_cancelled_encode_does_not_commit_new_state(self, qapp):
        from src.shared.ai_models.ai_mask_engine import AIMaskWorker

        worker = AIMaskWorker()
        worker.sam_embedding = np.ones((1, 4, 64, 64), dtype=np.float32)
        worker.sam_img_shape = (64, 64)
        encoded: list[bool] = []
        worker.sam_encode_finished.connect(lambda: encoded.append(True))

        class _CancelMidRunEncoder:
            def get_inputs(self):
                return [_FakeOnnxInput()]

            def run(self, _names, feed):
                # User cancels while the native encoder call is in flight;
                # the run itself still succeeds.
                worker.request_cancel()
                return [np.zeros((1, 4, 64, 64), dtype=np.float32)]

        worker.session_sam_enc = _CancelMidRunEncoder()
        worker.process_sam_encode(np.zeros((8, 12, 3), dtype=np.uint8))

        assert encoded == []
        assert worker.sam_embedding is None
        assert worker.sam_img_shape == (64, 64)  # unchanged until success

    def test_successful_encode_commits_shape_and_embedding(self, qapp):
        from src.shared.ai_models.ai_mask_engine import AIMaskWorker

        embedding = np.zeros((1, 4, 64, 64), dtype=np.float32)

        class _OkEncoder:
            def get_inputs(self):
                return [_FakeOnnxInput()]

            def run(self, _names, feed):
                return [embedding]

        worker = AIMaskWorker()
        worker.session_sam_enc = _OkEncoder()
        encoded: list[bool] = []
        worker.sam_encode_finished.connect(lambda: encoded.append(True))

        worker.process_sam_encode(np.zeros((9, 15, 3), dtype=np.uint8))

        assert encoded == [True]
        assert worker.sam_img_shape == (9, 15)
        assert worker.sam_embedding is embedding


# ---------------------------------------------------------------------------
# AUD-31520 / 31521 / 31522 / 31523：denoise_worker 控制器生命周期
# ---------------------------------------------------------------------------


@pytest.fixture()
def denoise_mod(monkeypatch):
    import src.shared.ai_models.denoise_worker as mod

    monkeypatch.setattr(
        mod.AIDenoiseWorker, "_load_onnx_session",
        lambda self, path: _FakePluginSession("denoise"),
    )
    return mod


def _pump_until(qapp, predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "event-loop condition not reached"
        qapp.processEvents()
        time.sleep(0.01)


class TestAud31520StaleThreadReference:
    def test_refs_cleared_and_second_run_does_not_raise(
        self, denoise_mod, qapp, tmp_path, monkeypatch,
    ):
        finished_arrays: list[np.ndarray] = []
        result = np.full((4, 4, 3), 0.5, dtype=np.float32)
        monkeypatch.setattr(
            denoise_mod.AIDenoiseWorker, "_process_single",
            lambda self, img, session: result,
        )
        controller = denoise_mod.AIDenoiseThreadController()
        controller.finished.connect(lambda arr: finished_arrays.append(arr))

        model = tmp_path / "m.onnx"
        model.write_bytes(b"x")
        params = {"model_path": str(model), "use_tiling": False}
        image = np.zeros((4, 4, 3), np.float32)

        controller.start_processing(image, params)
        _pump_until(qapp, lambda: len(finished_arrays) == 1)
        # deleteLater + the queued finished slot must clear both refs.
        _pump_until(qapp, lambda: controller._thread is None and controller._worker is None)

        assert controller.is_running() is False  # no RuntimeError on deleted wrapper
        controller.start_processing(image, params)
        _pump_until(qapp, lambda: len(finished_arrays) == 2)
        controller.wait()  # safe on cleared refs


class TestAud31521CancelledSignal:
    def test_abort_reports_cancelled_not_fatal_error(
        self, denoise_mod, qapp, tmp_path, monkeypatch,
    ):
        worker = denoise_mod.AIDenoiseWorker()
        cancelled: list[bool] = []
        errors: list[str] = []
        finished: list[np.ndarray] = []
        worker.cancelled.connect(lambda: cancelled.append(True))
        worker.error.connect(lambda msg: errors.append(msg))
        worker.finished.connect(lambda arr: finished.append(arr))

        def load_then_cancel(self, path):
            self.abort()  # user cancels while the model is loading
            return _FakePluginSession("denoise")

        monkeypatch.setattr(
            denoise_mod.AIDenoiseWorker, "_load_onnx_session", load_then_cancel
        )
        model = tmp_path / "m.onnx"
        model.write_bytes(b"x")
        worker.process(np.zeros((4, 4, 3), np.float32),
                       {"model_path": str(model), "use_tiling": False})

        assert cancelled == [True]
        assert errors == [], "cancellation must not surface as 发生致命错误"
        assert finished == []

    def test_real_failure_still_reports_error(
        self, denoise_mod, qapp, tmp_path, monkeypatch,
    ):
        worker = denoise_mod.AIDenoiseWorker()
        errors: list[str] = []
        cancelled: list[bool] = []
        worker.error.connect(lambda msg: errors.append(msg))
        worker.cancelled.connect(lambda: cancelled.append(True))

        def boom(self, img, session):
            raise ValueError("real inference failure")

        monkeypatch.setattr(denoise_mod.AIDenoiseWorker, "_process_single", boom)
        model = tmp_path / "m.onnx"
        model.write_bytes(b"x")
        worker.process(np.zeros((4, 4, 3), np.float32),
                       {"model_path": str(model), "use_tiling": False})

        assert len(errors) == 1
        assert "real inference failure" in errors[0]
        assert cancelled == []


class TestAud31522NonBlockingStart:
    def test_reentry_returns_immediately_and_queues(
        self, denoise_mod, qapp, tmp_path, monkeypatch,
    ):
        release = threading.Event()
        runs: list[int] = []
        results: list[np.ndarray] = []
        cancelled: list[bool] = []

        def slow_single(self, img, session):
            runs.append(1)
            release.wait(timeout=5.0)  # simulate one long inference tile
            return np.full((4, 4, 3), 0.25, dtype=np.float32)

        monkeypatch.setattr(denoise_mod.AIDenoiseWorker, "_process_single", slow_single)
        controller = denoise_mod.AIDenoiseThreadController()
        controller.finished.connect(lambda arr: results.append(arr))
        controller.cancelled.connect(lambda: cancelled.append(True))

        model = tmp_path / "m.onnx"
        model.write_bytes(b"x")
        image = np.zeros((4, 4, 3), np.float32)
        params = {"model_path": str(model), "use_tiling": False}
        controller.start_processing(image, params)

        _pump_until(qapp, lambda: bool(runs))
        start = time.monotonic()
        controller.start_processing(image, params)  # re-entry during a run
        elapsed = time.monotonic() - start
        assert elapsed < 0.2, "start_processing must not block on wait()"

        release.set()
        # First run is cancelled at its post-inference checkpoint; the
        # queued request then runs to completion.
        _pump_until(qapp, lambda: len(runs) == 2 and len(results) == 1)
        assert cancelled == [True]
        _pump_until(qapp, lambda: controller._thread is None)


class TestAud31523OverlapOneRamp:
    def test_overlap_one_produces_finite_output(self, denoise_mod):
        worker = denoise_mod.AIDenoiseWorker()

        class _OnesSession(_FakePluginSession):
            def run(self, _names, feed):
                tile = feed["input"]
                return [np.ones_like(tile)]

        img = np.ones((8, 8, 3), dtype=np.float32)
        out = worker._process_tiled(img, _OnesSession("ones"), tile_size=4, overlap=1)
        assert np.isfinite(out).all(), "overlap==1 must not produce NaN weights"
        np.testing.assert_allclose(out, 1.0, atol=1e-4)


# ---------------------------------------------------------------------------
# AUD-31600 / AUD-31601：凭据存储
# ---------------------------------------------------------------------------


@pytest.fixture()
def cred_mod():
    from src.shared.ai_models.generation import credential_store as mod

    return mod


class TestAud31600SecurityCliStdin:
    def test_set_password_feeds_secret_via_stdin_not_argv(self, cred_mod, monkeypatch):
        calls: list[tuple[list, dict]] = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(cred_mod.subprocess, "run", fake_run)
        backend = cred_mod._MacOSSecurityKeychain()

        backend.set_password("topos.ai-editing", "topos:demo", "sk-secret-123")

        argv, kwargs = calls[0]
        assert argv == ["security", "-i"]
        assert "sk-secret-123" not in argv
        assert kwargs["input"] == (
            'add-generic-password -U -s "topos.ai-editing" '
            '-a "topos:demo" -w "sk-secret-123"\n'
        )

    def test_special_characters_are_escaped(self, cred_mod, monkeypatch):
        calls: list[tuple[list, dict]] = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(cred_mod.subprocess, "run", fake_run)
        backend = cred_mod._MacOSSecurityKeychain()

        backend.set_password("svc", "user", 'pa"ss\\word 2')

        command = calls[0][1]["input"]
        assert command.count("\n") == 1  # exactly one interactive command line
        assert '"pa\\"ss\\\\word 2"' in command

    def test_newline_secret_rejected(self, cred_mod):
        backend = cred_mod._MacOSSecurityKeychain()
        with pytest.raises(ValueError):
            backend.set_password("svc", "user", "bad\nsecret")

    def test_keychain_write_failure_raises(self, cred_mod, monkeypatch):
        monkeypatch.setattr(
            cred_mod.subprocess, "run",
            lambda *a, **k: SimpleNamespace(
                returncode=1, stdout="add-generic-password: returned -25299\n",
                stderr="security: some keychain error",
            ),
        )
        backend = cred_mod._MacOSSecurityKeychain()
        with pytest.raises(RuntimeError):
            backend.set_password("svc", "user", "secret")


class _RecordingSecureStore:
    durable_backend_available = True

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.secrets: dict[str, str] = dict(initial or {})
        self.deleted: list[str] = []

    def get_secret(self, ref: str) -> str | None:
        return self.secrets.get(ref)

    def set_secret(self, ref: str, secret: str) -> None:
        self.secrets[ref] = secret

    def delete_secret(self, ref: str) -> None:
        self.deleted.append(ref)
        self.secrets.pop(ref, None)


class TestAud31601HybridStaleShadowing:
    def _file_store(self, tmp_path):
        from src.shared.ai_models.generation.credential_store import (
            FileCredentialStore,
        )

        return FileCredentialStore(tmp_path / "ai_profiles.json")

    def test_file_mode_write_removes_stale_keychain_copy(self, tmp_path):
        from src.shared.ai_models.generation.credential_store import (
            HybridCredentialStore,
        )

        secure = _RecordingSecureStore({"topos:demo": "old-rotated-key"})
        file_store = self._file_store(tmp_path)
        hybrid = HybridCredentialStore(secure, file_store, prefer="file")

        hybrid.set_secret("topos:demo", "new-key")

        assert file_store.get_secret("topos:demo") == "new-key"
        assert secure.secrets == {}, "stale keychain key must be removed"

        # Preference switch back to keychain: the shadowing key is gone,
        # the read falls through to the fresh file value.
        keychain_first = HybridCredentialStore(secure, file_store, prefer="keychain")
        assert keychain_first.get_secret("topos:demo") == "new-key"

    def test_keychain_mode_durable_write_removes_file_copy(self, tmp_path):
        from src.shared.ai_models.generation.credential_store import (
            HybridCredentialStore,
        )

        secure = _RecordingSecureStore()
        file_store = self._file_store(tmp_path)
        file_store.set_secret("topos:demo", "legacy-file-key")
        hybrid = HybridCredentialStore(secure, file_store, prefer="keychain")

        hybrid.set_secret("topos:demo", "fresh-keychain-key")

        assert secure.secrets == {"topos:demo": "fresh-keychain-key"}
        assert file_store.get_secret("topos:demo") is None

    def test_keychain_mode_memory_fallback_keeps_file_copy(self, tmp_path):
        from src.shared.ai_models.generation.credential_store import (
            HybridCredentialStore,
        )

        secure = _RecordingSecureStore()
        secure.durable_backend_available = False
        file_store = self._file_store(tmp_path)
        hybrid = HybridCredentialStore(secure, file_store, prefer="keychain")

        hybrid.set_secret("topos:demo", "volatile-key")

        assert file_store.get_secret("topos:demo") == "volatile-key"


# ---------------------------------------------------------------------------
# AUD-31610 / AUD-31611：image_gen_job 状态机与缓存键
# ---------------------------------------------------------------------------


def _gen_image(h=8, w=12, seed=0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random((h, w, 4)).astype(np.float32)


class TestAud31610HonestStateMachine:
    def _job(self, transport):
        from src.shared.ai_models.generation.image_gen_job import CloudImageGenJob

        return CloudImageGenJob(
            prompt="p", image=_gen_image(),
            transport=transport, api_key_provider=lambda: "key",
        )

    def test_state_is_uploading_during_transport(self):
        from src.shared.ai_models.generation.image_gen_job import UPLOADING

        observed: list[str] = []
        job = self._job(None)

        def probe_transport(request, key, size):
            observed.append(job.state.state)
            return _gen_image(seed=9)

        job.transport = probe_transport
        state, _ = job.run()
        assert state == "DONE"
        assert observed == [UPLOADING], (
            "during the network call the observable state must be UPLOADING, "
            "not a pre-jumped DOWNLOADING (AUD-31610)"
        )

    def test_failure_history_records_phase_it_died_in(self):
        from src.shared.ai_models.generation.image_gen_job import (
            FAILED, UPLOADING,
        )

        def boom(request, key, size):
            raise RuntimeError("upload rejected")

        job = self._job(boom)
        state, image = job.run()
        assert state == FAILED and image is None
        assert job.state.history == ["QUEUED", UPLOADING, FAILED]

    def test_stage_reporting_transport_advances_live_state(self):
        from src.shared.ai_models.generation.image_gen_job import (
            DOWNLOADING, RUNNING, UPLOADING,
        )

        observed: list[str] = []
        job = self._job(None)

        def reporting_transport(request, key, size):
            observed.append(job.state.state)
            request["on_stage"](RUNNING)
            observed.append(job.state.state)
            request["on_stage"](DOWNLOADING)
            observed.append(job.state.state)
            return _gen_image(seed=3)

        job.transport = reporting_transport
        state, _ = job.run()
        assert state == "DONE"
        assert observed == [UPLOADING, RUNNING, DOWNLOADING]
        assert job.state.history == ["QUEUED", "UPLOADING", "RUNNING",
                                     "DOWNLOADING", "DONE"]

    def test_opaque_transport_keeps_full_history_chain(self):
        job = self._job(lambda r, k, s: _gen_image(seed=5))
        state, _ = job.run()
        assert state == "DONE"
        assert job.state.history == ["QUEUED", "UPLOADING", "RUNNING",
                                     "DOWNLOADING", "DONE"]


class TestAud31611ChunkedCacheKey:
    def test_array_key_matches_bytes_key(self):
        from src.shared.ai_models.generation.image_gen_job import cache_key

        arr = _gen_image(seed=11)
        as_bytes = np.ascontiguousarray(arr.astype(np.float32)).tobytes()
        mask = _gen_image(seed=12)[..., 0]

        k_arr = cache_key("p", "", "m", 1, 1.0, arr, mask, "12x8")
        k_bytes = cache_key(
            "p", "", "m", 1, 1.0, as_bytes,
            np.ascontiguousarray(mask.astype(np.float32)).tobytes(), "12x8",
        )
        assert k_arr == k_bytes  # byte-identical digest → old caches stay valid

    def test_non_float32_input_matches_legacy_conversion(self):
        from src.shared.ai_models.generation.image_gen_job import cache_key

        u8 = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
        legacy = np.ascontiguousarray(u8.astype(np.float32)).tobytes()
        assert cache_key("p", "", "m", 0, 1.0, u8) == \
            cache_key("p", "", "m", 0, 1.0, legacy)

    def test_constructor_no_longer_materializes_tobytes(self):
        import src.shared.ai_models.generation.image_gen_job as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        init_fn = _method_of(ast.parse(source), "CloudImageGenJob", "__init__")
        assert init_fn is not None
        tobytes_calls = [
            n for n in ast.walk(init_fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "tobytes"
        ]
        assert tobytes_calls == [], (
            "__init__ still builds full-frame byte copies for the cache key"
        )


# ---------------------------------------------------------------------------
# AUD-31614 / 31615 / 31616：model_pack_manager 校验
# ---------------------------------------------------------------------------


def _reference_dir_digest(root: Path) -> str:
    """Sorted-walk reference implementation of the directory digest."""
    h = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            fp = Path(base) / name
            rel = fp.relative_to(root).as_posix()
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(fp.read_bytes())
    return h.hexdigest()


def _make_pack(tmp_path: Path, name: str = "pack") -> Path:
    pack = tmp_path / name
    (pack / "sub_a").mkdir(parents=True)
    (pack / "sub_b").mkdir(parents=True)
    (pack / "sub_a" / "weights.bin").write_bytes(b"aaa" * 100)
    (pack / "sub_b" / "cfg.json").write_bytes(b'{"v": 1}')
    (pack / "top.txt").write_bytes(b"top")
    return pack


@pytest.fixture()
def pack_mod():
    import src.shared.ai_models.generation.model_pack_manager as mod

    return mod


def _manager_with_pack(pack_mod, tmp_path, digest):
    return pack_mod.ModelPackManager(
        cache_dir=tmp_path / "cache",
        specs=(
            pack_mod.ModelPackSpec(
                pack_id="test_pack",
                capability="transcript",
                version="t",
                size_bytes=1,
                sha256=f"sha256:{digest}",
                languages=("en",),
                requires_gpu=False,
                min_vram_mb=0,
                license=pack_mod.ModelPackLicense(
                    name="T", commercial_use=True, redistribution=True,
                    attribution_required=False,
                ),
            ),
        ),
    )


class TestAud31614VerificationCache:
    def test_second_get_status_skips_rehash(
        self, pack_mod, tmp_path, monkeypatch,
    ):
        pack = _make_pack(tmp_path)
        digest = _reference_dir_digest(pack)
        manager = _manager_with_pack(pack_mod, tmp_path, digest)
        assert manager.mark_installed("test_pack", pack)

        calls = {"n": 0}
        real_verify = pack_mod.ModelPackManager._verify_dir

        def counting_verify(install_path, expected, *, allow_demo=False):
            calls["n"] += 1
            return real_verify(install_path, expected, allow_demo=allow_demo)

        monkeypatch.setattr(
            pack_mod.ModelPackManager, "_verify_dir", staticmethod(counting_verify)
        )

        assert manager.get_status("test_pack").verified is True
        assert calls["n"] == 1
        # Unchanged directory → fingerprint cache hit, no re-hash — even
        # across get_status AND require_usable (the pre-inference paths).
        assert manager.get_status("test_pack").verified is True
        assert manager.require_usable("test_pack") == str(pack)
        assert calls["n"] == 1

        # Directory content change → fingerprint miss → full re-hash.
        (pack / "sub_a" / "weights.bin").write_bytes(b"bbb" * 100)
        status = manager.get_status("test_pack")
        assert calls["n"] == 2
        assert status.verified is False

    def test_explicit_verify_rehashes_then_refreshes_cache(
        self, pack_mod, tmp_path, monkeypatch,
    ):
        pack = _make_pack(tmp_path)
        digest = _reference_dir_digest(pack)
        manager = _manager_with_pack(pack_mod, tmp_path, digest)
        assert manager.mark_installed("test_pack", pack)
        assert manager.get_status("test_pack").verified is True

        calls = {"n": 0}
        real_verify = pack_mod.ModelPackManager._verify_dir

        def counting_verify(install_path, expected, *, allow_demo=False):
            calls["n"] += 1
            return real_verify(install_path, expected, allow_demo=allow_demo)

        monkeypatch.setattr(
            pack_mod.ModelPackManager, "_verify_dir", staticmethod(counting_verify)
        )
        # Explicit verify must re-hash even though nothing changed...
        assert manager.verify("test_pack") is True
        assert calls["n"] == 1
        # ... and refresh the cached result for the following get_status.
        assert manager.get_status("test_pack").verified is True
        assert calls["n"] == 1


class TestAud31615SortedTraversal:
    def test_digest_is_traversal_order_independent(self, tmp_path, monkeypatch):
        import src.shared.ai_models.generation.model_pack_manager as mod

        pack = _make_pack(tmp_path)
        digest = _reference_dir_digest(pack)

        # Force os.walk to yield subdirectories in REVERSE order: before
        # AUD-31615 the digest depended on this enumeration order.
        real_walk = os.walk

        def reversed_walk(top, **kwargs):
            for root, dirs, files in real_walk(top, **kwargs):
                yield root, sorted(dirs, reverse=True), files

        monkeypatch.setattr(os, "walk", reversed_walk)
        assert mod.ModelPackManager._verify_dir(str(pack), f"sha256:{digest}")
        monkeypatch.undo()
        assert mod.ModelPackManager._verify_dir(str(pack), f"sha256:{digest}")


class TestAud31616VerifySuccessPersistence:
    def test_verify_success_updates_record_and_manifest(self, pack_mod, tmp_path):
        pack = _make_pack(tmp_path)
        digest = _reference_dir_digest(pack)
        manager = _manager_with_pack(pack_mod, tmp_path, digest)
        assert manager.mark_installed("test_pack", pack)

        # Simulate the stale-record case: the release manifest updated the
        # digest AFTER installation (record.sha256 no longer matches spec).
        manager._installed["test_pack"].sha256 = ""
        assert manager.get_status("test_pack").verified is False

        assert manager.verify("test_pack") is True
        assert manager.get_status("test_pack").verified is True
        assert manager.require_usable("test_pack") == str(pack)

        # The corrected truth persists to the manifest for fresh instances.
        fresh = _manager_with_pack(pack_mod, tmp_path, digest)
        assert fresh.get_status("test_pack").verified is True


# ---------------------------------------------------------------------------
# AUD-31620：shared 层不得反向 import features.edit
# ---------------------------------------------------------------------------


class TestAud31620NoReverseImport:
    def test_provider_profiles_has_no_features_import(self):
        import src.shared.ai_models.generation.provider_profiles as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        offenders = [
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.ImportFrom)
            and isinstance(n.module, str)
            and n.module.startswith("src.features")
        ]
        assert offenders == [], "shared generation layer must not import features"

    def test_shared_inference_matches_features_catalog(self):
        """Pin the duplicated inference table against drift."""
        from src.features.edit.ai_editing.ai_provider_catalog import (
            infer_capabilities,
        )
        from src.shared.ai_models.generation.provider_profiles import (
            _infer_capabilities,
        )

        kinds = [
            "openai", "anthropic", "ollama", "local_http", "openai_compat",
            "volcengine_plan_tts", "local_qwen_tts", "local_qwen_asr",
            "embedding", "openai_embedding", "unknown_kind", "",
        ]
        modality_sets = [
            (), ("text",), ("tool_call", "text"), ("image", "video"),
            ("audio",), ("voice",), ("asr",), ("embedding",),
            ("file", "media_upload"), ("image", "voice", "text"),
        ]
        for kind in kinds:
            for modalities in modality_sets:
                assert _infer_capabilities(kind, modalities) == \
                    infer_capabilities(kind, modalities), (
                        f"capability inference drifted for {kind!r}/{modalities!r}"
                    )

    def test_from_dict_infers_capabilities_without_features_layer(self):
        from src.shared.ai_models.generation.provider_profiles import (
            AiProviderProfile,
        )

        profile = AiProviderProfile.from_dict({
            "profile_id": "legacy",
            "display_name": "Legacy",
            "provider_kind": "ollama",
        })
        assert "language" in profile.capabilities

    def test_set_active_routes_inferred_capabilities(self):
        from src.shared.ai_models.generation.provider_profiles import (
            AiProviderProfile,
            ProfileRegistry,
        )

        registry = ProfileRegistry()
        registry.add(AiProviderProfile(
            profile_id="tts1", display_name="TTS",
            provider_kind="volcengine_plan_tts",
        ))
        registry.set_active("tts1")
        assert registry.active_profile_for_capability("tts") is not None
