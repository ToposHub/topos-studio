"""Disk-backed Assistant mode plugin discovery tests."""

from __future__ import annotations

import json
from pathlib import Path

from src.features.edit.ai_editing.assistant_plugins import (
    AssistantPluginRegistry,
)
from src.features.edit.ai_editing.assistant_modes import (
    get_assistant_mode_specs,
    get_media_assistant_modes,
    normalize_assistant_mode,
)


def _write_plugin(
    root: Path,
    plugin_id: str,
    payload: dict,
    files: dict[str, str] | None = None,
) -> None:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )
    for relative, content in (files or {}).items():
        target = plugin_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_registry_discovers_ordered_mode_and_reads_prompt(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "storyboard",
        {
            "api_version": "1",
            "kind": "assistant_mode",
            "id": "storyboard",
            "version": "1.2.0",
            "label": "分镜设计",
            "description": "创建分镜方案",
            "handler": "generic_chat",
            "capabilities": ["language"],
            "prompts": {"system": "system_prompt.md"},
            "ui": {"order": 5},
        },
        {"system_prompt.md": "你是分镜设计师。"},
    )

    registry = AssistantPluginRegistry(tmp_path)
    plugin = registry.get("storyboard")
    assert plugin is not None
    assert registry.list_plugins() == (plugin,)
    assert plugin.read_prompt("system") == "你是分镜设计师。"
    assert plugin.handler == "generic_chat"
    assert plugin.capabilities == ("language",)


def test_registry_rejects_prompt_path_escape(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "unsafe",
        {
            "kind": "assistant_mode",
            "id": "unsafe",
            "version": "1.0.0",
            "label": "不安全",
            "handler": "generic_chat",
            "capabilities": ["language"],
            "prompts": {"system": "../system.md"},
        },
    )

    registry = AssistantPluginRegistry(tmp_path)
    assert registry.list_plugins() == ()
    assert any(
        item.code == "ASSISTANT_PLUGIN_PROMPT_PATH_ESCAPE"
        for item in registry.diagnostics
    )


def test_repository_modes_are_loaded_from_central_plugin_directory() -> None:
    specs = get_assistant_mode_specs()
    assert [item.mode_id for item in specs] == [
        "chat", "motion_graphic", "video", "music", "image",
    ]
    assert get_media_assistant_modes() == {"video", "music", "image"}
    assert normalize_assistant_mode("motion_graphic") == "motion_graphic"


def test_motion_graphic_prompt_is_editable_plugin_data() -> None:
    registry = AssistantPluginRegistry()
    prompt = registry.prompt_for("motion_graphic", "html_build")
    assert "唯一的画布要求" in prompt
    assert "宿主不规定任何模板" in prompt
    assert "data-topos-page" in prompt
