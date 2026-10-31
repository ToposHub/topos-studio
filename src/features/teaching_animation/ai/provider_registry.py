"""Optional AI provider registration, disabled products stay clean."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .commands import TeachingAICommandRegistry


@dataclass(frozen=True)
class TeachingAIProviderDescriptor:
    provider_id: str = "teaching_animation"
    capabilities: tuple[str, ...] = ("scene_context", "scene_commands", "diff_apply_undo")
    command_registry: TeachingAICommandRegistry = TeachingAICommandRegistry()

    def to_dict(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "capabilities": list(self.capabilities)}


_PROVIDERS: dict[str, TeachingAIProviderDescriptor] = {}


def register_teaching_ai_provider() -> TeachingAIProviderDescriptor:
    descriptor = TeachingAIProviderDescriptor()
    _PROVIDERS[descriptor.provider_id] = descriptor
    return descriptor


def unregister_teaching_ai_provider() -> bool:
    return _PROVIDERS.pop("teaching_animation", None) is not None


def get_teaching_ai_provider() -> TeachingAIProviderDescriptor | None:
    return _PROVIDERS.get("teaching_animation")

