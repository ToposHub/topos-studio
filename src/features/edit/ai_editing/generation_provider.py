"""TAEP M11 — Generation Provider 抽象（§8.2 / §3.1 / §11.1 M11）。

provider-neutral adapter：生成能力必须通过 adapter，而非把某一厂商 SDK 绑进 UI（§8.2：
"生成能力必须通过 provider-neutral adapter"）。

设计（遵循 .clinerules / §0 / §1.2 / §8.2）：

- **Protocol + Registry**（§3.1："provider adapter 可换"）：``GenerationProvider`` Protocol
  + ``ProviderRegistry``（按 modality / capability 查找）。真实 provider（Seedance 视频 /
  ElevenLabs 语音 / Suno 音乐 / 图像）实现 Protocol 并注册；切换 provider = 换注册项。
- **确定性 stub**（v1 决策）：``DeterministicGenerationProvider`` 按 prompt hash 产固定
  占位候选，不联网（真实 API 接线留控制器批次，与 M8 OCR/WhisperX + M10 LLMClient 同策略）。
  便于测试 + 符合"可解释/可验证/可撤销"。
- **不绑 SDK**：Protocol 只定义 ``generate(request, *, progress) -> GenerationResult``，
  不 import 任何厂商库；真实 provider 在控制器层注入。
- **凭证经 CredentialStore**（§0 第四条）：API key 经 ``credential_ref``，不进 provider 实例。
- **provenance 必填**：provider 返回的 ``GeneratedAsset`` 必须带完整 ``GenerationProvenance``
  （provider_id/model/version 等），便于追溯（§1.2 #6）。
- **成本报告**：provider 在 ``GenerationResult.actual_cost_units`` 报告实际费用，供成本累计。

红线：本模块**不**：自己写 timeline / 调 approval / 持久化生成物（落地由 GenerationService
+ GenerationBin + 宿主闭环负责）。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

from src.features.edit.ai_editing.generation_models import (
    ContentSafetyResult,
    GeneratedAsset,
    GenerationModality,
    GenerationProvenance,
    GenerationRequest,
    GenerationResult,
    LicenseStatus,
)


# ---------------------------------------------------------------------- #
# GenerationProvider Protocol
# ---------------------------------------------------------------------- #


class GenerationProvider(Protocol):
    """provider-neutral 生成协议（§8.2 / §3.1）。

    每个具体 provider（视频/音乐/语音/图像）实现本协议并注册到 ``ProviderRegistry``。
    ``generate`` 是同步调用（生产由控制器放到 Job worker 线程，与 analysis provider 同策略）。
    """

    provider_id: str  # 如 "seedance" / "elevenlabs" / "suno" / "deterministic-stub"

    @property
    def supported_modalities(self) -> tuple[GenerationModality, ...]:
        """该 provider 支持的生成类型。"""
        ...

    def generate(
        self,
        request: GenerationRequest,
        *,
        progress: "Callable[[float, str], None] | None" = None,
    ) -> GenerationResult:
        """执行生成，返回候选列表（§8.2 G1）。

        Args:
            request: provider-neutral 生成请求。
            progress: 可选进度回调 ``(fraction 0..1, message)``。

        Returns:
            ``GenerationResult``（含多个 ``GeneratedAsset`` 候选 + 实际费用）。
            失败时 ``error_code`` 非空、``candidates`` 为空。
        """
        ...


# ---------------------------------------------------------------------- #
# ProviderRegistry —— 按 modality 查找 provider
# ---------------------------------------------------------------------- #


class ProviderRegistry:
    """生成 provider 注册表（§3.1："provider adapter 可换"）。

    按 ``modality`` 查找已注册 provider。多 provider 支持同一 modality 时，
    ``preferred`` 标记默认 provider（切换 = 改 preferred）。
    """

    def __init__(self) -> None:
        self._providers: dict[str, GenerationProvider] = {}
        self._preferred: dict[GenerationModality, str] = {}
        # AUD-13608：注册（如运行时换 provider）与 preferred_for/list_providers
        # 并发时可触发 "dictionary changed size during iteration"。与
        # goal_round_driver / goal_supervisor 同风格，一把 RLock 保护读写。
        self._lock = threading.RLock()

    def register(
        self, provider: GenerationProvider, *,
        modalities: tuple[GenerationModality, ...] | None = None,
        preferred: bool = False,
    ) -> None:
        """注册 provider。``modalities=None`` 时用 provider.supported_modalities。"""
        pid = provider.provider_id
        if not pid:
            raise ValueError("provider_id cannot be empty")
        mods = modalities if modalities is not None else tuple(provider.supported_modalities)
        if not mods:
            raise ValueError(f"provider {pid} declares no modalities")
        with self._lock:
            self._providers[pid] = provider
            if preferred:
                for m in mods:
                    self._preferred[m] = pid

    def get(self, provider_id: str) -> GenerationProvider | None:
        with self._lock:
            return self._providers.get(provider_id)

    def require(self, provider_id: str) -> GenerationProvider:
        p = self.get(provider_id)
        if p is None:
            raise KeyError(f"generation provider not registered: {provider_id}")
        return p

    def preferred_for(self, modality: GenerationModality) -> GenerationProvider | None:
        """返回该 modality 的首选 provider（None = 无注册）。"""
        with self._lock:
            pid = self._preferred.get(modality)
            if pid and pid in self._providers:
                return self._providers[pid]
            # 退化：取第一个支持该 modality 的（锁内快照，迭代不受并发注册影响）
            providers = list(self._providers.values())
        for p in providers:
            if modality in tuple(p.supported_modalities):
                return p
        return None

    def set_preferred(self, modality: GenerationModality, provider_id: str) -> bool:
        """设置某 modality 的首选 provider（已注册才有效）。"""
        with self._lock:
            if provider_id not in self._providers:
                return False
            p = self._providers[provider_id]
            if modality not in tuple(p.supported_modalities):
                return False
            self._preferred[modality] = provider_id
            return True

    def list_providers(self) -> list[GenerationProvider]:
        with self._lock:
            return list(self._providers.values())

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._providers)


# ---------------------------------------------------------------------- #
# DeterministicGenerationProvider —— 确定性 stub（v1，不联网）
# ---------------------------------------------------------------------- #


@dataclass
class DeterministicGenerationProvider:
    """确定性生成 provider stub（v1 / §8.2 G0-G1 测试用）。

    不联网：按 ``prompt`` 的 sha256 hash 产**固定的、确定性的**占位候选。
    符合 M7 planner 的"可解释/可验证/可撤销"模式。真实 provider（Seedance / ElevenLabs /
    Suno / 图像）接线留控制器批次。

    确定性原则：相同 prompt + modality → 相同候选 asset_id（便于 eval）。候选数受
    ``request.budget.max_candidates`` 约束。
    """

    provider_id: str = "deterministic-stub"
    # P0: this provider is intentionally never a production capability.  The
    # flag is consumed by capability_status and UI adapters.
    is_demo: bool = True
    _modalities: tuple[GenerationModality, ...] = field(
        default_factory=lambda: (
            GenerationModality.VIDEO, GenerationModality.MUSIC,
            GenerationModality.VOICE, GenerationModality.IMAGE,
        ),
    )

    @property
    def supported_modalities(self) -> tuple[GenerationModality, ...]:
        return self._modalities

    @property
    def capability_tier(self) -> str:
        """Lifecycle tier exposed to diagnostics (never silently production)."""
        return "demo"

    def generate(
        self,
        request: GenerationRequest,
        *,
        progress: "Callable[[float, str], None] | None" = None,
    ) -> GenerationResult:
        """产固定占位候选（确定性，不联网）。"""
        if progress is not None:
            progress(0.1, "stub: hashing prompt")
        if request.modality not in self._modalities:
            return GenerationResult(
                request_id=request.request_id,
                error_code="UNSUPPORTED_MODALITY",
                error_message=f"stub does not support {request.modality.value}",
            )

        n_candidates = max(1, min(
            request.budget.max_candidates,
            3,  # stub 固定最多 3 个候选
        ))
        prompt_hash = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
        candidates: list[GeneratedAsset] = []
        for i in range(n_candidates):
            if progress is not None:
                progress(0.3 + 0.2 * i / max(n_candidates, 1), f"stub: candidate {i + 1}")
            asset_id = f"gen-{request.request_id}-c{i + 1}"
            provenance = GenerationProvenance(
                provider_id=self.provider_id,
                model_id=f"stub-{request.modality.value}",
                model_version="stub-v1",
                requested_at=request.consent.confirmed_at or "",
                input_prompt_hash=prompt_hash[:16],  # 摘要（不存全文）
                seed=1000 + i,
                reference_asset_ids=request.effective_reference_asset_ids,
                parent_skill_id=request.requested_by,
                recipe_id=request.recipe.recipe_id if request.recipe else "",
                recipe_schema_version=request.recipe.schema_version if request.recipe else "",
                reference_bindings=tuple(
                    binding.to_dict() for binding in request.effective_reference_bindings
                ),
            )
            candidates.append(GeneratedAsset(
                asset_id=asset_id,
                request_id=request.request_id,
                modality=request.modality,
                provenance=provenance,
                license=LicenseStatus.UNKNOWN,  # stub 默认未知（需人工清权）
                safety=ContentSafetyResult(passed=True, provider_safety_version="stub-safety-v1"),
                cost_units=0.0,  # stub 不计费
                preview_handle=f"stub://preview/{asset_id}",
                duration_seconds=request.duration_seconds,
                created_at="",
            ))
        if progress is not None:
            progress(1.0, "stub: done")
        return GenerationResult(
            request_id=request.request_id,
            candidates=tuple(candidates),
            actual_cost_units=0.0,
        )


__all__ = [
    "GenerationProvider",
    "ProviderRegistry",
    "DeterministicGenerationProvider",
]
