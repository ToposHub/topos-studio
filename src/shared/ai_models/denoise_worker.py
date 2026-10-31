# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
AI Denoise Worker - Asynchronous ONNX Denoising with Progress Reporting

This module provides a dedicated worker for AI-powered image denoising
using ONNX Runtime, with full progress reporting for UI integration.

Designed for seamless integration with the Baking pipeline:
- Outputs float32 high-precision results ready for EXR caching
- Progress signals for UI progress bar updates
- Thread-safe execution without blocking main UI
"""

import os
import cv2
import time
import traceback
import numpy as np
import onnxruntime as ort
import logging

logger = logging.getLogger(__name__)
from PyQt6.QtCore import QObject, pyqtSignal, QThread, QMutex, pyqtSlot
from src.shared.ai_models.onnx_env import (
    get_configured_providers, get_session_options,
    get_configured_tile_size, get_configured_model_path,
    get_gpu_provider_info
)


class _ProcessingCancelled(Exception):
    """Raised at cooperative-abort checkpoints (AUD-31521).

    Unwinds :meth:`AIDenoiseWorker.process` as a *user cancellation* — the
    worker reports it through the dedicated ``cancelled`` signal instead of
    ``error``, so an abort is never presented to the UI as
    "发生致命错误" with a full traceback.
    """


class AIDenoiseWorker(QObject):
    """
    Asynchronous Worker for AI Denoising with Progress Reporting.
    
    This worker is designed to run in a QThread and provides:
    - Real-time progress updates during tiled processing
    - High-precision float32 output for Baking pipeline
    - Thread-safe cancellation support
    
    Signals:
        progress(int): Progress percentage (0-100) during tiled processing
        finished(np.ndarray): Denoising complete, returns float32 BGR [0, 1]
        error(str): Error occurred, returns error message
        cancelled(): Processing was aborted at the user's request

    Usage:
        worker = AIDenoiseWorker()
        thread = QThread()
        worker.moveToThread(thread)

        worker.progress.connect(progress_bar.setValue)
        worker.finished.connect(on_denoise_finished)
        worker.error.connect(on_error)
        worker.cancelled.connect(on_cancelled)

        thread.started.connect(lambda: worker.process(image, params))
        thread.start()
    """
    progress = pyqtSignal(int)
    finished = pyqtSignal(np.ndarray)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    # Shared model cache to avoid reloading from disk
    _session_cache = {}
    _current_model_path = None
    _cache_mutex = QMutex()

    # Default model path
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    MODEL_DIR = os.path.join(BASE_DIR, "models", "denoise")
    DEFAULT_MODEL = "swinir_denoise.onnx"

    def __init__(self):
        super().__init__()
        self._mutex = QMutex()
        self._abort = False
        self._working = False
        # Job staged by the controller for the thread-start handshake
        # (see submit/process_staged — AUD-31522).
        self._staged: tuple[np.ndarray, dict] | None = None

    def submit(self, image: np.ndarray, params: dict) -> None:
        """Stage one job for the ``QThread.started`` handshake.

        ``thread.started`` must be connected to a *bound method of the
        worker* (``process_staged``). Connecting a lambda/partial instead
        gives PyQt no QObject receiver, so the call would be queued to the
        CONTROLLER thread and the entire inference would run on the UI
        thread (the exact freeze AUD-31522 prohibits).
        """
        self._staged = (image, params)

    @pyqtSlot()
    def process_staged(self) -> None:
        """Run the job staged via :meth:`submit` (in the worker thread)."""
        staged = self._staged
        self._staged = None
        if staged is None:
            return
        image, params = staged
        self.process(image, params)

    def _get_model_path(self, model_name: str) -> str:
        """Get absolute path for a model file."""
        return os.path.join(self.MODEL_DIR, model_name)

    def _load_onnx_session(self, model_path: str) -> ort.InferenceSession:
        """
        Load an ONNX model with optimal settings and GPU acceleration.
        
        Uses shared cache to avoid reloading models between calls.
        
        Args:
            model_path: Path to the ONNX model file
            
        Returns:
            ort.InferenceSession: Loaded ONNX inference session
        """
        # Check cache first (thread-safe)
        AIDenoiseWorker._cache_mutex.lock()
        if model_path in AIDenoiseWorker._session_cache:
            session = AIDenoiseWorker._session_cache[model_path]
            AIDenoiseWorker._cache_mutex.unlock()
            return session
        AIDenoiseWorker._cache_mutex.unlock()

        # Release old session memory when switching models
        AIDenoiseWorker._cache_mutex.lock()
        if AIDenoiseWorker._current_model_path != model_path:
            if AIDenoiseWorker._current_model_path is not None:
                AIDenoiseWorker._session_cache.clear()
            AIDenoiseWorker._current_model_path = model_path
        AIDenoiseWorker._cache_mutex.unlock()

        # Get optimal providers
        providers = get_configured_providers()

        gpu_info = get_gpu_provider_info()
        if gpu_info:
            logger.info(f"Denoise Worker] GPU acceleration: {gpu_info['description']}")
        else:
            logger.info("Denoise Worker] Using CPU (no GPU acceleration)")

        # Configure session options
        sess_options = get_session_options()

        session = None
        last_error = None

        for provider in providers:
            if self._abort:
                raise _ProcessingCancelled()

            if provider == 'CPUExecutionProvider':
                request_providers = ['CPUExecutionProvider']
            else:
                request_providers = [provider, 'CPUExecutionProvider']

            if provider == 'CPUExecutionProvider' and session is not None:
                continue

            try:
                logger.info(f"Denoise Worker] Loading with: {request_providers}")
                session = ort.InferenceSession(
                    model_path,
                    sess_options=sess_options,
                    providers=request_providers
                )

                actual_providers = session.get_providers()
                primary_name = request_providers[0]
                if isinstance(primary_name, tuple):
                    primary_name = primary_name[0]

                if primary_name in actual_providers or primary_name == 'CPUExecutionProvider':
                    logger.info(f"Denoise Worker] Loaded with {primary_name}")
                    # Cache the session
                    AIDenoiseWorker._cache_mutex.lock()
                    AIDenoiseWorker._session_cache[model_path] = session
                    AIDenoiseWorker._cache_mutex.unlock()
                    return session
                else:
                    del session
                    session = None

            except Exception as e:
                last_error = e
                logger.error(f"Denoise Worker] Failed: {e}")
                if session is not None:
                    del session
                    session = None

        # Pure CPU fallback
        if session is None:
            try:
                session = ort.InferenceSession(
                    model_path,
                    sess_options=sess_options,
                    providers=['CPUExecutionProvider']
                )
                logger.info("Denoise Worker] Loaded with CPU fallback")
                AIDenoiseWorker._cache_mutex.lock()
                AIDenoiseWorker._session_cache[model_path] = session
                AIDenoiseWorker._cache_mutex.unlock()
                return session
            except Exception as e:
                raise RuntimeError(f"Failed to load model: {e}") from last_error

        return session

    def process(self, image: np.ndarray, params: dict):
        """
        Process image with AI denoising.

        This method should be called from a QThread.

        Args:
            image: float32 BGR image [0, 1], shape (H, W, 3)
            params: Dict with parameters:
                - model_path: Path to ONNX model (optional, uses default if not provided)
                - blend: Blend factor with original (0.0-1.0, default 1.0)
                - use_tiling: Enable tiled processing (default True)
                - tile_size: Tile size in pixels (default 512)
                - overlap: Overlap between tiles (default 64)
        """
        logger.info("=" * 50)
        logger.info("🚀 [Denoise Worker] 线程开始运行...")
        logger.info("=" * 50)

        self._mutex.lock()
        self._working = True
        self._abort = False
        self._mutex.unlock()

        try:
            # 1. 打印输入数据信息
            logger.info(f"📦 [Denoise Worker] 收到图像数据: shape={image.shape}, dtype={image.dtype}")

            # Get parameters
            model_path = params.get("model_path")
            if not model_path:
                config_model = get_configured_model_path()
                model_path = config_model if config_model else self._get_model_path(self.DEFAULT_MODEL)
            elif not os.path.isabs(model_path):
                model_path = self._get_model_path(os.path.basename(model_path))

            if not os.path.exists(model_path):
                raise FileNotFoundError(f"Model not found: {model_path}")

            logger.info(f"📁 [Denoise Worker] 模型路径: {model_path}")

            blend = params.get("blend", 1.0)
            use_tiling = params.get("use_tiling", True)
            tile_size = params.get("tile_size") or get_configured_tile_size()
            overlap = params.get("overlap", 64)

            logger.info(f"⚙️ [Denoise Worker] 参数: blend={blend}, use_tiling={use_tiling}, tile_size={tile_size}, overlap={overlap}")

            # 2. 加载模型
            logger.info("⏳ [Denoise Worker] 正在加载 ONNX 模型...")
            load_start = time.time()
            self.progress.emit(5)  # Loading model...
            session = self._load_onnx_session(model_path)
            load_end = time.time()
            logger.info(f"✅ [Denoise Worker] 模型加载完成！耗时: {load_end - load_start:.2f} 秒")

            if self._abort:
                raise _ProcessingCancelled()

            # Prepare input
            self.progress.emit(10)  # Preparing input...
            image = np.nan_to_num(image, nan=0.0, posinf=1.0, neginf=0.0)
            image = np.clip(image, 0, 1)
            original_image = image.copy()

            # Convert BGR to RGB
            img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            logger.info("🔄 [Denoise Worker] 图像预处理完成，准备推理...")

            # Process
            process_start = time.time()
            if use_tiling:
                logger.info(f"🧩 [Denoise Worker] 使用分块处理 (Tile Size: {tile_size}, Overlap: {overlap})...")
                output = self._process_tiled(img_rgb, session, tile_size, overlap)
            else:
                logger.info("🖼️ [Denoise Worker] 使用整图处理...")
                output = self._process_single(img_rgb, session)

            process_end = time.time()
            logger.info(f"✅ [Denoise Worker] 推理完成！耗时: {process_end - process_start:.2f} 秒")

            if self._abort:
                raise _ProcessingCancelled()

            # Post-process
            self.progress.emit(95)  # Finalizing...
            output = np.nan_to_num(output, nan=0.0, posinf=1.0, neginf=0.0)
            output = np.clip(output, 0, 1)
            output = cv2.cvtColor(output, cv2.COLOR_RGB2BGR)

            # Blend with original
            if blend < 1.0:
                output = cv2.addWeighted(output, blend, original_image, 1.0 - blend, 0)

            output = np.clip(output, 0, 1)
            output = np.ascontiguousarray(output, dtype=np.float32)

            self.progress.emit(100)
            logger.info("🎉 [Denoise Worker] 所有处理完成，准备发送 finished 信号！")
            self.finished.emit(output)

        except _ProcessingCancelled:
            # 用户主动取消（AUD-31521）：以独立的 cancelled 信号上报，
            # 不走 error 通道——取消不是"发生致命错误"，不携带 traceback。
            logger.info("🛑 [Denoise Worker] 用户请求取消，推理已中止")
            self.cancelled.emit()

        except Exception as e:
            # 抓取静默崩溃的核心法宝
            error_msg = f"发生致命错误: {str(e)}\n{traceback.format_exc()}"
            logger.info("❌" * 20)
            logger.error(f"[Denoise Worker] 子线程崩溃了！\n{error_msg}")
            logger.info("❌" * 20)
            self.error.emit(error_msg)

        finally:
            self._mutex.lock()
            self._working = False
            self._mutex.unlock()

    def _process_single(self, img_rgb: np.ndarray, session: ort.InferenceSession) -> np.ndarray:
        """Process entire image without tiling."""
        logger.info("📐 [Denoise Worker] _process_single: 准备输入张量...")
        img_nchw = img_rgb.transpose(2, 0, 1)[np.newaxis, :, :, :]
        img_nchw = np.ascontiguousarray(img_nchw, dtype=np.float32)
        logger.info(f"📐 [Denoise Worker] 输入张量 shape: {img_nchw.shape}")

        input_name = session.get_inputs()[0].name
        logger.info("⏳ [Denoise Worker] 正在调用 ONNX session.run()... (如果是首次使用 CoreML，可能会卡住 1~5 分钟进行编译，请耐心等待！)")

        inference_start = time.time()
        outputs = session.run(None, {input_name: img_nchw})
        inference_end = time.time()

        logger.info(f"✅ [Denoise Worker] ONNX 推理成功！耗时: {inference_end - inference_start:.2f} 秒")
        output = outputs[0].squeeze(0).transpose(1, 2, 0)
        logger.info(f"📐 [Denoise Worker] 输出张量 shape: {output.shape}")

        return output

    def _process_tiled(self, img_rgb: np.ndarray, session: ort.InferenceSession,
                       tile_size: int, overlap: int) -> np.ndarray:
        """
        Process image in tiles with Hanning window blending.

        Emits progress signals during processing.
        """
        logger.info("🧩 [Denoise Worker] _process_tiled: 开始分块处理...")
        h, w, c = img_rgb.shape
        logger.info(f"🧩 [Denoise Worker] 图像尺寸: {w}x{h}x{c}")

        output = np.zeros((h, w, c), dtype=np.float32)
        weight = np.zeros((h, w, c), dtype=np.float32)

        # Create complementary cosine ramps for smooth blending.
        # fade_in (top/left edge): 0 → 1
        # fade_out (bottom/right edge): 1 → 0
        # At every position in the overlap: fade_in + fade_out = 1.0
        if overlap > 0:
            # overlap==1 时分母为 0 会产生 NaN 权重（边缘黑边，且被
            # nan_to_num 静默掩盖）；与 ai_plugin_engine 的同名公式对齐，
            # 分母钳制为 >= 1（AUD-31523）。
            t = np.arange(overlap) / max(overlap - 1, 1)  # 0 to 1
            fade_in = 0.5 * (1 - np.cos(np.pi * t))   # 0 → 1
            fade_out = 0.5 * (1 + np.cos(np.pi * t))  # 1 → 0
            ramp_h_in = fade_in[:, np.newaxis, np.newaxis]
            ramp_h_out = fade_out[:, np.newaxis, np.newaxis]
            ramp_w_in = fade_in[np.newaxis, :, np.newaxis]
            ramp_w_out = fade_out[np.newaxis, :, np.newaxis]
        else:
            ramp_h_in = ramp_h_out = ramp_w_in = ramp_w_out = None

        stride = tile_size - overlap

        # Calculate total tiles
        total_tiles = 0
        y_positions = []
        x_positions = []

        for y in range(0, h, stride):
            y_end = min(y + tile_size, h)
            y_start = max(0, y_end - tile_size)
            y_positions.append(y_start)

        for x in range(0, w, stride):
            x_end = min(x + tile_size, w)
            x_start = max(0, x_end - tile_size)
            x_positions.append(x_start)

        total_tiles = len(y_positions) * len(x_positions)
        logger.info(f"🧩 [Denoise Worker] 总共 {total_tiles} 个分块 ({len(y_positions)}x{len(x_positions)})")

        processed_tiles = 0
        input_name = session.get_inputs()[0].name

        # Progress range: 10-90 for tile processing
        progress_start = 10
        progress_end = 90
        progress_range = progress_end - progress_start

        first_tile = True
        for y_idx, y in enumerate(y_positions):
            for x_idx, x in enumerate(x_positions):
                if self._abort:
                    raise _ProcessingCancelled()

                y1, y2 = y, y + tile_size
                x1, x2 = x, x + tile_size

                y1 = max(0, y1)
                y2 = min(h, y2)
                x1 = max(0, x1)
                x2 = min(w, x2)

                actual_tile_h = y2 - y1
                actual_tile_w = x2 - x1

                # Create weight mask
                tile_mask = np.ones((actual_tile_h, actual_tile_w, c), dtype=np.float32)

                if overlap > 0 and ramp_h_in is not None:
                    if y1 > 0:
                        ramp_len = min(overlap, actual_tile_h)
                        tile_mask[:ramp_len, :, :] *= ramp_h_in[:ramp_len, :, :]
                    if y2 < h:
                        ramp_len = min(overlap, actual_tile_h)
                        tile_mask[-ramp_len:, :, :] *= ramp_h_out[-ramp_len:, :, :]
                    if x1 > 0:
                        ramp_len = min(overlap, actual_tile_w)
                        tile_mask[:, :ramp_len, :] *= ramp_w_in[:, :ramp_len, :]
                    if x2 < w:
                        ramp_len = min(overlap, actual_tile_w)
                        tile_mask[:, -ramp_len:, :] *= ramp_w_out[:, -ramp_len:, :]

                # Extract tile
                tile = img_rgb[y1:y2, x1:x2, :]

                # Check if padding is needed (edge tiles)
                needs_padding = tile.shape[0] < tile_size or tile.shape[1] < tile_size
                pad_h = tile_size - actual_tile_h if actual_tile_h < tile_size else 0
                pad_w = tile_size - actual_tile_w if actual_tile_w < tile_size else 0

                if needs_padding:
                    # Use cv2.copyMakeBorder with BORDER_REFLECT_101 (mirror reflection)
                    # This provides better edge quality than zero-padding
                    # top, bottom, left, right padding
                    tile = cv2.copyMakeBorder(
                        tile,
                        0, pad_h,  # top, bottom
                        0, pad_w,  # left, right
                        cv2.BORDER_REFLECT_101
                    )
                    # Also pad the mask with zeros (mask should not be reflected)
                    tile_mask = cv2.copyMakeBorder(
                        tile_mask,
                        0, pad_h,
                        0, pad_w,
                        cv2.BORDER_CONSTANT,
                        value=0.0
                    )

                # Ensure exact tile_size for ONNX model input
                assert tile.shape[0] == tile_size and tile.shape[1] == tile_size, \
                    f"Tile size mismatch: expected {tile_size}x{tile_size}, got {tile.shape[1]}x{tile.shape[0]}"

                # Convert to NCHW
                tile_nchw = tile.transpose(2, 0, 1)[np.newaxis, :, :, :]
                tile_nchw = np.ascontiguousarray(tile_nchw, dtype=np.float32)

                # Run inference with detailed logging for first tile
                if first_tile:
                    logger.info("⏳ [Denoise Worker] 正在调用 ONNX session.run() (第1个分块)... (如果是首次使用 CoreML，可能会卡住 1~5 分钟进行编译，请耐心等待！)")
                    inference_start = time.time()
                    tile_output = session.run(None, {input_name: tile_nchw})[0]
                    inference_end = time.time()
                    logger.info(f"✅ [Denoise Worker] 第1个分块 ONNX 推理成功！耗时: {inference_end - inference_start:.2f} 秒")
                    first_tile = False
                else:
                    tile_output = session.run(None, {input_name: tile_nchw})[0]

                tile_output = tile_output.squeeze(0).transpose(1, 2, 0)

                # Crop to actual size
                tile_output = tile_output[:actual_tile_h, :actual_tile_w, :]
                tile_mask = tile_mask[:actual_tile_h, :actual_tile_w, :]

                # Accumulate
                output[y1:y2, x1:x2, :] += tile_output * tile_mask
                weight[y1:y2, x1:x2, :] += tile_mask

                # Update progress
                processed_tiles += 1
                progress_pct = progress_start + int(progress_range * processed_tiles / total_tiles)
                self.progress.emit(progress_pct)

        # Normalize
        output = output / np.maximum(weight, 1e-5)
        return output

    def is_working(self) -> bool:
        """Check if worker is currently processing."""
        return self._working

    def abort(self):
        """Request abort of current processing."""
        self._mutex.lock()
        self._abort = True
        self._mutex.unlock()


class AIDenoiseThreadController(QObject):
    """
    Convenience class for managing AIDenoiseWorker in a QThread.
    
    This class simplifies the thread management pattern:
    
    Usage:
        controller = AIDenoiseThreadController()
        controller.progress.connect(progress_bar.setValue)
        controller.finished.connect(on_denoise_finished)
        controller.error.connect(on_error)
        
        controller.start_processing(image, params)
        
        # Later, to cancel:
        controller.abort()
    """
    progress = pyqtSignal(int)
    finished = pyqtSignal(np.ndarray)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._thread = None
        self._worker = None
        # Queued (image, params) for a re-entry that arrived while the
        # previous run was still winding down (AUD-31522).
        self._pending: tuple[np.ndarray, dict] | None = None

    def start_processing(self, image: np.ndarray, params: dict):
        """Start denoising in a background thread.

        Re-entry while a previous run is still active NEVER blocks the
        calling thread (AUD-31522): the old run is cooperatively aborted
        (returns immediately; worst-case latency is one inference tile or
        one session load) and this request is queued.  It starts by itself
        from the old thread's finished handler — the UI thread never sits
        in ``QThread.wait()``.
        """
        if self._thread is not None and self._thread.isRunning():
            self.abort()
            self._pending = (image, params)
            return
        self._launch(image, params)

    def _launch(self, image: np.ndarray, params: dict) -> None:
        """Create worker + thread for one run (called with no active run)."""
        # Drop a finished-but-not-yet-swept previous run first: its own
        # deleteLater connections already released the native objects.
        self._worker = None
        self._thread = None
        self._pending = None

        thread = QThread()
        worker = AIDenoiseWorker()

        # Move worker to thread
        worker.moveToThread(thread)

        # Connect signals
        worker.progress.connect(self.progress.emit)
        worker.finished.connect(self._on_finished)
        worker.error.connect(self.error.emit)
        worker.cancelled.connect(self.cancelled.emit)

        # Start processing when the thread's event loop starts. The job is
        # staged on the worker and started through a BOUND slot: a lambda
        # has no QObject receiver, so PyQt would queue it back to this
        # (controller/UI) thread and run the whole inference here.
        worker.submit(image, params)
        thread.started.connect(worker.process_staged)

        # Worker self-deletes in ITS OWN thread (deleteLater is delivered
        # through the worker thread's queue and flushed when the thread
        # finishes) — on ALL exit paths, not just success (AUD-31525
        # side-effect). Calling deleteLater from the controller after the
        # thread died posts to a dead dispatcher and aborts the process.
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)

        # Clean up when finished
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        # Ref bookkeeping + orderly destruction live in _on_thread_finished
        # (NOT a direct finished→deleteLater connection: QThread emits
        # finished just BEFORE clearing its running flag, so an immediate
        # deleteLater can destroy the wrapper while the OS thread is in
        # its final teardown — native abort "QThread: Destroyed while
        # thread is still running").
        thread.finished.connect(self._on_thread_finished)

        thread.start()
        self._thread = thread
        self._worker = worker

    def _on_finished(self, result: np.ndarray):
        """Handle finished signal and clean up."""
        self.finished.emit(result)

    def _on_thread_finished(self) -> None:
        """Retire the signaling thread and clear the controller refs.

        Leaving ``self._thread`` pointing at a deleteLater-destroyed
        QThread made any later ``is_running()`` / ``start_processing()``
        raise RuntimeError on the deleted C++ object (AUD-31520).  The
        worker deletes itself through its own in-thread deleteLater
        connections.  A queued re-entry (AUD-31522) is launched once the
        refs are clear.  ``wait()`` here only covers the emitting
        thread's microsecond-scale final teardown — it never blocks on a
        running inference.
        """
        sender = self.sender()
        if not isinstance(sender, QThread):
            return
        sender.wait()  # full exit before destruction
        sender.deleteLater()
        if sender is self._thread:
            self._thread = None
            self._worker = None
        if self._pending is not None and self._thread is None:
            image, params = self._pending
            self._pending = None
            self._launch(image, params)

    def abort(self):
        """Abort current processing."""
        if self._worker:
            self._worker.abort()

    def is_running(self) -> bool:
        """Check if processing is running."""
        return self._thread is not None and self._thread.isRunning()

    def wait(self):
        """Wait for thread to finish."""
        if self._thread and self._thread.isRunning():
            self._thread.wait()