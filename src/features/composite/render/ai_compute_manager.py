"""
AI Compute Manager - Async ONNX inference pipeline for AI nodes.

Manages background AI computation workers with:
- GPU acceleration via ONNX Runtime (CUDA/CoreML/DirectML)
- Tiled processing for large images
- Streaming preview (low-res first)
- Progress reporting for UI integration
- Stale request detection and cancellation

Architecture:
    AI Node (UI thread)
        ↓ request_compute()
    AIComputeManager (Coordinator)
        ↓ creates worker
    AIInferenceWorker (Background thread)
        ↓ ONNX inference
    AICacheManager (Disk storage)
        ↓ saves EXR result
    UI updates via signals
"""

import logging
import os
import traceback
import numpy as np
import threading
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple
from PyQt6.QtCore import QObject, pyqtSignal, QThread, QMutex

from .ai_cache_manager import AICacheManager

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    import onnxruntime as ort

# AUD-7906/AUD-7907: cv2 is a declared dependency (opencv-python) but was
# previously imported only inside *some* methods — _generate_preview and
# _prepare_outpaint_input referenced it without any import, so every
# standard AI node died with NameError before producing a pixel.  Import it
# once at module scope (matching ai_cache_manager), guarded so a broken
# install degrades to a clear error message via _require_cv2() instead of a
# mid-inference NameError.
try:
    import cv2
except ImportError as _cv2_import_error:  # pragma: no cover - broken install
    cv2 = None  # type: ignore[assignment]
    _CV2_IMPORT_ERROR: Optional[Exception] = _cv2_import_error
else:
    _CV2_IMPORT_ERROR = None


def _require_cv2():
    """Return the cv2 module or raise an actionable error (AUD-7906/7907).

    Degradation path for environments where opencv-python failed to import:
    callers report this via the worker's error signal instead of crashing
    with NameError/AttributeError deep inside the inference pipeline.
    """
    if cv2 is None:  # pragma: no cover - requires broken install
        raise ImportError(
            "opencv-python is required for AI node inference but could not "
            f"be imported ({_CV2_IMPORT_ERROR}). "
            "Install it with: pip install opencv-python"
        )
    return cv2

# Node type → model id. B6: ai_remove joins the LaMa family — semantic
# "erase the masked object" is the same LaMa inpaint operation with a
# different mask convention.
AI_MODEL_MAPPING = {
    "ai_denoise": "swinir_denoise",
    "ai_inpaint": "lama_inpaint",
    "ai_expand": "lama_inpaint",   # Outpainting uses inpainting model
    "ai_remove": "lama_inpaint",   # B6: object removal = masked LaMa inpaint
    "ai_generate": "stable_diffusion_onnx",  # Optional: SD ONNX model
}

logger = logging.getLogger(__name__)


class _ComputeCancelled(Exception):
    """Raised internally when a worker observes cancellation mid-run.

    AUD-7910: cancellation used to travel as ``RuntimeError("Cancelled")``
    and landed in run()'s generic ``except Exception`` branch, so a user
    cancel was reported to the UI as ``"... inference error: Cancelled"``
    with a traceback on stderr. The dedicated type lets run() branch on it
    before the failure path.
    """


class AIComputeWorker(QThread):
    """
    Background worker for AI node computation.

    Delegates to AIInferenceEngine for actual inference, providing
    progress reporting and streaming preview support.

    Signals:
        finished: (cache_key, result_path, request_id) — computation succeeded
        preview: (cache_key, preview_image, request_id) — streaming preview
        progress: (cache_key, percentage) — progress update (0-100)
        error: (cache_key, error_message, request_id) — computation failed
        stale_detected: (cache_key, request_id) — request was stale, discarded
    """

    finished = pyqtSignal(str, str, int)     # cache_key, cache_path, request_id
    preview = pyqtSignal(str, np.ndarray, int)  # cache_key, preview_result, request_id
    progress = pyqtSignal(str, int)          # cache_key, percentage
    error = pyqtSignal(str, str, int)        # cache_key, error_message, request_id
    stale_detected = pyqtSignal(str, int)    # cache_key, request_id

    # AUD-7913: bounded ONNX session cache. Every InferenceSession (CUDA/
    # CoreML EP) pins hundreds of MB of device memory; the previous
    # path-keyed cache only ever grew (no cap, no eviction), so switching
    # between custom models accumulated sessions until GPU OOM. LRU
    # entries beyond the cap are dropped: onnxruntime releases its
    # resources when the session is destroyed, and a worker still holding
    # a reference keeps its own session alive regardless.
    MAX_SESSION_CACHE = 4
    _session_cache: Dict[str, "object"] = {}
    _session_lru: list = []  # model_path, least-recently-used first
    _cache_mutex = QMutex()

    @classmethod
    def _cached_session(cls, model_path: str) -> Optional[object]:
        """Return the cached session for ``model_path``, or None.

        A hit refreshes the LRU position (call under no other locks).
        """
        cls._cache_mutex.lock()
        try:
            session = cls._session_cache.get(model_path)
            if session is not None and model_path in cls._session_lru:
                cls._session_lru.remove(model_path)
                cls._session_lru.append(model_path)
        finally:
            cls._cache_mutex.unlock()
        return session

    @classmethod
    def _store_session(cls, model_path: str, session: object) -> None:
        """Cache the session, evicting LRU entries beyond the cap."""
        evicted: list = []
        cls._cache_mutex.lock()
        try:
            cls._session_cache[model_path] = session
            if model_path in cls._session_lru:
                cls._session_lru.remove(model_path)
            cls._session_lru.append(model_path)
            while len(cls._session_lru) > cls.MAX_SESSION_CACHE:
                lru_path = cls._session_lru.pop(0)
                cls._session_cache.pop(lru_path, None)
                evicted.append(lru_path)
        finally:
            cls._cache_mutex.unlock()
        if evicted:
            logger.info(
                "ONNX session cache at capacity (%d); evicted least "
                "recently used sessions for: %s",
                cls.MAX_SESSION_CACHE, ", ".join(evicted))

    def __init__(
        self,
        cache_key: str,
        node_type: str,
        input_images: Dict[str, np.ndarray],
        params: dict,
        cache_manager: AICacheManager,
        request_id: int = 0,
    ) -> None:
        super().__init__()
        self.cache_key = cache_key
        self.node_type = node_type
        self.input_images = input_images
        self.params = params
        self.cache_manager = cache_manager
        self.request_id = request_id
        self._cancelled = False
        self._is_stale_callback = None
        self._preview_result: Optional[np.ndarray] = None

    def set_stale_callback(self, callback):
        """Set callback to check if request is stale."""
        self._is_stale_callback = callback

    def cancel(self):
        """Request cancellation."""
        self._cancelled = True

    def get_preview_result(self) -> Optional[np.ndarray]:
        """Get the preview result if available."""
        return self._preview_result

    def run(self):
        """Execute AI inference in background."""
        try:
            # AUD-7906/7907: every node path below needs cv2 (preview
            # resize, outpaint canvas, tiling, color conversion); fail fast
            # with an actionable message on a broken install.
            _require_cv2()

            # Import inference engine (lazy to avoid circular imports)

            self.progress.emit(self.cache_key, 5)

            # Check if stale before starting
            if self._is_stale_callback and self._is_stale_callback(self.request_id):
                self.stale_detected.emit(self.cache_key, self.request_id)
                return

            # Special handling for ai_generate node (txt2img/img2img)
            if self.node_type == "ai_generate":
                self._run_generate_inference()
                return

            # Standard AI nodes (denoise, inpaint) require input image
            image = self.input_images.get("image")
            if image is None:
                self.error.emit(
                    self.cache_key, "No input image provided", self.request_id
                )
                return

            mask = self.input_images.get("mask")

            # Get model path for this node type
            model_path = self._get_model_path()
            if model_path is None:
                self.error.emit(
                    self.cache_key,
                    f"Model not available for {self.node_type}. "
                    f"Please download or convert the model first.",
                    self.request_id,
                )
                return

            self.progress.emit(self.cache_key, 10)

            # Load ONNX session
            session = self._load_onnx_session(model_path)
            if self._cancelled:
                return

            self.progress.emit(self.cache_key, 15)

            # Get processing parameters. AUD-7914: tile/overlap are user/
            # persistence-controlled — sanitize before use (overlap >=
            # tile_size raised "range() arg 3 must not be zero", overlap
            # == 1 produced all-NaN Hanning weights that _postprocess
            # zeroed into black seams).
            tile_size = self.params.get("tile_size", 512)
            overlap = self.params.get("overlap", 64)
            tile_size, overlap = self._sanitize_tiling_params(
                tile_size, overlap)
            use_tiling = self.params.get("use_tiling", True)
            enable_preview = self.params.get("enable_preview", True)
            preview_scale = self.params.get("preview_scale", 0.5)

            # Special handling for ai_expand: prepare expanded canvas and mask
            original_image = None
            expand_params = None
            if self.node_type == "ai_expand":
                original_image = image.copy()
                image, mask, expand_params = self._prepare_outpaint_input(image, self.params)

            # Generate streaming preview first
            if enable_preview and preview_scale < 1.0:
                self._generate_preview(image, mask, session, preview_scale)

            if self._cancelled:
                return

            # Process full resolution
            self.progress.emit(self.cache_key, 30)

            result = self._process_full_resolution(
                image, mask, session, tile_size, overlap, use_tiling
            )

            if self._cancelled:
                return

            # Post-process
            result = self._postprocess(result)

            # Special handling for ai_expand: blend result with original
            if self.node_type == "ai_expand" and original_image is not None and expand_params is not None:
                result = self._blend_outpaint_result(result, original_image, expand_params)

            self.progress.emit(self.cache_key, 95)

            # Save to cache
            cache_path = self.cache_manager.put(self.cache_key, result)

            self.progress.emit(self.cache_key, 100)

            # Emit finished
            self.finished.emit(self.cache_key, cache_path, self.request_id)

        except _ComputeCancelled:
            # AUD-7910: a cancellation is not a failure — return silently
            # instead of reporting "... inference error: Cancelled" (with a
            # traceback) through the error channel. The manager side that
            # cancelled has already popped this worker from _workers.
            return
        except Exception as e:
            error_msg = f"{self.node_type} inference error: {str(e)}"
            traceback.print_exc()
            self.error.emit(self.cache_key, error_msg, self.request_id)

    @staticmethod
    def _sanitize_tiling_params(tile_size: Any, overlap: Any) -> Tuple[int, int]:
        """Clamp tile/overlap parameters to values the tiler can use.

        AUD-7914: ``overlap >= tile_size`` makes the tile stride 0
        (``ValueError: range() arg 3 must not be zero``) and ``overlap ==
        1`` yields ``0 / (1 - 1) = NaN`` Hanning weights which
        ``_postprocess`` flattens into black seams. Invalid values fall
        back to the nearest safe value with a logged warning.

        Returns:
            (tile_size, overlap) with tile_size >= 1 and
            overlap == 0 or 2 <= overlap < tile_size.
        """
        try:
            tile_size = int(tile_size)
        except (TypeError, ValueError):
            logger.warning("Invalid tile_size %r; using default 512",
                           tile_size)
            tile_size = 512
        try:
            overlap = int(overlap)
        except (TypeError, ValueError):
            logger.warning("Invalid overlap %r; using default 64", overlap)
            overlap = 64
        if tile_size < 1:
            logger.warning("Invalid tile_size %d; using default 512",
                           tile_size)
            tile_size = 512
        if overlap < 0:
            logger.warning("Negative overlap %d clamped to 0", overlap)
            overlap = 0
        elif overlap == 1:
            # Degenerate 1-sample ramp: every weight is NaN. Nearest safe
            # value is 0 (no blending) — keeps the render alive instead of
            # producing black seams.
            logger.warning("overlap=1 produces NaN blend weights; "
                           "using 0 (no blending) instead")
            overlap = 0
        if overlap >= tile_size:
            clamped = max(0, tile_size // 2)
            logger.warning("overlap=%d >= tile_size=%d; clamping overlap "
                           "to %d", overlap, tile_size, clamped)
            overlap = clamped
        return tile_size, overlap

    def _prepare_outpaint_input(
        self,
        image: np.ndarray,
        params: dict,
    ) -> tuple:
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

        # Get expansion parameters. AUD-7914: expand_* come from
        # persistence/params — negative borders made copyMakeBorder fail;
        # clamp to >= 0 before use.
        expand_left = max(0, int(params.get("expand_left", 0) or 0))
        expand_right = max(0, int(params.get("expand_right", 0) or 0))
        expand_top = max(0, int(params.get("expand_top", 0) or 0))
        expand_bottom = max(0, int(params.get("expand_bottom", 0) or 0))
        blend_radius = int(params.get("blend_radius", 32) or 0)

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
        expand_params: dict,
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
        from src.shared.ai_models.model_manager import get_model_manager

        model_manager = get_model_manager()

        model_id = AI_MODEL_MAPPING.get(self.node_type)
        if model_id:
            path = model_manager.get_model_path(model_id)
            if path:
                return path

        # Check for custom model path
        custom_path = self.params.get("model_path")
        if custom_path and os.path.exists(custom_path):
            return custom_path

        return None

    def _load_onnx_session(self, model_path: str) -> "ort.InferenceSession":
        """Load ONNX session with GPU acceleration."""
        import onnxruntime as ort
        from src.shared.ai_models.onnx_env import (
            get_configured_providers,
            get_session_options,
            get_gpu_provider_info,
        )

        # Session cache (AUD-7913: bounded + LRU eviction)
        session = self._cached_session(model_path)
        if session is not None:
            return session

        providers = get_configured_providers()
        gpu_info = get_gpu_provider_info()

        if gpu_info and gpu_info.get("provider"):
            logger.info(f"GPU: {gpu_info['description']}")
        else:
            logger.info("CPU mode")

        sess_options = get_session_options()
        session = None

        for provider in providers:
            if self._cancelled:
                raise _ComputeCancelled()

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

                if primary in actual or primary == "CPUExecutionProvider":
                    logger.info(f"Loaded with {primary}")
                    break
                else:
                    del session
                    session = None
            except Exception as e:
                logger.warning(f"Failed with {provider}: {e}")
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
            logger.info("CPU fallback")

        # Cache (AUD-7913: bounded + LRU eviction)
        self._store_session(model_path, session)

        return session

    def _generate_preview(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: "ort.InferenceSession",
        preview_scale: float,
    ):
        """Generate low-resolution preview for streaming display."""
        h, w = image.shape[:2]
        new_h = int(h * preview_scale)
        new_w = int(w * preview_scale)

        image_small = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
        mask_small = None
        if mask is not None:
            mask_small = cv2.resize(mask, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

        result_small = self._run_inference(image_small, mask_small, session)

        if self._cancelled:
            return

        # Store preview result
        self._preview_result = result_small.copy()

        # Emit preview
        self.progress.emit(self.cache_key, 25)
        self.preview.emit(self.cache_key, result_small, self.request_id)

    def _process_full_resolution(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: "ort.InferenceSession",
        tile_size: int,
        overlap: int,
        use_tiling: bool,
    ) -> np.ndarray:
        """Process at full resolution."""
        if use_tiling:
            return self._process_tiled(image, mask, session, tile_size, overlap)
        else:
            return self._run_inference(image, mask, session)

    def _process_tiled(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: "ort.InferenceSession",
        tile_size: int,
        overlap: int,
    ) -> np.ndarray:
        """Process image in tiles with Hanning window blending."""
        h, w = image.shape[:2]
        c = image.shape[2] if image.ndim == 3 else 1

        output = np.zeros((h, w, c), dtype=np.float32)
        weight = np.zeros((h, w, c), dtype=np.float32)

        # Hanning ramps
        if overlap > 0:
            t = np.arange(overlap) / (overlap - 1)
            fade_in = 0.5 * (1 - np.cos(np.pi * t))
            fade_out = 0.5 * (1 + np.cos(np.pi * t))
            ramp_h_in = fade_in[:, np.newaxis, np.newaxis]
            ramp_h_out = fade_out[:, np.newaxis, np.newaxis]
            ramp_w_in = fade_in[np.newaxis, :, np.newaxis]
            ramp_w_out = fade_out[np.newaxis, :, np.newaxis]
        else:
            ramp_h_in = ramp_h_out = ramp_w_in = ramp_w_out = None

        stride = tile_size - overlap

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
        processed = 0

        progress_start = 30
        progress_end = 90

        for y in y_positions:
            for x in x_positions:
                if self._cancelled:
                    raise _ComputeCancelled()

                y1, y2 = y, min(y + tile_size, h)
                x1, x2 = x, min(x + tile_size, w)

                tile_h = y2 - y1
                tile_w = x2 - x1

                # Weight mask
                tile_mask = np.ones((tile_h, tile_w, c), dtype=np.float32)
                if overlap > 0 and ramp_h_in is not None:
                    if y1 > 0:
                        tile_mask[:min(overlap, tile_h), :, :] *= ramp_h_in[:min(overlap, tile_h), :, :]
                    if y2 < h:
                        tile_mask[-min(overlap, tile_h):, :, :] *= ramp_h_out[-min(overlap, tile_h):, :, :]
                    if x1 > 0:
                        tile_mask[:, :min(overlap, tile_w), :] *= ramp_w_in[:, :min(overlap, tile_w), :]
                    if x2 < w:
                        tile_mask[:, -min(overlap, tile_w):, :] *= ramp_w_out[:, -min(overlap, tile_w):, :]

                tile_img = image[y1:y2, x1:x2, :]
                tile_mask_input = mask[y1:y2, x1:x2] if mask is not None else None

                # Pad edge tiles
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

                tile_result = self._run_inference(tile_img, tile_mask_input, session)
                tile_result = tile_result[:tile_h, :tile_w, :]
                tile_mask = tile_mask[:tile_h, :tile_w, :]

                output[y1:y2, x1:x2, :] += tile_result * tile_mask
                weight[y1:y2, x1:x2, :] += tile_mask

                processed += 1
                pct = progress_start + int((progress_end - progress_start) * processed / total_tiles)
                self.progress.emit(self.cache_key, pct)

        return output / np.maximum(weight, 1e-5)

    def _run_inference(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: "ort.InferenceSession",
    ) -> np.ndarray:
        """Run ONNX inference for a single image/tile."""
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name

        # Prepare input based on node type
        if self.node_type in ("ai_inpaint", "ai_expand", "ai_remove"):
            # Inpainting / outpainting / removal: concatenate image and mask
            # as input (LaMa 4-channel NCHW; mask 1 = region to fill)
            img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            if mask is None:
                mask = np.zeros(image.shape[:2], dtype=np.float32)
            if mask.ndim == 3:
                # Defensive: upstream readbacks may arrive RGB; LaMa takes
                # a single-channel matte.
                mask = mask[:, :, 0]
            mask = np.clip(mask, 0, 1)
            combined = np.concatenate([img_rgb, mask[:, :, np.newaxis]], axis=2)
            nchw = combined.transpose(2, 0, 1)[np.newaxis, :, :, :]
        else:
            img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            nchw = img_rgb.transpose(2, 0, 1)[np.newaxis, :, :, :]

        nchw = np.ascontiguousarray(nchw, dtype=np.float32)

        outputs = session.run([output_name], {input_name: nchw})
        result = outputs[0]

        if result.ndim == 4:
            result = result.squeeze(0).transpose(1, 2, 0)

        return result.astype(np.float32)

    def _postprocess(self, result: np.ndarray) -> np.ndarray:
        """Apply post-processing."""
        result = np.nan_to_num(result, nan=0.0, posinf=1.0, neginf=0.0)
        result = np.clip(result, 0, 1)

        if result.shape[2] >= 3:
            result = cv2.cvtColor(result[:, :, :3], cv2.COLOR_RGB2BGR)

        # Apply blend
        blend = self.params.get("blend", 1.0)
        if blend < 1.0:
            original = self.input_images.get("image")
            if original is not None and original.shape[:2] == result.shape[:2]:
                result = cv2.addWeighted(result, blend, original, 1.0 - blend, 0)

        return np.ascontiguousarray(result, dtype=np.float32)

    def _run_generate_inference(self):
        """Execute AI generation inference (txt2img/img2img).

        This method handles image generation using diffusion models.
        Supports both txt2img (no input image) and img2img modes.

        The implementation provides:
        - Placeholder generation (creates noise/random output if no model)
        - Future integration with Stable Diffusion ONNX pipeline
        - Custom model path support
        - Progress reporting and preview generation

        Architecture:
        For full Stable Diffusion integration, the pipeline would consist of:
        - Text Encoder (CLIP): Encodes prompt to latent vectors
        - UNet: Diffusion denoising network
        - VAE Decoder: Decodes latent to image

        Currently, this is a placeholder that generates test patterns
        or uses custom ONNX models if provided.
        """
        try:
            self.progress.emit(self.cache_key, 5)

            # Check if stale
            if self._is_stale_callback and self._is_stale_callback(self.request_id):
                self.stale_detected.emit(self.cache_key, self.request_id)
                return

            # Get generation parameters
            mode = self.params.get("mode", "txt2img")
            prompt = self.params.get("prompt", "")
            strength = self.params.get("strength", 0.75)
            seed = self.params.get("seed", -1)
            width = self.params.get("width", 1024)
            height = self.params.get("height", 1024)
            model_path = self.params.get("model_path", "")

            # Validate prompt
            if not prompt or not prompt.strip():
                self.error.emit(
                    self.cache_key,
                    "Empty prompt - generation requires a text prompt",
                    self.request_id
                )
                return

            self.progress.emit(self.cache_key, 10)

            # Check for custom model
            if model_path and os.path.exists(model_path):
                # Use custom model
                self.progress.emit(self.cache_key, 15)
                result = self._run_custom_generate_model(model_path, prompt, width, height, seed)
                if self._cancelled:
                    return
                if result is None:
                    # AUD-7908: a silent `return` here left the worker
                    # registered in the manager's _workers map forever —
                    # is_processing() stayed True and request_compute()
                    # refused every retry for this cache_key. Report the
                    # terminal failure so _on_worker_error can clean up.
                    self.error.emit(
                        self.cache_key,
                        f"Custom model inference failed: {model_path}",
                        self.request_id,
                    )
                    return
            else:
                # Placeholder generation - creates a test pattern
                # This allows testing the node structure without a full SD model
                logger.debug("No model configured, generating test pattern")
                logger.debug(f"  Prompt: {prompt}")
                logger.debug(f"  Mode: {mode}, Size: {width}x{height}")

                self.progress.emit(self.cache_key, 20)

                # Generate placeholder image (gradient with prompt hash visualization)
                result = self._generate_placeholder(prompt, width, height, seed)

                if self._cancelled:
                    return

                self.progress.emit(self.cache_key, 60)

                # For img2img mode, blend with input
                if mode == "img2img":
                    input_image = self.input_images.get("image")
                    if input_image is not None:
                        # Resize result to match input if needed
                        if result.shape[:2] != input_image.shape[:2]:
                            result = cv2.resize(result, 
                                               (input_image.shape[1], input_image.shape[0]),
                                               interpolation=cv2.INTER_LINEAR)
                        
                        # Blend based on strength parameter
                        result = cv2.addWeighted(result, strength, 
                                                input_image, 1.0 - strength, 0)

                self.progress.emit(self.cache_key, 80)

            # Post-process result
            result = np.nan_to_num(result, nan=0.0, posinf=1.0, neginf=0.0)
            result = np.clip(result, 0, 1)

            # Ensure BGR format
            if result.shape[2] >= 3:
                result = cv2.cvtColor(result[:, :, :3], cv2.COLOR_RGB2BGR)

            result = np.ascontiguousarray(result, dtype=np.float32)

            self.progress.emit(self.cache_key, 90)

            # Generate preview at lower resolution
            if self.params.get("enable_preview", True):
                preview_scale = self.params.get("preview_scale", 0.5)
                preview_h = int(height * preview_scale)
                preview_w = int(width * preview_scale)
                preview = cv2.resize(result, (preview_w, preview_h), 
                                     interpolation=cv2.INTER_AREA)
                self._preview_result = preview.copy()
                self.preview.emit(self.cache_key, preview, self.request_id)

            self.progress.emit(self.cache_key, 95)

            # Save to cache
            cache_path = self.cache_manager.put(self.cache_key, result)

            self.progress.emit(self.cache_key, 100)

            # Emit finished
            self.finished.emit(self.cache_key, cache_path, self.request_id)

        except Exception as e:
            error_msg = f"AI generation error: {str(e)}"
            traceback.print_exc()
            self.error.emit(self.cache_key, error_msg, self.request_id)

    def _generate_placeholder(self, prompt: str, width: int, height: int, 
                              seed: int) -> np.ndarray:
        """Generate a placeholder test pattern when no model is available.

        Creates a gradient image with visual representation of the prompt
        to demonstrate the node structure is working.

        Args:
            prompt: Text prompt (used to create unique visual hash)
            width: Output width
            height: Output height
            seed: Random seed

        Returns:
            BGR float32 numpy array [0, 1]
        """
        import hashlib

        # Use prompt hash to create unique colors
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
        
        # Extract color components from hash
        r = int(prompt_hash[0:2], 16) / 255.0
        g = int(prompt_hash[2:4], 16) / 255.0
        b = int(prompt_hash[4:6], 16) / 255.0
        
        # Create base gradient
        gradient = np.zeros((height, width, 3), dtype=np.float32)
        
        # Horizontal gradient
        for x in range(width):
            t = x / width
            gradient[:, x, 0] = b * (1 - t) + 0.5 * t  # Blue to cyan
            gradient[:, x, 1] = g * (1 - t) + 0.8 * t  # Green to yellow
            gradient[:, x, 2] = r * (1 - t) + 1.0 * t  # Red to white
        
        # Add vertical variation based on seed
        # AUD-7918: np.random.seed() here reseeds the PROCESS-GLOBAL legacy
        # RNG from a worker thread, silently changing every other consumer's
        # random stream. Use a thread-local Generator instead (same seed →
        # same noise, no global state touched).
        rng = np.random.default_rng(seed) if seed >= 0 else np.random.default_rng()
        noise = rng.random((height, width, 3)) * 0.3
        gradient = gradient + noise
        
        # Add text overlay region (darker area for future text rendering)
        text_region_y = int(height * 0.1)
        text_region_h = int(height * 0.2)
        gradient[text_region_y:text_region_y+text_region_h, :, :] *= 0.5
        
        # Normalize
        gradient = np.clip(gradient, 0, 1)
        
        # Convert to RGB (will be converted to BGR in postprocess)
        return gradient.astype(np.float32)

    def _run_custom_generate_model(self, model_path: str, prompt: str,
                                    width: int, height: int, seed: int) -> Optional[np.ndarray]:
        """Run custom ONNX model for generation.

        This method loads and runs a custom ONNX generation model.
        Supports various model architectures via ONNX Runtime.

        Args:
            model_path: Path to custom ONNX model
            prompt: Text prompt
            width: Output width
            height: Output height
            seed: Random seed

        Returns:
            BGR float32 numpy array [0, 1], or None on failure
        """
        try:

            # Load ONNX session
            session = self._load_onnx_session(model_path)
            if self._cancelled:
                return None

            self.progress.emit(self.cache_key, 30)

            # Get model input/output info
            input_names = [inp.name for inp in session.get_inputs()]
            output_names = [out.name for out in session.get_outputs()]

            # Prepare inputs based on model structure
            inputs_dict = {}

            # Common input patterns:
            # - "prompt" / "text": Text prompt (string or encoded)
            # - "width" / "height": Dimension inputs
            # - "seed": Random seed
            # - "latent": Initial latent (for img2img)
            # - "image": Input image (for img2img)

            # Check for text input
            if "prompt" in input_names or "text" in input_names:
                text_input_name = "prompt" if "prompt" in input_names else "text"
                inputs_dict[text_input_name] = prompt

            # Check for dimension inputs
            if "width" in input_names:
                inputs_dict["width"] = np.array([width], dtype=np.int64)
            if "height" in input_names:
                inputs_dict["height"] = np.array([height], dtype=np.int64)

            # Check for seed input
            if "seed" in input_names:
                # AUD-7918: draw the random fallback seed from a thread-local
                # Generator — the legacy global draw mutates process-wide RNG
                # state from this worker thread.
                seed_val = (seed if seed >= 0
                            else int(np.random.default_rng().integers(
                                0, 2147483647)))
                inputs_dict["seed"] = np.array([seed_val], dtype=np.int64)

            # Check for image input (img2img)
            input_image = self.input_images.get("image")
            if "image" in input_names and input_image is not None:
                # Prepare image input
                img_rgb = cv2.cvtColor(input_image, cv2.COLOR_BGR2RGB)
                if img_rgb.shape[:2] != (height, width):
                    img_rgb = cv2.resize(img_rgb, (width, height), 
                                         interpolation=cv2.INTER_LINEAR)
                img_normalized = img_rgb.astype(np.float32) / 255.0
                inputs_dict["image"] = img_normalized.transpose(2, 0, 1)[np.newaxis, :, :, :]

            # Check for latent input
            if "latent" in input_names:
                # Initialize latent with random noise
                latent_shape = (1, 4, height // 8, width // 8)  # Typical SD latent shape
                # AUD-7918: thread-local Generator instead of reseeding the
                # process-global legacy RNG from this worker thread.
                rng = (np.random.default_rng(seed) if seed >= 0
                       else np.random.default_rng())
                latent = rng.standard_normal(latent_shape).astype(np.float32)
                inputs_dict["latent"] = latent

            self.progress.emit(self.cache_key, 40)

            # Run inference
            outputs = session.run(output_names, inputs_dict)

            self.progress.emit(self.cache_key, 70)

            # Extract result from outputs
            # Usually first output is the generated image
            result = outputs[0]

            # Handle different output formats
            if result.ndim == 4:
                # NCHW format
                result = result.squeeze(0).transpose(1, 2, 0)
            elif result.ndim == 3:
                # CHW format
                result = result.transpose(1, 2, 0)

            # Normalize to [0, 1]
            if result.dtype != np.float32:
                result = result.astype(np.float32)
            
            # Clip to valid range
            result = np.clip(result, 0, 1)

            return result

        except Exception as e:
            logger.error(f"Custom model inference error: {e}")
            logger.debug("Stack trace:", exc_info=True)
            return None


class AIComputeManager(QObject):
    """
    Manages async AI computation workers.

    Coordinates between the render thread (which detects cache misses)
    and the compute workers (which produce results and save to cache).

    Enhanced features:
    - Request tracking for stale detection
    - Streaming preview support
    - GPU acceleration with fallback

    Signals:
        compute_finished: (cache_key) — result ready in cache
        compute_preview: (cache_key) — preview result available
        compute_failed: (cache_key, error_message) — computation failed
        compute_progress: (cache_key, percentage) — progress update
        compute_stale: (cache_key) — computation discarded due to stale request
    """

    compute_finished = pyqtSignal(str)
    compute_preview = pyqtSignal(str)
    compute_failed = pyqtSignal(str, str)
    compute_progress = pyqtSignal(str, int)
    compute_stale = pyqtSignal(str)

    # B6 hardening: ONNX workers are expensive (each pins full-res inputs);
    # two run concurrently at most, and previews are FIFO-capped.
    MAX_CONCURRENT_COMPUTES = 2
    MAX_PREVIEW_RESULTS = 8

    def __init__(self, cache_manager: AICacheManager, parent=None) -> None:
        super().__init__(parent)
        self._cache_manager = cache_manager
        self._workers: Dict[str, AIComputeWorker] = {}
        self._lock = threading.Lock()
        self._latest_request_id: int = 0
        self._preview_results: Dict[str, np.ndarray] = {}
        # cache_key -> originating node id, so UI feedback (state/progress/
        # error) can find the node when the compute completes.
        self._node_ids: Dict[str, Optional[str]] = {}

    def set_latest_request_id(self, request_id: int):
        """Update latest request ID and stop superseded computations.

        Without the cancel sweep, scrubbing/playing an animated clip would
        leave every intermediate frame's LaMa worker running to completion
        (each holding a full-res image+mask pair).  Worker.cancel() only
        sets a flag, so it is safe to call after releasing the manager lock.
        """
        stale_workers = []
        with self._lock:
            self._latest_request_id = request_id
            for key, worker in list(self._workers.items()):
                if worker.request_id < request_id:
                    del self._workers[key]
                    stale_workers.append(worker)
        for worker in stale_workers:
            worker.cancel()

    def is_request_stale(self, request_id: int) -> bool:
        """Check if request is stale."""
        with self._lock:
            return request_id < self._latest_request_id

    def get_preview_result(self, cache_key: str) -> Optional[np.ndarray]:
        """Get preview result for a cache key."""
        # AUD-7916: every other _workers/_preview_results access holds
        # self._lock; the unlocked dict.get here relied on GIL atomicity and
        # broke the manager's threading convention.
        with self._lock:
            worker = self._workers.get(cache_key)
            if worker:
                return worker.get_preview_result()
            return self._preview_results.get(cache_key)

    def request_compute(
        self,
        cache_key: str,
        node_type: str,
        input_images: Dict[str, np.ndarray],
        params: dict,
        request_id: int = 0,
        node_id: Optional[str] = None,
    ) -> bool:
        """
        Request async AI computation.

        Args:
            cache_key: Content-addressable cache key
            node_type: Node type string
            input_images: Dict of input name -> BGR float32 numpy array
            params: Node parameters dict
            request_id: Request ID for stale detection

        Returns:
            True if computation started, False if already processing
        """
        with self._lock:
            if cache_key in self._workers:
                return False
            self._node_ids[cache_key] = node_id

            # Concurrency cap: a scrub across an animated clip produces a
            # cache key per frame — without a cap each one would spawn an
            # ONNX worker holding full-res inputs simultaneously.
            # AUD-7909: evicted workers are collected and terminal-signalled
            # AFTER the lock — their run() returns silently at a _cancelled
            # check, so without an explicit compute_stale emission the UI
            # would never hear back and the node stayed stuck "computing".
            evicted = []
            while len(self._workers) >= self.MAX_CONCURRENT_COMPUTES:
                oldest_key = next(iter(self._workers))
                evicted.append((oldest_key, self._workers.pop(oldest_key)))
                self._node_ids.pop(oldest_key, None)

            worker = AIComputeWorker(
                cache_key,
                node_type,
                input_images,
                params,
                self._cache_manager,
                request_id=request_id,
            )
            worker.set_stale_callback(self.is_request_stale)

            worker.finished.connect(self._on_worker_finished)
            worker.preview.connect(self._on_worker_preview)
            worker.progress.connect(self._on_worker_progress)
            worker.error.connect(self._on_worker_error)
            worker.stale_detected.connect(self._on_worker_stale)

            self._workers[cache_key] = worker
            worker.start()

        # Outside the lock (set_latest_request_id pattern): cancel() only
        # sets a flag; the stale emission lets consumers reset/retry.
        for oldest_key, oldest in evicted:
            oldest.cancel()
            self.compute_stale.emit(oldest_key)
        return True

    def is_processing(self, cache_key: str) -> bool:
        """Check if computation is in progress."""
        with self._lock:
            return cache_key in self._workers

    def take_node_id(self, cache_key: str) -> Optional[str]:
        """Pop and return the node id that originated a compute request."""
        with self._lock:
            return self._node_ids.pop(cache_key, None)

    def cancel(self, cache_key: str):
        """Cancel running computation.

        AUD-7912: the worker is popped under the lock but cancelled/waited
        OUTSIDE it — ONNX inference cannot be interrupted, so wait() can
        block for seconds; holding the manager lock through the wait
        stalled every renderer-thread call (request_compute / is_processing
        / cancel_all) for the whole timeout.
        """
        with self._lock:
            worker = self._workers.pop(cache_key, None)
            self._node_ids.pop(cache_key, None)
        if worker is not None:
            worker.cancel()
            worker.wait(3000)
            # Same teardown discipline as the terminal-signal handlers: the
            # pop above removed the last registry reference, so schedule the
            # QThread's deferred destruction here (AUD-7911 pattern).
            worker.deleteLater()

    def cancel_all(self):
        """Cancel all running computations (AUD-7912: wait outside the lock)."""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            self._node_ids.clear()
        for worker in workers:
            worker.cancel()
        for worker in workers:
            worker.wait(3000)
            worker.deleteLater()

    def cancel_stale_requests(self, current_request_id: int):
        """Cancel all requests older than the given ID (AUD-7912)."""
        with self._lock:
            self._latest_request_id = current_request_id
            stale_workers = [
                (key, worker) for key, worker in list(self._workers.items())
                if worker.request_id < current_request_id
            ]
            for key, _worker in stale_workers:
                self._workers.pop(key, None)
        for _key, worker in stale_workers:
            worker.cancel()
            worker.wait(1000)
            worker.deleteLater()

    def _on_worker_finished(self, cache_key: str, cache_path: str, request_id: int):
        """Handle worker completion."""
        with self._lock:
            worker = self._workers.pop(cache_key, None)
            # Store preview result
            if worker:
                preview = worker.get_preview_result()
                if preview is not None:
                    # FIFO-capped so long sessions don't accumulate one
                    # half-res float32 frame per cache key forever.
                    while len(self._preview_results) >= self.MAX_PREVIEW_RESULTS:
                        self._preview_results.pop(next(iter(self._preview_results)))
                    self._preview_results[cache_key] = preview
                worker.deleteLater()

            if request_id < self._latest_request_id:
                self.compute_stale.emit(cache_key)
                return

        self.compute_finished.emit(cache_key)

    def _on_worker_preview(self, cache_key: str, preview: np.ndarray, request_id: int):
        """Handle preview result."""
        with self._lock:
            if request_id < self._latest_request_id:
                return
            self._preview_results[cache_key] = preview

        self.compute_preview.emit(cache_key)

    def _on_worker_error(self, cache_key: str, error_msg: str, request_id: int):
        """Handle worker error."""
        with self._lock:
            worker = self._workers.pop(cache_key, None)
            if worker is not None:
                worker.deleteLater()
            if request_id < self._latest_request_id:
                self.compute_stale.emit(cache_key)
                return

        self.compute_failed.emit(cache_key, error_msg)

    def _on_worker_progress(self, cache_key: str, pct: int):
        """Handle progress update."""
        self.compute_progress.emit(cache_key, pct)

    def _on_worker_stale(self, cache_key: str, request_id: int):
        """Handle stale request.

        AUD-7911: match the other terminal handlers — the popped worker
        gets deleteLater() while this slot still holds a reference, so the
        QThread wrapper cannot be GC-destroyed while run() is potentially
        still returning on the worker thread ("QThread: Destroyed while
        thread is still running").
        """
        with self._lock:
            worker = self._workers.pop(cache_key, None)
            if worker is not None:
                worker.deleteLater()
        self.compute_stale.emit(cache_key)