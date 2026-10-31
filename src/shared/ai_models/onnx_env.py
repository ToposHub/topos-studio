"""
ONNX Runtime Environment Configuration

This module provides optimal execution provider selection for ONNX Runtime
with intelligent cross-platform hardware detection.

## GPU Acceleration Dependencies

To enable GPU acceleration, you need to install the appropriate ONNX Runtime package:

- **NVIDIA GPU (CUDA)**:
  - Uninstall: `pip uninstall onnxruntime`
  - Install: `pip install onnxruntime-gpu`
  - Requirements: CUDA 11.x/12.x + cuDNN (https://developer.nvidia.com/cuda-downloads)

- **Mac Apple Silicon (M1/M2/M3/M4)**:
  - Install: `pip install onnxruntime-silicon` (optional, CoreML is bundled)
  - Or use: `onnxruntime` with CoreML support

- **Windows AMD GPU (DirectML)**:
  - Uninstall: `pip uninstall onnxruntime`
  - Install: `pip install onnxruntime-directml`

- **Intel GPU/CPU (OpenVINO)**:
  - Install: `pip install onnxruntime-openvino`

If no GPU provider is available, the system will automatically fall back to CPU.

## Platform-Specific Notes

- **Intel Mac / Hackintosh**: CoreML is DISABLED due to known deadlock issues.
  Only CPU execution is supported on x86_64 macOS.
- **Apple Silicon (M1/M2/M3/M4)**: CoreML is enabled for optimal Metal GPU acceleration.
- **Windows**: DirectML is enabled for broad AMD/Intel/NVIDIA GPU support.
- **Linux**: CUDA is prioritized for NVIDIA GPUs.
"""

import platform
import logging

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover - 取决于 AI runtime 安装状态
    ort = None

logger = logging.getLogger(__name__)


def _require_onnxruntime() -> None:
    """在实际启动 AI 推理前给出可操作的依赖错误。"""
    if ort is None:
        raise RuntimeError(
            "ONNX Runtime is not installed. Install 'onnxruntime' to use AI inference."
        )

# Lazy config accessor — avoids import-time circular dependencies
_config_instance = None


def _get_config():
    """Lazy singleton ConfigManager accessor."""
    global _config_instance
    if _config_instance is None:
        from src.shared.config.config_manager import ConfigManager
        _config_instance = ConfigManager()
    return _config_instance


def get_configured_providers():
    """
    Returns execution providers based on user preferences from config.

    Reads:
        - gpu_acceleration: If False, forces CPU
        - ai_gpu_device: "auto", "cpu", "cuda", "coreml", "directml"

    Returns:
        list: Execution providers with highest priority first.
    """
    if ort is None:
        logger.warning("ONNX Runtime is unavailable; AI inference is disabled")
        return []
    config = _get_config()

    # If GPU acceleration is globally disabled, force CPU
    if config.get("gpu_acceleration") is False:
        logger.info("ENV] GPU acceleration disabled by user preference → CPU only")
        return ['CPUExecutionProvider']

    device = config.get("ai_gpu_device") or "auto"

    if device == "auto":
        return get_optimal_providers()

    if device == "cpu":
        logger.info("ENV] CPU-only mode selected by user")
        return ['CPUExecutionProvider']

    # For specific GPU providers, verify availability
    available = ort.get_available_providers()

    if device == "cuda":
        if 'CUDAExecutionProvider' in available:
            logger.info("ENV] CUDA selected by user preference")
            return ['CUDAExecutionProvider', 'CPUExecutionProvider']
        logger.info("ENV] CUDA requested but not available, falling back to auto")
        return get_optimal_providers()

    if device == "coreml":
        arch = platform.machine().lower()
        is_darwin = platform.system() == 'Darwin'
        is_arm64 = arch in ('arm64', 'aarch64')
        if 'CoreMLExecutionProvider' in available and is_darwin and is_arm64:
            logger.info("ENV] CoreML selected by user preference")
            return ['CoreMLExecutionProvider', 'CPUExecutionProvider']
        logger.info("ENV] CoreML requested but not available, falling back to auto")
        return get_optimal_providers()

    if device == "directml":
        if 'DmlExecutionProvider' in available:
            logger.info("ENV] DirectML selected by user preference")
            return ['DmlExecutionProvider', 'CPUExecutionProvider']
        logger.info("ENV] DirectML requested but not available, falling back to auto")
        return get_optimal_providers()

    return get_optimal_providers()


def get_session_options():
    """
    Returns configured SessionOptions based on user preferences.

    Reads:
        - processing_threads: Number of threads (0=auto)

    Returns:
        ort.SessionOptions: Configured session options.
    """
    _require_onnxruntime()
    config = _get_config()
    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess_options.log_severity_level = 3
    sess_options.enable_mem_pattern = True

    # Processing threads (0 = auto/leave default)
    threads = config.get("processing_threads") or 0
    if threads > 0:
        sess_options.intra_op_num_threads = threads
        logger.info(f"ENV] Processing threads set to {threads}")

    # NOTE: ai_inference_precision (fp16/fp32) is stored in config but requires
    # model quantization at load time to apply. Deferred to future implementation.

    return sess_options


def get_configured_tile_size():
    """Returns the user-configured default AI tile size, or 512."""
    config = _get_config()
    return config.get("ai_tile_size") or 512


def get_configured_model_path():
    """Returns user-configured AI model directory override, or empty string."""
    config = _get_config()
    return config.get("ai_model_path") or ""


def get_optimal_providers():
    """
    Returns the optimal list of Execution Providers for ONNX Runtime.

    This function dynamically detects available providers and returns them
    based on intelligent cross-platform hardware detection:

    Priority Order:
    1. CUDA (NVIDIA GPU) - Highest priority, works on Windows/Linux
    2. CoreML (Apple Silicon ONLY) - M1/M2/M3/M4 chips, NOT Intel Mac
    3. DirectML (Windows GPU) - AMD/Intel/NVIDIA on Windows
    4. CPU (Universal fallback) - Always included

    Safety Isolation:
    - CoreML is BLOCKED on x86_64/amd64 macOS (Intel Mac / Hackintosh)
      to prevent底层调度死锁 (underlying scheduling deadlock).

    Returns:
        list: Optimal execution providers for the current platform,
              with highest priority first. Always includes 'CPUExecutionProvider'.
    """
    if ort is None:
        logger.warning("ONNX Runtime is unavailable; AI inference is disabled")
        return []

    # 1. 获取系统信息
    os_name = platform.system()  # 'Darwin', 'Windows', 'Linux'
    arch = platform.machine().lower()  # 'arm64', 'x86_64', 'amd64'

    # 2. 获取 ONNX Runtime 支持的提供者
    try:
        available_providers = ort.get_available_providers()
    except Exception as e:
        logger.error(f"ENV] Failed to get available providers: {e}")
        logger.info(f"ENV] OS: {os_name}, Arch: {arch} -> Selected Providers: ['CPUExecutionProvider']")
        return ['CPUExecutionProvider']

    if not available_providers:
        logger.info("ENV] No providers available, falling back to CPU")
        logger.info(f"ENV] OS: {os_name}, Arch: {arch} -> Selected Providers: ['CPUExecutionProvider']")
        return ['CPUExecutionProvider']

    # 3. 智能调度与隔离逻辑
    selected_providers = []

    # 第一优先级：CUDA (NVIDIA 全平台满血)
    if 'CUDAExecutionProvider' in available_providers:
        selected_providers.append('CUDAExecutionProvider')
        logger.info("ENV] ✓ CUDA detected - NVIDIA GPU acceleration enabled")

    # 第二优先级：CoreML (仅限 Apple Silicon)
    # 关键安全隔离：Intel Mac / 黑苹果禁止使用 CoreML
    if 'CoreMLExecutionProvider' in available_providers:
        is_darwin = os_name == 'Darwin'
        is_arm64 = arch in ('arm64', 'aarch64')
        is_x86 = arch in ('x86_64', 'amd64', 'x64', 'i386', 'i686')

        if is_darwin and is_arm64:
            # Apple Silicon (M1/M2/M3/M4) - 安全启用 CoreML
            selected_providers.append('CoreMLExecutionProvider')
            logger.info("ENV] ✓ CoreML detected - Apple Silicon GPU (Metal) acceleration enabled")
        elif is_darwin and is_x86:
            # Intel Mac / 黑苹果 - 危险！禁止 CoreML 防止死锁
            logger.info("ENV] ✗ CoreML BLOCKED on Intel Mac/Hackintosh (x86_64) - prevents deadlock")
            logger.info("ENV]   → Falling back to CPU for stability")
        else:
            # 非 macOS 系统，理论上不应该有 CoreML，但以防万一
            logger.info(f"ENV] ~ CoreML available but skipped on {os_name}/{arch}")

    # 第三优先级：DirectML (Windows 泛用 GPU 加速)
    if 'DmlExecutionProvider' in available_providers:
        if os_name == 'Windows':
            selected_providers.append('DmlExecutionProvider')
            logger.info("ENV] ✓ DirectML detected - Windows GPU acceleration enabled")
        else:
            logger.info(f"ENV] ~ DirectML skipped on non-Windows platform ({os_name})")

    # 最终保底：CPU (全平台通用)
    # 无论 GPU 探测结果如何，必须添加 CPU 作为容灾保底
    if 'CPUExecutionProvider' not in selected_providers:
        selected_providers.append('CPUExecutionProvider')

    # 4. 打印最终决策
    logger.info(f"ENV] OS: {os_name}, Arch: {arch} -> Selected Providers: {selected_providers}")

    return selected_providers


def get_gpu_provider_info():
    """
    Returns information about the best available GPU provider.

    Returns:
        dict: Information about the GPU provider status, or None if no GPU available.
    """
    providers = get_optimal_providers()
    os_name = platform.system()
    arch = platform.machine().lower()

    for provider in providers:
        if provider == 'CUDAExecutionProvider':
            return {
                'name': 'CUDA',
                'provider': provider,
                'description': 'NVIDIA GPU (CUDA)',
                'install_hint': 'pip install onnxruntime-gpu'
            }
        elif provider == 'CoreMLExecutionProvider':
            return {
                'name': 'CoreML',
                'provider': provider,
                'description': 'Apple Silicon GPU (Metal)',
                'install_hint': 'pip install onnxruntime (or onnxruntime-silicon)'
            }
        elif provider == 'DmlExecutionProvider':
            return {
                'name': 'DirectML',
                'provider': provider,
                'description': 'Windows DirectML (AMD/Intel/NVIDIA)',
                'install_hint': 'pip install onnxruntime-directml'
            }

    # No GPU available - provide context-specific information
    if os_name == 'Darwin' and arch in ('x86_64', 'amd64'):
        return {
            'name': 'None',
            'provider': None,
            'description': 'Intel Mac - CPU only (CoreML blocked for stability)',
            'install_hint': 'CoreML is disabled on Intel Mac to prevent deadlock. Use CPU mode.'
        }

    return None


def get_system_info():
    """
    Returns detailed system information for debugging.

    Returns:
        dict: System information including OS, architecture, and available providers.
    """
    try:
        available_providers = ort.get_available_providers()
    except Exception:
        available_providers = []

    return {
        'os': platform.system(),
        'arch': platform.machine().lower(),
        'python_version': platform.python_version(),
        'onnxruntime_version': getattr(ort, '__version__', None),
        'available_providers': available_providers,
        'selected_providers': get_optimal_providers()
    }


if __name__ == "__main__":
    # Test the provider detection
    logger.info("=" * 60)
    logger.info("Runtime Provider Detection Test")
    logger.info("=" * 60)

    info = get_system_info()

    logger.info("System Information:")
    logger.info(f"  OS: {info['os']}")
    logger.info(f"  Architecture: {info['arch']}")
    logger.info(f"  Python: {info['python_version']}")
    logger.info(f"  ONNX Runtime: {info['onnxruntime_version']}")

    logger.info("Provider Status:")
    logger.info(f"  Available: {info['available_providers']}")
    logger.info(f"  Selected: {info['selected_providers']}")

    gpu_info = get_gpu_provider_info()
    if gpu_info and gpu_info.get('provider'):
        logger.info("GPU Acceleration Available:")
        logger.info(f"  Provider: {gpu_info['description']}")
        logger.info(f"  Install: {gpu_info['install_hint']}")
    elif gpu_info:
        logger.info(f"GPU Status: {gpu_info['description']}")
    else:
        logger.info("No GPU acceleration available. Using CPU only.")
        logger.info("  To enable GPU acceleration, see the module docstring for installation instructions.")
