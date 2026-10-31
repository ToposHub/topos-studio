"""
AI Model Manager - Model download, cache, and version management.

This module provides a unified interface for managing AI models used by
Topos Color's inference pipeline, including:
- Model download from remote sources (HuggingFace, GitHub releases, etc.)
- Local cache management with version tracking
- Automatic model validation and integrity checking
- Model metadata persistence

Architecture:
    ~/.topos_cache/models/
        manifest.json            — Model registry with version info
        denoise/
            nafnet.onnx          — Denoising models
            swinir.onnx
        inpaint/
            lama.onnx            — Inpainting models
            mat.onnx
        mask/
            birefnet.onnx        — Mask generation models
            sam_encoder.onnx
            sam_decoder.onnx
"""

import os
import json
import hashlib
import time
import threading
import urllib.request
import urllib.error
import logging

logger = logging.getLogger(__name__)
from typing import Dict, Optional, List
from dataclasses import dataclass
from PyQt6.QtCore import QObject, pyqtSignal, QThread

#: AUD-31811：MODEL_REGISTRY 是模块级可变全局（多处按 id 原位更新字段），
#: 保持全局（外部 ``from .model_manager import MODEL_REGISTRY`` 依赖它），
#: 但所有写路径统一经 ``_REGISTRY_LOCK`` 串行化。
_REGISTRY_LOCK = threading.Lock()


@dataclass
class ModelInfo:
    """Model metadata structure."""
    name: str                          # Model name (e.g., "lama.onnx")
    category: str                      # Category (denoise, inpaint, mask)
    version: str                       # Version string (e.g., "1.0.0")
    source_url: str                    # Download URL
    file_size: int                     # Expected file size in bytes
    sha256: Optional[str] = None       # SHA256 checksum for validation
    description: str = ""              # Human-readable description
    local_path: Optional[str] = None   # Local cache path (set after download)
    download_progress: float = 0.0     # Download progress (0.0-1.0)
    is_available: bool = False         # Whether model is downloaded and validated


# Predefined model registry with known models
MODEL_REGISTRY: Dict[str, ModelInfo] = {
    # Denoise models
    "nafnet_denoise": ModelInfo(
        name="nafnet.onnx",
        category="denoise",
        version="1.0.0",
        source_url="",  # Local conversion required
        file_size=0,    # Variable size
        sha256=None,
        description="NAFNet-SIDD denoising model (convert from .pth)",
        is_available=False,
    ),
    "swinir_denoise": ModelInfo(
        name="swinir.onnx",
        category="denoise",
        version="1.0.0",
        source_url="",  # Local conversion required
        file_size=0,
        sha256=None,
        description="SwinIR denoising model (convert from .pth)",
        is_available=False,
    ),
    # Inpaint models
    "lama_inpaint": ModelInfo(
        name="lama.onnx",
        category="inpaint",
        version="1.0.0",
        source_url="https://huggingface.co/smartywu/lama-onnx/resolve/main/lama.onnx",
        file_size=50 * 1024 * 1024,  # ~50MB estimate
        sha256=None,
        description="LaMa resolution-robust large mask inpainting",
        is_available=False,
    ),
    "mat_inpaint": ModelInfo(
        name="mat.onnx",
        category="inpaint",
        version="1.0.0",
        source_url="",  # Placeholder
        file_size=0,
        sha256=None,
        description="MAT mask-aware transformer inpainting",
        is_available=False,
    ),
    # Generate models (text-to-image / image-to-image)
    "stable_diffusion_onnx": ModelInfo(
        name="stable_diffusion.onnx",
        category="generate",
        version="1.0.0",
        source_url="",  # Local conversion or external download required
        file_size=0,    # Variable (depends on model variant)
        sha256=None,
        description="Stable Diffusion ONNX model for text-to-image generation",
        is_available=False,
    ),
    "sdxl_onnx": ModelInfo(
        name="sdxl.onnx",
        category="generate",
        version="1.0.0",
        source_url="",  # Local conversion required
        file_size=0,
        sha256=None,
        description="Stable Diffusion XL ONNX model (high quality generation)",
        is_available=False,
    ),
    # C2 frame interpolation (RIFE v4).  Local conversion required;
    # never downloaded silently — the download UI owns that decision.
    "rife_interpolate": ModelInfo(
        name="rife_interpolate.onnx",
        category="interpolate",
        version="1.0.0",
        source_url="",  # Local conversion required (tools/convert_rife_to_onnx.py)
        file_size=0,
        sha256=None,
        description="RIFE v4 frame interpolation (convert from .pth)",
        is_available=False,
    ),
    # Mask models
    "birefnet_mask": ModelInfo(
        name="birefnet.onnx",
        category="mask",
        version="1.0.0",
        source_url="",  # Local conversion required
        file_size=0,
        sha256=None,
        description="BiRefNet background removal",
        is_available=False,
    ),
    "sam_encoder": ModelInfo(
        name="sam_encoder.onnx",
        category="mask",
        version="1.0.0",
        source_url="https://huggingface.co/spencer/sam-vit-b-onnx/resolve/main/sam-vit-b-encoder.onnx",
        file_size=375 * 1024 * 1024,  # ~375MB
        sha256=None,
        description="SAM ViT-B encoder for interactive segmentation",
        is_available=False,
    ),
    "sam_decoder": ModelInfo(
        name="sam_decoder.onnx",
        category="mask",
        version="1.0.0",
        source_url="https://huggingface.co/spencer/sam-vit-b-onnx/resolve/main/sam-vit-b-decoder.onnx",
        file_size=17 * 1024 * 1024,  # ~17MB
        sha256=None,
        description="SAM ViT-B decoder for mask generation",
        is_available=False,
    ),
}


class ModelDownloadWorker(QThread):
    """Background worker for downloading models with progress reporting."""

    progress = pyqtSignal(str, float)   # model_id, progress (0.0-1.0)
    finished = pyqtSignal(str, bool, str)  # model_id, success, error_message

    def __init__(self, model_id: str, model_info: ModelInfo, target_path: str):
        super().__init__()
        self.model_id = model_id
        self.model_info = model_info
        self.target_path = target_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        # AUD-31809: 下载到 <name>.part，成功后 os.replace 原子落位。
        # 直接写最终路径 + 仅显式取消才清理的旧实现，会把网络中断/写盘
        # 异常留下的半截文件暴露给 _sync_registry"存在即可用"的判定。
        part_path = self.target_path + ".part"
        try:
            if not self.model_info.source_url:
                self.finished.emit(self.model_id, False, "No download URL available")
                return

            os.makedirs(os.path.dirname(self.target_path), exist_ok=True)

            # Download with progress tracking
            request = urllib.request.Request(
                self.model_info.source_url,
                headers={"User-Agent": "Topos-Color/1.0"}
            )

            with urllib.request.urlopen(request, timeout=60) as response:
                total_size = response.headers.get("Content-Length")
                total_size = int(total_size) if total_size else self.model_info.file_size

                downloaded = 0
                chunk_size = 8192

                with open(part_path, "wb") as f:
                    while True:
                        if self._cancelled:
                            self.finished.emit(self.model_id, False, "Cancelled")
                            return

                        chunk = response.read(chunk_size)
                        if not chunk:
                            break

                        f.write(chunk)
                        downloaded += len(chunk)

                        if total_size > 0:
                            progress = downloaded / total_size
                            self.progress.emit(self.model_id, progress)

            os.replace(part_path, self.target_path)
            self.finished.emit(self.model_id, True, "")

        except urllib.error.URLError as e:
            self.finished.emit(self.model_id, False, f"Download failed: {e.reason}")
        except Exception as e:
            self.finished.emit(self.model_id, False, f"Error: {str(e)}")
        finally:
            # 成功路径 .part 已被 os.replace 消费；其余路径（网络中断、写盘
            # 异常、取消）在此统一清理，不留半截残留。
            try:
                os.remove(part_path)
            except OSError:
                pass


class AIModelManager(QObject):
    """
    Unified AI model manager for Topos Color.

    Features:
    - Model registry with predefined models
    - Local cache management
    - Background download with progress tracking
    - Model validation (SHA256 checksum)
    - Version tracking and updates

    Signals:
        model_downloaded(str): Model ID finished downloading
        model_progress(str, float): Download progress (model_id, 0.0-1.0)
        model_error(str, str): Download error (model_id, error_message)
    """

    model_downloaded = pyqtSignal(str)
    model_progress = pyqtSignal(str, float)
    model_error = pyqtSignal(str, str)

    def __init__(self, cache_dir: Optional[str] = None):
        super().__init__()
        if cache_dir is None:
            # [R2-5] 默认走平台规范缓存目录；存量安装的 ~/.topos_cache/models
            # 含已下载权重，存在则继续使用（避免强制重新下载），否则用新目录
            legacy = os.path.join(os.path.expanduser("~"), ".topos_cache", "models")
            if os.path.exists(os.path.join(legacy, "manifest.json")):
                cache_dir = legacy
            else:
                from src.shared.config.config_manager import ConfigManager
                cache_dir = os.path.join(ConfigManager.get_platform_cache_dir(), "models")

        self._cache_dir = cache_dir
        self._manifest_path = os.path.join(cache_dir, "manifest.json")
        self._manifest: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._download_workers: Dict[str, ModelDownloadWorker] = {}

        os.makedirs(cache_dir, exist_ok=True)
        self._load_manifest()
        self._sync_registry()

    def _load_manifest(self):
        """Load manifest from disk."""
        if os.path.exists(self._manifest_path):
            try:
                with open(self._manifest_path, "r", encoding="utf-8") as f:
                    self._manifest = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._manifest = {}

    def _save_manifest(self):
        """Save manifest to disk.

        AUD-31810：原子写。truncate 式 ``open("w")`` 在崩溃/断电时把
        manifest.json 截断损坏，下次启动所有模型被判为未安装。先写同目录
        临时文件再 ``os.replace``（POSIX 原子）。
        """
        tmp_path = self._manifest_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._manifest_path)
        except IOError as e:
            logger.error(f"Failed to save manifest: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _sync_registry(self):
        """Sync local manifest with predefined registry.

        AUD-31809: "存在即可用"不足以判定模型可用——旧版本异常中断可能
        留下零字节/半截残留文件（注册表无 sha256 可校验）。至少要求
        文件非空，否则不标记 available。
        """
        with self._lock, _REGISTRY_LOCK:
            for model_id, model_info in MODEL_REGISTRY.items():
                local_path = self._get_model_path(model_info.category, model_info.name)

                # Check if a non-empty model file exists locally
                if os.path.exists(local_path):
                    try:
                        file_size = os.path.getsize(local_path)
                    except OSError:
                        file_size = 0
                    if file_size <= 0:
                        continue
                    model_info.local_path = local_path
                    model_info.is_available = True
                    model_info.file_size = file_size

                    # Update manifest
                    self._manifest[model_id] = {
                        "name": model_info.name,
                        "category": model_info.category,
                        "version": model_info.version,
                        "local_path": local_path,
                        "file_size": model_info.file_size,
                        "timestamp": time.time(),
                    }

            self._save_manifest()

    def _get_model_path(self, category: str, name: str) -> str:
        """Get the local path for a model."""
        return os.path.join(self._cache_dir, category, name)

    def get_model_info(self, model_id: str) -> Optional[ModelInfo]:
        """Get model info by ID."""
        return MODEL_REGISTRY.get(model_id)

    def get_model_path(self, model_id: str) -> Optional[str]:
        """Get the local path for a downloaded model."""
        model_info = MODEL_REGISTRY.get(model_id)
        if model_info and model_info.is_available:
            return model_info.local_path
        return None

    def is_model_available(self, model_id: str) -> bool:
        """Check if a model is downloaded and available."""
        model_info = MODEL_REGISTRY.get(model_id)
        return model_info.is_available if model_info else False

    def list_available_models(self, category: Optional[str] = None) -> List[str]:
        """List available model IDs, optionally filtered by category."""
        result = []
        for model_id, model_info in MODEL_REGISTRY.items():
            if model_info.is_available:
                if category is None or model_info.category == category:
                    result.append(model_id)
        return result

    def list_required_models(self, category: Optional[str] = None) -> List[str]:
        """List models that need to be downloaded."""
        result = []
        for model_id, model_info in MODEL_REGISTRY.items():
            if not model_info.is_available and model_info.source_url:
                if category is None or model_info.category == category:
                    result.append(model_id)
        return result

    def download_model(self, model_id: str) -> bool:
        """
        Start downloading a model in background.

        Args:
            model_id: Model ID from registry

        Returns:
            True if download started, False if already downloading or no URL
        """
        model_info = MODEL_REGISTRY.get(model_id)
        if not model_info:
            logger.info(f"Unknown model: {model_id}")
            return False

        if model_info.is_available:
            logger.info(f"Model already available: {model_id}")
            return False

        if not model_info.source_url:
            logger.info(f"No download URL for: {model_id}")
            return False

        target_path = self._get_model_path(model_info.category, model_info.name)

        # AUD-31811：check-then-act（已下载中？）与插入必须在锁内原子完成，
        # 两个线程同时 download_model 不再各自起一个 worker。
        with self._lock:
            if model_id in self._download_workers:
                logger.info(f"Already downloading: {model_id}")
                return False
            worker = ModelDownloadWorker(model_id, model_info, target_path)
            worker.progress.connect(self._on_download_progress)
            worker.finished.connect(self._on_download_finished)
            self._download_workers[model_id] = worker

        worker.start()

        return True

    def cancel_download(self, model_id: str) -> None:
        """Cancel an ongoing download.

        AUD-31811：绝不在调用线程同步 ``wait(3000)``——下载线程可能正阻塞
        在网络 IO 上，这会把 UI 线程卡住最长 3 秒。只投递协作取消请求：
        运行中的 worker 留在 ``_download_workers``（同时防止运行中的
        QThread 被提前 GC），其线程在下一个取消检查点自行退出，条目由
        finished 处理器（``_on_download_finished``）pop + ``deleteLater``。
        已退出但 queued 信号未送达的 worker 立即摘除。
        """
        worker = self._download_workers.get(model_id)
        if worker is None:
            return
        worker.cancel()
        if not worker.isRunning():
            self._download_workers.pop(model_id, None)

    def validate_model(self, model_id: str) -> bool:
        """
        Validate a downloaded model's integrity.

        Returns:
            True if valid, False if checksum mismatch or file missing
        """
        model_info = MODEL_REGISTRY.get(model_id)
        if not model_info or not model_info.local_path:
            return False

        if not os.path.exists(model_info.local_path):
            return False

        # If SHA256 is provided, verify checksum
        if model_info.sha256:
            sha256_hash = hashlib.sha256()
            with open(model_info.local_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256_hash.update(chunk)
            actual_sha256 = sha256_hash.hexdigest()

            if actual_sha256 != model_info.sha256:
                logger.info(f"SHA256 mismatch for {model_id}")
                logger.info(f"  Expected: {model_info.sha256}")
                logger.info(f"  Actual: {actual_sha256}")
                return False

        return True

    def register_local_model(self, model_id: str, local_path: str) -> bool:
        """
        Register a locally converted/model.

        Args:
            model_id: Model ID from registry
            local_path: Path to the local model file

        Returns:
            True if registered successfully
        """
        model_info = MODEL_REGISTRY.get(model_id)
        if not model_info:
            return False

        if not os.path.exists(local_path):
            logger.info(f"File not found: {local_path}")
            return False

        # Update model info（AUD-31811：注册表字段写路径持 _REGISTRY_LOCK）
        with _REGISTRY_LOCK:
            model_info.local_path = local_path
            model_info.is_available = True
            model_info.file_size = os.path.getsize(local_path)

            # Update manifest
            with self._lock:
                self._manifest[model_id] = {
                    "name": model_info.name,
                    "category": model_info.category,
                    "version": model_info.version,
                    "local_path": local_path,
                    "file_size": model_info.file_size,
                    "timestamp": time.time(),
                }
                self._save_manifest()

        logger.info(f"Registered local model: {model_id} -> {local_path}")
        return True

    def get_cache_size(self) -> int:
        """Get total cache size in bytes."""
        total = 0
        for model_id, model_info in MODEL_REGISTRY.items():
            if model_info.is_available and model_info.local_path:
                total += model_info.file_size
        return total

    def clear_cache(self):
        """Clear all cached models."""
        with self._lock, _REGISTRY_LOCK:
            for model_id, model_info in MODEL_REGISTRY.items():
                if model_info.local_path and os.path.exists(model_info.local_path):
                    try:
                        os.remove(model_info.local_path)
                    except OSError:
                        pass
                model_info.local_path = None
                model_info.is_available = False
                model_info.file_size = 0

            self._manifest.clear()
            self._save_manifest()

    def _on_download_progress(self, model_id: str, progress: float):
        """Handle download progress update."""
        with _REGISTRY_LOCK:
            model_info = MODEL_REGISTRY.get(model_id)
            if model_info:
                model_info.download_progress = progress
        self.model_progress.emit(model_id, progress)

    def _on_download_finished(self, model_id: str, success: bool, error_message: str):
        """Handle download completion."""
        worker = self._download_workers.pop(model_id, None)
        if worker:
            worker.deleteLater()

        if success:
            with _REGISTRY_LOCK:
                model_info = MODEL_REGISTRY.get(model_id)
                if model_info:
                    model_info.is_available = True
                    model_info.local_path = self._get_model_path(
                        model_info.category, model_info.name
                    )
                    model_info.file_size = os.path.getsize(model_info.local_path)

                    with self._lock:
                        self._manifest[model_id] = {
                            "name": model_info.name,
                            "category": model_info.category,
                            "version": model_info.version,
                            "local_path": model_info.local_path,
                            "file_size": model_info.file_size,
                            "timestamp": time.time(),
                        }
                        self._save_manifest()

            self.model_downloaded.emit(model_id)
        else:
            self.model_error.emit(model_id, error_message)


# Convenience function for getting the default model manager
_model_manager_instance: Optional[AIModelManager] = None
#: AUD-31811：单例创建的 check-then-act 由模块级锁串行化（双检）。
_model_manager_lock = threading.Lock()


def get_model_manager() -> AIModelManager:
    """Get or create the singleton AIModelManager instance."""
    global _model_manager_instance
    if _model_manager_instance is None:
        with _model_manager_lock:
            if _model_manager_instance is None:
                _model_manager_instance = AIModelManager()
    return _model_manager_instance