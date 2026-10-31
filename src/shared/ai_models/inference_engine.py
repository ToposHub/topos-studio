"""
AI Inference Engine - Unified ONNX inference pipeline for all AI nodes.

This module provides a unified inference engine that supports:
- Multiple node types (denoise, inpaint, mask)
- Tiled processing for large images
- Streaming preview (low-res first, full-res later)
- Progress reporting for UI integration
- GPU acceleration via ONNX Runtime

Architecture:
    The inference engine is designed to work with AIComputeManager:
    - AIComputeManager receives requests from AI nodes
    - InferenceWorker processes the request in background
    - Results are saved to cache via AICacheManager

Key Features:
    1. **Tiled Processing**: Splits large images into tiles to fit GPU memory,
       with Hanning window blending for seamless results.

    2. **Streaming Preview**: During inference, emits intermediate low-res
       results for preview while full-res processing continues.

    3. **Progress Reporting**: Real-time progress updates for UI progress bars.

    4. **Model Caching**: ONNX sessions are cached to avoid repeated loading.
"""

import os
import cv2
import threading
import traceback
from collections import OrderedDict
import numpy as np
import onnxruntime as ort
import logging

logger = logging.getLogger(__name__)
from typing import Dict, Optional, Tuple, Callable
from PyQt6.QtCore import QObject, pyqtSignal, QThread, QMutex

from .onnx_env import (
    get_configured_providers,
    get_session_options,
    get_configured_tile_size,
    get_gpu_provider_info,
)
from .model_manager import get_model_manager


class _InferenceCancelledError(RuntimeError):
    """用户主动取消（AUD-31803）。

    与真实推理失败区分：run() 的统一 except 识别本异常后静默返回，
    不走 error/inference_failed 信号——主动取消不得被上报为推理失败。
    """


class AIInferenceWorker(QThread):
    """
    Unified background worker for all AI inference tasks.

    Supports:
    - Multiple node types: denoise, inpaint, mask
    - Tiled processing with progress reporting
    - Streaming preview results
    - Cancel support

    Signals:
        finished: (cache_key, result_image, request_id) — inference completed
        preview: (cache_key, preview_image, request_id) — streaming preview result
        progress: (cache_key, percentage) — progress update (0-100)
        error: (cache_key, error_message, request_id) — inference failed
    """

    finished = pyqtSignal(str, np.ndarray, int)  # cache_key, result, request_id
    preview = pyqtSignal(str, np.ndarray, int)   # cache_key, preview_result, request_id
    progress = pyqtSignal(str, int)              # cache_key, percentage
    error = pyqtSignal(str, str, int)            # cache_key, error_message, request_id

    # Shared ONNX session cache.
    # AUD-31807: LRU 上限（OrderedDict，命中移到末尾、超限淘汰最旧）。
    # 自定义 params["model_path"] 可指向任意路径，无上限时每次新路径都
    # 常驻一份会话（内存/显存）。``_loading_locks`` 为 per-path 互斥锁表
    # （check-then-act 修复）：同一模型并发首载只创建一份会话。
    _SESSION_CACHE_MAX = 4
    _session_cache: "OrderedDict[str, ort.InferenceSession]" = OrderedDict()
    _loading_locks: Dict[str, threading.Lock] = {}
    _cache_mutex = QMutex()

    def __init__(
        self,
        cache_key: str,
        node_type: str,
        input_images: Dict[str, np.ndarray],
        params: Dict,
        request_id: int = 0,
    ):
        super().__init__()
        self.cache_key = cache_key
        self.node_type = node_type
        self.input_images = input_images
        self.params = params
        self.request_id = request_id
        self._cancelled = False
        self._stale_callback: Optional[Callable[[int], bool]] = None
        # AUD-31804: HDR 输入的峰值缩放（SDR 恒为 1.0）。
        self._hdr_scale: float = 1.0

    def set_stale_callback(self, callback: Callable[[int], bool]):
        """Set callback to check if request is stale."""
        self._stale_callback = callback

    def cancel(self):
        """Request cancellation."""
        self._cancelled = True

    def run(self):
        """Execute inference in background thread."""
        try:
            # Report initial progress
            self.progress.emit(self.cache_key, 5)

            # Check if stale
            if self._stale_callback and self._stale_callback(self.request_id):
                self.error.emit(self.cache_key, "Request stale", self.request_id)
                return

            # Get primary image
            image = self.input_images.get("image")
            if image is None:
                self.error.emit(self.cache_key, "No input image", self.request_id)
                return

            mask = self.input_images.get("mask")  # Optional for inpaint

            # Get model for this node type
            model_path = self._get_model_path()
            if model_path is None:
                self.error.emit(
                    self.cache_key,
                    f"Model not available for {self.node_type}",
                    self.request_id,
                )
                return

            self.progress.emit(self.cache_key, 10)

            # Load ONNX session
            session = self._load_onnx_session(model_path)
            if self._cancelled:
                return

            self.progress.emit(self.cache_key, 15)

            # Get processing parameters
            tile_size = self.params.get("tile_size") or get_configured_tile_size()
            overlap = self.params.get("overlap", 64)
            # AUD-31802: 钳制 tile/overlap 关系，保证 stride > 0、
            # overlap 权重无除零（手改配置或 params 可绕过 UI 限制）。
            tile_size, overlap = self._normalize_tiling(tile_size, overlap)
            use_tiling = self.params.get("use_tiling", True)
            enable_preview = self.params.get("enable_preview", True)
            preview_scale = self.params.get("preview_scale", 0.5)

            # AUD-31804: 记录输入 HDR 峰值，输入侧显式缩放到模型域 [0,1]，
            # 输出侧对称恢复（不再无条件 clip(0,1) 截断高光）。
            self._hdr_scale = self._hdr_scale_for(image)

            # Special handling for ai_expand: prepare expanded canvas and mask
            original_image = None
            expand_params = None
            if self.node_type == "ai_expand":
                original_image = image.copy()
                image, mask, expand_params = self._prepare_outpaint_input(image, self.params)

            # Step 1: Generate streaming preview (low-res)
            if enable_preview and preview_scale < 1.0:
                self._emit_preview(image, mask, session, preview_scale)

            if self._cancelled:
                return

            # Step 2: Full resolution processing
            self.progress.emit(self.cache_key, 30)

            result = self._process_full_resolution(
                image, mask, session, tile_size, overlap, use_tiling
            )

            if self._cancelled:
                return

            # Finalize
            result = self._postprocess(result, self.params)

            # Special handling for ai_expand: blend result with original
            if self.node_type == "ai_expand" and original_image is not None and expand_params is not None:
                result = self._blend_outpaint_result(result, original_image, expand_params)

            self.progress.emit(self.cache_key, 100)

            # Emit finished
            self.finished.emit(self.cache_key, result, self.request_id)

        except _InferenceCancelledError:
            # AUD-31803: 用户主动取消——既非失败也非完成，静默退出，
            # 与 run() 中其他 `if self._cancelled: return` 路径行为一致。
            return
        except Exception as e:
            error_msg = f"{self.node_type} inference error: {str(e)}\n{traceback.format_exc()}"
            logger.error(f"Error: {error_msg}")
            self.error.emit(self.cache_key, error_msg, self.request_id)

    _MIN_TILE_SIZE = 128

    @staticmethod
    def _normalize_tiling(
        tile_size: Optional[int], overlap: Optional[int]
    ) -> Tuple[int, int]:
        """校验/钳制 tile_size 与 overlap 的关系（AUD-31802）。

        - tile_size 下限 128（过小瓦片只增加调度开销）。
        - overlap 钳到 ``[0, tile_size // 2]``，保证
          ``stride = tile_size - overlap > 0``：旧代码对
          ``overlap >= tile_size`` 会静默产出整幅黑图（stride<0 时
          range 为空）或抛 ValueError（stride==0）。
        """
        size = int(tile_size) if tile_size else 512
        size = max(size, AIInferenceWorker._MIN_TILE_SIZE)
        ov = int(overlap) if overlap else 0
        ov = max(0, min(ov, size // 2))
        return size, ov

    @staticmethod
    def _hdr_scale_for(image: np.ndarray) -> float:
        """HDR 浮点输入的峰值缩放（AUD-31804）。

        ONNX 模型按 [0,1] 训练：直接喂 HDR（>1.0）数值饱和。输入侧除以
        峰值映射到 [0,1]，输出侧乘回，保证与输出范围处理对称。SDR
        （峰值 <= 1.0）与整数 dtype 输入恒为 1.0，行为不变。
        """
        if not np.issubdtype(image.dtype, np.floating):
            return 1.0
        finite = image[np.isfinite(image)]
        if finite.size == 0:
            return 1.0
        peak = float(finite.max())
        return peak if peak > 1.0 else 1.0

    def _prepare_outpaint_input(
        self,
        image: np.ndarray,
        params: Dict,
    ) -> Tuple[np.ndarray, np.ndarray, Dict]:
        """Prepare expanded canvas and mask for outpainting.

        Creates an expanded canvas with:
        - Original image placed in center
        - Border regions filled with reflected padding
        - Mask indicating AI-generated regions (with feather gradient)

        Args:
            image: Original image (BGR float32, [0, 1])
            params: Outpaint parameters (expand_left/right/top/bottom, blend_radius)

        Returns:
            Tuple of (expanded_canvas, mask, expand_params) where:
            - expanded_canvas: BGR float32 array with expanded dimensions
            - mask: float32 array, 1 = AI region, 0 = original
            - expand_params: Dict with expansion parameters for blending
        """
        h, w = image.shape[:2]

        # Get expansion parameters
        expand_left = int(params.get("expand_left", 0))
        expand_right = int(params.get("expand_right", 0))
        expand_top = int(params.get("expand_top", 0))
        expand_bottom = int(params.get("expand_bottom", 0))
        blend_radius = int(params.get("blend_radius", 32))

        # Calculate new dimensions
        new_w = w + expand_left + expand_right
        new_h = h + expand_top + expand_bottom

        # Ensure minimum size
        new_w = max(new_w, 1)
        new_h = max(new_h, 1)

        # Create expanded canvas with reflected padding
        canvas = cv2.copyMakeBorder(
            image,
            expand_top,
            expand_bottom,
            expand_left,
            expand_right,
            cv2.BORDER_REFLECT_101
        )

        # Ensure canvas matches expected size
        if canvas.shape[0] != new_h or canvas.shape[1] != new_w:
            canvas = cv2.resize(canvas, (new_w, new_h), interpolation=cv2.INTER_AREA)

        # Create inpainting mask
        # 1.0 = region to inpaint (expanded borders)
        # 0.0 = original image region (preserve)
        mask = np.ones((new_h, new_w), dtype=np.float32)

        # Mark original image region as 0 (no inpaint needed)
        y1 = max(0, min(expand_top, new_h))
        y2 = max(0, min(expand_top + h, new_h))
        x1 = max(0, min(expand_left, new_w))
        x2 = max(0, min(expand_left + w, new_w))

        if y1 < y2 and x1 < x2:
            mask[y1:y2, x1:x2] = 0.0

        # Add feather gradient at borders for seamless blending
        if blend_radius > 0:
            mask = self._create_outpaint_feather_mask(
                mask, expand_left, expand_right, expand_top, expand_bottom,
                blend_radius, h, w
            )

        expand_params = {
            "expand_left": expand_left,
            "expand_right": expand_right,
            "expand_top": expand_top,
            "expand_bottom": expand_bottom,
            "blend_radius": blend_radius,
            "orig_h": h,
            "orig_w": w,
        }

        return canvas, mask, expand_params

    def _create_outpaint_feather_mask(
        self,
        mask: np.ndarray,
        expand_left: int,
        expand_right: int,
        expand_top: int,
        expand_bottom: int,
        blend_radius: int,
        orig_h: int,
        orig_w: int,
    ) -> np.ndarray:
        """Create feather gradient at border for seamless blending.

        Args:
            mask: Base mask (1 in expanded region, 0 in original)
            expand_*: Expansion pixels for each direction
            blend_radius: Feather zone width
            orig_h/w: Original image dimensions

        Returns:
            Feathered mask with gradient transitions
        """
        new_h, new_w = mask.shape[:2]

        # Calculate original image region in new canvas
        y1 = expand_top
        y2 = expand_top + orig_h
        x1 = expand_left
        x2 = expand_left + orig_w

        # Clamp to valid bounds
        y1 = max(0, min(y1, new_h))
        y2 = max(0, min(y2, new_h))
        x1 = max(0, min(x1, new_w))
        x2 = max(0, min(x2, new_w))

        # Horizontal gradients (left/right borders)
        if expand_left > 0 and x1 > 0:
            feather_start = max(0, x1 - blend_radius)
            feather_end = min(x1, new_w)
            if feather_start < feather_end:
                width = feather_end - feather_start
                gradient = np.linspace(1.0, 0.0, width)
                mask[y1:y2, feather_start:feather_end] = gradient

        if expand_right > 0 and x2 < new_w:
            feather_start = x2
            feather_end = min(x2 + blend_radius, new_w)
            if feather_start < feather_end:
                width = feather_end - feather_start
                gradient = np.linspace(0.0, 1.0, width)
                mask[y1:y2, feather_start:feather_end] = gradient

        # Vertical gradients (top/bottom borders)
        if expand_top > 0 and y1 > 0:
            feather_start = max(0, y1 - blend_radius)
            feather_end = min(y1, new_h)
            if feather_start < feather_end:
                height = feather_end - feather_start
                gradient = np.linspace(1.0, 0.0, height)
                mask[feather_start:feather_end, x1:x2] = gradient[:, np.newaxis]

        if expand_bottom > 0 and y2 < new_h:
            feather_start = y2
            feather_end = min(y2 + blend_radius, new_h)
            if feather_start < feather_end:
                height = feather_end - feather_start
                gradient = np.linspace(0.0, 1.0, height)
                mask[feather_start:feather_end, x1:x2] = gradient[:, np.newaxis]

        return mask

    def _blend_outpaint_result(
        self,
        result: np.ndarray,
        original: np.ndarray,
        expand_params: Dict,
    ) -> np.ndarray:
        """Blend AI-generated result with original image for seamless output.

        Args:
            result: AI-generated full canvas (BGR float32)
            original: Original image (BGR float32)
            expand_params: Dict with expansion parameters

        Returns:
            Blended result with seamless transitions
        """
        expand_left = expand_params["expand_left"]
        expand_right = expand_params["expand_right"]
        expand_top = expand_params["expand_top"]
        expand_bottom = expand_params["expand_bottom"]
        blend_radius = expand_params["blend_radius"]
        orig_h = expand_params["orig_h"]
        orig_w = expand_params["orig_w"]

        new_h, new_w = result.shape[:2]

        # Create blend mask (inverse of inpaint mask)
        # 1.0 = use original, 0.0 = use AI result
        blend_mask = np.zeros((new_h, new_w), dtype=np.float32)

        # Mark original region
        y1 = max(0, min(expand_top, new_h))
        y2 = max(0, min(expand_top + orig_h, new_h))
        x1 = max(0, min(expand_left, new_w))
        x2 = max(0, min(expand_left + orig_w, new_w))

        if y1 < y2 and x1 < x2:
            blend_mask[y1:y2, x1:x2] = 1.0

        # Add feather gradient
        if blend_radius > 0:
            # Horizontal feathers
            if expand_left > 0 and x1 > 0:
                feather_start = max(0, x1 - blend_radius)
                feather_end = min(x1, new_w)
                if feather_start < feather_end:
                    width = feather_end - feather_start
                    gradient = np.linspace(0.0, 1.0, width)
                    blend_mask[y1:y2, feather_start:feather_end] = gradient

            if expand_right > 0 and x2 < new_w:
                feather_start = x2
                feather_end = min(x2 + blend_radius, new_w)
                if feather_start < feather_end:
                    width = feather_end - feather_start
                    gradient = np.linspace(1.0, 0.0, width)
                    blend_mask[y1:y2, feather_start:feather_end] = gradient

            # Vertical feathers
            if expand_top > 0 and y1 > 0:
                feather_start = max(0, y1 - blend_radius)
                feather_end = min(y1, new_h)
                if feather_start < feather_end:
                    height = feather_end - feather_start
                    gradient = np.linspace(0.0, 1.0, height)
                    blend_mask[feather_start:feather_end, x1:x2] = gradient[:, np.newaxis]

            if expand_bottom > 0 and y2 < new_h:
                feather_start = y2
                feather_end = min(y2 + blend_radius, new_h)
                if feather_start < feather_end:
                    height = feather_end - feather_start
                    gradient = np.linspace(1.0, 0.0, height)
                    blend_mask[feather_start:feather_end, x1:x2] = gradient[:, np.newaxis]

        # Blend in feather zone
        if y1 < y2 and x1 < x2:
            # Extract original region
            original_region = original[:min(orig_h, y2 - y1), :min(orig_w, x2 - x1)]

            # Get blend mask for this region
            region_mask = blend_mask[y1:y2, x1:x2]

            # Expand mask to match channels
            if region_mask.ndim == 2:
                region_mask = region_mask[:, :, np.newaxis]

            # Blend
            result[y1:y2, x1:x2] = (
                original_region * region_mask +
                result[y1:y2, x1:x2] * (1.0 - region_mask)
            )

        return result

    def _get_model_path(self) -> Optional[str]:
        """Get model path based on node type."""
        model_manager = get_model_manager()

        # Map node types to model IDs
        model_mapping = {
            "ai_denoise": "swinir_denoise",
            "ai_inpaint": "lama_inpaint",
            "ai_expand": "lama_inpaint",  # Outpainting uses inpainting model
        }

        model_id = model_mapping.get(self.node_type)
        if model_id:
            return model_manager.get_model_path(model_id)

        # Check for custom model path in params
        custom_path = self.params.get("model_path")
        if custom_path and os.path.exists(custom_path):
            return custom_path

        return None

    def _load_onnx_session(self, model_path: str) -> ort.InferenceSession:
        """Load ONNX session with caching.

        AUD-31807：三重防护——
        1. 缓存命中（LRU 触碰）直接返回；
        2. 未命中时取 per-path 互斥锁，锁内二次检查（double-checked），
           并发同路径首载只建一份会话（GPU EP 下双份显存峰值的问题消除）；
        3. 缓存插入时按 ``_SESSION_CACHE_MAX`` 淘汰最旧会话（引用释放后
           由 GC/ONNX Runtime 回收其原生资源）。
        """
        # 1. Cache read (LRU touch) under the mutex
        cached = AIInferenceWorker._cache_get(model_path)
        if cached is not None:
            return cached

        # 2. Per-path load lock (double-checked loading)
        path_lock = AIInferenceWorker._path_lock(model_path)
        with path_lock:
            cached = AIInferenceWorker._cache_get(model_path)
            if cached is not None:
                # 另一线程在我们等锁期间已完成加载
                return cached

            # Get providers
            providers = get_configured_providers()

            gpu_info = get_gpu_provider_info()
            if gpu_info and gpu_info.get("provider"):
                logger.info(f"Using GPU: {gpu_info['description']}")
            else:
                logger.info("Using CPU")

            sess_options = get_session_options()

            session = None
            for provider in providers:
                if self._cancelled:
                    raise _InferenceCancelledError()

                request_providers = [provider, "CPUExecutionProvider"]
                if provider == "CPUExecutionProvider":
                    request_providers = ["CPUExecutionProvider"]

                try:
                    session = ort.InferenceSession(
                        model_path, sess_options=sess_options, providers=request_providers
                    )
                    actual = session.get_providers()
                    primary = request_providers[0]
                    if isinstance(primary, tuple):
                        primary = primary[0]

                    if primary in actual:
                        logger.info(f"Loaded with {primary}")
                        break
                    else:
                        del session
                        session = None
                except Exception as e:
                    logger.error(f"Failed with {provider}: {e}")
                    if session:
                        del session
                        session = None

            # CPU fallback
            if session is None:
                session = ort.InferenceSession(
                    model_path,
                    sess_options=sess_options,
                    providers=["CPUExecutionProvider"],
                )
                logger.info("Loaded with CPU fallback")

            # 3. Cache insert with LRU eviction
            AIInferenceWorker._cache_put(model_path, session)

        return session

    # ------------------------------------------------------------------
    # 类级缓存工具（AUD-31807）：统一持 ``_cache_mutex`` 访问。
    # ------------------------------------------------------------------
    @staticmethod
    def _cache_get(model_path: str) -> Optional[ort.InferenceSession]:
        AIInferenceWorker._cache_mutex.lock()
        try:
            session = AIInferenceWorker._session_cache.get(model_path)
            if session is not None:
                AIInferenceWorker._session_cache.move_to_end(model_path)
            return session
        finally:
            AIInferenceWorker._cache_mutex.unlock()

    @staticmethod
    def _path_lock(model_path: str) -> threading.Lock:
        AIInferenceWorker._cache_mutex.lock()
        try:
            lock = AIInferenceWorker._loading_locks.get(model_path)
            if lock is None:
                lock = threading.Lock()
                AIInferenceWorker._loading_locks[model_path] = lock
            return lock
        finally:
            AIInferenceWorker._cache_mutex.unlock()

    @staticmethod
    def _cache_put(model_path: str, session: ort.InferenceSession) -> None:
        AIInferenceWorker._cache_mutex.lock()
        try:
            cache = AIInferenceWorker._session_cache
            cache[model_path] = session
            cache.move_to_end(model_path)
            while len(cache) > AIInferenceWorker._SESSION_CACHE_MAX:
                _evicted_path, evicted = cache.popitem(last=False)
                logger.info(
                    "ONNX session cache full; evicted %s", _evicted_path,
                )
                del evicted
            # per-path load 锁仅在下表膨胀时清理（自定义路径可任意多，但
            # 单个锁对象极小）：只移除已不在缓存中的路径的锁。正在持锁加载
            # 的线程仍持有该锁对象引用，删除表项对其无影响。
            if len(AIInferenceWorker._loading_locks) > 32:
                for stale in [
                    p for p in AIInferenceWorker._loading_locks
                    if p not in cache
                ]:
                    AIInferenceWorker._loading_locks.pop(stale, None)
        finally:
            AIInferenceWorker._cache_mutex.unlock()

    def _emit_preview(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: ort.InferenceSession,
        preview_scale: float,
    ):
        """Generate and emit streaming preview (low-res result)."""
        h, w = image.shape[:2]
        new_h = int(h * preview_scale)
        new_w = int(w * preview_scale)

        # Resize for preview
        image_small = cv2.resize(
            image, (new_w, new_h), interpolation=cv2.INTER_AREA
        )
        mask_small = None
        if mask is not None:
            mask_small = cv2.resize(
                mask, (new_w, new_h), interpolation=cv2.INTER_NEAREST
            )

        # Process at preview resolution
        # AUD-31808：_run_inference 的死参数 tile_size/overlap 已删除，
        # 调用点同步收敛（分块策略完全由 _process_tiled / 全分辨率路径决定）。
        result_small = self._run_inference(image_small, mask_small, session)

        if self._cancelled:
            return

        # Emit preview result
        self.progress.emit(self.cache_key, 25)
        self.preview.emit(self.cache_key, result_small, self.request_id)

    def _process_full_resolution(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: ort.InferenceSession,
        tile_size: int,
        overlap: int,
        use_tiling: bool,
    ) -> np.ndarray:
        """Process image at full resolution."""
        if use_tiling:
            return self._process_tiled(image, mask, session, tile_size, overlap)
        else:
            return self._run_inference(image, mask, session)

    def _process_tiled(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: ort.InferenceSession,
        tile_size: int,
        overlap: int,
    ) -> np.ndarray:
        """
        Process image in tiles with Hanning window blending.

        This method splits large images into overlapping tiles,
        processes each tile independently, and blends results
        using Hanning windows for seamless output.
        """
        h, w, c = image.shape[:3]
        output = np.zeros((h, w, c), dtype=np.float32)
        weight = np.zeros((h, w, c), dtype=np.float32)

        # Create Hanning window ramps for blending
        if overlap > 0:
            if overlap == 1:
                # AUD-31802: 单像素重叠无渐变可言；权重恒 1，
                # 避免 np.arange(1)/0 的除零（旧代码产生 inf/NaN 权重）。
                t = np.ones(1, dtype=np.float64)
            else:
                t = np.arange(overlap) / (overlap - 1)
            fade_in = 0.5 * (1 - np.cos(np.pi * t))   # 0 -> 1
            fade_out = 0.5 * (1 + np.cos(np.pi * t))  # 1 -> 0
            ramp_h_in = fade_in[:, np.newaxis, np.newaxis]
            ramp_h_out = fade_out[:, np.newaxis, np.newaxis]
            ramp_w_in = fade_in[np.newaxis, :, np.newaxis]
            ramp_w_out = fade_out[np.newaxis, :, np.newaxis]
        else:
            ramp_h_in = ramp_h_out = ramp_w_in = ramp_w_out = None

        stride = tile_size - overlap

        # Calculate tile positions
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
        if total_tiles == 0:
            # AUD-31802: 空瓦片计划必须显式报错；旧代码会输出全零"黑图"
            # 并作为 finished 结果正常发出。
            raise ValueError(
                f"tiled processing produced no tiles "
                f"(image {w}x{h}, tile_size={tile_size}, overlap={overlap})"
            )
        processed_tiles = 0

        # Progress range: 30-90 for tile processing
        progress_start = 30
        progress_end = 90
        progress_range = progress_end - progress_start

        for y in y_positions:
            for x in x_positions:
                if self._cancelled:
                    raise _InferenceCancelledError()

                # Extract tile region
                y1, y2 = y, min(y + tile_size, h)
                x1, x2 = x, min(x + tile_size, w)

                tile_h = y2 - y1
                tile_w = x2 - x1

                # Create weight mask with Hanning blending
                tile_mask = np.ones((tile_h, tile_w, c), dtype=np.float32)
                if overlap > 0 and ramp_h_in is not None:
                    if y1 > 0:
                        ramp_len = min(overlap, tile_h)
                        tile_mask[:ramp_len, :, :] *= ramp_h_in[:ramp_len, :, :]
                    if y2 < h:
                        ramp_len = min(overlap, tile_h)
                        tile_mask[-ramp_len:, :, :] *= ramp_h_out[-ramp_len:, :, :]
                    if x1 > 0:
                        ramp_len = min(overlap, tile_w)
                        tile_mask[:, :ramp_len, :] *= ramp_w_in[:, :ramp_len, :]
                    if x2 < w:
                        ramp_len = min(overlap, tile_w)
                        tile_mask[:, -ramp_len:, :] *= ramp_w_out[:, -ramp_len:, :]

                # Extract tile and mask
                tile_img = image[y1:y2, x1:x2, :]
                tile_mask_input = None
                if mask is not None:
                    tile_mask_input = mask[y1:y2, x1:x2]

                # Pad if needed (edge tiles)
                pad_h = tile_size - tile_h if tile_h < tile_size else 0
                pad_w = tile_size - tile_w if tile_w < tile_size else 0

                if pad_h > 0 or pad_w > 0:
                    tile_img = cv2.copyMakeBorder(
                        tile_img, 0, pad_h, 0, pad_w, cv2.BORDER_REFLECT_101
                    )
                    tile_mask = cv2.copyMakeBorder(
                        tile_mask, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=0.0
                    )
                    if tile_mask_input is not None:
                        tile_mask_input = cv2.copyMakeBorder(
                            tile_mask_input, 0, pad_h, 0, pad_w, cv2.BORDER_REFLECT_101
                        )

                # Run inference on tile
                tile_result = self._run_inference(
                    tile_img, tile_mask_input, session
                )

                # Crop to original size
                tile_result = tile_result[:tile_h, :tile_w, :]
                tile_mask = tile_mask[:tile_h, :tile_w, :]

                # Accumulate
                output[y1:y2, x1:x2, :] += tile_result * tile_mask
                weight[y1:y2, x1:x2, :] += tile_mask

                # Update progress
                processed_tiles += 1
                progress_pct = progress_start + int(
                    progress_range * processed_tiles / total_tiles
                )
                self.progress.emit(self.cache_key, progress_pct)

        # Normalize by weight
        output = output / np.maximum(weight, 1e-5)
        return output

    def _run_inference(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: ort.InferenceSession,
    ) -> np.ndarray:
        """
        Run ONNX inference for a single tile or entire image.

        This method handles input preparation and output extraction
        based on the node type.

        AUD-31808：删除从未被使用的 ``tile_size`` / ``overlap`` 死形参
        （分块逻辑在 _process_tiled，本方法只做单次前向）。
        """
        # Get input/output names
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name

        # AUD-31804: 输入侧显式映射到模型域 [0,1]（除以 HDR 峰值），
        # 输出侧对称乘回——同一管线内输入/输出/blend 的值域保持一致。
        scale = self._hdr_scale
        source = image if scale == 1.0 else (image / scale)

        # Prepare input based on node type
        if self.node_type == "ai_inpaint":
            # Inpainting: concatenate image and mask as input
            img_input = self._prepare_inpaint_input(source, mask)
        elif self.node_type == "ai_expand":
            # Outpainting: same input format as inpainting
            img_input = self._prepare_inpaint_input(source, mask)
        elif self.node_type == "ai_denoise":
            # Denoising: RGB image input
            img_input = self._prepare_denoise_input(source)
        else:
            # Generic: assume RGB
            img_input = self._prepare_generic_input(source)

        # Run inference
        outputs = session.run([output_name], {input_name: img_input})

        # Extract output
        result = outputs[0]

        # Convert from NCHW to HWC
        if result.ndim == 4:
            result = result.squeeze(0).transpose(1, 2, 0)

        if scale != 1.0:
            result = result * scale

        return result.astype(np.float32)

    def _prepare_inpaint_input(
        self, image: np.ndarray, mask: Optional[np.ndarray]
    ) -> np.ndarray:
        """
        Prepare input tensor for inpainting model (LaMa).

        LaMa expects: [1, 4, H, W] float32
        - Channels 0-2: RGB image [0, 1]
        - Channel 3: Mask (1 = masked, 0 = unmasked)
        """
        # Convert BGR to RGB
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        # Ensure mask
        if mask is None:
            mask = np.zeros(image.shape[:2], dtype=np.float32)
        else:
            mask = mask.astype(np.float32)

        # Normalize mask: 1 = area to inpaint
        mask = np.clip(mask, 0, 1)

        # Stack image and mask
        combined = np.concatenate([img_rgb, mask[:, :, np.newaxis]], axis=2)

        # Convert to NCHW
        nchw = combined.transpose(2, 0, 1)[np.newaxis, :, :, :]
        return np.ascontiguousarray(nchw, dtype=np.float32)

    def _prepare_denoise_input(self, image: np.ndarray) -> np.ndarray:
        """
        Prepare input tensor for denoising model (NAFNet/SwinIR).

        Models expect: [1, 3, H, W] float32 RGB [0, 1]
        """
        # Convert BGR to RGB
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        # Convert to NCHW
        nchw = img_rgb.transpose(2, 0, 1)[np.newaxis, :, :, :]
        return np.ascontiguousarray(nchw, dtype=np.float32)

    def _prepare_generic_input(self, image: np.ndarray) -> np.ndarray:
        """Prepare generic RGB input."""
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        nchw = img_rgb.transpose(2, 0, 1)[np.newaxis, :, :, :]
        return np.ascontiguousarray(nchw, dtype=np.float32)

    def _postprocess(self, result: np.ndarray, params: Dict) -> np.ndarray:
        """Apply post-processing to the result."""
        # AUD-31804: 输出范围与输入缩放对称。SDR（scale==1.0）保持旧
        # clip(0,1) 行为；HDR 输入恢复到 [0, scale]，不再截断高光。blend
        # 的原图与结果因此处于同一值域。
        scale = self._hdr_scale
        result = np.nan_to_num(result, nan=0.0, posinf=scale, neginf=0.0)
        result = np.clip(result, 0, scale)

        # Convert RGB back to BGR
        if result.shape[2] >= 3:
            result = cv2.cvtColor(result[:, :, :3], cv2.COLOR_RGB2BGR)

        # Apply blend if specified
        blend = params.get("blend", 1.0)
        if blend < 1.0:
            original = self.input_images.get("image")
            if original is not None and original.shape[:2] == result.shape[:2]:
                result = cv2.addWeighted(result, blend, original, 1.0 - blend, 0)

        return np.ascontiguousarray(result, dtype=np.float32)


class AIInferenceEngine(QObject):
    """
    Unified AI inference engine for managing inference workers.

    This class provides:
    - Worker management with cancellation support
    - Stale request detection
    - Progress aggregation
    - Session caching

    Signals:
        inference_finished: (cache_key) — inference completed
        inference_preview: (cache_key) — preview result available
        inference_progress: (cache_key, percentage) — progress update
        inference_failed: (cache_key, error_message) — inference failed
    """

    inference_finished = pyqtSignal(str)
    inference_preview = pyqtSignal(str)
    inference_progress = pyqtSignal(str, int)
    inference_failed = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self._workers: Dict[str, AIInferenceWorker] = {}
        # AUD-31801: wait 超时但仍运行中的 worker 在此保留强引用，
        # 防止 QThread 在运行中被 GC 销毁（"Destroyed while still running"）。
        self._retired_workers: list[AIInferenceWorker] = []
        self._lock = QMutex()
        self._latest_request_id: int = 0
        self._cache_save_fn: Optional[Callable[[str, np.ndarray], None]] = None
        # AUD-31808：预览结果确实存储（原注释声称 "Store preview for UI
        # access" 但 result 被丢弃）。cache_key → 最近一次预览图；run 终结
        # 时移除，并有数量上限（低分辨率预览图仍占内存）。
        self._latest_previews: Dict[str, np.ndarray] = {}
        self._PREVIEW_STORE_MAX = 8

    def latest_preview(self, cache_key: str) -> Optional[np.ndarray]:
        """Return the most recent preview image for ``cache_key`` (AUD-31808)."""
        self._lock.lock()
        try:
            return self._latest_previews.get(cache_key)
        finally:
            self._lock.unlock()

    def set_latest_request_id(self, request_id: int):
        """Update latest request ID for stale detection."""
        self._lock.lock()
        self._latest_request_id = request_id
        self._lock.unlock()

    def is_request_stale(self, request_id: int) -> bool:
        """Check if a request is stale."""
        self._lock.lock()
        stale = request_id < self._latest_request_id
        self._lock.unlock()
        return stale

    def register_cache_fn(self, fn: Callable[[str, np.ndarray], None]) -> None:
        """
        Register a cache-save callback.

        This allows features (e.g. composite) to provide their cache
        implementation without src/shared needing to import src/features.
        """
        self._cache_save_fn = fn

    def request_inference(
        self,
        cache_key: str,
        node_type: str,
        input_images: Dict[str, np.ndarray],
        params: Dict,
        request_id: int = 0,
    ) -> bool:
        """
        Request AI inference.

        Args:
            cache_key: Unique cache key for this request
            node_type: Node type (ai_denoise, ai_inpaint, etc.)
            input_images: Dict of input name -> numpy array
            params: Inference parameters
            request_id: Request ID for stale detection

        Returns:
            True if inference started, False if already processing
        """
        self._lock.lock()
        self._prune_retired_workers_locked()
        if cache_key in self._workers:
            self._lock.unlock()
            return False

        worker = AIInferenceWorker(
            cache_key, node_type, input_images, params, request_id
        )
        worker.set_stale_callback(self.is_request_stale)

        worker.finished.connect(self._on_worker_finished)
        worker.preview.connect(self._on_worker_preview)
        worker.progress.connect(self._on_worker_progress)
        worker.error.connect(self._on_worker_error)

        self._workers[cache_key] = worker
        self._lock.unlock()

        worker.start()
        return True

    def is_processing(self, cache_key: str) -> bool:
        """Check if inference is in progress for a key."""
        self._lock.lock()
        self._prune_retired_workers_locked()
        processing = cache_key in self._workers
        self._lock.unlock()
        return processing

    def cancel(self, cache_key: str):
        """Cancel inference for a key.

        AUD-31800: 持锁仅做 dict 摘取；``wait()`` 在临界区外执行，
        避免长时间无法中断的推理把引擎锁（以及 UI 调用方）阻塞数秒。
        """
        self._lock.lock()
        worker = self._workers.pop(cache_key, None)
        self._lock.unlock()
        if worker is None:
            return
        worker.cancel()
        self._reap_worker(worker)

    def cancel_all(self):
        """Cancel all running inference.

        与 ``cancel`` 相同的锁纪律（AUD-31800）：先在临界区内摘取全部
        worker，再在锁外逐个 cancel/wait。
        """
        self._lock.lock()
        workers = list(self._workers.values())
        self._workers.clear()
        self._prune_retired_workers_locked()
        self._lock.unlock()
        for worker in workers:
            worker.cancel()
        for worker in workers:
            self._reap_worker(worker)

    def _reap_worker(self, worker: AIInferenceWorker) -> None:
        """Wait for a cancelled worker; keep a reference while it runs.

        AUD-31801: ONNX ``session.run()`` 不可中断，``wait(3000)`` 超时后
        线程可能仍在 run() 中——直接丢弃最后的强引用会让运行中的 QThread
        被 GC 析构（崩溃）。挂到待回收列表，线程真正结束后由
        ``_prune_retired_workers_locked`` 清理（AUD-31807：清理时
        ``deleteLater()``）。
        """
        finished = worker.wait(3000)
        if finished or worker.isFinished():
            self._schedule_worker_release(worker)
            return
        self._lock.lock()
        self._retired_workers.append(worker)
        self._lock.unlock()

    @staticmethod
    def _schedule_worker_release(worker) -> None:
        """Schedule native QThread wrapper release (AUD-31807).

        Duck-typed on purpose: production workers are QThread (always have
        ``deleteLater``); test stubs may not — they still get the dict
        bookkeeping without the native release.
        """
        delete_later = getattr(worker, "deleteLater", None)
        if callable(delete_later):
            delete_later()

    def _prune_retired_workers_locked(self) -> None:
        """Drop retired workers whose threads have really finished.

        调用方必须已持有 ``self._lock``。AUD-31807：真正结束的 worker 在
        摘除引用的同时调度 ``deleteLater()``，原生 QThread 包装由事件循环
        释放（与 model_manager 的下载 worker 一致）。
        """
        if self._retired_workers:
            remaining: list[AIInferenceWorker] = []
            for worker in self._retired_workers:
                if worker.isFinished():
                    self._schedule_worker_release(worker)
                else:
                    remaining.append(worker)
            self._retired_workers = remaining

    def _on_worker_finished(self, cache_key: str, result: np.ndarray, request_id: int):
        """Handle worker completion."""
        self._lock.lock()
        worker = self._workers.pop(cache_key, None)
        self._latest_previews.pop(cache_key, None)
        self._prune_retired_workers_locked()
        self._lock.unlock()
        # AUD-31807：worker 线程已随 finished 信号进入收尾，调度其对象释放。
        if worker is not None:
            self._schedule_worker_release(worker)
        # Check stale
        if request_id < self._latest_request_id:
            return

        # Save result to cache (via AICacheManager)
        self._save_to_cache(cache_key, result)

        self.inference_finished.emit(cache_key)

    def _on_worker_preview(self, cache_key: str, result: np.ndarray, request_id: int):
        """Handle preview result."""
        self._lock.lock()
        if request_id < self._latest_request_id:
            self._lock.unlock()
            return
        # AUD-31808：预览图入库（容量上限，最旧先出），供 UI 事后访问。
        self._latest_previews[cache_key] = result
        while len(self._latest_previews) > self._PREVIEW_STORE_MAX:
            self._latest_previews.pop(next(iter(self._latest_previews)))
        self._lock.unlock()

        self.inference_preview.emit(cache_key)

    def _on_worker_progress(self, cache_key: str, percentage: int):
        """Handle progress update."""
        self.inference_progress.emit(cache_key, percentage)

    def _on_worker_error(self, cache_key: str, error_msg: str, request_id: int):
        """Handle inference error."""
        self._lock.lock()
        worker = self._workers.pop(cache_key, None)
        self._latest_previews.pop(cache_key, None)
        self._prune_retired_workers_locked()
        self._lock.unlock()
        # AUD-31807：error 路径同样 deleteLater（原来只有 dict pop）。
        if worker is not None:
            self._schedule_worker_release(worker)
        if request_id < self._latest_request_id:
            return

        self.inference_failed.emit(cache_key, error_msg)

    def _save_to_cache(self, cache_key: str, result: np.ndarray):
        """Save result via registered cache callback."""
        if self._cache_save_fn is not None:
            try:
                self._cache_save_fn(cache_key, result)
            except Exception as e:
                logger.debug(f"Inference cache save failed for {cache_key}: {e}")


# Singleton instance
_inference_engine_instance: Optional[AIInferenceEngine] = None


def get_inference_engine() -> AIInferenceEngine:
    """Get or create the singleton inference engine."""
    global _inference_engine_instance
    if _inference_engine_instance is None:
        _inference_engine_instance = AIInferenceEngine()
    return _inference_engine_instance