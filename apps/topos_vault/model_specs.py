"""vault 模型包 spec 定义（与主项目 ModelPackManager 集成）。

vault 的模型权重放 ``models/ai_editing/``（与主项目共享），每个 pack 有
``manifest.json`` 记录版本/大小/sha256/许可证/GPU 需求。

本模块定义 vault 用到的所有模型包的元数据，提供两个能力：
1. 独立运行时：vault 自己用这些 spec 检查模型是否已安装、在哪个路径
2. 集成运行时：把 vault specs 转换成主项目的 ModelPackSpec，注册进 ModelPackManager

不直接 import 主项目的 ModelPackSpec——vault 要能脱离 src/ 独立运行。
两边的 spec 通过字段名对齐（数据契约），不通过类型继承耦合。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.topos_vault.paths import get_models_dir


# ---------------------------------------------------------------------- #
# 许可证
# ---------------------------------------------------------------------- #

@dataclass(frozen=True)
class ModelLicense:
    """模型许可证（对齐主项目 ModelPackLicense 字段）。"""
    name: str
    commercial_use: bool
    redistribution: bool
    attribution_required: bool
    notes: str = ""


# 许可证常量
_APACHE2 = ModelLicense(
    name="Apache-2.0", commercial_use=True, redistribution=True,
    attribution_required=True,
)
_QWEN_APACHE = ModelLicense(
    name="Qwen (Apache-2.0)", commercial_use=True, redistribution=True,
    attribution_required=True,
    notes="Qwen 系列模型 Apache-2.0 商用友好。下载前核对具体 checkpoint 条款。",
)
_MIT = ModelLicense(
    name="MIT", commercial_use=True, redistribution=True,
    attribution_required=True,
)


# ---------------------------------------------------------------------- #
# Spec
# ---------------------------------------------------------------------- #

@dataclass(frozen=True)
class VaultModelPackSpec:
    """vault 模型包描述（对齐主项目 ModelPackSpec 字段，但不依赖它）。

    字段与 ``src.features.edit.ai_editing.model_pack_manager.ModelPackSpec``
    一一对应，便于在集成模式下转换。
    """
    pack_id: str                     # "qwen3_vl_8b_awq"
    capability: str                  # "visual_caption" / "transcript_embed" / ...
    version: str
    size_bytes: int                  # 下载体积（UI 展示）
    sha256: str                      # 完整性校验
    languages: tuple[str, ...]       # ("zh", "en")
    requires_gpu: bool
    min_vram_mb: int
    license: ModelLicense
    download_url: str = ""
    notes: str = ""                  # "质量优先" / "轻量备选" 等

    @property
    def install_dir(self) -> Path:
        """该 pack 的安装目录：models/ai_editing/<pack_id>/。"""
        return get_models_dir(self.pack_id)

    @property
    def is_installed(self) -> bool:
        """pack 目录是否存在（粗判，精确校验用 verify_integrity）。"""
        return self.install_dir.exists() and any(self.install_dir.iterdir())

    def to_dict(self) -> dict[str, Any]:
        """转成字典（对齐主项目 ModelPackSpec.to_dict 的结构）。"""
        return {
            "pack_id": self.pack_id,
            "capability": self.capability,
            "version": self.version,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "languages": list(self.languages),
            "requires_gpu": self.requires_gpu,
            "min_vram_mb": self.min_vram_mb,
            "license": {
                "name": self.license.name,
                "commercial_use": self.license.commercial_use,
                "redistribution": self.license.redistribution,
                "attribution_required": self.license.attribution_required,
                "notes": self.license.notes,
            },
            "download_url": self.download_url,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------- #
# 内置 specs（vault 用到的所有模型）
# ---------------------------------------------------------------------- #
# 注意：sha256 暂用 placeholder（与主项目 R4 阶段一致），真实校验值在模型下载
# worker 接入后填。pack_id 命名遵循 ``<model>_<variant>`` 约定。

# --- transcript_embed（台词/文本向量）---
SPEC_QWEN3_EMBEDDING = VaultModelPackSpec(
    pack_id="qwen3_embedding_0_6b",
    capability="transcript_embed",
    version="Qwen3-Embedding-0.6B",
    size_bytes=1_200_000_000,         # ~1.2GB
    sha256="sha256:placeholder-qwen3-embedding",
    languages=("zh", "en", "multilingual"),
    requires_gpu=False,              # 0.6B CPU 可跑（慢但能用）
    min_vram_mb=0,
    license=_QWEN_APACHE,
    download_url="https://huggingface.co/Qwen/Qwen3-Embedding-0.6B",
    notes="默认台词向量模型。同等参数量下比 BGE-M3 强 7.9%。1024 维可配。",
)

# --- visual_embed（画面向量，视频+图片共用）---
SPEC_SIGLIP2 = VaultModelPackSpec(
    pack_id="siglip2_base",
    capability="visual_embed",
    version="siglip2-base-patch16-224",
    size_bytes=400_000_000,          # ~400MB
    sha256="sha256:placeholder-siglip2",
    languages=("zh", "en", "multilingual"),
    requires_gpu=False,
    min_vram_mb=0,
    license=_APACHE2,
    # provider 用 sentence-transformers 的封装版（配好 pooling/router），
    # 本地缓存目录名与 pack_id 对齐
    download_url="https://huggingface.co/sentence-transformers/siglip2-base-patch16-224",
    notes="2026 最强开源图文编码器。base-patch16-224 输出 768 维（视频镜头和图片共用）。",
)

# --- visual_caption（画面理解，视频+图片共用）---
SPEC_QWEN3_VL_8B_AWQ = VaultModelPackSpec(
    pack_id="qwen3_vl_8b_awq",
    capability="visual_caption",
    version="Qwen3-VL-8B-Instruct-AWQ-4bit",
    size_bytes=5_500_000_000,        # ~5.5GB（4bit 量化）
    sha256="sha256:placeholder-qwen3-vl-8b-awq",
    languages=("zh", "en"),
    requires_gpu=True,
    min_vram_mb=6000,                # 4bit 约 6GB 显存
    license=_QWEN_APACHE,
    download_url="https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct",
    notes="24GB 显存主力画面理解模型。4bit AWQ 量化。中英双语 caption/tag。",
)

# --- audio_understand（音乐/音频语义理解）---
SPEC_QWEN25_OMNI_7B_AWQ = VaultModelPackSpec(
    pack_id="qwen2_5_omni_7b_awq",
    capability="audio_understand",
    version="Qwen2.5-Omni-7B-AWQ-4bit",
    size_bytes=5_000_000_000,
    sha256="sha256:placeholder-qwen25-omni",
    languages=("zh", "en"),
    requires_gpu=True,
    min_vram_mb=8000,
    license=_QWEN_APACHE,
    download_url="https://huggingface.co/Qwen/Qwen2.5-Omni-7B",
    notes="音乐情绪/乐器/风格理解。BPM 等数值用 librosa 单独算（大模型做不了精确 BPM）。",
)

# --- face_recognize（人脸）---
SPEC_INSIGHTFACE = VaultModelPackSpec(
    pack_id="insightface_buffalo_l",
    capability="face_recognize",
    version="buffalo_l",
    size_bytes=330_000_000,          # ~330MB（ONNX）
    sha256="sha256:placeholder-insightface",
    languages=(),                    # 语言无关
    requires_gpu=False,             # ONNX CPU 可跑
    min_vram_mb=0,
    license=_MIT,
    download_url="https://github.com/deepinsight/insightface/releases",
    notes="2026 金标准。ONNX 推理，CPU 也能跑。512 维人脸向量。",
)


# ---------------------------------------------------------------------- #
# 全部 vault specs
# ---------------------------------------------------------------------- #

VAULT_MODEL_SPECS: tuple[VaultModelPackSpec, ...] = (
    SPEC_QWEN3_EMBEDDING,
    SPEC_SIGLIP2,
    SPEC_QWEN3_VL_8B_AWQ,
    SPEC_QWEN25_OMNI_7B_AWQ,
    SPEC_INSIGHTFACE,
)


def list_vault_specs() -> tuple[VaultModelPackSpec, ...]:
    """所有 vault 模型包 spec。"""
    return VAULT_MODEL_SPECS


def get_spec(pack_id: str) -> VaultModelPackSpec | None:
    """按 pack_id 查 spec。"""
    for s in VAULT_MODEL_SPECS:
        if s.pack_id == pack_id:
            return s
    return None


def specs_by_capability(capability: str) -> list[VaultModelPackSpec]:
    """某能力的所有 pack（可能有多个变体：大/小/量化）。"""
    return [s for s in VAULT_MODEL_SPECS if s.capability == capability]


def check_installation_status() -> list[dict[str, Any]]:
    """检查所有 vault 模型的安装状态（供 CLI status 用）。

    Returns:
        [{pack_id, capability, installed, path, size_gb, requires_gpu}, ...]
    """
    result = []
    for spec in VAULT_MODEL_SPECS:
        result.append({
            "pack_id": spec.pack_id,
            "capability": spec.capability,
            "installed": spec.is_installed,
            "path": str(spec.install_dir) if spec.is_installed else None,
            "size_gb": round(spec.size_bytes / 1_000_000_000, 1),
            "requires_gpu": spec.requires_gpu,
            "min_vram_mb": spec.min_vram_mb,
            "display_name": spec.notes or spec.version,
        })
    return result


# ---------------------------------------------------------------------- #
# 与主项目 ModelPackManager 集成（可选，仅集成模式调用）
# ---------------------------------------------------------------------- #

def to_main_project_specs() -> list:
    """把 vault specs 转成主项目的 ModelPackSpec（集成模式用）。

    本函数延迟 import 主项目模块——vault 独立运行时不调用就不触发依赖。
    主项目启动时如果想统一管理 vault 模型，调本函数拿到 spec 列表，
    传入 ModelPackManager(specs=BUILTIN_SPECS + tuple(to_main_project_specs()))。
    """
    from src.features.edit.ai_editing.model_pack_manager import (
        ModelPackSpec, ModelPackLicense,
    )

    def convert(s: VaultModelPackSpec) -> ModelPackSpec:
        return ModelPackSpec(
            pack_id=s.pack_id,
            capability=s.capability,
            version=s.version,
            size_bytes=s.size_bytes,
            sha256=s.sha256,
            languages=s.languages,
            requires_gpu=s.requires_gpu,
            min_vram_mb=s.min_vram_mb,
            license=ModelPackLicense(
                name=s.license.name,
                commercial_use=s.license.commercial_use,
                redistribution=s.license.redistribution,
                attribution_required=s.license.attribution_required,
                notes=s.license.notes,
            ),
            download_url=s.download_url,
            notes=s.notes,
        )

    return [convert(s) for s in VAULT_MODEL_SPECS]
