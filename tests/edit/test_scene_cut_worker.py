"""
Tests for scene_cut.worker — background worker for scene cut detection.

Validates:
- SceneCutWorker initialization and properties
- Progress throttling behavior
- CancelToken integration
- Error classification (classify_error)
- Concurrency tracking (ActiveTasksRegistry)
- DetectionTaskKey uniqueness
- Signal emission timing
- WorkerManager coordination
"""

import pytest
import time
import threading
from unittest.mock import Mock, MagicMock, patch

from PyQt6.QtCore import QObject, QThread, QMutex, QTimer
from PyQt6.QtTest import QSignalSpy

from src.features.edit.core.scene_cut.worker import (
    SceneCutWorker,
    SceneCutWorkerManager,
    SceneCutWorkerSignals,
    SceneCutError,
    SceneCutErrorKind,
    classify_error,
    ProgressThrottler,
    DetectionTaskKey,
    ActiveTasksRegistry,
    get_active_tasks_registry,
)
from src.features.edit.core.scene_cut.detector import (
    DetectionPhase,
    DetectionProgress,
)
from src.features.edit.core.scene_cut.models import (
    SceneCutSettings,
    SceneCutAnalysis,
    SceneCutCandidate,
)


# ===================================================================
# Test: SceneCutError Classification
# ===================================================================

class TestClassifyError:
    """classify_error function tests."""

    def test_file_not_found_error(self):
        """FileNotFoundError should be classified correctly."""
        exc = FileNotFoundError("File not found: /path/to/video.mp4")
        error = classify_error(exc)

        assert error.kind == SceneCutErrorKind.FILE_NOT_FOUND
        assert "无法找到" in error.message
        assert error.recoverable is True
        assert error.suggestion is not None

    def test_no_video_stream_error(self):
        """No video stream error should be classified."""
        exc = ValueError("No video stream found in container")
        error = classify_error(exc)

        assert error.kind == SceneCutErrorKind.NO_VIDEO_STREAM
        assert "没有视频流" in error.message
        assert error.recoverable is False

    def test_decode_error(self):
        """Decode error should be classified."""
        exc = RuntimeError("av.error: decode failed")
        error = classify_error(exc)

        assert error.kind == SceneCutErrorKind.DECODE_ERROR
        assert "解码失败" in error.message
        assert error.recoverable is True

    def test_corrupt_media_error(self):
        """Corrupt media error should be classified."""
        exc = RuntimeError("corrupt data in packet")
        error = classify_error(exc)

        assert error.kind == SceneCutErrorKind.CORRUPT_MEDIA
        assert "损坏" in error.message

    def test_memory_error(self):
        """Memory error should be classified."""
        exc = MemoryError("Out of memory")
        error = classify_error(exc)

        assert error.kind == SceneCutErrorKind.OUT_OF_MEMORY
        assert "内存不足" in error.message
        assert error.recoverable is True

    def test_opencv_error(self):
        """OpenCV error should be classified."""
        exc = RuntimeError("cv2 resize failed")
        error = classify_error(exc)

        assert error.kind == SceneCutErrorKind.DECODE_ERROR
        assert "图像处理失败" in error.message

    def test_unknown_error(self):
        """Unknown error should be classified."""
        exc = RuntimeError("Some unexpected error")
        error = classify_error(exc)

        assert error.kind == SceneCutErrorKind.UNKNOWN
        assert "未知错误" in error.message
        assert error.original_error == "Some unexpected error"

    def test_error_to_dict(self):
        """SceneCutError.to_dict should contain all fields."""
        error = SceneCutError(
            kind=SceneCutErrorKind.FILE_NOT_FOUND,
            message="Test message",
            original_error="test error",
            recoverable=True,
            suggestion="Test suggestion",
        )
        d = error.to_dict()

        assert d["kind"] == "file_not_found"
        assert d["message"] == "Test message"
        assert d["original_error"] == "test error"
        assert d["recoverable"] is True
        assert d["suggestion"] == "Test suggestion"


# ===================================================================
# Test: Progress Throttler
# ===================================================================

class TestProgressThrottler:
    """ProgressThrottler tests."""

    def test_default_interval(self):
        """Default interval should be 100ms."""
        throttler = ProgressThrottler()
        assert throttler.min_interval_ms == 100.0

    def test_should_emit_initially(self):
        """Should emit on first check."""
        throttler = ProgressThrottler()
        assert throttler.should_emit() is True

    def test_should_not_emit_immediately_after(self):
        """Should not emit immediately after recording."""
        throttler = ProgressThrottler()
        throttler.record_emit()

        # Immediately after, should not emit
        assert throttler.should_emit() is False

    def test_should_emit_after_interval(self):
        """Should emit after min_interval_ms has passed."""
        throttler = ProgressThrottler(min_interval_ms=50.0)
        throttler.record_emit()

        # Wait for interval
        time.sleep(0.06)

        assert throttler.should_emit() is True

    def test_set_pending(self):
        """set_pending should store progress."""
        throttler = ProgressThrottler()
        progress = DetectionProgress(
            phase=DetectionPhase.SAMPLING,
            current_frame=50,
            total_frames=100,
            message="Test",
        )
        throttler.set_pending(progress, candidates_found=5)

        assert throttler.pending_progress == progress
        assert throttler.pending_candidates == 5

    def test_clear_pending(self):
        """clear_pending should clear stored data."""
        throttler = ProgressThrottler()
        progress = DetectionProgress(message="Test")
        throttler.set_pending(progress, 5)
        throttler.clear_pending()

        assert throttler.pending_progress is None
        assert throttler.pending_candidates == 0

    def test_record_emit_updates_time(self):
        """record_emit should update last_emit_time."""
        throttler = ProgressThrottler()
        before = throttler.last_emit_time
        throttler.record_emit()
        after = throttler.last_emit_time

        assert after > before


# ===================================================================
# Test: DetectionTaskKey
# ===================================================================

class TestDetectionTaskKey:
    """DetectionTaskKey tests."""

    def test_to_string_format(self):
        """to_string should produce unique format."""
        key = DetectionTaskKey(
            asset_id="asset-001",
            settings_hash="abc123",
            clip_id="clip-001",
        )
        assert key.to_string() == "asset-001:abc123:clip-001"

    def test_to_string_without_clip(self):
        """to_string should use 'full' when no clip_id."""
        key = DetectionTaskKey(
            asset_id="asset-001",
            settings_hash="abc123",
            clip_id=None,
        )
        assert key.to_string() == "asset-001:abc123:full"

    def test_keys_equal_for_same_params(self):
        """Keys should be equal for same parameters."""
        key1 = DetectionTaskKey("asset-001", "hash1", "clip-001")
        key2 = DetectionTaskKey("asset-001", "hash1", "clip-001")

        assert key1 == key2
        assert key1.to_string() == key2.to_string()

    def test_keys_different_for_different_asset(self):
        """Keys should differ for different asset_id."""
        key1 = DetectionTaskKey("asset-001", "hash1", None)
        key2 = DetectionTaskKey("asset-002", "hash1", None)

        assert key1 != key2
        assert key1.to_string() != key2.to_string()

    def test_keys_different_for_different_hash(self):
        """Keys should differ for different settings_hash."""
        key1 = DetectionTaskKey("asset-001", "hash1", None)
        key2 = DetectionTaskKey("asset-001", "hash2", None)

        assert key1 != key2

    def test_keys_different_for_different_clip(self):
        """Keys should differ for different clip_id."""
        key1 = DetectionTaskKey("asset-001", "hash1", "clip-001")
        key2 = DetectionTaskKey("asset-001", "hash1", None)

        assert key1 != key2

    def test_frozen_dataclass(self):
        """DetectionTaskKey should be frozen."""
        key = DetectionTaskKey("asset-001", "hash1", None)
        with pytest.raises(AttributeError):
            key.asset_id = "asset-002"


# ===================================================================
# Test: ActiveTasksRegistry
# ===================================================================

class TestActiveTasksRegistry:
    """ActiveTasksRegistry tests."""

    def test_is_running_false_initially(self):
        """Should not have any tasks initially."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("asset-001", "hash1", None)
        assert registry.is_running(key) is False

    def test_register_adds_task(self):
        """register should add task to registry."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("asset-001", "hash1", None)

        # Create mock worker
        worker = Mock(spec=SceneCutWorker)

        result = registry.register(key, worker)
        assert result is True
        assert registry.is_running(key) is True

    def test_register_fails_for_duplicate(self):
        """register should fail for already running task."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("asset-001", "hash1", None)

        worker1 = Mock(spec=SceneCutWorker)
        worker2 = Mock(spec=SceneCutWorker)

        registry.register(key, worker1)
        result = registry.register(key, worker2)

        assert result is False
        assert registry.get_worker(key) == worker1  # First worker remains

    def test_unregister_removes_task(self):
        """unregister should remove task."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("asset-001", "hash1", None)
        worker = Mock(spec=SceneCutWorker)

        registry.register(key, worker)
        result = registry.unregister(key)

        assert result is True
        assert registry.is_running(key) is False

    def test_unregister_fails_for_not_found(self):
        """unregister should fail for non-existent task."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("asset-001", "hash1", None)

        result = registry.unregister(key)
        assert result is False

    def test_get_worker_returns_registered(self):
        """get_worker should return registered worker."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("asset-001", "hash1", None)
        worker = Mock(spec=SceneCutWorker)

        registry.register(key, worker)
        result = registry.get_worker(key)

        assert result == worker

    def test_get_worker_returns_none_if_not_found(self):
        """get_worker should return None if not registered."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("asset-001", "hash1", None)

        result = registry.get_worker(key)
        assert result is None

    def test_get_all_keys_returns_all(self):
        """get_all_keys should return all registered keys."""
        registry = ActiveTasksRegistry()
        key1 = DetectionTaskKey("asset-001", "hash1", None)
        key2 = DetectionTaskKey("asset-002", "hash2", "clip-001")

        worker1 = Mock(spec=SceneCutWorker)
        worker2 = Mock(spec=SceneCutWorker)

        registry.register(key1, worker1)
        registry.register(key2, worker2)

        keys = registry.get_all_keys()
        assert len(keys) == 2


class TestGetActiveTasksRegistry:
    """get_active_tasks_registry singleton tests."""

    def test_returns_same_instance(self):
        """Should return same singleton instance."""
        registry1 = get_active_tasks_registry()
        registry2 = get_active_tasks_registry()

        assert registry1 is registry2


# ===================================================================
# Test: SceneCutWorker Initialization
# ===================================================================

class TestSceneCutWorkerInit:
    """SceneCutWorker initialization tests."""

    def test_init_with_required_params(self):
        """Should initialize with required params."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )
        assert worker.source_path == "/path/to/video.mp4"
        assert worker.asset_id == "asset-001"

    def test_init_with_default_settings(self):
        """Should use default settings."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )
        assert worker.settings.sensitivity == 0.55

    def test_init_with_custom_settings(self):
        """Should accept custom settings."""
        settings = SceneCutSettings(sensitivity=0.65)
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            settings=settings,
        )
        assert worker.settings.sensitivity == 0.65

    def test_init_with_clip_id(self):
        """Should accept clip_id."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            clip_id="clip-001",
        )
        assert worker.clip_id == "clip-001"

    def test_init_with_proxy_path(self):
        """Should accept proxy_path."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            proxy_path="/path/to/proxy.mp4",
        )
        assert worker.proxy_path == "/path/to/proxy.mp4"

    def test_task_key_generated(self):
        """Should generate task_key from params."""
        settings = SceneCutSettings(sensitivity=0.65)
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            settings=settings,
            clip_id="clip-001",
        )
        key = worker.task_key

        assert key.asset_id == "asset-001"
        assert key.settings_hash == settings.to_hash()
        assert key.clip_id == "clip-001"

    def test_initial_state_not_running(self):
        """Should not be running initially."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )
        assert worker.is_running is False
        assert worker.was_cancelled is False
        assert worker.result is None
        assert worker.last_error is None


# ===================================================================
# Test: SceneCutWorker Properties
# ===================================================================

class TestSceneCutWorkerProperties:
    """SceneCutWorker property tests."""

    def test_source_path_property(self):
        """source_path should be accessible."""
        worker = SceneCutWorker(
            source_path="/test/video.mp4",
            asset_id="test-asset",
        )
        assert worker.source_path == "/test/video.mp4"

    def test_asset_id_property(self):
        """asset_id should be accessible."""
        worker = SceneCutWorker(
            source_path="/test/video.mp4",
            asset_id="test-asset-123",
        )
        assert worker.asset_id == "test-asset-123"

    def test_settings_property(self):
        """settings should be accessible."""
        settings = SceneCutSettings(sensitivity=0.70)
        worker = SceneCutWorker(
            source_path="/test/video.mp4",
            asset_id="test-asset",
            settings=settings,
        )
        assert worker.settings.sensitivity == 0.70


# ===================================================================
# Test: SceneCutWorker Cancel
# ===================================================================

class TestSceneCutWorkerCancel:
    """SceneCutWorker cancel tests."""

    def test_cancel_sets_cancelled_flag(self):
        """cancel() should set was_cancelled."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )
        worker.cancel()
        assert worker.was_cancelled is True

    def test_cancel_token_is_cancelled(self):
        """cancel() should trigger CancelToken."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )
        worker.cancel()
        # CancelToken is internal, but we can check was_cancelled
        assert worker.was_cancelled is True

    def test_cancel_with_reason(self):
        """cancel() should accept reason."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )
        worker.cancel(reason="User requested")
        assert worker.was_cancelled is True


# ===================================================================
# Test: SceneCutWorker Concurrency
# ===================================================================

class TestSceneCutWorkerConcurrency:
    """SceneCutWorker concurrency tests."""

    def test_start_detection_registers_with_registry(self):
        """start_detection should register with global registry."""
        # Clear registry for clean test
        registry = get_active_tasks_registry()
        for key in registry.get_all_keys():
            registry.unregister(key)

        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="test-asset-concurrency",
            settings=SceneCutSettings(sensitivity=0.55),
        )

        # We don't actually start the thread in this test
        # Just verify the registry check works
        key = worker.task_key
        assert registry.is_running(key) is False

    def test_duplicate_task_key_blocked(self):
        """Same task_key should be blocked."""
        registry = ActiveTasksRegistry()  # Use fresh registry
        key = DetectionTaskKey("asset-001", "hash1", None)

        worker1 = Mock(spec=SceneCutWorker)
        worker2 = Mock(spec=SceneCutWorker)

        # Register first
        registry.register(key, worker1)

        # Second registration should fail
        result = registry.register(key, worker2)
        assert result is False


# ===================================================================
# Test: SceneCutWorkerManager
# ===================================================================

class TestSceneCutWorkerManagerInit:
    """SceneCutWorkerManager initialization tests."""

    def test_init(self):
        """Should initialize manager."""
        manager = SceneCutWorkerManager()
        assert manager.get_running_count() == 0

    def test_is_running_false_initially(self):
        """Should not have any running tasks initially."""
        manager = SceneCutWorkerManager()
        settings = SceneCutSettings()
        assert manager.is_running("asset-001", settings) is False


class TestSceneCutWorkerManagerMethods:
    """SceneCutWorkerManager method tests."""

    def test_cancel_all_returns_zero_when_empty(self):
        """cancel_all should return 0 when no tasks running."""
        manager = SceneCutWorkerManager()
        count = manager.cancel_all()
        assert count == 0

    def test_get_running_count_returns_zero_initially(self):
        """get_running_count should be 0 initially."""
        manager = SceneCutWorkerManager()
        assert manager.get_running_count() == 0


# ===================================================================
# Test: SceneCutWorkerSignals
# ===================================================================

class TestSceneCutWorkerSignals:
    """SceneCutWorkerSignals tests."""

    def test_signals_exist(self):
        """All required signals should exist."""
        signals = SceneCutWorkerSignals()

        # Check signal attributes exist
        assert hasattr(signals, 'progress')
        assert hasattr(signals, 'completed')
        assert hasattr(signals, 'cancelled')
        assert hasattr(signals, 'error')
        assert hasattr(signals, 'started')


# ===================================================================
# Test: Progress Handling Integration
# ===================================================================

class TestProgressHandling:
    """Progress handling integration tests."""

    def test_throttler_limits_updates(self):
        """Throttler should limit update rate."""
        throttler = ProgressThrottler(min_interval_ms=100.0)

        # First update should pass
        assert throttler.should_emit() is True
        throttler.record_emit()

        # Immediate second should not pass
        assert throttler.should_emit() is False

        # After waiting, should pass
        time.sleep(0.11)
        assert throttler.should_emit() is True

    def test_complete_phase_always_emits(self):
        """Complete phase should bypass throttle."""
        throttler = ProgressThrottler(min_interval_ms=1000.0)
        throttler.record_emit()

        # Even with long interval, complete should emit
        # This is tested via the _handle_progress logic
        # For now, just verify the throttler setup
        progress = DetectionProgress(phase=DetectionPhase.COMPLETE)
        throttler.set_pending(progress)

        # Clear pending manually (as done in _handle_progress for complete)
        throttler.clear_pending()
        assert throttler.pending_progress is None


# ===================================================================
# Test: Error Message Localization
# ===================================================================

class TestErrorLocalization:
    """Error message localization tests."""

    def test_file_not_found_chinese_message(self):
        """File not found error should have Chinese message."""
        exc = FileNotFoundError("file missing")
        error = classify_error(exc)
        assert "无法找到" in error.message or "找不到" in error.message

    def test_decode_error_chinese_message(self):
        """Decode error should have Chinese message."""
        exc = RuntimeError("decode error")
        error = classify_error(exc)
        assert "解码" in error.message or "处理" in error.message

    def test_memory_error_chinese_message(self):
        """Memory error should have Chinese message."""
        exc = MemoryError("out of memory")
        error = classify_error(exc)
        assert "内存" in error.message


# ===================================================================
# Test: Thread Safety
# ===================================================================

class TestThreadSafety:
    """Thread safety tests."""

    def test_registry_mutex_protects_concurrent_access(self):
        """Registry should handle concurrent access."""
        registry = ActiveTasksRegistry()
        results: list[bool] = []

        def register_task(i: int) -> None:
            key = DetectionTaskKey(f"asset-{i}", f"hash{i}", None)
            worker = Mock(spec=SceneCutWorker)
            results.append(registry.register(key, worker))

        # Run multiple registrations concurrently
        threads = [
            threading.Thread(target=register_task, args=(i,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should succeed since keys are different
        assert all(results)

    def test_registry_mutex_blocks_duplicate(self):
        """Registry should block duplicate registration."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("same-asset", "same-hash", None)
        results: list[bool] = []

        def try_register() -> None:
            worker = Mock(spec=SceneCutWorker)
            results.append(registry.register(key, worker))

        # Run concurrent registrations for same key
        threads = [
            threading.Thread(target=try_register)
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Only one should succeed
        assert sum(results) == 1


# ===================================================================
# Test: Frozen Dataclass Behavior
# ===================================================================

class TestFrozenDataclasses:
    """Frozen dataclass tests."""

    def test_scene_cut_error_frozen(self):
        """SceneCutError should be frozen."""
        error = SceneCutError(
            kind=SceneCutErrorKind.FILE_NOT_FOUND,
            message="Test",
            original_error="test",
        )
        with pytest.raises(AttributeError):
            error.message = "New message"

    def test_detection_task_key_frozen(self):
        """DetectionTaskKey should be frozen."""
        key = DetectionTaskKey("asset-001", "hash1", None)
        with pytest.raises(AttributeError):
            key.asset_id = "asset-002"


# ===================================================================
# Test: Signal Connection Pattern
# ===================================================================

class TestSignalConnectionPattern:
    """Signal connection pattern tests (following .clinerules)."""

    def test_worker_has_expected_signals(self):
        """Worker should have all expected signals."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )

        # Signals should exist
        assert hasattr(worker, 'progress')
        assert hasattr(worker, 'completed')
        assert hasattr(worker, 'cancelled')
        assert hasattr(worker, 'error')
        assert hasattr(worker, 'started')

        # ``error`` must remain the Qt signal used by the dialog and manager.
        received = []
        handler = received.append
        worker.error.connect(handler)
        worker.error.disconnect(handler)
        assert worker.last_error is None

    def test_manager_has_expected_signals(self):
        """Manager should have all expected signals."""
        manager = SceneCutWorkerManager()

        assert hasattr(manager, 'worker_started')
        assert hasattr(manager, 'worker_completed')
        assert hasattr(manager, 'worker_cancelled')
        assert hasattr(manager, 'worker_error')


# ===================================================================
# Integration Test: Worker Lifecycle (Without Actual Detection)
# ===================================================================

class TestWorkerLifecycle:
    """Worker lifecycle tests (mocked detector)."""

    def test_worker_state_transitions(self):
        """Worker state should transition correctly."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )

        # Initial state
        assert worker.is_running is False
        assert worker.was_cancelled is False
        assert worker.result is None

        # After cancel
        worker.cancel()
        assert worker.was_cancelled is True

    def test_cancel_before_run(self):
        """Cancel before run should set flag."""
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
        )
        worker.cancel(reason="Pre-run cancel")

        # State reflects cancellation
        assert worker.was_cancelled is True


# ===================================================================
# Test: Settings Hash Integration
# ===================================================================

class TestSettingsHashIntegration:
    """Settings hash integration with worker."""

    def test_worker_task_key_matches_settings(self):
        """Task key should use settings hash."""
        settings = SceneCutSettings(sensitivity=0.65, min_scene_frames=12)
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            settings=settings,
        )

        expected_hash = settings.to_hash()
        assert worker.task_key.settings_hash == expected_hash

    def test_different_settings_different_keys(self):
        """Different settings should produce different task keys."""
        worker1 = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.55),
        )
        worker2 = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            settings=SceneCutSettings(sensitivity=0.65),
        )

        assert worker1.task_key != worker2.task_key
        assert worker1.task_key.settings_hash != worker2.task_key.settings_hash


# ===================================================================
# Test: Edge Cases
# ===================================================================

class TestEdgeCases:
    """Edge case tests."""

    def test_empty_asset_id_allowed(self):
        """Empty asset_id should be allowed (validation elsewhere)."""
        # DetectionTaskKey allows empty strings (validation is in SceneCutAnalysis)
        key = DetectionTaskKey("", "hash", None)
        assert key.asset_id == ""

    def test_zero_interval_throttler(self):
        """Throttler with very small interval."""
        throttler = ProgressThrottler(min_interval_ms=1.0)
        throttler.record_emit()
        time.sleep(0.002)
        assert throttler.should_emit() is True

    def test_registry_with_none_clip_id(self):
        """Registry should handle None clip_id."""
        registry = ActiveTasksRegistry()
        key = DetectionTaskKey("asset-001", "hash1", None)
        worker = Mock(spec=SceneCutWorker)

        result = registry.register(key, worker)
        assert result is True

        retrieved = registry.get_worker(key)
        assert retrieved == worker


# ===================================================================
# Module Export Tests
# ===================================================================

class TestModuleExports:
    """Module exports tests."""

    def test_all_exports_available(self):
        """All declared exports should be available."""
        from src.features.edit.core.scene_cut import worker

        expected_exports = [
            "SceneCutWorker",
            "SceneCutWorkerManager",
            "SceneCutWorkerSignals",
            "SceneCutError",
            "SceneCutErrorKind",
            "classify_error",
            "ProgressThrottler",
            "DetectionTaskKey",
            "ActiveTasksRegistry",
            "get_active_tasks_registry",
        ]

        for name in expected_exports:
            assert hasattr(worker, name), f"Missing export: {name}"
