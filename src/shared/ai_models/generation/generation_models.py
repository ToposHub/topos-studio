"""TAEP M11 — 生成媒体数据合同（§8.2 / §10.1 / §11.1 M11 / §11.2 M11）。

provider-neutral 的生成请求数据模型：覆盖视频/音乐/语音/图像四类生成。生成能力必须通过
provider-neutral adapter，而非把某一厂商 SDK 绑进 UI（§8.2）。

设计（遵循 .clinerules / §0 / §1.2 / §8.2 / §11.2 M11）：

- **不可变 + 版本化 + provenance 完整**（§1.2 不变量 #6："来源、成本、版权可追溯"）：
  ``GenerationRequest`` / ``GeneratedAsset`` 都是 frozen dataclass；每个生成 artifact 至少
  记录 provider/model/version、请求时间、输入来源摘要与 hash、seed、参考素材、授权声明、
  内容安全结果、费用、用户/审批、父任务/Skill（§8.2）。
- **用户同意 + 成本上限是硬 gate**（§11.2 M11 step 1 / §2.3）：``GenerationConsent`` 必须
  明确 provider/成本/版权/上传/目标位置；``GenerationBudget.max_cost_units`` 是硬上限，
  超限 → 拒绝（不静默）。
- **取消/失败不污染工程**（§11.2 M11 完成检查 / §0 第三条）：生成候选先入"生成素材箱"
  （``GenerationBin``），不入 timeline；取消/失败的候选可丢弃，不影响原素材。
- **许可未知不能表述为"可商用"**（§1.2 不变量 #6 / §11.2 M11 step 4）：``LicenseStatus``
  分 KNOWN_LICENSED / CLEARED / UNKNOWN / PENDING；UNKNOWN 时 ``is_commercial_ready=False``，
  导出时阻断"商用就绪"声明。
- **内容安全结果**（§8.2 / §15.5）：``ContentSafetyResult`` 记录 provider 返回的安全分类
  （NSFW/暴力/版权等）；不安全 → 不可导入。
- **不记 secret / 绝对路径**（§0 第四条）：``GenerationProvenance`` 只记 hash/摘要，不记原始
  prompt 全文（除非用户开 ``retain_prompts``）/ 不记绝对路径。

与既有结构的关系：
- 镜像 ``ExportRequest``（request→preflight→job→artifact）+ ``MusicAsset``（LicenseStatus +
  is_applicable gating）+ ``AnalysisBudget``（max_cost_units）模式。
- ``GeneratedAsset`` 与 core ``MediaAsset`` 分离：生成候选先进 ``GenerationBin``，用户显式
  import 后才经 ``import_to_media_pool`` 注入点成为 ``MediaAsset``（§8.2 G2）。
- **不**改动 core ``project.py`` 的 ``MediaAsset``（那是接线批次）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping


# ---------------------------------------------------------------------- #
# GenerationModality —— 生成类型（§8.2 四类）
# ---------------------------------------------------------------------- #


class GenerationModality(str, Enum):
    """生成类型（§8.2：视频/音乐/语音/图像）。"""

    VIDEO = "video"       # 视频生成（text-to-video / image-to-video）
    MUSIC = "music"       # 音乐生成
    VOICE = "voice"       # 语音/配音生成（TTS）
    IMAGE = "image"       # 图像生成


# ---------------------------------------------------------------------- #
# LicenseStatus —— 生成资产授权（镜像 music_policy.LicenseStatus + §1.2 #6）
# ---------------------------------------------------------------------- #


class LicenseStatus(str, Enum):
    """生成资产的授权状态（§1.2 不变量 #6："未知权利状态不能表述为可商用"）。

    镜像 ``music_policy.LicenseStatus`` 语义，独立定义（生成与音乐是不同来源）。
    """

    KNOWN_LICENSED = "known_licensed"   # 有明确商用授权（provider 声明 + 用户确认）
    CLEARED = "cleared"                  # 已人工清权
    UNKNOWN = "unknown"                  # 授权未知（默认；不可商用就绪）
    PENDING = "pending"                  # 待清权

    @property
    def commercial_ready(self) -> bool:
        """是否"商用就绪"（§11.2 M11 step 4：许可未知时阻断商用就绪声明）。"""
        return self in (LicenseStatus.KNOWN_LICENSED, LicenseStatus.CLEARED)


# ---------------------------------------------------------------------- #
# ContentSafetyResult —— 内容安全（§8.2 / §15.5）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContentSafetyResult:
    """生成内容的安全分类结果（§8.2："内容安全结果"）。

    provider 返回的安全分类（NSFW/暴力/版权/肖像等）。``passed=False`` → 不可导入媒体池。
    """

    passed: bool = True                # 是否通过安全检查
    categories: tuple[str, ...] = ()   # 命中的风险类别（如 "nsfw"/"violence"/"copyright"）
    provider_safety_version: str = ""  # provider 安全模型版本（provenance）
    detail: str = ""

    @property
    def blocked(self) -> bool:
        """是否被安全检查阻断（不可导入）。"""
        return not self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "categories": list(self.categories),
            "provider_safety_version": self.provider_safety_version,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GenerationQualityResult:
    """技术/时序/品牌 QA 结果；失败候选不可进入 Media Pool。"""

    passed: bool = True
    failed_checks: tuple[str, ...] = ()
    checker_version: str = ""
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return not self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed_checks": list(self.failed_checks),
            "checker_version": self.checker_version,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------- #
# GenerationConsent —— 用户同意（§2.3 / §11.2 M11 step 1）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class GenerationConsent:
    """单次生成的用户同意记录（§2.3："每次确认 provider、成本、版权/上传与目标位置"）。

    红线（§0 第一条："未经用户确认不得……生成付费素材"）：``confirmed=False`` 的请求
    被 ``GenerationService`` 拒绝（不静默放行）。
    """

    confirmed: bool = False            # 用户是否已确认（硬 gate）
    provider_id: str = ""              # 确认的 provider（§2.3："每次确认 provider"）
    cost_acknowledged: bool = False    # 用户是否知悉成本（§2.3："成本"）
    upload_acknowledged: bool = False  # 用户是否知悉上传范围（§2.3："上传"）
    copyright_acknowledged: bool = False  # 用户是否知悉版权风险（§2.3："版权"）
    target_location: str = ""          # 目标位置（§2.3："目标位置"，生成素材箱 / 媒体池）
    confirmed_at: str = ""             # 确认时间（ISO）

    @property
    def is_complete(self) -> bool:
        """同意是否完整（confirmed + 四项 acknowledge 都到位）。"""
        return (
            self.confirmed
            and self.cost_acknowledged
            and self.upload_acknowledged
            and self.copyright_acknowledged
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "provider_id": self.provider_id,
            "cost_acknowledged": self.cost_acknowledged,
            "upload_acknowledged": self.upload_acknowledged,
            "copyright_acknowledged": self.copyright_acknowledged,
            "target_location": self.target_location,
            "confirmed_at": self.confirmed_at,
        }


# ---------------------------------------------------------------------- #
# GenerationBudget —— 成本上限（§10.2 / §11.2 M11 step 1）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class GenerationBudget:
    """生成成本上限（§10.2："生成视频/音乐另有硬上限"）。

    镜像 ``AnalysisBudget.max_cost_units``（货币或 token 单位，由 provider 解释）。
    ``max_cost_units`` 是硬上限：provider 报告的实际费用超限 → 标超支（不自动扣）。
    ``max_candidates`` 防止一次生成过多候选（§8.2 G1："回收多个候选"但有上限）。
    """

    max_cost_units: float | None = None    # 硬成本上限（货币/token，provider 解释）
    max_duration_seconds: float | None = None  # 单次生成最大时长（视频/音乐）
    max_candidates: int = 4                 # 一次请求最多回收几个候选

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cost_units": self.max_cost_units,
            "max_duration_seconds": self.max_duration_seconds,
            "max_candidates": self.max_candidates,
        }


# ---------------------------------------------------------------------- #
# ReferenceBinding / GenerationRecipe —— provider-neutral recipe（M11 B4）
# ---------------------------------------------------------------------- #


class ReferenceKind(str, Enum):
    """参考素材在生成 recipe 中的语义角色。"""

    FIRST_FRAME = "first_frame"
    LAST_FRAME = "last_frame"
    CHARACTER = "character"
    PRODUCT = "product"
    STYLE = "style"
    AUDIO = "audio"


@dataclass(frozen=True)
class ReferenceBinding:
    """把一个已选 MediaAsset 映射到 provider-neutral 参考角色。"""

    kind: ReferenceKind
    asset_id: str
    label: str = ""
    weight: float = 1.0
    frame: int | None = None

    def __post_init__(self) -> None:
        if not self.asset_id:
            raise ValueError("reference asset_id cannot be empty")
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError("reference weight must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "asset_id": self.asset_id,
            "label": self.label,
            "weight": self.weight,
            "frame": self.frame,
        }


@dataclass(frozen=True)
class GenerationRecipe:
    """版本化、厂商无关的生成参数合同。"""

    recipe_id: str
    modality: GenerationModality
    schema_version: str = "1.0"
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration_seconds: float | None = None
    aspect_ratio: str = ""
    style: str = ""
    negative_prompt: str = ""
    seed: int | None = None
    output_format: str = ""
    references: tuple[ReferenceBinding, ...] = ()
    output_constraints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.recipe_id:
            raise ValueError("recipe_id cannot be empty")
        if self.schema_version != "1.0":
            raise ValueError(f"unsupported generation recipe schema: {self.schema_version}")
        if self.width is not None and self.width <= 0:
            raise ValueError("recipe width must be positive")
        if self.height is not None and self.height <= 0:
            raise ValueError("recipe height must be positive")
        if self.fps is not None and self.fps <= 0:
            raise ValueError("recipe fps must be positive")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("recipe duration must be positive")

    @property
    def reference_asset_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(binding.asset_id for binding in self.references))

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "schema_version": self.schema_version,
            "modality": self.modality.value,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_seconds": self.duration_seconds,
            "aspect_ratio": self.aspect_ratio,
            "style": self.style,
            "negative_prompt": self.negative_prompt,
            "seed": self.seed,
            "output_format": self.output_format,
            "references": [ref.to_dict() for ref in self.references],
            "output_constraints": dict(self.output_constraints),
        }

    @classmethod
    def migrate(cls, payload: Mapping[str, Any]) -> "GenerationRecipe":
        """把 0.x recipe 迁移为 1.0；未知字段不传播到 provider。"""

        raw = dict(payload)
        resolution = str(raw.get("resolution", ""))
        width = raw.get("width")
        height = raw.get("height")
        if (width is None or height is None) and "x" in resolution:
            left, right = resolution.lower().split("x", 1)
            width, height = int(left), int(right)
        raw_modality = raw.get("modality", GenerationModality.VIDEO.value)
        modality = raw_modality if isinstance(raw_modality, GenerationModality) else GenerationModality(str(raw_modality))
        references: list[ReferenceBinding] = []
        for item in raw.get("references", ()) or ():
            if not isinstance(item, Mapping):
                continue
            references.append(ReferenceBinding(
                kind=ReferenceKind(str(item.get("kind", ReferenceKind.STYLE.value))),
                asset_id=str(item.get("asset_id", "")),
                label=str(item.get("label", "")),
                weight=float(item.get("weight", 1.0)),
                frame=int(item["frame"]) if item.get("frame") is not None else None,
            ))
        return cls(
            recipe_id=str(raw.get("recipe_id", raw.get("id", "recipe-migrated"))),
            modality=modality,
            schema_version="1.0",
            width=int(width) if width is not None else None,
            height=int(height) if height is not None else None,
            fps=float(raw["fps"]) if raw.get("fps") is not None else None,
            duration_seconds=(
                float(raw["duration_seconds"]) if raw.get("duration_seconds") is not None else None
            ),
            aspect_ratio=str(raw.get("aspect_ratio", "")),
            style=str(raw.get("style", "")),
            negative_prompt=str(raw.get("negative_prompt", "")),
            seed=int(raw["seed"]) if raw.get("seed") is not None else None,
            output_format=str(raw.get("output_format", "")),
            references=tuple(references),
            output_constraints=dict(raw.get("output_constraints", {}) or {}),
        )


# ---------------------------------------------------------------------- #
# GenerationProvenance —— 来源/可复现（§8.2 / §1.2 #6）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class GenerationProvenance:
    """生成来源与可复现信息（§8.2："每个生成 artifact 至少记录……"）。

    红线（§0 第四条）：不记 secret / 绝对路径。``input_prompt_hash`` 是 prompt 的摘要 hash，
    不存全文（除非用户开 ``retain_prompts``，由 provider 层处理）。``reference_asset_ids``
    只记 asset_id，不记路径。
    """

    provider_id: str = ""              # 如 "seedance" / "elevenlabs" / "suno"
    model_id: str = ""                 # provider 的模型 id
    model_version: str = ""            # 模型版本（provenance 关键）
    requested_at: str = ""             # 请求时间（ISO）
    input_prompt_hash: str = ""        # 输入 prompt 的 sha256（不存全文）
    seed: int | None = None            # 可复现 seed（provider 支持时）
    reference_asset_ids: tuple[str, ...] = ()  # 参考素材（只记 id，不记路径）
    parent_skill_id: str = ""          # 触发生成的 Skill（可空）
    approved_by: str = ""              # 批准的用户/会话
    license_evidence_ref: str = ""     # 许可证据引用（ID/摘要，不存合同或 secret）
    license_region: str = ""           # 许可证适用区域（如 global / CN / US）
    license_checked_at: str = ""       # 许可证据核验时间（ISO）
    recipe_id: str = ""
    recipe_schema_version: str = ""
    reference_bindings: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "requested_at": self.requested_at,
            "input_prompt_hash": self.input_prompt_hash,
            "seed": self.seed,
            "reference_asset_ids": list(self.reference_asset_ids),
            "parent_skill_id": self.parent_skill_id,
            "approved_by": self.approved_by,
            "license_evidence_ref": self.license_evidence_ref,
            "license_region": self.license_region,
            "license_checked_at": self.license_checked_at,
            "recipe_id": self.recipe_id,
            "recipe_schema_version": self.recipe_schema_version,
            "reference_bindings": [dict(item) for item in self.reference_bindings],
        }


# ---------------------------------------------------------------------- #
# GenerationRequest —— provider-neutral 请求（§10.1 / §11.2 M11 step 1）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class GenerationRequest:
    """provider-neutral 生成请求（§8.2 / §10.1 / §11.2 M11 step 1）。

    frozen + 版本化。``consent`` 未确认 → ``GenerationService`` 拒绝（§0 红线第一条）。
    ``budget.max_cost_units`` 是硬上限。生成结果先入"生成素材箱"，不入 timeline。
    """

    request_id: str
    modality: GenerationModality       # video / music / voice / image
    prompt: str                        # 生成提示词（provider-neutral）
    consent: GenerationConsent         # 用户同意（硬 gate）
    budget: GenerationBudget = field(default_factory=lambda: GenerationBudget())
    negative_prompt: str = ""          # 反向提示（可选）
    duration_seconds: float | None = None  # 目标时长（视频/音乐）
    aspect_ratio: str = ""             # "16:9" / "9:16" / "1:1"
    style: str = ""                    # 风格提示（可选）
    reference_asset_ids: tuple[str, ...] = ()  # 参考素材（image-to-video 等）
    requested_by: str = ""             # 触发者（skill_id / user / cli）
    recipe: GenerationRecipe | None = None
    reference_bindings: tuple[ReferenceBinding, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.prompt:
            raise ValueError("prompt cannot be empty")
        if self.recipe is not None and self.recipe.modality != self.modality:
            raise ValueError("recipe modality must match request modality")

    @property
    def prompt_hash(self) -> str:
        """prompt 的 sha256 摘要前 16 位（镜像 ``GenerationProvenance.input_prompt_hash``）。

        红线（§0 第四条 / M12）：默认不采集 prompt 原文，序列化时输出此 hash 替代。
        """
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:16]

    @property
    def effective_reference_bindings(self) -> tuple[ReferenceBinding, ...]:
        return self.reference_bindings or (self.recipe.references if self.recipe else ())

    @property
    def effective_reference_asset_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            (*self.reference_asset_ids,
             *(binding.asset_id for binding in self.effective_reference_bindings)),
        ))

    def to_dict(self, *, retain_prompts: bool = False) -> dict[str, Any]:
        """序列化。``retain_prompts=False``（默认）时 prompt/negative_prompt 输出 hash 摘要
        而非原文（红线 §0 第四条"默认不采集 prompt"；对齐 ``provider_profiles.DataPolicy.
        retain_prompts`` 开关）。``retain_prompts=True`` 时才输出原文（仅在受信任、
        显式开启持久化的场景）。"""
        return {
            "request_id": self.request_id,
            "modality": self.modality.value,
            "prompt": self.prompt if retain_prompts else self.prompt_hash,
            "prompt_redacted": not retain_prompts,
            "consent": self.consent.to_dict(),
            "budget": self.budget.to_dict(),
            "negative_prompt": (
                self.negative_prompt if retain_prompts
                else (hashlib.sha256(self.negative_prompt.encode("utf-8")).hexdigest()[:16]
                      if self.negative_prompt else "")
            ),
            "duration_seconds": self.duration_seconds,
            "aspect_ratio": self.aspect_ratio,
            "style": self.style,
            "reference_asset_ids": list(self.effective_reference_asset_ids),
            "reference_bindings": [
                binding.to_dict() for binding in self.effective_reference_bindings
            ],
            "recipe": self.recipe.to_dict() if self.recipe else None,
            "requested_by": self.requested_by,
        }


# ---------------------------------------------------------------------- #
# GeneratedAsset —— 单个生成候选（§10.1 / §8.2）
# ---------------------------------------------------------------------- #


class GeneratedAssetState(str, Enum):
    """生成候选在素材箱里的状态机（§8.2 G1-G2 / §11.2 M11 step 2）。

    generated → previewed → imported（成 MediaAsset）
                       ↘ discarded
    """

    GENERATED = "generated"    # 刚生成，待预览
    PREVIEWED = "previewed"    # 用户已预览
    IMPORTED = "imported"      # 已导入媒体池（成 MediaAsset）
    DISCARDED = "discarded"    # 已丢弃（不污染工程）

    @property
    def terminal(self) -> bool:
        return self in (GeneratedAssetState.IMPORTED, GeneratedAssetState.DISCARDED)


@dataclass(frozen=True)
class GeneratedAsset:
    """单个生成候选（§8.2 / §10.1）。

    生成素材箱的元素。``state`` 跟踪 generated→imported 的生命周期。``license`` 未知时
    ``is_commercial_ready=False``（§1.2 #6）。``safety.blocked=True`` 时不可导入（§8.2）。
    生成物本体（文件/URL）由 provider 层管理；此处只记元数据 + provenance（可重建/可追溯）。
    """

    asset_id: str                      # 生成资产 id（如 "gen-req1-cand1"）
    request_id: str                    # 关联的 GenerationRequest
    modality: GenerationModality
    provenance: GenerationProvenance
    license: LicenseStatus = LicenseStatus.UNKNOWN
    safety: ContentSafetyResult = field(default_factory=lambda: ContentSafetyResult())
    qa: GenerationQualityResult = field(default_factory=lambda: GenerationQualityResult())
    cost_units: float = 0.0            # 实际费用（provider 报告）
    preview_handle: str = ""           # 预览句柄（URL/path 由 provider 层解释）
    expected_sha256: str = ""           # provider 返回的输出校验和（可选，物化时硬校验）
    duration_seconds: float | None = None
    state: GeneratedAssetState = GeneratedAssetState.GENERATED
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")

    # ---------------- gating ---------------- #

    @property
    def is_commercial_ready(self) -> bool:
        """是否商用就绪（§11.2 M11 step 4：许可未知 → False）。"""
        return self.license.commercial_ready and not self.safety.blocked

    @property
    def importable(self) -> bool:
        """是否可导入媒体池（§8.2 G2：安全检查通过 + 未 imported/discarded）。"""
        return (
            not self.safety.blocked
            and not self.qa.blocked
            and self.state in (GeneratedAssetState.GENERATED, GeneratedAssetState.PREVIEWED)
        )

    # ---------------- 状态迁移（返回新 frozen 实例）---------------- #

    def with_state(self, state: GeneratedAssetState) -> "GeneratedAsset":
        return replace(self, state=state)

    def with_preview_handle(self, preview_handle: str) -> "GeneratedAsset":
        """更新为可复用的本地缓存句柄，不改变生成状态或 provenance。"""
        return replace(self, preview_handle=preview_handle)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "request_id": self.request_id,
            "modality": self.modality.value,
            "provenance": self.provenance.to_dict(),
            "license": self.license.value,
            "safety": self.safety.to_dict(),
            "qa": self.qa.to_dict(),
            "cost_units": self.cost_units,
            "preview_handle": self.preview_handle,
            "expected_sha256": self.expected_sha256,
            "duration_seconds": self.duration_seconds,
            "state": self.state.value,
            "created_at": self.created_at,
            "is_commercial_ready": self.is_commercial_ready,
            "importable": self.importable,
        }


# ---------------------------------------------------------------------- #
# GenerationResult —— provider 一次 generate 的产物（多候选）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class GenerationResult:
    """``GenerationProvider.generate`` 的产物（§8.2 G1："回收多个候选"）。

    ``candidates`` 是一次生成的多个候选（受 ``budget.max_candidates`` 约束）。
    ``actual_cost_units`` 是本次生成的实际费用（供成本累计/审计）。
    """

    request_id: str
    candidates: tuple[GeneratedAsset, ...] = ()
    actual_cost_units: float = 0.0
    error_code: str = ""               # 失败时填（provider 错误码）
    error_message: str = ""

    @property
    def succeeded(self) -> bool:
        return not self.error_code and len(self.candidates) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "candidates": [c.to_dict() for c in self.candidates],
            "actual_cost_units": self.actual_cost_units,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "succeeded": self.succeeded,
        }


__all__ = [
    "GenerationModality",
    "LicenseStatus",
    "ContentSafetyResult",
    "GenerationQualityResult",
    "GenerationConsent",
    "GenerationBudget",
    "ReferenceKind",
    "ReferenceBinding",
    "GenerationRecipe",
    "GenerationProvenance",
    "GenerationRequest",
    "GeneratedAssetState",
    "GeneratedAsset",
    "GenerationResult",
]
