"""AI Assistant mode metadata and the disk-backed plugin bridge.

The composer only emits a stable identifier.  Metadata is discovered from
``plugins/ai_assistant/*/plugin.json`` so a new mode can be added without
editing this module.  The small fallback below is only used by packaged builds
where the optional plugin data directory is missing; a present plugin
directory is authoritative, which means deleting a plugin really removes the
mode on the next reload/startup.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.features.edit.ai_editing.assistant_plugins import (
    get_assistant_plugin_registry,
)


@dataclass(frozen=True)
class AssistantModeSpec:
    """One user-selectable Assistant mode."""

    mode_id: str
    label: str
    description: str


_FALLBACK_ASSISTANT_MODE_SPECS: tuple[AssistantModeSpec, ...] = (
    AssistantModeSpec(
        "chat", "普通对话", "讨论剧本、创意和剪辑问题，不直接调用媒体生成器。",
    ),
    AssistantModeSpec(
        "motion_graphic", "Motion Graphic", "生成网页式演示动画，并显示确认执行卡片。",
    ),
    AssistantModeSpec(
        "video", "视频生成", "调用已配置的视频生成模型，结果先进入生成素材箱。",
    ),
    AssistantModeSpec(
        "music", "音乐生成", "调用已配置的音乐生成模型，结果先进入生成素材箱。",
    ),
    AssistantModeSpec(
        "image", "图片生成", "调用已配置的图片生成模型，结果先进入生成素材箱。",
    ),
)


def get_assistant_mode_specs() -> tuple[AssistantModeSpec, ...]:
    """Return the current plugin-backed selector catalog."""

    registry = get_assistant_plugin_registry()
    if registry.exists:
        return tuple(
            AssistantModeSpec(item.plugin_id, item.label, item.description)
            for item in registry.list_plugins()
        )
    return _FALLBACK_ASSISTANT_MODE_SPECS


def get_media_assistant_modes() -> frozenset[str]:
    """Return media modes from manifests, with a compatibility fallback."""

    registry = get_assistant_plugin_registry()
    if registry.exists:
        return registry.media_modes()
    return frozenset({"video", "music", "image"})


# Compatibility exports retained for existing callers and tests.  New code
# that needs to observe a runtime reload should use the getter functions.
ASSISTANT_MODE_SPECS: tuple[AssistantModeSpec, ...] = get_assistant_mode_specs()
DEFAULT_ASSISTANT_MODE = "chat"
MEDIA_ASSISTANT_MODES = get_media_assistant_modes()


def normalize_assistant_mode(value: object) -> str:
    """Return a safe, registered mode id.

    Unknown values fall back to ordinary chat so a stale preference or a
    future mode from a newer build can never accidentally trigger a write or
    paid generation action.
    """

    candidate = str(value or "").strip().lower()
    if any(spec.mode_id == candidate for spec in get_assistant_mode_specs()):
        return candidate
    return DEFAULT_ASSISTANT_MODE


def assistant_mode_spec(value: object) -> AssistantModeSpec:
    """Resolve a mode to its display metadata."""

    mode_id = normalize_assistant_mode(value)
    return next(spec for spec in get_assistant_mode_specs() if spec.mode_id == mode_id)


__all__ = [
    "ASSISTANT_MODE_SPECS",
    "DEFAULT_ASSISTANT_MODE",
    "MEDIA_ASSISTANT_MODES",
    "AssistantModeSpec",
    "assistant_mode_spec",
    "get_assistant_mode_specs",
    "get_media_assistant_modes",
    "normalize_assistant_mode",
]
