"""真实 provider 的共用基础设施：device 解析 + 模型懒加载。

对齐主项目 ``_resolve_torch_device``（CUDA → MPS → CPU）模式，
但 vault 自包含不依赖 src/。

所有真实 provider 都通过本模块解析 device，保证跨平台行为一致。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# torch device 解析（CUDA → MPS → CPU，失败兜底）
# ---------------------------------------------------------------------- #

def resolve_torch_device() -> tuple[str, Any]:
    """解析 torch 推理设备（对齐主项目 transcript_provider.py:86 模式）。

    优先级（互斥）：
        1. 环境变量 TOPOS_VAULT_DEVICE 强制指定
        2. CUDA（torch.cuda.is_available()）
        3. MPS（Apple Silicon，torch.backends.mps.is_available()）
        4. CPU 兜底

    Returns:
        (device_str, dtype) —— device_str 如 "cuda:0"/"mps"/"cpu"，
        dtype 是对应的推荐精度（cuda 用 bfloat16，mps 用 float16，cpu 用 float32）。
    """
    try:
        import torch
    except ImportError:
        raise RuntimeError("torch 未安装，无法解析 device")

    # 1. 环境变量强制
    env_device = os.environ.get("TOPOS_VAULT_DEVICE", "").strip().lower()
    if env_device:
        if env_device.startswith("cuda"):
            return env_device, torch.bfloat16
        if env_device == "mps":
            return "mps", torch.float16
        return "cpu", torch.float32

    # 2. CUDA
    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16

    # 3. MPS（Apple Silicon）
    if torch.backends.mps.is_available():
        return "mps", torch.float16

    # 4. CPU
    return "cpu", torch.float32


def is_gpu_available() -> bool:
    """是否有可用的 GPU（CUDA 或 MPS）。"""
    try:
        import torch
        return torch.cuda.is_available() or torch.backends.mps.is_available()
    except ImportError:
        return False


# ---------------------------------------------------------------------- #
# 模型懒加载基类
# ---------------------------------------------------------------------- #

class LazyModelLoader:
    """模型懒加载器：首次使用时才加载，避免启动时全装进内存。

    真实 provider 继承本类，实现 ``_load_model()`` 返回模型对象。
    ``is_available()`` 检查依赖和模型文件是否存在（不实际加载）。
    """

    def __init__(self, provider_id: str, model_pack_id: str | None = None):
        self.provider_id = provider_id
        self.model_pack_id = model_pack_id
        self._model: Any = None
        self._loaded = False
        self._load_error: str | None = None

    @property
    def model(self) -> Any:
        """获取模型实例（首次访问时懒加载）。"""
        if not self._loaded:
            self._load()
        return self._model

    def is_available(self) -> bool:
        """provider 是否可用（依赖齐全 + 模型已装）。

        本方法不实际加载模型（快），只做静态检查。
        子类可覆盖做更精确的检查。
        """
        if self._loaded and self._model is not None:
            return True
        if self._load_error:
            return False
        # 默认实现：检查依赖是否齐全（子类可加模型文件检查）
        return self._check_dependencies()

    def _check_dependencies(self) -> bool:
        """检查运行依赖是否齐全（子类覆盖）。"""
        return True

    def _load(self) -> None:
        """实际加载模型（子类实现 _load_model）。"""
        self._loaded = True
        try:
            self._model = self._load_model()
        except Exception as e:
            self._load_error = str(e)
            logger.warning("provider %s 模型加载失败：%s", self.provider_id, e)
            self._model = None

    def _load_model(self) -> Any:
        """子类实现：加载并返回模型对象。"""
        raise NotImplementedError

    def unload(self) -> None:
        """释放模型（显存紧张时调用，分时复用）。"""
        self._model = None
        self._loaded = False
        self._load_error = None

    def _resolve_local_or_hf(self, hf_id: str) -> str:
        """优先用本地 models/ai_editing/<model_pack_id>/ 路径，否则用 HF id。

        sentence-transformers / transformers 都同时接受本地目录路径和 HF repo id：
        传路径 → 直接加载本地（不联网，适合用户已下好的场景）；
        传 HF id → 本地缓存命中则用缓存，否则联网下载。

        本方法让 vault 与主项目共享 models/ai_editing/ 下的权重：
        用户把模型放到 models/ai_editing/<pack_id>/ 就不用重复下载。
        """
        if not self.model_pack_id:
            return hf_id
        try:
            from apps.topos_vault.model_specs import get_spec
            spec = get_spec(self.model_pack_id)
            if spec is not None and spec.is_installed:
                return str(spec.install_dir)
        except Exception as e:
            logger.debug("本地模型路径检查失败 %s：%s", self.model_pack_id, e)
        return hf_id


# ---------------------------------------------------------------------- #
# 模型路径检查
# ---------------------------------------------------------------------- #

def is_model_pack_installed(pack_id: str) -> bool:
    """检查模型包是否已安装（目录存在且有文件）。

    对齐 model_specs.py 的 VaultModelPackSpec.is_installed 逻辑。
    """
    if not pack_id:
        return False
    try:
        from apps.topos_vault.model_specs import get_spec
        spec = get_spec(pack_id)
        if spec is None:
            return False
        return spec.is_installed
    except Exception:
        return False
