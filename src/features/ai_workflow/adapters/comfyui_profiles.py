"""ComfyProfileRegistry —— ComfyUI 连接 profile 的用户级持久化（批次 3）。

仿 :class:`src.features.edit.ai_editing.provider_profiles.ProfileRegistry` 的
ConfigManager 模式：profile 存用户设置（``comfyui.profiles`` /
``comfyui.active_profile_id``），cloud 凭据只存 ``credential_ref``（secret 由
既有 :class:`CredentialStore` 管理，``make_credential_ref(profile_id)`` 生成）。

红线（comfyui_models.ComfyConnectionProfile）：

- ``base_url`` 必须 http/https；cloud profile 必须带 ``credential_ref``。
- 本地（127.0.0.1/localhost）免凭据。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyConnectionProfile,
)

_LOG = logging.getLogger(__name__)


class ComfyProfileRegistry:
    """ComfyUI 连接 profile 注册表（用户设置持久化）。"""

    CONFIG_KEY_PROFILES = "comfyui.profiles"
    CONFIG_KEY_ACTIVE = "comfyui.active_profile_id"

    def __init__(self) -> None:
        self._profiles: dict[str, ComfyConnectionProfile] = {}
        self._active_profile_id: str = ""

    # ---------------- 增删查 ---------------- #

    def add(self, profile: ComfyConnectionProfile) -> None:
        self._profiles[profile.profile_id] = profile

    def remove(self, profile_id: str) -> None:
        self._profiles.pop(profile_id, None)
        if self._active_profile_id == profile_id:
            self._active_profile_id = ""

    def get(self, profile_id: str) -> Optional[ComfyConnectionProfile]:
        return self._profiles.get(profile_id)

    def list(self) -> List[ComfyConnectionProfile]:
        return list(self._profiles.values())

    @property
    def active_profile(self) -> Optional[ComfyConnectionProfile]:
        if not self._active_profile_id:
            return None
        return self._profiles.get(self._active_profile_id)

    def set_active(self, profile_id: str) -> None:
        if profile_id and profile_id not in self._profiles:
            raise KeyError(f"comfy profile {profile_id!r} not registered")
        self._active_profile_id = profile_id

    @property
    def active_profile_id(self) -> str:
        return self._active_profile_id

    # ---------------- 持久化 ---------------- #

    def save_to_config(self, config: Any) -> None:
        config.set(
            self.CONFIG_KEY_PROFILES,
            [p.to_dict() for p in self._profiles.values()],
            auto_save=False,
        )
        config.set(
            self.CONFIG_KEY_ACTIVE, self._active_profile_id, auto_save=True,
        )

    def load_from_config(self, config: Any) -> None:
        raw_profiles = config.get(self.CONFIG_KEY_PROFILES, []) or []
        self._profiles = {}
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            try:
                profile = ComfyConnectionProfile.from_dict(item)
            except (ValueError, KeyError, TypeError) as e:
                # AUD-23106：单条坏配置不阻断整体加载，但必须可见——此前宽泛
                # ``except Exception`` 且零日志，用户配置的云 profile 会在下次
                # 启动时无声消失。收窄为 from_dict 契约内的异常类型并记 warning。
                _LOG.warning(
                    "dropping invalid ComfyUI profile entry (profile_id=%r): %s",
                    item.get("profile_id"), e,
                )
                continue
            self._profiles[profile.profile_id] = profile
        active = str(config.get(self.CONFIG_KEY_ACTIVE, "") or "")
        self._active_profile_id = (
            active if active in self._profiles else ""
        )


__all__ = ["ComfyProfileRegistry"]
