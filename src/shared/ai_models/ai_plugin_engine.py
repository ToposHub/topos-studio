"""
AI Plugin Engine - ONNX Runtime Inference for AI Plugins

Processes AI plugin effects using ONNX Runtime with tiled inference.
Supports upscale, denoise, enhance and other ONNX model-based effects.

Reuses the tiled processing architecture from denoise_engine.py with
cosine-window blending for seamless tile boundaries.
"""

import os
import threading
import time
import cv2
import numpy as np
import onnxruntime as ort
import logging

logger = logging.getLogger(__name__)
from PyQt6.QtCore import QObject, pyqtSignal, QThread, QMutex
from src.shared.ai_models.onnx_env import (
    get_configured_providers, get_session_options, get_gpu_provider_info
)


class _InferenceCancelled(Exception):
    """Raised internally at cancel checkpoints to unwind a cancelled run.

    Never escapes :meth:`AIPluginWorker.run`; a cancelled worker reports
    through the dedicated ``cancelled`` signal instead of ``finished`` /
    ``error`` so a user cancel is not presented as a result or a failure.
    """


class AIPluginWorker(QThread):
    """
    Async worker for AI plugin ONNX inference.

    Cancellation is cooperative (AUD-31500): callers set the abort flag via
    :meth:`request_cancel` and the worker checks it at every tile boundary
    and stage transition. The thread is NEVER terminated — it can be inside
    an ONNX native ``session.run``/model-load call where
    ``QThread.terminate()`` risks corrupting native runtime state. The
    non-interruptible window is one inference tile (or one session load,
    bounded by the load timeout).

    Signals:
        finished: (layer_id, instance_id, result_np_array)
        progress: (layer_id, instance_id, percentage 0-100)
        error: (layer_id, instance_id, error_message)
        cancelled: (layer_id, instance_id)
    """
    finished = pyqtSignal(str, str, np.ndarray)
    progress = pyqtSignal(str, str, int)
    error = pyqtSignal(str, str, str)
    cancelled = pyqtSignal(str, str)

    # Shared session cache across all AI plugin workers.  Guarded by
    # ``_cache_mutex`` (AUD-31501): workers for different plugin instances
    # run on separate QThreads and previously cleared/overwrote the
    # class-level cache and ``_current_model_path`` without a lock, so
    # concurrent check-then-clear sequences could empty a cache entry
    # another thread was still using and force duplicate model loads.
    _session_cache = {}
    _current_model_path = None
    _cache_mutex = QMutex()
    # AUD-31503：超时后仍在加载的 daemon loader 线程收容所。ONNX 加载
    # （GPU graph 编译）不可安全终止，超时只能放弃等待；但 loader 线程
    # 及其将要产出的 session 仍需被引用到线程自然退出，否则引用半途丢失。
    # 锁保护：多个 worker 可同时超时。
    _stray_loaders: set = set()
    _stray_loaders_mutex = QMutex()

    def __init__(self, layer_id, instance_id, image, plugin_data, plugin_params):
        """
        Args:
            layer_id: Layer ID string
            instance_id: Plugin instance ID string
            image: float32 RGB image [0, 1], shape (H, W, 3)
            plugin_data: Plugin metadata dict from PluginManager
            plugin_params: User-adjusted parameters dict
        """
        super().__init__()
        self.setObjectName("AIPluginWorker")
        self.layer_id = layer_id
        self.instance_id = instance_id
        self.image = image
        self.plugin_data = plugin_data
        self.plugin_params = plugin_params
        # Cooperative-cancel flag (AUD-31500). Mutex-guarded to mirror
        # AIMaskWorker's pattern; a plain bool would also be GIL-atomic.
        self._mutex = QMutex()
        self._abort = False

    def request_cancel(self) -> None:
        """Cooperatively cancel this worker's inference.

        Safe to call from any thread (UI/engine side). Does not touch ONNX
        Runtime cancellation APIs (provider-specific, unsafe mid-call) and
        never terminates the thread; the worker notices the flag at the
        next checkpoint and unwinds.
        """
        self._mutex.lock()
        try:
            self._abort = True
        finally:
            self._mutex.unlock()

    def _is_cancelled(self) -> bool:
        self._mutex.lock()
        try:
            return self._abort
        finally:
            self._mutex.unlock()

    def _check_cancelled(self) -> None:
        """Cancel checkpoint: raise if a cancel was requested."""
        if self._is_cancelled():
            raise _InferenceCancelled()

    def run(self):
        was_cancelled = False
        try:
            result = self._process()
        except _InferenceCancelled:
            # A checkpoint fired — definitive cancellation.
            result = None
            was_cancelled = True
        except Exception as e:
            if self._is_cancelled():
                # Exception racing a cancel request → treat as cancelled.
                result = None
                was_cancelled = True
            else:
                import traceback
                traceback.print_exc()
                self.error.emit(self.layer_id, self.instance_id, str(e))
                return
        if was_cancelled or self._is_cancelled():
            # Suppress stale results; release the (possibly large) input
            # buffer early while the thread winds down.
            self.image = None
            logger.info(
                f"AI plugin inference cancelled (instance {self.instance_id})")
            self.cancelled.emit(self.layer_id, self.instance_id)
            return
        self.finished.emit(self.layer_id, self.instance_id, result)

    def _process(self):
        """Main processing logic."""
        model_path = self.plugin_data.get("model_path", "")
        tile_size = self.plugin_data.get("tile_size", 512)
        overlap = self.plugin_data.get("overlap", 32)
        scale = self.plugin_data.get("scale", 1.0)
        input_format = self.plugin_data.get("input_format", "RGB")
        output_format = self.plugin_data.get("output_format", "RGB")
        blend = self.plugin_params.get("blend", 1.0)

        # Resolve model path relative to project root
        PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if not os.path.isabs(model_path):
            model_path = os.path.join(PROJECT_ROOT, model_path)

        if not os.path.exists(model_path):
            raise FileNotFoundError(f"AI model not found: {model_path}")

        # Load ONNX session (model-switch cache cleanup happens inside
        # _load_session's critical section — AUD-31501)
        session = self._load_session(model_path)
        # Cancel checkpoint: session loads can take up to the load timeout
        # (GPU graph compilation) and cannot be interrupted mid-load; a
        # cancel requested during loading unwinds here instead.
        self._check_cancelled()

        # Prepare input image
        img = np.nan_to_num(self.image, nan=0.0, posinf=1.0, neginf=0.0)
        img = np.clip(img, 0, 1)

        # Convert color format if needed
        if input_format == "BGR":
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        h, w = img.shape[:2]
        logger.info(f"Processing {w}x{h} with scale={scale}, tile={tile_size}, overlap={overlap}")

        self.progress.emit(self.layer_id, self.instance_id, 0)

        t_start = time.time()

        if scale > 1.0:
            output = self._process_upscale(img, session, tile_size, overlap, scale)
        else:
            output = self._process_tiled(img, session, tile_size, overlap)

        elapsed = time.time() - t_start
        logger.info(f"Inference completed in {elapsed:.1f}s")

        # Post-process
        output = np.nan_to_num(output, nan=0.0, posinf=1.0, neginf=0.0)
        output = np.clip(output, 0, 1)

        # Convert output format if needed
        if output_format == "BGR":
            output = cv2.cvtColor(output, cv2.COLOR_BGR2RGB)

        # Blend with original if needed
        if blend < 1.0:
            if scale > 1.0 and output.shape[:2] != img.shape[:2]:
                # Upscale: resize original to output size for blending
                orig_resized = cv2.resize(img, (output.shape[1], output.shape[0]),
                                          interpolation=cv2.INTER_LINEAR)
                output = cv2.addWeighted(output, blend, orig_resized, 1.0 - blend, 0)
                output = np.clip(output, 0, 1)
            elif output.shape[:2] == img.shape[:2]:
                output = cv2.addWeighted(output, blend, img, 1.0 - blend, 0)
                output = np.clip(output, 0, 1)

        return np.ascontiguousarray(output, dtype=np.float32)

    def _load_session(self, model_path):
        """Load ONNX session with GPU acceleration and caching."""
        # Cache read + model-switch cleanup in ONE critical section
        # (AUD-31501): with separate locks a second worker could clear the
        # cache between this worker's read and load.  Model loading itself
        # stays OUTSIDE the mutex — it can run for minutes (GPU graph
        # compilation) and has its own cancel checkpoints.
        AIPluginWorker._cache_mutex.lock()
        try:
            if model_path in AIPluginWorker._session_cache:
                return AIPluginWorker._session_cache[model_path]
            if AIPluginWorker._current_model_path != model_path:
                if AIPluginWorker._current_model_path is not None:
                    AIPluginWorker._session_cache.clear()
                AIPluginWorker._current_model_path = model_path
        finally:
            AIPluginWorker._cache_mutex.unlock()

        providers = get_configured_providers()
        gpu_info = get_gpu_provider_info()
        if gpu_info:
            logger.info(f"GPU: {gpu_info['description']}")
        else:
            logger.info("No GPU, using CPU")

        sess_options = get_session_options()

        session = None

        for provider in providers:
            self._check_cancelled()
            if provider == 'CPUExecutionProvider':
                request_providers = ['CPUExecutionProvider']
            else:
                request_providers = [provider, 'CPUExecutionProvider']

            if provider == 'CPUExecutionProvider' and session is not None:
                continue

            try:
                if provider != 'CPUExecutionProvider':
                    session = self._load_with_timeout(model_path, sess_options, request_providers, timeout=60)
                    if session is None:
                        logger.info(f"GPU provider {provider} timed out, skipping...")
                        continue
                else:
                    session = ort.InferenceSession(
                        model_path, sess_options=sess_options, providers=request_providers
                    )

                actual_providers = session.get_providers()
                primary_name = provider if isinstance(provider, str) else provider[0]

                if primary_name in actual_providers or primary_name == 'CPUExecutionProvider':
                    logger.info(f"Loaded with {primary_name}")
                    self._cache_session(model_path, session)
                    return session
                else:
                    del session
                    session = None

            except Exception as e:
                logger.error(f"Failed with {request_providers}: {e}")
                if session is not None:
                    del session
                    session = None

        # CPU fallback
        if session is None:
            session = ort.InferenceSession(
                model_path, sess_options=sess_options, providers=['CPUExecutionProvider']
            )
            self._cache_session(model_path, session)
            return session

        return session

    @staticmethod
    def _cache_session(model_path: str, session: "ort.InferenceSession") -> None:
        """Store a loaded session in the shared, mutex-guarded cache (AUD-31501).

        Re-checks ``_current_model_path`` under the lock: if another worker
        switched models while this one was loading (loads happen outside
        the mutex), the freshly loaded session belongs to a model that is
        no longer current and must not repopulate the cleared cache.
        """
        AIPluginWorker._cache_mutex.lock()
        try:
            if AIPluginWorker._current_model_path == model_path:
                AIPluginWorker._session_cache[model_path] = session
        finally:
            AIPluginWorker._cache_mutex.unlock()

    @staticmethod
    def _load_with_timeout(
        model_path: str,
        sess_options: "ort.SessionOptions",
        providers: list,
        timeout: int = 60,
    ) -> "ort.InferenceSession | None":
        """Load ONNX session with timeout to prevent GPU compilation hangs.

        AUD-31503：loader 线程不可终止（可能正处在 ONNX 原生调用中），
        超时后把线程收容进 ``_stray_loaders`` 直到其自然退出（顺手清理已
        退出的收容线程），不再让引用半途丢失；真实加载异常以
        ``logger.exception`` 留痕，不再与"超时"混为同一个静默 ``None``。
        """
        result: list = [None]

        def _load() -> None:
            try:
                result[0] = ort.InferenceSession(
                    model_path, sess_options=sess_options, providers=providers
                )
            except Exception as exc:
                logger.error(
                    "ONNX session load failed for %s (providers=%s): %s",
                    model_path, providers, exc,
                )
                result[0] = None

        loader = threading.Thread(
            target=_load, daemon=True, name="AIPluginSessionLoader"
        )
        loader.start()
        loader.join(timeout=timeout)
        if loader.is_alive():
            # Park the (unkillable) loader so its reference — and the
            # session it will eventually produce — survives until the
            # thread exits; prune already-finished strays meanwhile.
            AIPluginWorker._stray_loaders_mutex.lock()
            try:
                AIPluginWorker._stray_loaders.add(loader)
                AIPluginWorker._stray_loaders = {
                    t for t in AIPluginWorker._stray_loaders if t.is_alive()
                }
            finally:
                AIPluginWorker._stray_loaders_mutex.unlock()
            logger.warning(
                "ONNX session load exceeded %ss timeout; loader thread "
                "parked until it exits (model: %s)", timeout, model_path,
            )
            return None
        return result[0]

    def _process_tiled(self, img_rgb, session, tile_size, overlap):
        """
        Process image in tiles with cosine-window blending.
        For scale=1.0 models (same input/output size).
        """
        h, w, c = img_rgb.shape
        output = np.zeros((h, w, c), dtype=np.float32)
        weight = np.zeros((h, w, c), dtype=np.float32)

        # Cosine ramps for smooth blending
        if overlap > 0:
            t = np.arange(overlap) / max(overlap - 1, 1)
            fade_in = 0.5 * (1 - np.cos(np.pi * t))
            fade_out = 0.5 * (1 + np.cos(np.pi * t))
            ramp_h_in = fade_in[:, np.newaxis, np.newaxis]
            ramp_h_out = fade_out[:, np.newaxis, np.newaxis]
            ramp_w_in = fade_in[np.newaxis, :, np.newaxis]
            ramp_w_out = fade_out[np.newaxis, :, np.newaxis]
        else:
            ramp_h_in = ramp_h_out = ramp_w_in = ramp_w_out = None

        stride = tile_size - overlap
        input_name = session.get_inputs()[0].name

        # Count tiles for progress
        total_tiles = 0
        for y in range(0, h, stride):
            for x in range(0, w, stride):
                total_tiles += 1

        processed = 0

        for y in range(0, h, stride):
            if y + tile_size > h:
                y = max(0, h - tile_size)
            for x in range(0, w, stride):
                # Cancel checkpoint at every tile boundary (AUD-31500):
                # worst-case non-interruptible window is one tile.
                self._check_cancelled()
                if x + tile_size > w:
                    x = max(0, w - tile_size)

                y1, y2 = max(0, y), min(h, y + tile_size)
                x1, x2 = max(0, x), min(w, x + tile_size)
                actual_h, actual_w = y2 - y1, x2 - x1

                # Weight mask with cosine ramps
                tile_mask = np.ones((actual_h, actual_w, c), dtype=np.float32)
                if overlap > 0 and ramp_h_in is not None:
                    if y1 > 0:
                        tile_mask[:min(overlap, actual_h), :, :] *= ramp_h_in[:min(overlap, actual_h), :, :]
                    if y2 < h:
                        tile_mask[-min(overlap, actual_h):, :, :] *= ramp_h_out[-min(overlap, actual_h):, :, :]
                    if x1 > 0:
                        tile_mask[:, :min(overlap, actual_w), :] *= ramp_w_in[:, :min(overlap, actual_w), :]
                    if x2 < w:
                        tile_mask[:, -min(overlap, actual_w):, :] *= ramp_w_out[:, -min(overlap, actual_w):, :]

                tile = img_rgb[y1:y2, x1:x2, :]

                # Pad edge tiles
                if tile.shape[0] < tile_size or tile.shape[1] < tile_size:
                    padded = np.zeros((tile_size, tile_size, c), dtype=np.float32)
                    padded[:actual_h, :actual_w, :] = tile
                    tile = padded
                    padded_mask = np.zeros((tile_size, tile_size, c), dtype=np.float32)
                    padded_mask[:actual_h, :actual_w, :] = tile_mask
                    tile_mask = padded_mask

                # NCHW format
                tile_nchw = np.ascontiguousarray(
                    tile.transpose(2, 0, 1)[np.newaxis], dtype=np.float32
                )

                tile_output = session.run(None, {input_name: tile_nchw})[0]
                tile_output = tile_output.squeeze(0).transpose(1, 2, 0)

                # For scale=1 models, output may be same size or padded
                out_h = min(tile_output.shape[0], actual_h)
                out_w = min(tile_output.shape[1], actual_w)
                tile_output = tile_output[:out_h, :out_w, :]
                tile_mask = tile_mask[:out_h, :out_w, :]

                output[y1:y1 + out_h, x1:x1 + out_w, :] += tile_output * tile_mask
                weight[y1:y1 + out_h, x1:x1 + out_w, :] += tile_mask

                processed += 1
                self.progress.emit(self.layer_id, self.instance_id, int(100 * processed / total_tiles))

                if x == max(0, w - tile_size) or x2 >= w:
                    break
            if y == max(0, h - tile_size) or y2 >= h:
                break

        output = output / np.maximum(weight, 1e-5)
        return output

    def _process_upscale(self, img_rgb, session, tile_size, overlap, scale):
        """
        Process image for upscaling. Tiles the input, runs inference on each tile,
        and reassembles the output at the higher resolution.
        """
        h, w, c = img_rgb.shape
        out_h, out_w = int(h * scale), int(w * scale)
        output = np.zeros((out_h, out_w, c), dtype=np.float32)
        weight = np.zeros((out_h, out_w, c), dtype=np.float32)

        # Overlap in output space
        out_overlap = int(overlap * scale)

        # Cosine ramps for output-space blending
        if out_overlap > 0:
            t = np.arange(out_overlap) / max(out_overlap - 1, 1)
            fade_in = 0.5 * (1 - np.cos(np.pi * t))
            fade_out = 0.5 * (1 + np.cos(np.pi * t))
            ramp_h_in = fade_in[:, np.newaxis, np.newaxis]
            ramp_h_out = fade_out[:, np.newaxis, np.newaxis]
            ramp_w_in = fade_in[np.newaxis, :, np.newaxis]
            ramp_w_out = fade_out[np.newaxis, :, np.newaxis]
        else:
            ramp_h_in = ramp_h_out = ramp_w_in = ramp_w_out = None

        stride = tile_size - overlap
        input_name = session.get_inputs()[0].name

        # Count tiles
        total_tiles = 0
        for y in range(0, h, stride):
            for x in range(0, w, stride):
                total_tiles += 1

        processed = 0

        for y in range(0, h, stride):
            if y + tile_size > h:
                y = max(0, h - tile_size)
            for x in range(0, w, stride):
                # Cancel checkpoint at every tile boundary (AUD-31500):
                # worst-case non-interruptible window is one tile.
                self._check_cancelled()
                if x + tile_size > w:
                    x = max(0, w - tile_size)

                y1, y2 = max(0, y), min(h, y + tile_size)
                x1, x2 = max(0, x), min(w, x + tile_size)
                actual_h, actual_w = y2 - y1, x2 - x1

                tile = img_rgb[y1:y2, x1:x2, :]

                # Pad edge tiles to full tile_size
                if tile.shape[0] < tile_size or tile.shape[1] < tile_size:
                    padded = np.zeros((tile_size, tile_size, c), dtype=np.float32)
                    padded[:actual_h, :actual_w, :] = tile
                    tile = padded

                tile_nchw = np.ascontiguousarray(
                    tile.transpose(2, 0, 1)[np.newaxis], dtype=np.float32
                )

                tile_output = session.run(None, {input_name: tile_nchw})[0]
                tile_output = tile_output.squeeze(0).transpose(1, 2, 0)

                # Scale output coordinates
                oy1 = int(y1 * scale)
                ox1 = int(x1 * scale)
                # Actual output tile size = actual input tile size * scale
                o_actual_h = int(actual_h * scale)
                o_actual_w = int(actual_w * scale)

                # Crop output to actual scaled size
                tile_output = tile_output[:o_actual_h, :o_actual_w, :]

                # Weight mask in output space
                tile_mask = np.ones((o_actual_h, o_actual_w, c), dtype=np.float32)
                if out_overlap > 0 and ramp_h_in is not None:
                    if y1 > 0:
                        ramp_len = min(out_overlap, o_actual_h)
                        tile_mask[:ramp_len, :, :] *= ramp_h_in[:ramp_len, :, :]
                    if y2 < h:
                        ramp_len = min(out_overlap, o_actual_h)
                        tile_mask[-ramp_len:, :, :] *= ramp_h_out[-ramp_len:, :, :]
                    if x1 > 0:
                        ramp_len = min(out_overlap, o_actual_w)
                        tile_mask[:, :ramp_len, :] *= ramp_w_in[:, :ramp_len, :]
                    if x2 < w:
                        ramp_len = min(out_overlap, o_actual_w)
                        tile_mask[:, -ramp_len:, :] *= ramp_w_out[:, -ramp_len:, :]

                oy2 = min(oy1 + o_actual_h, out_h)
                ox2 = min(ox1 + o_actual_w, out_w)
                crop_h = oy2 - oy1
                crop_w = ox2 - ox1

                output[oy1:oy2, ox1:ox2, :] += tile_output[:crop_h, :crop_w, :] * tile_mask[:crop_h, :crop_w, :]
                weight[oy1:oy2, ox1:ox2, :] += tile_mask[:crop_h, :crop_w, :]

                processed += 1
                self.progress.emit(self.layer_id, self.instance_id, int(100 * processed / total_tiles))

                if x == max(0, w - tile_size) or x2 >= w:
                    break
            if y == max(0, h - tile_size) or y2 >= h:
                break

        output = output / np.maximum(weight, 1e-5)
        return output


class AIPluginEngine(QObject):
    """
    Controller for AI plugin inference. Manages async workers.

    Signals:
        processing_finished: (layer_id, instance_id, result_array)
        progress_updated: (layer_id, instance_id, percentage)
        error_occurred: (layer_id, instance_id, error_message)
    """
    processing_finished = pyqtSignal(str, str, np.ndarray)
    progress_updated = pyqtSignal(str, str, int)
    error_occurred = pyqtSignal(str, str, str)

    def __init__(self):
        super().__init__()
        self._workers = {}  # {instance_id: AIPluginWorker}
        # Cancelled-but-possibly-still-running workers (zombie mode,
        # AUD-31500): their reference is kept until the thread exits
        # naturally so a running QThread is never destroyed mid-run.
        self._retiring_workers: set = set()

    def _sweep_retiring_workers(self) -> None:
        """Drop references to cancelled workers whose threads have exited.

        The ``cancelled`` signal handler already removes them in the common
        case; the sweep covers the corner where the queued signal arrived
        before ``run()`` had fully returned (dropping the last reference to
        a still-running QThread would destroy it mid-run).
        """
        if not self._retiring_workers:
            return
        self._retiring_workers = {
            w for w in self._retiring_workers if not w.isFinished()
        }

    def _release_worker(self, instance_id: str, worker: "AIPluginWorker") -> None:
        """Drop bookkeeping for a signaling worker, if it is still current.

        Identity-guarded so a stale (cancelled) worker that finishes after a
        new worker replaced it cannot pop the new worker's entry.
        """
        if self._workers.get(instance_id) is worker:
            self._workers.pop(instance_id, None)
        if worker.isFinished():
            self._retiring_workers.discard(worker)

    def process_plugin(self, layer_id, instance_id, image, plugin_data, plugin_params):
        """
        Start async ONNX inference for an AI plugin.

        Re-entry for the same instance cancels the previous run
        cooperatively (AUD-31500): the old worker notices the cancel flag at
        its next checkpoint and exits on its own. It is deliberately NOT
        terminated and NOT waited for — ``QThread.terminate()`` on a thread
        inside an ONNX native call can corrupt runtime state, and
        ``wait()`` on the UI path would freeze the interface. The old
        worker's reference is held in ``_retiring_workers`` until it exits
        naturally.

        Args:
            layer_id: Layer ID string
            instance_id: Plugin instance ID string
            image: float32 RGB [0, 1] numpy array
            plugin_data: Plugin metadata dict
            plugin_params: User parameters dict
        """
        self._sweep_retiring_workers()
        # Cancel existing worker for this instance (cooperative only).
        old = self._workers.get(instance_id)
        if old is not None:
            if old.isRunning():
                old.request_cancel()
                self._retiring_workers.add(old)
            else:
                # Finished but its queued signal handler has not run yet.
                self._workers.pop(instance_id, None)

        worker = AIPluginWorker(layer_id, instance_id, image.copy(), plugin_data, plugin_params)
        worker.finished.connect(self._on_worker_finished)
        worker.progress.connect(self._on_worker_progress)
        worker.error.connect(self._on_worker_error)
        worker.cancelled.connect(self._on_worker_cancelled)

        self._workers[instance_id] = worker
        worker.start()

    def _on_worker_finished(self, layer_id, instance_id, result):
        worker = self.sender()
        if isinstance(worker, AIPluginWorker):
            self._release_worker(instance_id, worker)
        self.processing_finished.emit(layer_id, instance_id, result)

    def _on_worker_progress(self, layer_id, instance_id, pct):
        self.progress_updated.emit(layer_id, instance_id, pct)

    def _on_worker_error(self, layer_id, instance_id, msg):
        worker = self.sender()
        if isinstance(worker, AIPluginWorker):
            self._release_worker(instance_id, worker)
        self.error_occurred.emit(layer_id, instance_id, msg)

    def _on_worker_cancelled(self, layer_id, instance_id):
        """Bookkeeping cleanup when a worker exits via cooperative cancel."""
        worker = self.sender()
        if isinstance(worker, AIPluginWorker):
            self._release_worker(instance_id, worker)

    def is_processing(self, instance_id=None):
        if instance_id:
            w = self._workers.get(instance_id)
            return w is not None and w.isRunning()
        return any(w.isRunning() for w in self._workers.values())

    def cancel(self, instance_id):
        """Request cooperative cancellation of an instance's active worker.

        Returns immediately — never blocks in ``wait()`` and never calls
        ``QThread.terminate()`` (AUD-31500): the worker may currently be
        inside an ONNX native ``session.run``. The worker emits
        ``cancelled`` and cleans up its own bookkeeping when its thread
        exits; worst-case latency is one inference tile (single-tile
        models: one full-image pass) or one session load.
        """
        self._sweep_retiring_workers()
        w = self._workers.get(instance_id)
        if w is None:
            return
        if w.isRunning():
            w.request_cancel()
            self._retiring_workers.add(w)
        else:
            self._workers.pop(instance_id, None)
