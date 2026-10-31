"""
AI Mask Engine - BiRefNet & SAM Models for Image Segmentation

This module provides AI-powered mask generation using BiRefNet (background removal)
and SAM (Segment Anything Model) for interactive segmentation.

## GPU Acceleration Setup

To enable GPU acceleration for faster inference:

### NVIDIA GPU (CUDA)
```bash
pip uninstall onnxruntime
pip install onnxruntime-gpu
```
Requirements: CUDA 11.x/12.x + cuDNN installed
Download: https://developer.nvidia.com/cuda-downloads

### Mac Apple Silicon (M1/M2/M3/M4)
CoreML provider is bundled with standard onnxruntime.
```bash
pip install onnxruntime  # or onnxruntime-silicon for optimized builds
```

### Windows AMD/Intel GPU (DirectML)
```bash
pip uninstall onnxruntime
pip install onnxruntime-directml
```

If GPU provider fails to load, the system will automatically fall back to CPU.
"""

import os
import cv2
import numpy as np
import logging

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover - 取决于 AI runtime 安装状态
    ort = None

logger = logging.getLogger(__name__)
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, QMutex
from src.shared.ai_models.onnx_env import (
    get_configured_providers, get_session_options, get_gpu_provider_info
)

class AIMaskWorker(QObject):
    """
    Asynchronous Worker for AI Mask Generation (BiRefNet & SAM).
    """
    mask_ready = pyqtSignal(np.ndarray)
    error = pyqtSignal(str)
    sam_encode_finished = pyqtSignal()
    
    # Models paths (Calculated absolutely relative to project root)
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    MODEL_DIR = os.path.join(BASE_DIR, "models", "mask")
    
    BIREFNET_MODEL = "birefnet.onnx"
    SAM_ENCODER = "sam-vit-b-encoder.onnx"
    SAM_DECODER = "sam-vit-b-decoder.onnx"

    def __init__(self):
        super().__init__()
        self._mutex = QMutex()
        self._abort = False
        self._cancel_generation = 0
        
        # ONNX Sessions (Lazy loaded)
        self.session_birefnet = None
        self.session_sam_enc = None
        self.session_sam_dec = None
        
        # SAM State
        self.sam_embedding = None
        self.sam_img_shape = (0, 0) # Original image shape (h, w)
        self.sam_input_size = (1024, 1024) # Standard SAM input size
        # Incremental decode cache (AUD-31510): Smart Brush re-emits the
        # full point list on every click; storing the processed prefix and
        # accumulators makes each decode run only the NEW points instead
        # of re-running all N historical ones (O(N^2) per session).
        # ``embedding`` is stored by identity: a re-encoded embedding
        # (new image / re-encode) invalidates the cache even if the
        # array contents happened to match.
        self._sam_decode_cache = None

    def _begin_operation(self) -> int:
        """Start one inference operation and return its cancellation generation."""
        self._mutex.lock()
        try:
            self._abort = False
            return self._cancel_generation
        finally:
            self._mutex.unlock()

    def request_cancel(self) -> None:
        """Cooperatively cancel the active inference operation.

        The method intentionally does not call ONNX Runtime cancellation APIs:
        those are provider-specific and can be unsafe during a native call.
        Instead, the worker checks a generation token at every Python-side
        stage and suppresses stale result/error signals after the native call
        returns.  It is safe to invoke directly from the UI shutdown path.
        """
        self._mutex.lock()
        try:
            self._cancel_generation += 1
            self._abort = True
        finally:
            self._mutex.unlock()

    def _is_cancelled(self, generation: int) -> bool:
        self._mutex.lock()
        try:
            return self._cancel_generation != generation
        finally:
            self._mutex.unlock()

    def _get_model_path(self, model_name):
        return os.path.join(self.MODEL_DIR, model_name)

    def _load_model(self, model_name):
        """
        Load an ONNX model with optimal settings and GPU acceleration if available.
        
        This method implements:
        1. Graph optimization for better performance
        2. GPU provider selection with automatic fallback to CPU
        3. Graceful degradation if GPU provider fails to load
        
        Args:
            model_name: Name of the ONNX model file
            
        Returns:
            ort.InferenceSession: Loaded ONNX inference session
            
        Raises:
            FileNotFoundError: If model file doesn't exist
            RuntimeError: If model fails to load even with CPU fallback
        """
        if ort is None:
            raise RuntimeError(
                "ONNX Runtime is not installed. Install 'onnxruntime' to use AI masks."
            )
        path = self._get_model_path(model_name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Model not found: {path}")
        
        # Get optimal providers (GPU prioritized, CPU as fallback)
        providers = get_configured_providers()
        
        # Print GPU info for user awareness
        gpu_info = get_gpu_provider_info()
        if gpu_info:
            logger.info(f"Mask Engine] GPU acceleration available: {gpu_info['description']}")
        else:
            logger.info("Mask Engine] No GPU acceleration detected, using CPU.")
            logger.info("Mask Engine] For GPU acceleration, see module docstring for setup instructions.")
        
        # Configure session options for optimal performance
        sess_options = get_session_options()
        
        # Try loading with GPU provider first, fallback to CPU on failure
        # Key: Build provider list with CPU as fallback for unsupported operators
        session = None
        last_error = None
        
        for provider in providers:
            # --- HOTFIX: Prevent Segmentation Fault on Mac Apple Silicon ---
            # SAM's Vision Transformer (ViT) architecture causes context leaks and hard crashes 
            # in CoreML via ONNX Runtime. We must force CPU for SAM models to maintain stability, 
            # while allowing CNN models like BiRefNet to enjoy GPU acceleration.
            if provider == 'CoreMLExecutionProvider' and 'sam' in model_name.lower():
                logger.info(f"Mask Engine] Skipping CoreML for {model_name} to prevent segfault. Forcing CPU.")
                continue
            
            # Build request_providers with CPU fallback for subgraph execution
            # This allows GPU providers to fall back to CPU for unsupported operators
            if provider == 'CPUExecutionProvider':
                # Pure CPU mode
                request_providers = ['CPUExecutionProvider']
            else:
                # CoreML/CUDA/DirectML/other GPU providers: GPU first, CPU as fallback.
                # ONNX Runtime will automatically partition the graph and fallback unsupported nodes to CPU.
                request_providers = [provider, 'CPUExecutionProvider']
            
            # Skip CPU if we already successfully loaded with a GPU provider
            if provider == 'CPUExecutionProvider' and session is not None:
                continue
                
            try:
                logger.info(f"Mask Engine] Loading {model_name} with providers: {request_providers}")
                session = ort.InferenceSession(
                    path,
                    sess_options=sess_options,
                    providers=request_providers
                )
                
                # Verify the session is using the requested providers
                actual_providers = session.get_providers()
                primary_provider = request_providers[0]
                
                # Check if primary provider (GPU) is being used
                if isinstance(primary_provider, tuple):
                    primary_name = primary_provider[0]
                else:
                    primary_name = primary_provider
                
                if primary_name in actual_providers:
                    logger.info(f"Mask Engine] Successfully loaded {model_name} with {primary_name}")
                    logger.info(f"Mask Engine] Session providers: {actual_providers}")
                    return session
                elif primary_name == 'CPUExecutionProvider':
                    logger.info(f"Mask Engine] Successfully loaded {model_name} with CPU")
                    logger.info(f"Mask Engine] Session providers: {actual_providers}")
                    return session
                else:
                    logger.info(f"Mask Engine] Primary provider {primary_name} not available, trying next...")
                    del session
                    session = None
                    
            except Exception as e:
                last_error = e
                logger.error(f"Mask Engine] Failed to load with {request_providers}: {e}")
                if provider != 'CPUExecutionProvider':
                    logger.info("Mask Engine] Falling back to next provider...")
                if session is not None:
                    del session
                    session = None
        
        # If all providers failed, try pure CPU as last resort
        if session is None:
            logger.error("Mask Engine] All providers failed, attempting pure CPU fallback...")
            try:
                session = ort.InferenceSession(
                    path,
                    sess_options=sess_options,
                    providers=['CPUExecutionProvider']
                )
                logger.info(f"Mask Engine] Loaded {model_name} with CPU fallback")
                return session
            except Exception as e:
                error_msg = f"Failed to load model {model_name} even with CPU: {e}"
                logger.error(f"Mask Engine] {error_msg}")
                raise RuntimeError(error_msg) from last_error
        
        return session

    @pyqtSlot(np.ndarray)
    def process_birefnet(self, img):
        """
        Run BiRefNet for background removal.
        img: HxWxC uint8 (BGR)
        """
        logger.info(": Start BiRefNet")
        generation = self._begin_operation()
        try:
            if self._is_cancelled(generation):
                return
            if self.session_birefnet is None:
                logger.info(f": Loading BiRefNet model from {self.BIREFNET_MODEL}")
                self.session_birefnet = self._load_model(self.BIREFNET_MODEL)
                logger.info(": BiRefNet model loaded")
            
            h, w = img.shape[:2]
            logger.info(f": Processing image {w}x{h}")
            
            # Preprocess
            input_size = (1024, 1024)
            img_resized = cv2.resize(img, input_size, interpolation=cv2.INTER_LINEAR)
            img_input = img_resized.astype(np.float32) / 255.0
            img_input = (img_input - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225] # ImageNet normalization
            img_input = img_input.transpose(2, 0, 1)[None, :, :, :] # 1xCxHxW
            img_input = img_input.astype(np.float32)
            
            # Inference
            logger.info(": Running BiRefNet Inference...")
            input_name = self.session_birefnet.get_inputs()[0].name
            outputs = self.session_birefnet.run(None, {input_name: img_input})
            if self._is_cancelled(generation):
                return
            logger.info(": BiRefNet Inference Finished")
            
            # Postprocess (Output is typically 1x1xHxW sigmoid or logits)
            mask = outputs[0][0, 0, :, :]
            
            # BiRefNet output might be logits, apply sigmoid if needed
            if mask.max() > 1.0 or mask.min() < 0.0:
                # Safe sigmoid to prevent overflow
                mask = 1 / (1 + np.exp(-np.clip(mask, -50, 50)))
                
            mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)

            if self._is_cancelled(generation):
                return
            
            logger.info(": Emitting mask_ready")
            self.mask_ready.emit(mask)
            logger.info(": BiRefNet Process Finished Successfully")
            
        except Exception as e:
            logger.info(f": BiRefNet Exception: {str(e)}")
            if not self._is_cancelled(generation):
                self.error.emit(f"BiRefNet Error: {str(e)}")

    @pyqtSlot(np.ndarray)
    def process_sam_encode(self, img):
        """
        Run SAM Encoder to generate image embedding.
        img: HxWxC uint8 (RGB)
        """
        logger.info(": Start SAM Encode")
        generation = self._begin_operation()
        try:
            if self._is_cancelled(generation):
                return
            if self.session_sam_enc is None:
                logger.info(f": Loading SAM Encoder from {self.SAM_ENCODER}")
                self.session_sam_enc = self._load_model(self.SAM_ENCODER)
                logger.info(": SAM Encoder loaded")

            # Invalidate the stale embedding BEFORE inference (AUD-31511):
            # ``sam_img_shape`` and ``sam_embedding`` must flip together,
            # only on success.  Writing the new shape up front (previous
            # behaviour) left a failed/cancelled encode with the NEW
            # shape paired to the OLD embedding, and a later decode then
            # silently produced a mask aligned to the wrong image.
            self.sam_embedding = None
            self._sam_decode_cache = None
            h, w = img.shape[:2]
            
            # Preprocess
            # FIX 1: The image is already RGB from image_engine._prepare_image_for_ai
            # Removed the incorrect cvtColor that was swapping it back to BGR
            img_rgb = img
            
            # FIX 2: SAM requires aspect-ratio preserved longest-side resize and padding
            scale = self.sam_input_size[0] / max(h, w)
            new_h = int(h * scale + 0.5)
            new_w = int(w * scale + 0.5)
            img_resized = cv2.resize(img_rgb, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            
            # Pad to 1024x1024
            img_padded = np.zeros((self.sam_input_size[1], self.sam_input_size[0], 3), dtype=np.uint8)
            img_padded[:new_h, :new_w, :] = img_resized
            
            img_input = img_padded.astype(np.float32)
            # Pixel mean/std for SAM
            pixel_mean = np.array([123.675, 116.28, 103.53], dtype=np.float32)
            pixel_std = np.array([58.395, 57.12, 57.375], dtype=np.float32)
            img_input = (img_input - pixel_mean) / pixel_std
            img_input = img_input.transpose(2, 0, 1)[None, :, :, :]
            
            # Inference
            logger.info(": Running SAM Encoder Inference...")
            input_name = self.session_sam_enc.get_inputs()[0].name
            img_input = img_input.astype(np.float32)
            embedding = self.session_sam_enc.run(None, {input_name: img_input})[0]
            if self._is_cancelled(generation):
                return
            logger.info(": SAM Encoding Finished")

            # Commit shape + embedding atomically AFTER the successful
            # (non-cancelled) inference (AUD-31511).
            self.sam_img_shape = (h, w)
            self.sam_embedding = embedding

            # Notify UI that encoding is complete
            self.sam_encode_finished.emit()
            
        except Exception as e:
            logger.info(f": SAM Encode Exception: {str(e)}")
            if not self._is_cancelled(generation):
                self.error.emit(f"SAM Encode Error: {str(e)}")

    @pyqtSlot(list, list)
    def process_sam_decode(self, points, labels):
        """
        Run SAM Decoder with cached embedding and prompt points.
        points: list of (x, y) normalized coordinates [0.0-1.0]
        labels: list of integers (1=FG, 0=BG)
        """
        logger.info(f": Start SAM Decode with {len(points)} points")
        generation = self._begin_operation()
        try:
            if self._is_cancelled(generation):
                return
            if self.sam_embedding is None:
                self.error.emit("SAM Embedding not ready. Please toggle Smart Brush off/on.")
                return
                
            if self.session_sam_dec is None:
                logger.info(f": Loading SAM Decoder from {self.SAM_DECODER}")
                self.session_sam_dec = self._load_model(self.SAM_DECODER)
                logger.info(": SAM Decoder loaded")
            
            # 坐标换算参数 (保留原有的尺度计算)
            h, w = self.sam_img_shape
            scale = self.sam_input_size[0] / max(h, w)
            new_h = int(h * scale + 0.5)
            new_w = int(w * scale + 0.5)
            orig_im_size = np.array([new_h, new_w], dtype=np.float32)

            # 准备占位用的空蒙版输入
            mask_input = np.zeros((1, 1, 256, 256), dtype=np.float32)
            has_mask_input = np.zeros((1,), dtype=np.float32)

            # 增量解码（AUD-31510）：调用方每次点击都携带全量点表重发 decode，
            # 逐点全量重算会让 N 点会话累计 N(N+1)/2 次解码器推理。若本次点表
            # 恰好以缓存前缀开头且 embedding 未变，则只对新增点推理，累加器从
            # 缓存副本续算；撤销/删点（前缀不匹配）自动回退为全量重算。
            prefix_len = 0
            cached = self._sam_decode_cache
            if (
                cached is not None
                and cached["embedding"] is self.sam_embedding
                and cached["shape"] == self.sam_img_shape
                and len(cached["points"]) <= len(points)
                and cached["points"] == tuple(tuple(p) for p in points[:len(cached["points"])])
                and cached["labels"] == tuple(labels[:len(cached["labels"])])
            ):
                prefix_len = len(cached["points"])
                # 复制后续可安全累加；缓存本体只在完整成功后提交
                pos_mask_accum = cached["pos"].copy()
                neg_mask_accum = cached["neg"].copy()
            else:
                pos_mask_accum = np.zeros((h, w), dtype=np.float32)
                neg_mask_accum = np.zeros((h, w), dtype=np.float32)

            # 遍历每一个点，单独执行推理，实现"真正的笔刷累加"逻辑
            for p, l in list(zip(points, labels))[prefix_len:]:
                if self._is_cancelled(generation):
                    return
                pt_x = p[0] * w * scale
                pt_y = p[1] * h * scale

                # 构造单点 Prompt
                input_points = np.array([[[pt_x, pt_y]]], dtype=np.float32)
                input_labels = np.array([[l]], dtype=np.float32)

                inputs = {
                    "image_embeddings": self.sam_embedding,
                    "point_coords": input_points,
                    "point_labels": input_labels,
                    "mask_input": mask_input,
                    "has_mask_input": has_mask_input,
                    "orig_im_size": orig_im_size
                }

                outputs = self.session_sam_dec.run(None, inputs)
                if self._is_cancelled(generation):
                    return
                masks = outputs[0]
                iou_preds = outputs[1]

                best_idx = np.argmax(iou_preds[0])
                best_mask = masks[0, best_idx, :, :]

                # 裁剪 Padding
                if best_mask.shape == (256, 256) or best_mask.shape == (1024, 1024):
                    out_h, out_w = best_mask.shape
                    valid_h = int(new_h * out_h / 1024)
                    valid_w = int(new_w * out_w / 1024)
                    best_mask = best_mask[:valid_h, :valid_w]

                # 恢复到原图尺寸并二值化
                mask_resized = cv2.resize(best_mask, (w, h), interpolation=cv2.INTER_LINEAR)
                mask_bin = (mask_resized > 0.0).astype(np.float32)

                # 累加到对应的缓冲区 (使用 maximum 取并集)
                if l == 1:
                    pos_mask_accum = np.maximum(pos_mask_accum, mask_bin)
                else:
                    neg_mask_accum = np.maximum(neg_mask_accum, mask_bin)

            # 最终合成：正向选区 减去 负向选区
            final_mask = np.clip(pos_mask_accum - neg_mask_accum, 0.0, 1.0)

            if self._is_cancelled(generation):
                return

            # 仅在完整、未取消的解码后提交增量前缀（AUD-31510）：中途取消的
            # 部分累加结果绝不能成为下一次解码的起点。
            self._sam_decode_cache = {
                "embedding": self.sam_embedding,
                "shape": self.sam_img_shape,
                "points": tuple(tuple(p) for p in points),
                "labels": tuple(labels),
                "pos": pos_mask_accum,
                "neg": neg_mask_accum,
            }

            logger.info(f": Emitting mask_ready (SAM Additive, {prefix_len} cached + "
                        f"{len(points) - prefix_len} new points)")
            self.mask_ready.emit(final_mask)
            logger.info(": SAM Decode Finished Successfully")
            
        except Exception as e:
            logger.info(f": SAM Decode Exception: {str(e)}")
            if not self._is_cancelled(generation):
                self.error.emit(f"SAM Decode Error: {str(e)}")
