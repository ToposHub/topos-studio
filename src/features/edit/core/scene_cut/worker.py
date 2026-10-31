"""
Scene Cut Detection Worker.

Implements background worker for scene cut detection using QThread.
Provides progress signals (throttled to max 10 updates/second),
cancel support, error handling, and concurrency management.

## Design Principles

- Use QThread for background execution
- Worker wraps SceneCutDetector
- Progress signals throttled (max 10 Hz)
- Cancel via CancelToken
- Errors converted to user-friendly messages
- Concurrency tracking (prevent duplicate runs)

## Signal Safety

Following .clinerules:
- All signals are emitted from worker thread
- Received in main thread (Qt auto-connection)
- No OpenGL calls in worker thread

## Reference

- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md Section P2
- .clinerules Section 2 (PyQt Signal Safety)
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass
from typing import Any, Optional
from enum import Enum

from PyQt6.QtCore import (
    QThread,
    QObject,
    pyqtSignal,
    QMutex,
    QMutexLocker,
)

from .detector import (
    SceneCutDetector,
    DetectionProgress,
)
from .frame_sampler import CancelToken, CancelledError
from .models import (
    SceneCutSettings,
    SceneCutAnalysis,
)


logger = logging.getLogger(__name__)


# ================================================================
# Error Classification
# ================================================================

class SceneCutErrorKind(Enum):
    """Classification of detection errors for user messaging."""

    FILE_NOT_FOUND = "file_not_found"
    """Source file does not exist."""

    DECODE_ERROR = "decode_error"
    """Video decoding failed."""

    CORRUPT_MEDIA = "corrupt_media"
    """Media file is corrupted or unreadable."""

    NO_VIDEO_STREAM = "no_video_stream"
    """Container has no video stream."""

    OUT_OF_MEMORY = "out_of_memory"
    """Insufficient memory for processing."""

    UNKNOWN = "unknown"
    """Unclassified error."""


@dataclass(frozen=True)
class SceneCutError:
    """
    User-friendly error information.

    Attributes:
        kind: Error classification.
        message: User-friendly error message.
        original_error: Original exception message.
        recoverable: Whether the error might be recoverable.
        suggestion: Optional suggestion for user action.
    """

    kind: SceneCutErrorKind
    message: str
    original_error: str
    recoverable: bool = False
    suggestion: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "kind": self.kind.value,
            "message": self.message,
            "original_error": self.original_error,
            "recoverable": self.recoverable,
            "suggestion": self.suggestion,
        }


def classify_error(exception: Exception) -> SceneCutError:
    """
    Convert exception to user-friendly SceneCutError.

    Args:
        exception: The caught exception.

    Returns:
        SceneCutError with classified information.
    """
    error_str = str(exception)
    error_type = type(exception).__name__

    # PyAV errors
    if "FileNotFoundError" in error_type or "not found" in error_str.lower():
        return SceneCutError(
            kind=SceneCutErrorKind.FILE_NOT_FOUND,
            message="无法找到视频文件。请检查文件路径是否正确。",
            original_error=error_str,
            recoverable=True,
            suggestion="确认文件存在后重新尝试。",
        )

    if "No video stream" in error_str:
        return SceneCutError(
            kind=SceneCutErrorKind.NO_VIDEO_STREAM,
            message="该文件没有视频流。请确保选择的是视频文件。",
            original_error=error_str,
            recoverable=False,
            suggestion="选择包含视频轨道的媒体文件。",
        )

    # PyAV decode errors
    if "av.error" in error_str.lower() or "decode" in error_str.lower():
        return SceneCutError(
            kind=SceneCutErrorKind.DECODE_ERROR,
            message="视频解码失败。文件可能损坏或使用不支持的编码格式。",
            original_error=error_str,
            recoverable=True,
            suggestion="尝试使用其他编码格式的文件，或检查文件完整性。",
        )

    if "corrupt" in error_str.lower() or "invalid" in error_str.lower():
        return SceneCutError(
            kind=SceneCutErrorKind.CORRUPT_MEDIA,
            message="媒体文件可能已损坏。请检查文件完整性。",
            original_error=error_str,
            recoverable=False,
            suggestion="尝试重新导出或使用其他版本的文件。",
        )

    # Memory errors
    if "MemoryError" in error_type or "memory" in error_str.lower():
        return SceneCutError(
            kind=SceneCutErrorKind.OUT_OF_MEMORY,
            message="内存不足。请尝试检测较短的视频片段。",
            original_error=error_str,
            recoverable=True,
            suggestion="缩小检测范围或使用更低的采样分辨率。",
        )

    # OpenCV errors
    if "cv2" in error_str.lower() or "opencv" in error_str.lower():
        return SceneCutError(
            kind=SceneCutErrorKind.DECODE_ERROR,
            message="图像处理失败。请确保 OpenCV 已正确安装。",
            original_error=error_str,
            recoverable=True,
            suggestion="检查 OpenCV 安装，或尝试重新运行。",
        )

    # Default unknown error
    return SceneCutError(
        kind=SceneCutErrorKind.UNKNOWN,
        message=f"检测过程中发生未知错误: {error_str}",
        original_error=error_str,
        recoverable=False,
        suggestion="请查看日志文件获取详细信息。",
    )


# ================================================================
# Progress Throttler
# ================================================================

@dataclass
class ProgressThrottler:
    """
    Progress update throttler.

    Limits progress signal emission rate to prevent UI overload.
    Target: max 10 updates per second (100ms minimum interval).

    Attributes:
        min_interval_ms: Minimum interval between updates (default 100ms).
        last_emit_time: Last emission timestamp (seconds).
        pending_progress: Pending progress to emit.
        pending_candidates: Pending candidates count.
    """

    min_interval_ms: float = 100.0
    last_emit_time: float = 0.0
    pending_progress: Optional[DetectionProgress] = None
    pending_candidates: int = 0

    def should_emit(self) -> bool:
        """
        Check if enough time has passed to emit.

        Returns:
            True if should emit now.
        """
        now = time.time()
        elapsed_ms = (now - self.last_emit_time) * 1000.0
        return elapsed_ms >= self.min_interval_ms

    def record_emit(self) -> None:
        """Record emission time."""
        self.last_emit_time = time.time()

    def set_pending(
        self,
        progress: DetectionProgress,
        candidates_found: int = 0,
    ) -> None:
        """
        Set pending progress data.

        Args:
            progress: DetectionProgress to emit.
            candidates_found: Number of candidates found.
        """
        self.pending_progress = progress
        self.pending_candidates = candidates_found

    def clear_pending(self) -> None:
        """Clear pending data."""
        self.pending_progress = None
        self.pending_candidates = 0


# ================================================================
# Task Key for Concurrency Control
# ================================================================

@dataclass(frozen=True)
class DetectionTaskKey:
    """
    Key for tracking running detection tasks.

    Prevents duplicate runs for same asset with same settings.

    Attributes:
        asset_id: Asset identifier.
        settings_hash: Hash of detection settings.
        clip_id: Clip ID if detecting on timeline clip.
    """

    asset_id: str
    settings_hash: str
    clip_id: Optional[str] = None

    def to_string(self) -> str:
        """Convert to unique string representation."""
        clip_part = self.clip_id or "full"
        return f"{self.asset_id}:{self.settings_hash}:{clip_part}"


# ================================================================
# Active Tasks Registry
# ================================================================

class ActiveTasksRegistry:
    """
    Registry for tracking active detection tasks.

    Thread-safe tracking of running workers to prevent duplicate runs.

    Usage:
        registry = ActiveTasksRegistry()

        key = DetectionTaskKey(asset_id, settings_hash, clip_id)
        if registry.is_running(key):
            # Task already running, skip or notify user
            return

        registry.register(key, worker)
        # ... run worker ...
        registry.unregister(key)
    """

    def __init__(self) -> None:
        """Initialize registry with mutex."""
        self._mutex = QMutex()
        self._tasks: dict[str, SceneCutWorker] = {}

    def is_running(self, key: DetectionTaskKey) -> bool:
        """
        Check if a task with this key is already running.

        Args:
            key: DetectionTaskKey to check.

        Returns:
            True if task is running.
        """
        with QMutexLocker(self._mutex):
            return key.to_string() in self._tasks

    def get_worker(self, key: DetectionTaskKey) -> Optional[SceneCutWorker]:
        """
        Get the worker for a running task.

        Args:
            key: DetectionTaskKey to look up.

        Returns:
            SceneCutWorker if running, None otherwise.
        """
        with QMutexLocker(self._mutex):
            return self._tasks.get(key.to_string())

    def register(self, key: DetectionTaskKey, worker: SceneCutWorker) -> bool:
        """
        Register a new task.

        Args:
            key: DetectionTaskKey for this task.
            worker: SceneCutWorker instance.

        Returns:
            True if registered successfully, False if already running.
        """
        with QMutexLocker(self._mutex):
            key_str = key.to_string()
            if key_str in self._tasks:
                return False
            self._tasks[key_str] = worker
            return True

    def unregister(self, key: DetectionTaskKey) -> bool:
        """
        Unregister a completed task.

        Args:
            key: DetectionTaskKey to unregister.

        Returns:
            True if unregistered, False if not found.
        """
        with QMutexLocker(self._mutex):
            key_str = key.to_string()
            if key_str in self._tasks:
                del self._tasks[key_str]
                return True
            return False

    def get_all_keys(self) -> list[DetectionTaskKey]:
        """
        Get all currently running task keys.

        Returns:
            List of DetectionTaskKey.
        """
        with QMutexLocker(self._mutex):
            return [
                self._parse_key(k)
                for k in self._tasks.keys()
            ]

    def _parse_key(self, key_str: str) -> DetectionTaskKey:
        """Parse key string back to DetectionTaskKey."""
        parts = key_str.split(":")
        if len(parts) >= 3:
            return DetectionTaskKey(
                asset_id=parts[0],
                settings_hash=parts[1],
                clip_id=parts[2] if parts[2] != "full" else None,
            )
        return DetectionTaskKey(asset_id=key_str, settings_hash="")


# Global registry instance
_global_registry: Optional[ActiveTasksRegistry] = None
_registry_lock = threading.Lock()


def get_active_tasks_registry() -> ActiveTasksRegistry:
    """
    Get the global ActiveTasksRegistry instance.

    Returns:
        ActiveTasksRegistry singleton.
    """
    global _global_registry
    with _registry_lock:
        if _global_registry is None:
            _global_registry = ActiveTasksRegistry()
        return _global_registry


# ================================================================
# Scene Cut Worker Signals
# ================================================================

class SceneCutWorkerSignals(QObject):
    """
    Signals for SceneCutWorker.

    Separated to allow signals from QRunnable (QRunnable cannot emit signals).

    Signals:
        progress: Emitted with DetectionProgress (throttled).
        candidates_found: Emitted when candidates are updated.
        completed: Emitted when detection finishes successfully.
        cancelled: Emitted when detection is cancelled.
        error: Emitted when detection encounters an error.
        started: Emitted when worker starts execution.
    """

    progress = pyqtSignal(object)  # DetectionProgress
    """Progress update signal."""

    candidates_found = pyqtSignal(int)
    """Candidates count update signal."""

    completed = pyqtSignal(object)  # SceneCutAnalysis
    """Successful completion signal."""

    cancelled = pyqtSignal()
    """Cancellation signal."""

    error = pyqtSignal(object)  # SceneCutError
    """Error signal."""

    started = pyqtSignal()
    """Worker started signal."""


# ================================================================
# Scene Cut Worker
# ================================================================

class SceneCutWorker(QThread):
    """
    Background worker for scene cut detection.

    Wraps SceneCutDetector and runs in a separate thread.
    Provides throttled progress updates, cancellation support,
    error handling, and concurrency management.

    Usage:
        ```python
        worker = SceneCutWorker(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            settings=SceneCutSettings(),
        )

        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.error.connect(self._on_error)

        worker.start()  # Starts background thread

        # To cancel:
        worker.cancel()

        # To wait for completion:
        worker.wait()
        ```

    Signals:
        progress: DetectionProgress updates (throttled to ~10 Hz).
        completed: SceneCutAnalysis on successful completion.
        cancelled: Emitted when cancelled.
        error: SceneCutError on failure.
        started: Emitted when thread starts.

    Attributes:
        source_path: Source video file path.
        asset_id: Asset identifier.
        settings: Detection settings.
        clip_id: Optional clip ID for timeline clip detection.
        proxy_path: Optional proxy media path.
    """

    # Direct signals from QThread
    progress = pyqtSignal(object)  # DetectionProgress
    completed = pyqtSignal(object)  # SceneCutAnalysis
    cancelled = pyqtSignal()
    error = pyqtSignal(object)  # SceneCutError
    started = pyqtSignal()

    def __init__(
        self,
        source_path: str,
        asset_id: str,
        settings: SceneCutSettings = SceneCutSettings(),
        clip_id: Optional[str] = None,
        proxy_path: Optional[str] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """
        Initialize scene cut worker.

        Args:
            source_path: Source video file path.
            asset_id: Asset identifier.
            settings: Detection settings.
            clip_id: Optional clip ID for timeline clip detection.
            proxy_path: Optional proxy media path.
            parent: Optional parent QObject.
        """
        super().__init__(parent)

        self._source_path = source_path
        self._asset_id = asset_id
        self._settings = settings
        self._clip_id = clip_id
        self._proxy_path = proxy_path

        # Create cancel token
        self._cancel_token = CancelToken()

        # Progress throttler
        self._throttler = ProgressThrottler(min_interval_ms=100.0)

        # Detector (created in run)
        self._detector: Optional[SceneCutDetector] = None

        # Result
        self._result: Optional[SceneCutAnalysis] = None

        # Error
        self._error: Optional[SceneCutError] = None

        # State tracking
        self._is_running = False
        self._was_cancelled = False

        # Concurrency key
        self._task_key = DetectionTaskKey(
            asset_id=asset_id,
            settings_hash=settings.to_hash(),
            clip_id=clip_id,
        )

        logger.debug(
            f"SceneCutWorker created: asset={asset_id}, "
            f"settings_hash={settings.to_hash()}"
        )

    # ==================== Properties ====================

    @property
    def source_path(self) -> str:
        """Source file path."""
        return self._source_path

    @property
    def asset_id(self) -> str:
        """Asset identifier."""
        return self._asset_id

    @property
    def settings(self) -> SceneCutSettings:
        """Detection settings."""
        return self._settings

    @property
    def clip_id(self) -> Optional[str]:
        """Clip ID if detecting on timeline clip."""
        return self._clip_id

    @property
    def proxy_path(self) -> Optional[str]:
        """Proxy media path."""
        return self._proxy_path

    @property
    def task_key(self) -> DetectionTaskKey:
        """Task key for concurrency tracking."""
        return self._task_key

    @property
    def result(self) -> Optional[SceneCutAnalysis]:
        """Detection result (after completion)."""
        return self._result

    @property
    def last_error(self) -> Optional[SceneCutError]:
        """Error information from the most recent run, if it failed."""
        return self._error

    @property
    def is_running(self) -> bool:
        """Whether worker is currently running."""
        return self._is_running

    @property
    def was_cancelled(self) -> bool:
        """Whether worker was cancelled."""
        return self._was_cancelled

    # ==================== Control Methods ====================

    def cancel(self, reason: Optional[str] = None) -> None:
        """
        Request cancellation of the detection.

        Safe to call from any thread. Worker will stop at next check point.

        Args:
            reason: Optional reason for cancellation.
        """
        logger.info(f"SceneCutWorker.cancel requested: {reason or 'user request'}")
        self._cancel_token.cancel(reason)
        self._was_cancelled = True

    def start_detection(self) -> bool:
        """
        Start detection with concurrency check.

        Registers with global registry and starts thread if not already running.

        Returns:
            True if started successfully, False if already running.
        """
        registry = get_active_tasks_registry()

        # Check if already running
        if registry.is_running(self._task_key):
            logger.warning(
                f"Detection already running for: {self._task_key.to_string()}"
            )
            return False

        # Register this worker
        if not registry.register(self._task_key, self):
            logger.warning(f"Failed to register worker: {self._task_key.to_string()}")
            return False

        # Start thread
        self.start()
        return True

    # ==================== QThread Implementation ====================

    def run(self) -> None:
        """
        Main worker thread execution.

        Called by QThread.start(). Runs in separate thread.
        Emits signals for progress, completion, cancellation, or error.

        Following .clinerules:
        - No OpenGL calls in worker thread
        - All signals emitted from this thread
        - Main thread receives via Qt auto-connection
        """
        logger.info(f"SceneCutWorker.run started: {self._asset_id}")
        self._is_running = True
        self._was_cancelled = False
        self._error = None
        self._result = None

        # Emit started signal
        self.started.emit()

        try:
            # Create detector
            self._detector = SceneCutDetector(
                source_path=self._source_path,
                asset_id=self._asset_id,
                settings=self._settings,
                clip_id=self._clip_id,
                proxy_path=self._proxy_path,
            )

            # Progress callback with throttling
            def progress_callback(progress: DetectionProgress) -> None:
                self._handle_progress(progress)

            # Run detection
            analysis = self._detector.detect(
                progress_callback=progress_callback,
                cancel_token=self._cancel_token,
            )

            # Check if cancelled during detection
            if self._cancel_token.is_cancelled():
                logger.info(f"Detection cancelled: {self._asset_id}")
                self._was_cancelled = True
                self.cancelled.emit()
            else:
                # Store result
                self._result = analysis
                logger.info(
                    f"Detection completed: {self._asset_id}, "
                    f"{len(analysis.candidates)} candidates"
                )
                self.completed.emit(analysis)

        except CancelledError:
            # Explicit cancellation
            logger.info(f"Detection cancelled by CancelToken: {self._asset_id}")
            self._was_cancelled = True
            self.cancelled.emit()

        except Exception as e:
            # Any other error
            logger.error(f"Detection error: {self._asset_id} - {e}")
            self._error = classify_error(e)
            self.error.emit(self._error)

        finally:
            # Cleanup
            self._is_running = False
            self._detector = None

            # Unregister from global registry
            registry = get_active_tasks_registry()
            registry.unregister(self._task_key)

            logger.debug(f"SceneCutWorker.run finished: {self._asset_id}")

    def _handle_progress(self, progress: DetectionProgress) -> None:
        """
        Handle progress update with throttling.

        Called by detector's progress callback. Throttles signal emission
        to max 10 Hz (100ms minimum interval).

        Args:
            progress: DetectionProgress from detector.
        """
        # Always store pending progress
        self._throttler.set_pending(progress, progress.candidates_found)

        # Check if should emit now
        if self._throttler.should_emit():
            self._throttler.record_emit()
            self.progress.emit(progress)

            if progress.candidates_found > 0:
                self._throttler.clear_pending()

        # Always emit on completion phases (no throttle)
        if progress.is_complete:
            self._throttler.clear_pending()
            self.progress.emit(progress)


# ================================================================
# Worker Manager (Optional Helper)
# ================================================================

class SceneCutWorkerManager(QObject):
    """
    Manager for coordinating multiple SceneCutWorker instances.

    Provides convenience methods for starting, tracking, and canceling workers.
    Handles concurrency checks automatically.

    Signals:
        worker_started: Emitted when a worker starts.
        worker_completed: Emitted when a worker completes.
        worker_cancelled: Emitted when a worker is cancelled.
        worker_error: Emitted when a worker encounters an error.

    Usage:
        ```python
        manager = SceneCutWorkerManager()

        # Start detection
        worker = manager.start_detection(
            source_path="/path/to/video.mp4",
            asset_id="asset-001",
            settings=settings,
        )

        # Cancel all running tasks
        manager.cancel_all()
        ```
    """

    worker_started = pyqtSignal(str)  # asset_id
    worker_completed = pyqtSignal(str, object)  # asset_id, SceneCutAnalysis
    worker_cancelled = pyqtSignal(str)  # asset_id
    worker_error = pyqtSignal(str, object)  # asset_id, SceneCutError

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """Initialize worker manager."""
        super().__init__(parent)
        self._registry = get_active_tasks_registry()
        self._workers: dict[str, SceneCutWorker] = {}

    def start_detection(
        self,
        source_path: str,
        asset_id: str,
        settings: SceneCutSettings = SceneCutSettings(),
        clip_id: Optional[str] = None,
        proxy_path: Optional[str] = None,
    ) -> Optional[SceneCutWorker]:
        """
        Start a new detection task.

        Creates worker and starts with concurrency check.

        Args:
            source_path: Source video file path.
            asset_id: Asset identifier.
            settings: Detection settings.
            clip_id: Optional clip ID.
            proxy_path: Optional proxy path.

        Returns:
            SceneCutWorker if started, None if already running.
        """
        worker = SceneCutWorker(
            source_path=source_path,
            asset_id=asset_id,
            settings=settings,
            clip_id=clip_id,
            proxy_path=proxy_path,
            parent=self,
        )

        # Capture the exact registry key used by start_detection so that
        # cleanup on completed/cancelled/error uses the identical format
        # (three segments: asset_id:settings_hash:clip_part).
        worker_key = worker.task_key.to_string()

        # Connect signals
        worker.started.connect(lambda: self._on_worker_started(asset_id))
        worker.completed.connect(
            lambda analysis: self._on_worker_completed(
                worker_key, asset_id, analysis
            )
        )
        worker.cancelled.connect(
            lambda: self._on_worker_cancelled(worker_key, asset_id)
        )
        worker.error.connect(
            lambda err: self._on_worker_error(worker_key, asset_id, err)
        )

        # Start with concurrency check
        if worker.start_detection():
            self._workers[worker_key] = worker
            return worker

        return None

    def cancel_worker(self, asset_id: str, settings: SceneCutSettings) -> bool:
        """
        Cancel a specific running worker.

        Args:
            asset_id: Asset identifier.
            settings: Settings to match.

        Returns:
            True if worker found and cancelled.
        """
        key = DetectionTaskKey(asset_id, settings.to_hash())
        worker = self._registry.get_worker(key)
        if worker is not None:
            worker.cancel()
            return True
        return False

    def cancel_all(self) -> int:
        """
        Cancel all running workers.

        Returns:
            Number of workers cancelled.
        """
        count = 0
        for key in self._registry.get_all_keys():
            worker = self._registry.get_worker(key)
            if worker is not None:
                worker.cancel()
                count += 1
        return count

    def get_running_count(self) -> int:
        """
        Get number of running workers.

        Returns:
            Count of active workers.
        """
        return len(self._registry.get_all_keys())

    def is_running(
        self,
        asset_id: str,
        settings: SceneCutSettings,
        clip_id: Optional[str] = None,
    ) -> bool:
        """
        Check if detection is running for given parameters.

        Args:
            asset_id: Asset identifier.
            settings: Settings to match.
            clip_id: Optional clip ID.

        Returns:
            True if running.
        """
        key = DetectionTaskKey(asset_id, settings.to_hash(), clip_id)
        return self._registry.is_running(key)

    # ==================== Internal Handlers ====================

    def _on_worker_started(self, asset_id: str) -> None:
        """Handle worker started."""
        self.worker_started.emit(asset_id)

    def _remove_worker(self, worker_key: str) -> None:
        """
        Drop the manager's strong reference to a finished worker.

        Args:
            worker_key: Registry key string (``DetectionTaskKey.to_string()``)
                identical to the key used when the worker was stored.
        """
        self._workers.pop(worker_key, None)

    def _on_worker_completed(
        self,
        worker_key: str,
        asset_id: str,
        analysis: SceneCutAnalysis,
    ) -> None:
        """Handle worker completed."""
        # Clean up worker reference (same key format as the write path).
        self._remove_worker(worker_key)
        self.worker_completed.emit(asset_id, analysis)

    def _on_worker_cancelled(self, worker_key: str, asset_id: str) -> None:
        """Handle worker cancelled."""
        self._remove_worker(worker_key)
        self.worker_cancelled.emit(asset_id)

    def _on_worker_error(
        self,
        worker_key: str,
        asset_id: str,
        error: SceneCutError,
    ) -> None:
        """Handle worker error."""
        self._remove_worker(worker_key)
        self.worker_error.emit(asset_id, error)


# ================================================================
# Module Exports
# ================================================================

__all__ = [
    # Main classes
    "SceneCutWorker",
    "SceneCutWorkerManager",
    "SceneCutWorkerSignals",

    # Error handling
    "SceneCutError",
    "SceneCutErrorKind",
    "classify_error",

    # Progress throttling
    "ProgressThrottler",

    # Concurrency tracking
    "DetectionTaskKey",
    "ActiveTasksRegistry",
    "get_active_tasks_registry",
]
