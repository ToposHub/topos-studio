"""Background Model3D import service tests (no Qt/GL required)."""

import threading
import time

import pytest

from src.features.composite.render3d import model_loader
from src.features.composite.render3d.model_import_service import (
    IMPORT_CANCELLED,
    IMPORT_FAILED,
    IMPORT_PHASE_CANCELLED,
    IMPORT_PHASE_DONE,
    IMPORT_PHASE_FAILED,
    IMPORT_PHASE_PARSE,
    IMPORT_PHASE_PROBE,
    IMPORT_SUCCEEDED,
    ModelImportRequest,
    ModelImportService,
    estimate_import_resources,
    reset_shared_model_import_service,
)


pytestmark = pytest.mark.composite_core


class TestModelImportService:
    def test_resource_estimate_is_json_safe_and_budget_aware(self, tmp_path):
        path = tmp_path / "asset.gltf"
        path.write_bytes(b"x" * 1024)
        estimate = estimate_import_resources(str(path), budget_bytes=1024)
        assert estimate.source_bytes == 1024
        assert estimate.total_bytes == estimate.cpu_bytes + estimate.gpu_bytes
        assert estimate.over_budget is True
        payload = estimate.to_dict()
        assert payload["source_bytes"] == 1024
        assert payload["over_budget"] is True

    def test_explicit_resource_budget_blocks_loader(self, tmp_path, monkeypatch):
        path = tmp_path / "asset.gltf"
        path.write_bytes(b"placeholder")
        called = []
        monkeypatch.setattr(model_loader, "trimesh_available", lambda: True)
        monkeypatch.setattr(
            model_loader, "load_model", lambda *args, **kwargs: called.append(1))
        service = ModelImportService(max_workers=1)
        try:
            handle = service.submit(ModelImportRequest.from_path(
                str(path), resource_budget_bytes=1))
            assert handle.resource_estimate.over_budget is True
            result = handle.result(timeout=2.0)
        finally:
            service.shutdown(wait=True)
        assert result.status == IMPORT_FAILED
        assert result.phase == IMPORT_PHASE_FAILED
        assert result.resource_estimate is not None
        assert "resource.budget_exceeded" in result.error
        assert called == []

    def test_import_is_run_off_caller_thread(self, tmp_path, monkeypatch):
        path = tmp_path / "asset.gltf"
        path.write_bytes(b"placeholder")
        caller_thread = threading.current_thread().name
        loader_threads = []

        monkeypatch.setattr(model_loader, "trimesh_available", lambda: True)

        def fake_load(path, normalize=False, flip_uv_v=False):
            loader_threads.append(threading.current_thread().name)
            return {"path": path, "normalize": normalize, "flip_uv_v": flip_uv_v}

        monkeypatch.setattr(model_loader, "load_model", fake_load)
        service = ModelImportService(max_workers=1)
        try:
            handle = service.submit(ModelImportRequest.from_path(
                str(path), normalize=True, flip_uv_v=True))
            result = handle.result(timeout=2.0)
        finally:
            service.shutdown(wait=True)

        assert result.status == IMPORT_SUCCEEDED
        assert result.succeeded is True
        assert result.root["normalize"] is True
        assert result.root["flip_uv_v"] is True
        assert loader_threads
        assert loader_threads[0] != caller_thread
        assert handle.progress == 1.0
        assert service.active_handles() == []

    def test_runtime_probe_failure_does_not_call_loader(self, tmp_path, monkeypatch):
        path = tmp_path / "asset.usd"
        path.write_bytes(b"placeholder")
        called = []
        monkeypatch.setattr(model_loader, "load_model", lambda *args, **kwargs: called.append(1))
        service = ModelImportService(max_workers=1)
        try:
            result = service.submit(
                ModelImportRequest.from_path(str(path))).result(timeout=2.0)
        finally:
            service.shutdown(wait=True)

        assert result.status == IMPORT_FAILED
        assert result.phase == IMPORT_PHASE_FAILED
        assert result.probe is not None
        assert result.probe.reason_code == "format.planned"
        assert called == []

    def test_cancel_running_import_discards_root(self, tmp_path, monkeypatch):
        path = tmp_path / "asset.gltf"
        path.write_bytes(b"placeholder")
        started = threading.Event()
        release = threading.Event()
        monkeypatch.setattr(model_loader, "trimesh_available", lambda: True)

        def slow_load(*args, **kwargs):
            started.set()
            release.wait(timeout=2.0)
            return object()

        monkeypatch.setattr(model_loader, "load_model", slow_load)
        service = ModelImportService(max_workers=1)
        try:
            handle = service.submit(ModelImportRequest.from_path(str(path)))
            assert started.wait(timeout=2.0)
            handle.cancel()
            release.set()
            result = handle.result(timeout=2.0)
        finally:
            release.set()
            service.shutdown(wait=True)

        assert result.status == IMPORT_CANCELLED
        assert result.cancelled is True
        assert result.root is None
        assert handle.progress == 1.0

    def test_cancel_queued_import(self, tmp_path, monkeypatch):
        first_path = tmp_path / "first.gltf"
        second_path = tmp_path / "second.gltf"
        first_path.write_bytes(b"placeholder")
        second_path.write_bytes(b"placeholder")
        started = threading.Event()
        release = threading.Event()
        monkeypatch.setattr(model_loader, "trimesh_available", lambda: True)

        def slow_load(path, **kwargs):
            if path == str(first_path):
                started.set()
                release.wait(timeout=2.0)
            return object()

        monkeypatch.setattr(model_loader, "load_model", slow_load)
        service = ModelImportService(max_workers=1)
        try:
            first = service.submit(ModelImportRequest.from_path(str(first_path)))
            assert started.wait(timeout=2.0)
            second = service.submit(ModelImportRequest.from_path(str(second_path)))
            assert second.cancel() is True
            second_result = second.result(timeout=2.0)
            release.set()
            first_result = first.result(timeout=2.0)
        finally:
            release.set()
            service.shutdown(wait=True)

        assert second_result.status == IMPORT_CANCELLED
        assert second_result.phase == IMPORT_PHASE_CANCELLED
        assert first_result.status == IMPORT_SUCCEEDED

    def test_progress_snapshot_exposes_parse_and_handoff_phases(
            self, tmp_path, monkeypatch):
        path = tmp_path / "asset.gltf"
        path.write_bytes(b"placeholder")
        started = threading.Event()
        release = threading.Event()
        monkeypatch.setattr(model_loader, "trimesh_available", lambda: True)

        def slow_load(*args, **kwargs):
            started.set()
            release.wait(timeout=2.0)
            return object()

        monkeypatch.setattr(model_loader, "load_model", slow_load)
        service = ModelImportService(max_workers=1)
        try:
            handle = service.submit(ModelImportRequest.from_path(str(path)))
            assert started.wait(timeout=2.0)
            snapshot = handle.progress_snapshot()
            assert snapshot["state"] == "running"
            # The worker is blocked inside load_model, so it must have entered
            # the parse phase before the result can be handed off.
            assert snapshot["phase"] == IMPORT_PHASE_PARSE
            assert 0.0 < snapshot["progress"] < 1.0
            release.set()
            result = handle.result(timeout=2.0)
            assert result.phase == IMPORT_PHASE_DONE
            assert result.progress == 1.0
            assert handle.progress_snapshot() == {
                "progress": 1.0,
                "phase": IMPORT_PHASE_DONE,
                "state": IMPORT_SUCCEEDED,
            }
        finally:
            release.set()
            service.shutdown(wait=True)

    def test_model_node_consumes_result_without_blocking_first_call(
            self, tmp_path, monkeypatch):
        from src.features.composite.nodes.model3d_node import Model3DNode

        path = tmp_path / "asset.gltf"
        path.write_bytes(b"placeholder")
        callback_called = threading.Event()
        sentinel = object()
        monkeypatch.setattr(model_loader, "trimesh_available", lambda: True)
        monkeypatch.setattr(model_loader, "load_model", lambda *args, **kwargs: sentinel)

        node = Model3DNode("async-model")
        node.set_model_import_ready_callback(callback_called.set)
        try:
            assert node._ensure_model(str(path)) is None
            assert node._model_cache_status == "loading"
            assert node._model_import_status == "loading"
            assert node._model_import_phase in {
                "queued", IMPORT_PHASE_PROBE, IMPORT_PHASE_PARSE,
                IMPORT_PHASE_DONE}
            deadline = time.monotonic() + 2.0
            root = None
            while time.monotonic() < deadline and root is None:
                root = node._ensure_model(str(path))
                if root is None:
                    time.sleep(0.005)
            assert root is sentinel
            assert node._model_cache_status == "miss"
            assert node._model_import_status == "handoff"
            assert node._model_import_probe is not None
            assert node._model_import_probe["format_id"] == "gltf"
            assert callback_called.wait(timeout=1.0)
        finally:
            node.cleanup()
            reset_shared_model_import_service()
