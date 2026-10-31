"""
AI Models Module - ONNX-based AI inference for Topos Color.

This module provides:
- ONNX Runtime environment configuration (GPU/CPU auto-detection)
- Model management (download, cache, version tracking)
- Inference engine (tiled processing, streaming preview)
- Pre-trained models (denoise, inpaint, mask)

Modules:
    onnx_env: ONNX Runtime configuration with GPU provider detection
    model_manager: Model download, cache, and registration
    inference_engine: Unified ONNX inference with progress reporting
    denoise_worker: Denoising worker for AIDenoiseNode
    ai_mask_engine: Mask generation (BiRefNet, SAM)
    nafnet: NAFNet denoising model architecture
    swinir: SwinIR denoising model architecture
"""

from .onnx_env import (
    get_optimal_providers,
    get_configured_providers,
    get_session_options,
    get_configured_tile_size,
    get_configured_model_path,
    get_gpu_provider_info,
    get_system_info,
)

from .model_manager import (
    AIModelManager,
    ModelInfo,
    MODEL_REGISTRY,
    get_model_manager,
)

try:
    from .inference_engine import (
        AIInferenceWorker,
        AIInferenceEngine,
        get_inference_engine,
    )
except ModuleNotFoundError as exc:
    # AI runtime 缺失时仍允许颜色/剪辑模块导入；实际调用 AI 时再明确报错。
    if exc.name != "onnxruntime":
        raise
    AIInferenceWorker = None
    AIInferenceEngine = None

    def get_inference_engine():
        raise RuntimeError(
            "ONNX Runtime is not installed. Install 'onnxruntime' to use AI inference."
        )

__all__ = [
    # ONNX Environment
    "get_optimal_providers",
    "get_configured_providers",
    "get_session_options",
    "get_configured_tile_size",
    "get_configured_model_path",
    "get_gpu_provider_info",
    "get_system_info",
    
    # Model Manager
    "AIModelManager",
    "ModelInfo",
    "MODEL_REGISTRY",
    "get_model_manager",
    
    # Inference Engine
    "AIInferenceWorker",
    "AIInferenceEngine",
    "get_inference_engine",
]
