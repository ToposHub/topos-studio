"""TAEP AI Provider Profile 与凭据（Phase 9 UI-1）。

模型设置不散在聊天面板、工程文件或脚本中（TAEP §11.4）。

- ``AiProviderProfile`` 只存模型配置；API key/令牌通过 ``CredentialStore``
  保存，应用默认使用用户级 ``~/.topos/ai_profiles.json``，工程与 audit 只保存
  profile ID 或 credential_ref。
- ``CredentialStore`` 是抽象接口；测试用 fake store，生产默认使用用户级文件。
- 界面永不回显 token。
- profile 配置完成后必须运行“测试连接 + 拉取 capability”任务。
- 仅当 profile 声明了对应 modality 且用户同意数据策略时，才允许上传分析数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.shared.ai_models.generation.user_ai_config import (
    load_user_ai_config,
    save_user_ai_config,
)


@dataclass(frozen=True)
class DataPolicy:
    """数据策略（TAEP §11.4）。"""

    execution: str = "local"  # local / cloud
    allow_media_upload: bool = False
    retain_prompts: bool = False  # 是否保留完整 prompt/回包


@dataclass(frozen=True)
class ProviderLimits:
    """profile 限制。"""

    max_context_tokens: int = 200_000
    max_parallel_jobs: int = 1
    monthly_cost_alert: float | None = None
    request_timeout_seconds: float = 120.0
    max_retries: int = 2
    max_cost_units_per_request: float | None = None


@dataclass(frozen=True)
class AiProviderProfile:
    """单个模型 provider 的非 secret 配置（TAEP §11.4 ``AiProviderProfile``）。

    secret（API key）**不**存这里，只存 ``credential_ref``（如
    ``topos:local-qwen-video``）。
    """

    profile_id: str
    display_name: str
    provider_kind: str = "openai"  # openai / ollama / anthropic（兼容旧值 local_http/openai_compat）
    endpoint: str = ""
    model_id: str = ""
    credential_ref: str = ""  # 指向 credential store，不含 secret
    modalities: tuple[str, ...] = ("text", "tool_call")
    data_policy: DataPolicy = field(default_factory=DataPolicy)
    limits: ProviderLimits = field(default_factory=ProviderLimits)
    # These fields are appended to preserve the old positional constructor
    # order used by a few integrations.
    source_kind: str = "custom"  # local / ollama / preset / custom
    preset_id: str = ""            # non-secret catalog preset ID
    capabilities: tuple[str, ...] = ()  # language / vision / asr / tts / embedding

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("profile_id cannot be empty")
        if not self.display_name:
            raise ValueError("display_name cannot be empty")

    @property
    def allows_media_upload(self) -> bool:
        """是否允许上传分析数据（需 modality + 数据策略双满足）。"""
        return self.data_policy.allow_media_upload and "media_upload" in self.modalities

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "provider_kind": self.provider_kind,
            "source_kind": self.source_kind,
            "preset_id": self.preset_id,
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "credential_ref": self.credential_ref,  # 只是引用，不含 secret
            "modalities": list(self.modalities),
            "capabilities": list(self.capabilities),
            "data_policy": {
                "execution": self.data_policy.execution,
                "allow_media_upload": self.data_policy.allow_media_upload,
                "retain_prompts": self.data_policy.retain_prompts,
            },
            "limits": {
                "max_context_tokens": self.limits.max_context_tokens,
                "max_parallel_jobs": self.limits.max_parallel_jobs,
                "monthly_cost_alert": self.limits.monthly_cost_alert,
                "request_timeout_seconds": self.limits.request_timeout_seconds,
                "max_retries": self.limits.max_retries,
                "max_cost_units_per_request": self.limits.max_cost_units_per_request,
            },
        }
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AiProviderProfile":
        # AUD-31621: 手编 JSON 中 data_policy/limits 可能是非映射值（如
        # ``"local"``/``42``）。降级为默认值，而不是让 ``dp.get`` 抛
        # AttributeError 逃出"单条损坏跳过"策略、拖垮整批 profile 加载。
        dp = data.get("data_policy", {})
        if not isinstance(dp, dict):
            dp = {}
        lm = data.get("limits", {})
        if not isinstance(lm, dict):
            lm = {}
        provider_kind = str(data.get("provider_kind", "openai"))
        modalities = tuple(data.get("modalities", ("text", "tool_call")))
        raw_capabilities = data.get("capabilities")
        if isinstance(raw_capabilities, (list, tuple)):
            capabilities = tuple(str(item) for item in raw_capabilities)
        else:
            # Older profile files did not have capability tags.  Inference
            # stays inside the shared layer (AUD-31620) — a runtime import
            # of the features catalog here broke profile loading entirely
            # whenever the edit side was packaged away.
            capabilities = _infer_capabilities(provider_kind, modalities)
        return AiProviderProfile(
            profile_id=str(data["profile_id"]),
            display_name=str(data["display_name"]),
            provider_kind=provider_kind,
            source_kind=str(data.get("source_kind", "custom")),
            preset_id=str(data.get("preset_id", "")),
            endpoint=str(data.get("endpoint", "")),
            model_id=str(data.get("model_id", "")),
            credential_ref=str(data.get("credential_ref", "")),
            modalities=modalities,
            capabilities=capabilities,
            data_policy=DataPolicy(
                execution=str(dp.get("execution", "local")),
                allow_media_upload=bool(dp.get("allow_media_upload", False)),
                retain_prompts=bool(dp.get("retain_prompts", False)),
            ),
            limits=ProviderLimits(
                max_context_tokens=int(lm.get("max_context_tokens", 200_000)),
                max_parallel_jobs=int(lm.get("max_parallel_jobs", 1)),
                monthly_cost_alert=lm.get("monthly_cost_alert"),
                request_timeout_seconds=float(lm.get("request_timeout_seconds", 120.0)),
                max_retries=int(lm.get("max_retries", 2)),
                max_cost_units_per_request=lm.get("max_cost_units_per_request"),
            ),
        )


def _infer_capabilities(
    provider_kind: str, modalities: tuple[str, ...]
) -> tuple[str, ...]:
    """为旧版应用写入的 profile 推断 capability 标签（AUD-31620）。

    纯数据推导，供 shared 生成层自足使用——shared 层不得在运行时反向
    import ``features.edit.ai_editing``（模块边界：任何模块可不经 edit 使
    用 generation；旧的反向 import 一旦 edit 侧目录被裁剪/重构，旧格式
    profile 反序列化抛 ImportError，且不被加载降级捕获）。逻辑与
    ``features.edit.ai_editing.ai_provider_catalog.infer_capabilities`` 保持
    一致，由 tests/shared/test_fix_wd15g80_aud.py 钉住两者等价，防漂移。
    """
    values = {str(item) for item in modalities}
    kind = (provider_kind or "").lower()
    inferred: list[str] = []
    if values.intersection({"text", "tool_call"}) or kind in {
        "openai", "openai_responses", "volcengine_responses", "anthropic",
        "claude", "deepseek", "ollama", "local_http", "openai_compat",
    }:
        inferred.append("language")
    if values.intersection({"image", "video"}):
        inferred.append("vision")
    if "video" in values:
        inferred.append("video_understanding")
    if "audio" in values:
        inferred.append("audio_understanding")
    if values.intersection({"file", "media_upload"}):
        inferred.extend(("document_understanding", "file_input"))
    if "voice" in values or kind in {"volcengine_plan_tts", "local_qwen_tts"}:
        inferred.append("tts")
    if "asr" in values or kind == "local_qwen_asr":
        inferred.append("asr")
    if "embedding" in values or kind in {"embedding", "openai_embedding"}:
        inferred.append("embedding")
    return tuple(dict.fromkeys(inferred))


class CredentialStore(Protocol):
    """凭据存储抽象（TAEP §11.4）。

    测试用 fake store；生产使用用户级凭据文件。界面永不回显 token。
    """

    def get_secret(self, credential_ref: str) -> str | None: ...

    def set_secret(self, credential_ref: str, secret: str) -> None: ...

    def delete_secret(self, credential_ref: str) -> None: ...


class FakeCredentialStore:
    """测试用内存 credential store（TAEP §11.4：测试用 fake store）。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get_secret(self, credential_ref: str) -> str | None:
        return self._store.get(credential_ref)

    def set_secret(self, credential_ref: str, secret: str) -> None:
        self._store[credential_ref] = secret

    def delete_secret(self, credential_ref: str) -> None:
        self._store.pop(credential_ref, None)


class ProfileRegistry:
    """profile 注册表（管理多个 AiProviderProfile）。

    profile 配置存储在用户设置（不写入 .topos 工程文件）；当前活动 profile
    只影响后续任务，不修改项目（TAEP §11.4）。
    """

    def __init__(self) -> None:
        self._profiles: dict[str, AiProviderProfile] = {}
        self._active_profile_id: str = ""
        self._active_by_capability: dict[str, str] = {}

    def add(self, profile: AiProviderProfile) -> None:
        self._profiles[profile.profile_id] = profile

    def remove(self, profile_id: str) -> None:
        self._profiles.pop(profile_id, None)
        if self._active_profile_id == profile_id:
            self._active_profile_id = ""
        self._active_by_capability = {
            capability: pid
            for capability, pid in self._active_by_capability.items()
            if pid != profile_id
        }

    def get(self, profile_id: str) -> AiProviderProfile | None:
        return self._profiles.get(profile_id)

    def list(self) -> list[AiProviderProfile]:
        return list(self._profiles.values())

    @property
    def active_profile(self) -> AiProviderProfile | None:
        if not self._active_profile_id:
            return None
        return self._profiles.get(self._active_profile_id)

    def set_active(self, profile_id: str) -> None:
        if profile_id and profile_id not in self._profiles:
            raise KeyError(f"profile {profile_id!r} not registered")
        self._active_profile_id = profile_id
        profile = self._profiles.get(profile_id)
        if profile is not None:
            capabilities = profile.capabilities or _infer_capabilities(
                profile.provider_kind, profile.modalities,
            )
            for capability in capabilities:
                self._active_by_capability[capability] = profile_id

    def set_active_for_capability(self, capability: str, profile_id: str) -> None:
        """Set the active profile for one routed capability."""
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise KeyError(f"profile {profile_id!r} not registered")
        capabilities = profile.capabilities or _infer_capabilities(
            profile.provider_kind, profile.modalities,
        )
        if capability not in capabilities:
            raise ValueError(f"profile {profile_id!r} does not provide {capability!r}")
        self._active_by_capability[capability] = profile_id

    def active_profile_for_capability(self, capability: str) -> AiProviderProfile | None:
        profile_id = self._active_by_capability.get(capability, "")
        return self._profiles.get(profile_id) if profile_id else None

    def is_active(self, profile_id: str) -> bool:
        """Whether a profile is active globally or for at least one capability."""
        return profile_id == self._active_profile_id or profile_id in self._active_by_capability.values()

    def to_list(self) -> list[dict[str, Any]]:
        """序列化为 list[dict]（用户设置持久化；不含 secret）。"""
        return [p.to_dict() for p in self._profiles.values()]

    # ---------------- 配置持久化（M1.4）---------------- #

    #: 配置 key（稳定；写入用户设置，不写入 .topos 工程）。
    CONFIG_KEY_PROFILES = "ai_editing.profiles"
    CONFIG_KEY_ACTIVE = "ai_editing.active_profile_id"
    CONFIG_KEY_ACTIVE_BY_CAPABILITY = "ai_editing.active_profile_ids"

    def save_to_config(self, config: Any) -> None:
        """把 profiles + active id 写入用户设置（``ConfigManager`` 风格）。

        只写**非 secret** 配置（profile 的 ``credential_ref`` 是引用，不是 secret 本身）。
        API key 由生产 ``CredentialStore`` 单独管理；用户级文件会同时保存 profile
        与 credentials，旧 ConfigManager 仍只保存非 secret 配置。
        """
        config.set(self.CONFIG_KEY_PROFILES, self.to_list(), auto_save=False)
        config.set(self.CONFIG_KEY_ACTIVE, self._active_profile_id, auto_save=True)
        config.set(self.CONFIG_KEY_ACTIVE_BY_CAPABILITY, dict(self._active_by_capability), auto_save=True)

    def load_from_config(self, config: Any) -> None:
        """从用户设置加载 profiles + active id（保留未知字段，不抛错）。

        - config 无该 key 或损坏 → 保持空注册表，不抛（首启/迁移友好）。
        - 只读非 secret；secret 仍由 ``CredentialStore`` 在用时按 ``credential_ref`` 取。
        """
        raw = config.get(self.CONFIG_KEY_PROFILES)
        if not isinstance(raw, list):
            return  # 首启或损坏：保持当前状态
        # 清空再加载（避免与内存残留重复）
        self._profiles = {}
        self._active_profile_id = ""
        self._active_by_capability = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                profile = AiProviderProfile.from_dict(item)
            except (KeyError, TypeError, ValueError):
                # 损坏的单条 profile 跳过，不整体失败（降级，TAEP §9 可诊断可降级）
                continue
            self._profiles[profile.profile_id] = profile
        active = config.get(self.CONFIG_KEY_ACTIVE)
        if isinstance(active, str) and active and active in self._profiles:
            self._active_profile_id = active
            self.set_active(active)
        raw_active_by_capability = config.get(self.CONFIG_KEY_ACTIVE_BY_CAPABILITY)
        if isinstance(raw_active_by_capability, dict):
            for capability, profile_id in raw_active_by_capability.items():
                if isinstance(capability, str) and isinstance(profile_id, str) and profile_id in self._profiles:
                    try:
                        self.set_active_for_capability(capability, profile_id)
                    except ValueError:
                        continue

    def save_to_user_file(self, path: Path | None = None) -> None:
        """保存 profile 与 active id 到用户级 ``~/.topos`` 文件。"""

        data = load_user_ai_config(path)
        data["profiles"] = self.to_list()
        data["active_profile_id"] = self._active_profile_id
        data["active_profile_ids"] = dict(self._active_by_capability)
        save_user_ai_config(data, path)

    def load_from_user_file(self, path: Path | None = None) -> bool:
        """从用户级文件加载 profile；成功读取 profile 列表返回 ``True``。"""

        data = load_user_ai_config(path)
        raw = data.get("profiles")
        if not isinstance(raw, list):
            return False
        self._profiles = {}
        self._active_by_capability = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                profile = AiProviderProfile.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            self._profiles[profile.profile_id] = profile
        self._active_profile_id = ""
        active = data.get("active_profile_id")
        if isinstance(active, str) and active in self._profiles:
            self._active_profile_id = active
            self.set_active(active)
        raw_active_by_capability = data.get("active_profile_ids")
        if isinstance(raw_active_by_capability, dict):
            for capability, profile_id in raw_active_by_capability.items():
                if isinstance(capability, str) and isinstance(profile_id, str) and profile_id in self._profiles:
                    try:
                        self.set_active_for_capability(capability, profile_id)
                    except ValueError:
                        continue
        return True

    @property
    def active_profile_id(self) -> str:
        return self._active_profile_id
