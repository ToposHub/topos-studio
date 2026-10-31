"""
Denoise Engine - ONNX Runtime Implementation

This module provides AI-powered image denoising using ONNX Runtime.
Supports SwinIR models with tiled processing for large images.

## GPU Acceleration Setup

To enable GPU acceleration for faster inference:

### NVIDIA GPU (CUDA)
```bash
pip uninstall onnxruntime
pip install onnxruntime-gpu
```
Requirements: CUDA 11.x/12.x + cuDNN installed

### Mac Apple Silicon (M1/M2/M3/M4)
CoreML provider is bundled with standard onnxruntime.
```bash
pip install onnxruntime
```

### Windows AMD/Intel GPU (DirectML)
```bash
pip uninstall onnxruntime
pip install onnxruntime-directml
```

If GPU provider fails to load, the system will automatically fall back to CPU.

## Threading Boundaries

- ``DenoiseEngine`` is a controller that lives on the UI (main) thread; its
  public methods are UI-thread only and non-blocking (cancel() waits at most
  ~3s before falling back to terminate()).
- ``DenoiseWorker`` is a QThread: bilateral filtering and ONNX inference run
  entirely on the worker thread. Results, progress and errors cross back to
  the UI thread exclusively via Qt signals -- no OpenGL/UI calls are made
  from the worker.
- ``_session_cache`` is a class-level LRU keyed by model path (max
  ``_SESSION_CACHE_MAX`` sessions), shared by the denoise and deblur
  engines without cross-engine eviction; every read/write goes through
  ``_session_cache_lock`` so concurrent worker threads stay safe.
"""

import os
import time
import logging
import threading
from collections import OrderedDict
import cv2
import numpy as np
import onnxruntime as ort
from PyQt6.QtCore import QObject, pyqtSignal, QThread
from src.shared.ai_models.onnx_env import get_optimal_providers, get_gpu_provider_info

logger = logging.getLogger(__name__)


def get_model_tile_config(model_name: str) -> dict:
    """
    Detect recommended tile_size and overlap from model filename.

    NAFNet-width64 has a ~256px receptive field and needs much larger overlap
    than SwinIR or smaller NAFNet models to avoid tile-boundary artifacts.

    Args:
        model_name: ONNX model filename (e.g. "NAFNet-SIDD-width64.onnx")

    Returns:
        dict with keys: tile_size (int), overlap (int)
    """
    name = os.path.basename(model_name).lower()

    # NAFNet with width64 -> large receptive field
    if "nafnet" in name and "width64" in name:
        return {"tile_size": 1024, "overlap": 256}

    # Default for all other models (SwinIR, NAFNet-width32, etc.)
    return {"tile_size": 512, "overlap": 64}


class DenoiseWorker(QThread):
    """
    Asynchronous Worker for AI Denoising (ONNX Runtime).
    
    Signals:
        finished: Emitted when denoising completes, returns float32 BGR image
        progress: Emitted during tiled processing, returns percentage (0-100)
        error: Emitted on error, returns error message string
    """
    finished = pyqtSignal(np.ndarray)
    progress = pyqtSignal(int)
    error = pyqtSignal(str)

    # AUD-0411：按 model_path 键控的 LRU 会话缓存（denoise/deblur 双引擎
    # 交替使用不再互清缓存），跨 worker 线程的读写由 _session_cache_lock
    # 保护；超出容量的最旧会话被淘汰释放。
    _session_cache: "OrderedDict[str, ort.InferenceSession]" = OrderedDict()
    _session_cache_lock = threading.Lock()
    _SESSION_CACHE_MAX = 2

    # AUD-0415：超时后仍悬挂的加载线程台账。threading 模块只持运行线程的
    # 弱引用——不保留强引用的话 Thread 对象可能在运行中被 GC；这里同时
    # 供后续调用检查回收（悬挂线程最终完成时记录其结果被丢弃）。
    _hung_loader_threads: list = []
    _hung_loader_lock = threading.Lock()

    def __init__(self, image: np.ndarray, mode: str, params: dict):
        """
        Store task inputs; executed on the caller's (UI) thread before start().

        Args:
            image: float32 BGR image [0, 1]. Callers pass a private copy; the
                worker treats it as owned and may modify it in-place.
            mode: "Basic" (bilateral filter) or "AI" (ONNX inference).
            params: Mode-specific parameter dict (see DenoiseEngine.apply_denoise).
        """
        super().__init__()
        self.setObjectName("DenoiseWorker")
        self.image = image  # float32 BGR [0, 1]
        self.mode = mode    # "Basic" or "AI"
        self.params = params

    def run(self):
        """
        QThread entry point: execute the denoising task on the worker thread.

        Dispatches to the bilateral ("Basic") or ONNX ("AI") pipeline and emits
        :attr:`finished` with the result image, or :attr:`error` with the
        message of any exception raised on this thread.
        """
        try:
            if self.mode == "Basic":
                result = self._run_classic_split_denoise()
            elif self.mode == "AI":
                result = self._run_ai()
            else:
                result = self.image
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

    def _run_classic_split_denoise(self) -> np.ndarray:
        """
        High-fidelity 32-bit Float Edge-Preserving Denoise (Bilateral Filter).
        Maintains full dynamic range and sharpness, bypassing OpenCV clipping.

        Splits the image into YUV-style luma/chroma planes via pure arithmetic
        (lossless, supports negative and >1.0 HDR values), applies a bilateral
        filter per plane driven by ``luma_strength`` / ``color_strength``, then
        reconstructs BGR mathematically.

        Returns:
            np.ndarray: denoised float32 BGR image, same shape as the input.
        """
        img_bgr = self.image.astype(np.float32)

        luma_strength = self.params.get("luma_strength", 0.05)
        color_strength = self.params.get("color_strength", 0.1)

        # 1. 纯数学提取 YUV (绝对无损，支持负数和 >1.0 的 HDR)
        B = img_bgr[:, :, 0]
        G = img_bgr[:, :, 1]
        R = img_bgr[:, :, 2]

        Y = 0.299 * R + 0.587 * G + 0.114 * B
        U = B - Y
        V = R - Y

        # 2. 引入保边双边滤波 (Bilateral Filter，原生支持 float32)
        def apply_bilateral(channel, strength, is_chroma=False):
            if strength <= 0.001:
                return channel

            d = int(np.ceil(strength * 15.0)) | 1
            sigma_color = strength * (0.5 if is_chroma else 0.15)
            sigma_space = strength * 25.0

            return cv2.bilateralFilter(channel, d, sigma_color, sigma_space)

        Y_denoised = apply_bilateral(Y, luma_strength, is_chroma=False)
        U_denoised = apply_bilateral(U, color_strength, is_chroma=True)
        V_denoised = apply_bilateral(V, color_strength, is_chroma=True)

        # 3. 数学逆运算完美重建 BGR
        R_out = Y_denoised + V_denoised
        B_out = Y_denoised + U_denoised
        G_out = (Y_denoised - 0.299 * R_out - 0.114 * B_out) / 0.587

        result_bgr = np.stack([B_out, G_out, R_out], axis=-1)
        return result_bgr.astype(np.float32)

    def _run_ai(self) -> np.ndarray:
        """
        AI Denoising using ONNX Runtime (SwinIR).

        Input: float32 BGR, [0, 1] or HDR（>1.0 高光经帧内 max-scaling
        送入模型并在输出端等比还原，AUD-0412）
        Output: float32 BGR, preserves the input dynamic range

        Loads (or reuses a cached) ONNX session, optionally processes the
        image in overlapping tiles with complementary cosine-ramp blending,
        and blends the result with the original according to ``blend``.

        Returns:
            np.ndarray: denoised float32 BGR image, same shape as the input.

        Raises:
            FileNotFoundError: If ``model_path`` is missing or does not exist.
            RuntimeError: If tile processing is cancelled by the user, or the
                model cannot be loaded with any execution provider.
        """
        model_path = self.params.get("model_path")
        blend = self.params.get("blend", 1.0)
        use_tiling = self.params.get("use_tiling", True)
        tile_size = self.params.get("tile_size", 512)
        overlap = self.params.get("overlap", 64)

        if not model_path or not os.path.exists(model_path):
            raise FileNotFoundError(f"Model not found: {model_path}")

        # AUD-0411：会话缓存按 model_path 键控 + LRU 淘汰（见
        # _cache_session），不再因"最近模型"切换整库互清。

        # Load or get cached ONNX session
        session = self._load_onnx_session(model_path)

        # Prepare input image
        h, w = self.image.shape[:2]
        logger.info(f"Engine] Image size: {w}x{h}, tiling={'ON' if use_tiling else 'OFF'}"
              f" (tile={tile_size}, overlap={overlap})")

        self.progress.emit(0)  # Signal: preparation started

        # AUD-0412：保留 HDR 动态范围。模型需要 [0,1] 输入，但整帧硬
        # clip 会把 >1.0 高光永久压平（与 Basic 路径的 HDR 支持不一致）。
        # 送入模型的副本按帧最大值 max-scaling 归一到 [0,1]，输出等比
        # 反缩放；blend 与最终结果使用未裁剪的 original，高光可逆保留。
        original = np.nan_to_num(self.image, nan=0.0, posinf=1.0, neginf=0.0)
        scale = float(max(1.0, original.max())) if original.size else 1.0
        model_input = np.clip(original / scale, 0.0, 1.0)

        # Convert BGR to RGB for model input
        img_input = cv2.cvtColor(model_input, cv2.COLOR_BGR2RGB)

        # Estimate tile count for progress
        if use_tiling:
            stride = tile_size - overlap
            tiles_y = max(1, -(-h // stride))  # ceil division
            tiles_x = max(1, -(-w // stride))
            est_tiles = tiles_y * tiles_x
            logger.info(f"Engine] Estimated tiles: {tiles_y}x{tiles_x}={est_tiles}")

        t_start = time.time()

        if use_tiling:
            output = self._process_tiled_onnx(img_input, session, tile_size, overlap)
        else:
            output = self._process_single_onnx(img_input, session)

        elapsed = time.time() - t_start
        logger.info(f"Engine] Inference completed in {elapsed:.1f}s")

        # Post-process output: undo max-scaling, keep HDR range (AUD-0412)
        output = np.nan_to_num(output, nan=0.0, posinf=scale, neginf=0.0)
        output = np.clip(output, 0.0, 1.0) * scale
        output = cv2.cvtColor(output, cv2.COLOR_RGB2BGR)

        # Blend with the unclipped original if needed
        if blend < 1.0:
            output = cv2.addWeighted(output, blend, original, 1.0 - blend, 0)

        return np.ascontiguousarray(output, dtype=np.float32)

    @classmethod
    def _cache_session(cls, model_path: str, session: "ort.InferenceSession") -> None:
        """AUD-0411：加锁写入 LRU 会话缓存并按容量淘汰最旧会话。"""
        with cls._session_cache_lock:
            cls._session_cache[model_path] = session
            cls._session_cache.move_to_end(model_path)
            while len(cls._session_cache) > cls._SESSION_CACHE_MAX:
                cls._session_cache.popitem(last=False)

    def _load_onnx_session(self, model_path: str) -> ort.InferenceSession:
        """
        Load an ONNX model with optimal settings and GPU acceleration.

        Uses a timeout when trying GPU providers (CoreML/CUDA/DirectML)
        to prevent hangs on models that are too complex for GPU compilation
        (e.g., SwinIR with 4100+ ONNX graph nodes).

        Args:
            model_path: Path to the ONNX model file

        Returns:
            ort.InferenceSession: Loaded ONNX inference session
        """
        # AUD-0411：缓存命中在锁内读取并提升为最近使用
        with DenoiseWorker._session_cache_lock:
            cached = DenoiseWorker._session_cache.get(model_path)
            if cached is not None:
                DenoiseWorker._session_cache.move_to_end(model_path)
                return cached

        # Get optimal providers (GPU prioritized, CPU as fallback)
        providers = get_optimal_providers()

        # Print GPU info for user awareness
        gpu_info = get_gpu_provider_info()
        if gpu_info:
            logger.info(f"Engine] GPU acceleration available: {gpu_info['description']}")
        else:
            logger.info("Engine] No GPU acceleration detected, using CPU.")

        # Configure session options for optimal performance
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.log_severity_level = 3  # Suppress warnings
        sess_options.enable_mem_pattern = True

        session = None
        last_error = None

        for provider in providers:
            # Build provider list with CPU fallback
            if provider == 'CPUExecutionProvider':
                request_providers = ['CPUExecutionProvider']
            else:
                request_providers = [provider, 'CPUExecutionProvider']

            # Skip CPU if we already loaded with GPU
            if provider == 'CPUExecutionProvider' and session is not None:
                continue

            try:
                logger.info(f"Engine] Loading model with providers: {request_providers}")

                # Use timeout for GPU providers to prevent hangs during
                # model compilation (e.g., CoreML compiling SwinIR can hang forever)
                if provider != 'CPUExecutionProvider':
                    session = self._load_with_timeout(
                        model_path, sess_options, request_providers, timeout=30
                    )
                    if session is None:
                        logger.info(f"Engine] GPU provider {provider} timed out (30s), skipping...")
                        continue
                else:
                    session = ort.InferenceSession(
                        model_path,
                        sess_options=sess_options,
                        providers=request_providers
                    )

                actual_providers = session.get_providers()
                primary_provider = request_providers[0]

                if isinstance(primary_provider, tuple):
                    primary_name = primary_provider[0]
                else:
                    primary_name = primary_provider

                if primary_name in actual_providers:
                    logger.info(f"Engine] Successfully loaded with {primary_name}")
                    self._cache_session(model_path, session)
                    return session
                elif primary_name == 'CPUExecutionProvider':
                    logger.info("Engine] Successfully loaded with CPU")
                    self._cache_session(model_path, session)
                    return session
                else:
                    logger.info(f"Engine] Primary provider {primary_name} not available, trying next...")
                    del session
                    session = None

            except Exception as e:
                last_error = e
                logger.error(f"Engine] Failed to load with {request_providers}: {e}")
                if session is not None:
                    del session
                    session = None

        # Pure CPU fallback
        if session is None:
            logger.error("Engine] All providers failed, attempting pure CPU fallback...")
            try:
                session = ort.InferenceSession(
                    model_path,
                    sess_options=sess_options,
                    providers=['CPUExecutionProvider']
                )
                logger.info("Engine] Loaded with CPU fallback")
                self._cache_session(model_path, session)
                return session
            except Exception as e:
                error_msg = f"Failed to load model even with CPU: {e}"
                raise RuntimeError(error_msg) from last_error

        return session

    @staticmethod
    def _load_with_timeout(model_path: str, sess_options: ort.SessionOptions,
                           providers: list, timeout: int = 30) -> ort.InferenceSession:
        """
        Load ONNX session with timeout to prevent GPU compilation hangs.

        Some models (e.g., SwinIR) produce ONNX graphs too complex for
        CoreML/CUDA compilation, causing InferenceSession to hang indefinitely.
        This method wraps the loading in a separate thread with a timeout.

        AUD-0415: 超时后仍存活的加载线程不会消失——保留强引用入台账
        （threading 只持弱引用），并在后续调用时回收已结束的悬挂线程，
        记录其迟到的加载结果被丢弃。

        Args:
            model_path: Path to ONNX model
            sess_options: Session options
            providers: Provider list
            timeout: Maximum seconds to wait

        Returns:
            InferenceSession or None if timed out
        """
        result = [None]

        def _load():
            try:
                result[0] = ort.InferenceSession(
                    model_path,
                    sess_options=sess_options,
                    providers=providers
                )
            except Exception:
                result[0] = None

        # AUD-0415: 先回收此前悬挂、现已自行结束的加载线程（其迟到结果
        # 被丢弃——会话缓存早已回退到其他 provider）。
        with DenoiseWorker._hung_loader_lock:
            still_hung = []
            for entry in DenoiseWorker._hung_loader_threads:
                thread, hung_model_path = entry
                if thread.is_alive():
                    still_hung.append(entry)
                else:
                    logger.info(
                        "Engine] Previously hung loader thread for %s (ident=%s) "
                        "eventually finished; its late session was discarded",
                        hung_model_path, thread.ident)
            DenoiseWorker._hung_loader_threads = still_hung

        loader_thread = threading.Thread(target=_load, daemon=True)
        loader_thread.start()
        loader_thread.join(timeout=timeout)

        if loader_thread.is_alive():
            # Thread is still running = compilation hung
            # AUD-0415: 保留强引用并留痕，避免 Thread 对象被 GC 且泄漏不可见。
            with DenoiseWorker._hung_loader_lock:
                DenoiseWorker._hung_loader_threads.append(
                    (loader_thread, model_path))
            logger.warning(
                "Engine] Model loading exceeded %ds (providers=%s); loader thread "
                "ident=%s left running (tracked in _hung_loader_threads)",
                timeout, providers, loader_thread.ident)
            return None

        return result[0]

    def _process_single_onnx(self, img_rgb: np.ndarray, session: ort.InferenceSession) -> np.ndarray:
        """
        Process entire image without tiling.
        
        Args:
            img_rgb: float32 RGB image [0, 1], shape (H, W, 3)
            session: ONNX InferenceSession
            
        Returns:
            float32 RGB output [0, 1], shape (H, W, 3)
        """
        # Convert HWC to NCHW format
        img_nchw = img_rgb.transpose(2, 0, 1)[np.newaxis, :, :, :]
        img_nchw = np.ascontiguousarray(img_nchw, dtype=np.float32)

        # Get input name
        input_name = session.get_inputs()[0].name

        # Run inference
        outputs = session.run(None, {input_name: img_nchw})
        output = outputs[0]

        # Convert NCHW back to HWC
        output = output.squeeze(0).transpose(1, 2, 0)

        return output

    def _process_tiled_onnx(self, img_rgb: np.ndarray, session: ort.InferenceSession,
                            tile_size: int, overlap: int) -> np.ndarray:
        """
        Process image in tiles with smooth edge blending using Hanning window.
        
        Args:
            img_rgb: float32 RGB image [0, 1], shape (H, W, 3)
            session: ONNX InferenceSession
            tile_size: Size of each tile (e.g., 512, 1024)
            overlap: Overlap between tiles for smooth blending
            
        Returns:
            float32 RGB output [0, 1], shape (H, W, 3)
        """
        h, w, c = img_rgb.shape
        output = np.zeros((h, w, c), dtype=np.float32)
        weight = np.zeros((h, w, c), dtype=np.float32)

        # Create complementary cosine ramps for smooth blending.
        # fade_in (top/left edge): 0 → 1
        # fade_out (bottom/right edge): 1 → 0
        # At every position in the overlap: fade_in + fade_out = 1.0
        # This avoids the Hanning window bug where both adjacent tiles had
        # weight ≈ 0 at overlap boundaries, producing black grid lines.
        if overlap > 0:
            t = np.arange(overlap) / (overlap - 1)  # 0 to 1
            fade_in = 0.5 * (1 - np.cos(np.pi * t))   # 0 → 1
            fade_out = 0.5 * (1 + np.cos(np.pi * t))  # 1 → 0
            ramp_h_in = fade_in[:, np.newaxis, np.newaxis]
            ramp_h_out = fade_out[:, np.newaxis, np.newaxis]
            ramp_w_in = fade_in[np.newaxis, :, np.newaxis]
            ramp_w_out = fade_out[np.newaxis, :, np.newaxis]
        else:
            ramp_h_in = ramp_h_out = ramp_w_in = ramp_w_out = None

        stride = tile_size - overlap

        # Calculate total number of tiles for progress reporting
        total_tiles = 0
        for y in range(0, h, stride):
            for x in range(0, w, stride):
                total_tiles += 1

        processed_tiles = 0
        input_name = session.get_inputs()[0].name
        tiling_start = time.time()

        for y in range(0, h, stride):
            # Phase 5: Check for cooperative cancellation
            if self.isInterruptionRequested():
                logger.info("Denoise worker interrupted at tile row %d", y)
                raise RuntimeError("Cancelled by user")
            
            # Adjust y to ensure full tile coverage at edges
            if y + tile_size > h:
                y = h - tile_size
                if y < 0:
                    y = 0
            for x in range(0, w, stride):
                # Phase 5: Check for cooperative cancellation in inner loop too
                if self.isInterruptionRequested():
                    logger.info("Denoise worker interrupted at tile (%d, %d)", y, x)
                    raise RuntimeError("Cancelled by user")
                
                if x + tile_size > w:
                    x = w - tile_size
                    if x < 0:
                        x = 0

                y1, y2 = y, y + tile_size
                x1, x2 = x, x + tile_size

                # Clamp coordinates to image bounds
                y1 = max(0, y1)
                y2 = min(h, y2)
                x1 = max(0, x1)
                x2 = min(w, x2)

                # Get actual tile dimensions
                actual_tile_h = y2 - y1
                actual_tile_w = x2 - x1

                # Create tile-specific weight mask with complementary ramps
                tile_mask = np.ones((actual_tile_h, actual_tile_w, c), dtype=np.float32)

                if overlap > 0 and ramp_h_in is not None:
                    # Apply complementary ramps on edges
                    # Top edge → fade_in (0→1): low weight at tile top, high toward center
                    if y1 > 0:
                        ramp_len = min(overlap, actual_tile_h)
                        tile_mask[:ramp_len, :, :] *= ramp_h_in[:ramp_len, :, :]
                    # Bottom edge → fade_out (1→0): high weight at center, low at tile bottom
                    if y2 < h:
                        ramp_len = min(overlap, actual_tile_h)
                        tile_mask[-ramp_len:, :, :] *= ramp_h_out[-ramp_len:, :, :]
                    # Left edge → fade_in (0→1)
                    if x1 > 0:
                        ramp_len = min(overlap, actual_tile_w)
                        tile_mask[:, :ramp_len, :] *= ramp_w_in[:, :ramp_len, :]
                    # Right edge → fade_out (1→0)
                    if x2 < w:
                        ramp_len = min(overlap, actual_tile_w)
                        tile_mask[:, -ramp_len:, :] *= ramp_w_out[:, -ramp_len:, :]

                # Extract tile
                tile = img_rgb[y1:y2, x1:x2, :]
                
                # Pad tile to full tile_size if needed (for edge tiles)
                if tile.shape[0] < tile_size or tile.shape[1] < tile_size:
                    padded_tile = np.zeros((tile_size, tile_size, c), dtype=np.float32)
                    padded_tile[:tile.shape[0], :tile.shape[1], :] = tile
                    tile = padded_tile
                    # Also pad the mask
                    padded_mask = np.zeros((tile_size, tile_size, c), dtype=np.float32)
                    padded_mask[:actual_tile_h, :actual_tile_w, :] = tile_mask
                    tile_mask = padded_mask

                # Convert HWC to NCHW
                tile_nchw = tile.transpose(2, 0, 1)[np.newaxis, :, :, :]
                tile_nchw = np.ascontiguousarray(tile_nchw, dtype=np.float32)

                # Run ONNX inference
                tile_t0 = time.time()
                tile_output = session.run(None, {input_name: tile_nchw})[0]
                tile_elapsed = time.time() - tile_t0

                # Convert NCHW back to HWC
                tile_output = tile_output.squeeze(0).transpose(1, 2, 0)

                # Crop output to actual tile size (remove padding)
                tile_output = tile_output[:actual_tile_h, :actual_tile_w, :]
                tile_mask = tile_mask[:actual_tile_h, :actual_tile_w, :]

                # Accumulate weighted results
                output[y1:y2, x1:x2, :] += tile_output * tile_mask
                weight[y1:y2, x1:x2, :] += tile_mask

                # Update progress
                processed_tiles += 1
                progress_pct = int(100 * processed_tiles / total_tiles)
                self.progress.emit(progress_pct)

                # Print progress with ETA
                if processed_tiles == 1 or processed_tiles % 5 == 0 or processed_tiles == total_tiles:
                    avg_tile_time = (time.time() - tiling_start) / processed_tiles
                    remaining = (total_tiles - processed_tiles) * avg_tile_time
                    logger.info(f"Engine] Tile {processed_tiles}/{total_tiles} "
                          f"({tile_elapsed:.1f}s) ETA: {remaining:.0f}s remaining")

                # Break at edges
                if x == w - tile_size or x2 >= w:
                    break
            if y == h - tile_size or y2 >= h:
                break

        # Normalize by weight to get final output
        output = output / np.maximum(weight, 1e-5)
        return output


class DenoiseEngine(QObject):
    """
    Main Denoise Engine Controller.
    
    Signals:
        processing_finished: Emitted when denoising completes
        progress_updated: Emitted during processing with percentage
        error_occurred: Emitted on error
        busy: Emitted when a task is rejected because another is running
    """
    processing_finished = pyqtSignal(np.ndarray)
    progress_updated = pyqtSignal(int)
    error_occurred = pyqtSignal(str)
    busy = pyqtSignal()

    def __init__(self, model_subdir: str = "denoise"):
        """
        Create the engine controller (UI thread).

        Resolves the model directory to ``<project_root>/models/<model_subdir>``
        from which get_available_models() discovers .onnx files. No worker
        thread is created until apply_denoise() is called.

        Args:
            model_subdir: Directory name under ``models/`` holding ONNX models.
        """
        super().__init__()
        self.worker = None
        self.project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.model_dir = os.path.join(self.project_root, "models", model_subdir)

    def apply_denoise(self, image: np.ndarray, mode: str, params: dict) -> bool:
        """
        Apply denoising to an image asynchronously.
        
        Args:
            image: float32 BGR image [0, 1]
            mode: "Basic" for bilateral filter, "AI" for ONNX model
            params: Dict with parameters:
                - For Basic: luma_strength, color_strength
                - For AI: model_path, blend, use_tiling, tile_size, overlap
        
        Returns:
            bool: True if task started, False if busy
        """
        if self.worker and self.worker.isRunning():
            self.busy.emit()
            return False

        if mode == "AI" and "model_path" in params:
            if not os.path.isabs(params["model_path"]):
                params["model_path"] = os.path.join(self.model_dir, os.path.basename(params["model_path"]))

        self.worker = DenoiseWorker(image.copy(), mode, params)
        self.worker.finished.connect(self.processing_finished.emit)
        self.worker.progress.connect(self.progress_updated.emit)
        self.worker.error.connect(self.error_occurred.emit)
        self.worker.start()
        return True

    def get_available_models(self) -> list:
        """
        Get list of available ONNX denoise models.
        
        Returns:
            List of model filenames (e.g., ["swinir_denoise.onnx"])
        """
        if not os.path.exists(self.model_dir):
            try:
                os.makedirs(self.model_dir, exist_ok=True)
            except Exception:
                pass
            return []

        models = []
        for f in os.listdir(self.model_dir):
            if f.endswith(".onnx"):
                models.append(f)
        return sorted(models)

    def is_processing(self) -> bool:
        """Check if a denoising task is currently running.

        Returns:
            bool: True while a DenoiseWorker thread is executing.
        """
        return self.worker is not None and self.worker.isRunning()

    def cancel(self) -> bool:
        """Cancel the current denoising task using cooperative interruption.
        
        Phase 5: Collaborative cancellation instead of terminate/wait.
        - Request interruption (worker checks isInterruptionRequested in tile loop)
        - Wait with timeout (max 2 seconds)
        - If still running, force terminate as last resort
        
        This prevents UI thread blocking while ensuring cancellation completes.
        See docs/color_tools_acceptance_residual_plan_2026-05-24.md Phase 5.
        """
        if not (self.worker and self.worker.isRunning()):
            return True

        self.worker.requestInterruption()
        if self.worker.wait(2000):
            return True

        logger.warning("Denoise worker did not respond to interruption, forcing terminate")
        self.worker.terminate()
        if self.worker.wait(1000):
            return True

        logger.error("Denoise worker did not stop within timeout after terminate")
        return False


# Alias for semantic clarity in other modules
RestorationEngine = DenoiseEngine
