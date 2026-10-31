"""能力契约（Protocol）+ 注册表 + 配置驱动的 provider 选择。

设计对齐项目主 app：
- Protocol 用 ``typing.Protocol``（duck typing，不强制继承）
- 每个 provider 有稳定 ``provider_id``（如 ``qwen3_vl_8b_awq``）
- 多个实现归到同一 ``Capability``（如多个画面理解模型都属 VISUAL_CAPTION）
- stub 实现兜底（无 GPU/无依赖时仍能跑，用于测试/CI）

切换三档：
    环境变量 TOPOS_VAULT_VISUAL=qwen3_vl_8b_awq
    配置文件 [providers] visual = qwen3_vl_8b_awq
    代码 get_provider(Capability.VISUAL_CAPTION)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------- #
# 能力枚举（6 个）
# ---------------------------------------------------------------------- #

class Capability(str, Enum):
    """vault 的 6 个 AI 能力。每个能力可有多个 provider 实现。"""
    TRANSCRIPT_EMBED = "transcript_embed"   # 台词/文本 → 向量
    VISUAL_EMBED = "visual_embed"            # 画面 → 向量（视频+图片共用）
    VISUAL_CAPTION = "visual_caption"        # 画面 → caption/tags（视频+图片共用）
    AUDIO_UNDERSTAND = "audio_understand"    # 音乐 → 情绪/描述
    FACE_RECOGNIZE = "face_recognize"        # 画面 → 人脸向量/属性
    ASR = "asr"                              # 视频 → 台词（复用主项目）


# ---------------------------------------------------------------------- #
# 能力契约
# ---------------------------------------------------------------------- #

@runtime_checkable
class VaultProvider(Protocol):
    """所有 provider 的共同接口。"""
    provider_id: str
    capability: Capability

    def is_available(self) -> bool:
        """本 provider 是否可用（模型已装/依赖齐全/GPU 满足）。"""
        ...


# ---------------------------------------------------------------------- #
# Provider 描述（注册时的元数据）
# ---------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProviderDescriptor:
    """provider 的注册描述。"""
    provider_id: str                 # 稳定 id，如 "qwen3_vl_8b_awq"
    capability: Capability
    display_name: str                # 展示名，如 "Qwen3-VL 8B (AWQ 4bit)"
    requires_gpu: bool               # 是否强制 GPU
    min_vram_mb: int                 # 最小显存（requires_gpu=False 时为 0）
    model_pack_id: str | None = None # 对应 ModelPackSpec.pack_id（模型共享用）
    notes: str = ""                  # 备注（如"质量优先"/"轻量备选"）


# ---------------------------------------------------------------------- #
# 注册表
# ---------------------------------------------------------------------- #

class ProviderRegistry:
    """provider 注册表（按 capability 分组）。

    每个能力可注册多个 provider；选哪个由 ProviderConfig 决定（env/config/默认）。
    """

    def __init__(self):
        self._providers: dict[str, VaultProvider] = {}          # id → 实例
        self._descriptors: dict[str, ProviderDescriptor] = {}   # id → 描述
        self._defaults: dict[Capability, str] = {}              # capability → 默认 id

    def register(
        self,
        provider: VaultProvider,
        descriptor: ProviderDescriptor,
        is_default: bool = False,
    ) -> None:
        """注册一个 provider。

        Args:
            provider: provider 实例（实现某 Protocol）。
            descriptor: 元数据描述。
            is_default: 是否设为该 capability 的默认（同 capability 后注册的覆盖先注册的）。
        """
        pid = descriptor.provider_id
        if pid in self._providers:
            raise ValueError(f"provider_id {pid!r} 已注册")
        if descriptor.capability != provider.capability:
            raise ValueError(
                f"descriptor.capability ({descriptor.capability}) 与 "
                f"provider.capability ({provider.capability}) 不一致"
            )
        self._providers[pid] = provider
        self._descriptors[pid] = descriptor
        if is_default:
            self._defaults[descriptor.capability] = pid

    def get(self, provider_id: str) -> VaultProvider | None:
        """按 id 查 provider 实例。"""
        return self._providers.get(provider_id)

    def list_by_capability(self, capability: Capability) -> list[ProviderDescriptor]:
        """列出某能力的所有 provider 描述。"""
        return [
            d for d in self._descriptors.values() if d.capability == capability
        ]

    def get_default_id(self, capability: Capability) -> str | None:
        """某能力的默认 provider id。"""
        return self._defaults.get(capability)


# 全局单例（与项目 PanelRegistry/ActionRegistry 模式一致）
_REGISTRY = ProviderRegistry()


def get_registry() -> ProviderRegistry:
    """获取全局 provider 注册表。"""
    return _REGISTRY


# ---------------------------------------------------------------------- #
# 配置驱动的 provider 选择
# ---------------------------------------------------------------------- #

# 能力 → 环境变量名 映射（对齐项目 TOPOS_*_REAL 的命名约定）
_ENV_VARS: dict[Capability, str] = {
    Capability.TRANSCRIPT_EMBED: "TOPOS_VAULT_TRANSCRIPT_EMBED",
    Capability.VISUAL_EMBED: "TOPOS_VAULT_VISUAL_EMBED",
    Capability.VISUAL_CAPTION: "TOPOS_VAULT_VISUAL_CAPTION",
    Capability.AUDIO_UNDERSTAND: "TOPOS_VAULT_AUDIO_UNDERSTAND",
    Capability.FACE_RECOGNIZE: "TOPOS_VAULT_FACE_RECOGNIZE",
    Capability.ASR: "TOPOS_VAULT_ASR",
}


@dataclass
class ProviderConfig:
    """provider 选择配置。

    优先级（高到低）：
        1. 环境变量 TOPOS_VAULT_<CAP>
        2. 本配置对象的显式设置
        3. 注册时的默认 provider
        4. 第一个可用的 provider（含 stub 兜底）
    """
    overrides: dict[Capability, str] | None = None

    def select(
        self, capability: Capability, registry: ProviderRegistry | None = None,
    ) -> VaultProvider:
        """选择一个 provider（按优先级）。"""
        reg = registry or _REGISTRY

        # 1. 环境变量
        env_var = _ENV_VARS.get(capability)
        if env_var:
            env_val = os.environ.get(env_var)
            if env_val:
                p = reg.get(env_val)
                if p is not None:
                    return p
                raise ValueError(
                    f"环境变量 {env_var}={env_val!r} 指定的 provider 未注册"
                )

        # 2. 显式 override
        if self.overrides and capability in self.overrides:
            pid = self.overrides[capability]
            p = reg.get(pid)
            if p is not None:
                return p
            raise ValueError(f"override 指定的 provider {pid!r} 未注册")

        # 3. 默认 provider（但必须 is_available，否则 fallback）
        default_id = reg.get_default_id(capability)
        if default_id:
            p = reg.get(default_id)
            if p is not None and p.is_available():
                return p
            # 默认 provider 不可用（如真实模型未装）→ 继续 fallback

        # 4. 第一个可用的（通常 fallback 到 stub）
        for desc in reg.list_by_capability(capability):
            p = reg.get(desc.provider_id)
            if p is not None and p.is_available():
                return p

        raise RuntimeError(f"能力 {capability.value} 没有可用的 provider")


def get_provider(capability: Capability) -> VaultProvider:
    """便捷函数：用默认配置选 provider。"""
    return ProviderConfig().select(capability)


def list_providers(capability: Capability | None = None) -> list[ProviderDescriptor]:
    """列出 provider（可按能力过滤）。"""
    if capability is None:
        return list(_REGISTRY._descriptors.values())
    return _REGISTRY.list_by_capability(capability)
