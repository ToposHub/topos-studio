"""Disk-backed AI Assistant mode plugins.

The Assistant mode selector is intentionally data-driven.  A plugin is a
directory under ``plugins/ai_assistant`` containing a validated ``plugin.json``
and optional prompt files.  Discovery only reads JSON and text; it never
imports Python from a plugin directory or grants a plugin host permissions.

This module is the small bridge between the user-facing "everything is a
plugin" layout and the existing, host-owned Assistant controller.  Handlers,
approval, timeline transactions and rendering remain in the application.  A
manifest can select a built-in handler, but it cannot create a new permission
by editing its own prompt or metadata.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


ASSISTANT_PLUGIN_API_VERSION = "1"
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def default_assistant_plugin_directory() -> Path:
    """Return the central on-disk Assistant plugin directory.

    ``TOPOS_AI_ASSISTANT_PLUGIN_DIR`` is deliberately an explicit override so
    tests, portable builds and power users can use another directory without
    changing source code.  The normal source checkout path is stable relative
    to this module and does not depend on the process working directory.
    """

    override = os.environ.get("TOPOS_AI_ASSISTANT_PLUGIN_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    repository_root = Path(__file__).resolve().parents[4]
    return repository_root / "plugins" / "ai_assistant"


@dataclass(frozen=True)
class AssistantPluginDiagnostic:
    """A non-fatal discovery diagnostic shown by diagnostics/preferences UI."""

    plugin_id: str
    code: str
    message: str
    severity: str = "error"
    path: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "plugin_id": self.plugin_id,
            "code": self.code,
            "message": self.message[:1_000],
            "severity": self.severity,
            "path": self.path,
        }


@dataclass(frozen=True)
class AssistantPluginManifest:
    """Validated, read-only metadata for one Assistant mode plugin."""

    plugin_id: str
    label: str
    description: str
    version: str
    api_version: str
    plugin_dir: Path = field(compare=False)
    capabilities: tuple[str, ...] = ()
    prompts: Mapping[str, str] = field(default_factory=dict, compare=False)
    handler: str = "generic_chat"
    media_modality: str = ""
    order: int = 100
    source: str = "local_dev"
    unverified: bool = True

    def prompt_path(self, name: str) -> Path | None:
        """Resolve a declared prompt path, never allowing path traversal."""

        relative = str(self.prompts.get(name, "") or "").strip()
        if not relative:
            return None
        candidate = (self.plugin_dir / relative).resolve()
        root = self.plugin_dir.resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def read_prompt(self, name: str) -> str:
        """Read one user-editable prompt, returning empty when unavailable."""

        path = self.prompt_path(name)
        if path is None:
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.plugin_id,
            "label": self.label,
            "description": self.description,
            "version": self.version,
            "api_version": self.api_version,
            "path": str(self.plugin_dir),
            "capabilities": list(self.capabilities),
            "prompts": dict(self.prompts),
            "handler": self.handler,
            "media_modality": self.media_modality,
            "order": self.order,
            "source": self.source,
            "unverified": self.unverified,
        }


class AssistantPluginRegistry:
    """Discover and query Assistant mode plugins from one central directory.

    Reloading is atomic: a malformed new plugin is reported in diagnostics and
    does not remove valid plugins discovered during the same scan.  The
    registry never executes plugin code.  A newly added or removed directory
    takes effect on the next application startup, or immediately after an
    explicit :func:`reload_assistant_plugins` call.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_assistant_plugin_directory()
        self._plugins: dict[str, AssistantPluginManifest] = {}
        self._diagnostics: tuple[AssistantPluginDiagnostic, ...] = ()
        self.reload()

    @property
    def exists(self) -> bool:
        return self.directory.is_dir()

    @property
    def diagnostics(self) -> tuple[AssistantPluginDiagnostic, ...]:
        return self._diagnostics

    def reload(self) -> tuple[AssistantPluginManifest, ...]:
        """Rescan direct child plugin folders and return valid manifests."""

        plugins: dict[str, AssistantPluginManifest] = {}
        diagnostics: list[AssistantPluginDiagnostic] = []
        if not self.directory.is_dir():
            self._plugins = {}
            self._diagnostics = ()
            return ()

        try:
            candidates = sorted(
                (path for path in self.directory.iterdir() if path.is_dir()),
                key=lambda path: path.name.casefold(),
            )
        except OSError as exc:
            self._plugins = {}
            self._diagnostics = (AssistantPluginDiagnostic(
                "", "ASSISTANT_PLUGIN_DIRECTORY_UNREADABLE", str(exc), path=str(self.directory),
            ),)
            return ()

        for plugin_dir in candidates:
            manifest, plugin_diagnostics = self._read_manifest(plugin_dir)
            diagnostics.extend(plugin_diagnostics)
            if manifest is None:
                continue
            if manifest.plugin_id in plugins:
                diagnostics.append(AssistantPluginDiagnostic(
                    manifest.plugin_id,
                    "ASSISTANT_PLUGIN_DUPLICATE_ID",
                    "重复的 Assistant plugin id；后发现的目录已忽略",
                    path=str(plugin_dir),
                ))
                continue
            plugins[manifest.plugin_id] = manifest

        self._plugins = plugins
        self._diagnostics = tuple(diagnostics)
        return self.list_plugins()

    def list_plugins(self) -> tuple[AssistantPluginManifest, ...]:
        """Return active plugins in deterministic UI order."""

        return tuple(sorted(
            self._plugins.values(),
            key=lambda item: (item.order, item.label.casefold(), item.plugin_id),
        ))

    def get(self, plugin_id: object) -> AssistantPluginManifest | None:
        return self._plugins.get(str(plugin_id or "").strip().lower())

    def mode_ids(self) -> frozenset[str]:
        return frozenset(self._plugins)

    def media_modes(self) -> frozenset[str]:
        return frozenset(
            item.plugin_id for item in self._plugins.values() if item.media_modality
        )

    def capabilities_for(self, mode: object) -> frozenset[str] | None:
        plugin = self.get(mode)
        return frozenset(plugin.capabilities) if plugin is not None else None

    def prompt_for(self, plugin_id: object, name: str, fallback: str = "") -> str:
        plugin = self.get(plugin_id)
        if plugin is None:
            return fallback
        return plugin.read_prompt(name) or fallback

    @staticmethod
    def _read_manifest(
        plugin_dir: Path,
    ) -> tuple[AssistantPluginManifest | None, tuple[AssistantPluginDiagnostic, ...]]:
        manifest_path = plugin_dir / "plugin.json"
        plugin_id = plugin_dir.name
        diagnostics: list[AssistantPluginDiagnostic] = []
        if not manifest_path.is_file():
            return None, (AssistantPluginDiagnostic(
                plugin_id,
                "ASSISTANT_PLUGIN_MANIFEST_MISSING",
                "插件目录缺少 plugin.json",
                path=str(plugin_dir),
            ),)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return None, (AssistantPluginDiagnostic(
                plugin_id,
                "ASSISTANT_PLUGIN_MANIFEST_INVALID",
                f"plugin.json 无法读取或解析：{exc}",
                path=str(manifest_path),
            ),)
        if not isinstance(payload, dict):
            return None, (AssistantPluginDiagnostic(
                plugin_id,
                "ASSISTANT_PLUGIN_MANIFEST_INVALID",
                "plugin.json 顶层必须是 JSON 对象",
                path=str(manifest_path),
            ),)

        declared_id = str(payload.get("id", payload.get("plugin_id", "")) or "").strip().lower()
        if not declared_id or declared_id != plugin_id or not _PLUGIN_ID_RE.fullmatch(declared_id):
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_ID_INVALID",
                "plugin id 必须与文件夹名称一致，并使用小写字母、数字、下划线或短横线",
                path=str(manifest_path),
            ))

        kind = str(payload.get("kind", "assistant_mode") or "").strip()
        if kind != "assistant_mode":
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_KIND_UNSUPPORTED",
                f"不支持的插件类型：{kind or '(empty)'}",
                path=str(manifest_path),
            ))

        api_version = str(payload.get("api_version", ASSISTANT_PLUGIN_API_VERSION) or "").strip()
        if api_version != ASSISTANT_PLUGIN_API_VERSION:
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_API_UNSUPPORTED",
                f"插件 API 版本 {api_version!r} 不受当前宿主支持",
                path=str(manifest_path),
            ))

        ui = payload.get("ui") if isinstance(payload.get("ui"), dict) else {}
        label = str(payload.get("label", ui.get("label", payload.get("display_name", ""))) or "").strip()
        description = str(payload.get("description", "") or "").strip()
        version = str(payload.get("version", "1.0.0") or "").strip()
        capabilities_value = payload.get("capabilities", ())
        if not isinstance(capabilities_value, (list, tuple)):
            capabilities_value = ()
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_CAPABILITIES_INVALID",
                "capabilities 必须是字符串数组",
                path=str(manifest_path),
            ))
        capabilities = tuple(dict.fromkeys(
            str(value).strip().lower() for value in capabilities_value if str(value).strip()
        ))
        if not capabilities:
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_CAPABILITIES_EMPTY",
                "Assistant plugin 至少需要声明一个能力",
                path=str(manifest_path),
            ))

        handler = str(payload.get("handler", "generic_chat") or "").strip()
        if handler == "generic_chat" and "language" not in capabilities:
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_HANDLER_CAPABILITY_MISMATCH",
                "generic_chat 插件必须声明 language 能力",
                path=str(manifest_path),
            ))

        try:
            order = int(ui.get("order", payload.get("order", 100)))
        except (TypeError, ValueError):
            order = 100
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_ORDER_INVALID",
                "UI order 不是整数，已使用 100",
                severity="warning",
                path=str(manifest_path),
            ))

        prompts_value = payload.get("prompts", {})
        prompts: dict[str, str] = {}
        if isinstance(prompts_value, dict):
            for name, relative in prompts_value.items():
                if not isinstance(name, str) or not isinstance(relative, str):
                    diagnostics.append(AssistantPluginDiagnostic(
                        declared_id or plugin_id,
                        "ASSISTANT_PLUGIN_PROMPT_PATH_INVALID",
                        "prompts 的键和值必须是字符串",
                        path=str(manifest_path),
                    ))
                    continue
                prompts[name.strip()] = relative.strip()
        elif prompts_value:
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_PROMPTS_INVALID",
                "prompts 必须是对象，例如 {\"system\": \"system_prompt.md\"}",
                path=str(manifest_path),
            ))
        shorthand_prompt = payload.get("system_prompt", "")
        if isinstance(shorthand_prompt, str) and shorthand_prompt.strip() and "system" not in prompts:
            prompts["system"] = shorthand_prompt.strip()

        for name, relative in prompts.items():
            candidate = (plugin_dir / relative).resolve()
            try:
                candidate.relative_to(plugin_dir.resolve())
            except ValueError:
                diagnostics.append(AssistantPluginDiagnostic(
                    declared_id or plugin_id,
                    "ASSISTANT_PLUGIN_PROMPT_PATH_ESCAPE",
                    f"提示词路径越出插件目录：{relative}",
                    path=str(manifest_path),
                ))
                continue
            if not candidate.is_file():
                diagnostics.append(AssistantPluginDiagnostic(
                    declared_id or plugin_id,
                    "ASSISTANT_PLUGIN_PROMPT_MISSING",
                    f"提示词文件不存在：{relative}",
                    severity="warning",
                    path=str(manifest_path),
                ))

        fatal_codes = {
            "ASSISTANT_PLUGIN_ID_INVALID",
            "ASSISTANT_PLUGIN_KIND_UNSUPPORTED",
            "ASSISTANT_PLUGIN_API_UNSUPPORTED",
            "ASSISTANT_PLUGIN_CAPABILITIES_EMPTY",
            "ASSISTANT_PLUGIN_HANDLER_CAPABILITY_MISMATCH",
            "ASSISTANT_PLUGIN_PROMPT_PATH_ESCAPE",
        }
        if not label:
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_LABEL_MISSING",
                "插件必须声明 label 或 ui.label",
                path=str(manifest_path),
            ))
            fatal_codes.add("ASSISTANT_PLUGIN_LABEL_MISSING")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            diagnostics.append(AssistantPluginDiagnostic(
                declared_id or plugin_id,
                "ASSISTANT_PLUGIN_VERSION_INVALID",
                f"版本号必须是 semver：{version!r}",
                path=str(manifest_path),
            ))
            fatal_codes.add("ASSISTANT_PLUGIN_VERSION_INVALID")
        if any(item.code in fatal_codes for item in diagnostics):
            return None, tuple(diagnostics)

        media_modality = str(payload.get("media_modality", "") or "").strip().lower()
        return AssistantPluginManifest(
            plugin_id=declared_id,
            label=label,
            description=description,
            version=version,
            api_version=api_version,
            plugin_dir=plugin_dir,
            capabilities=capabilities,
            prompts=prompts,
            handler=handler,
            media_modality=media_modality,
            order=order,
            source=str(payload.get("source", "local_dev") or "local_dev"),
            unverified=str(payload.get("source", "local_dev") or "local_dev") == "local_dev",
        ), tuple(diagnostics)


_DEFAULT_ASSISTANT_PLUGIN_REGISTRY = AssistantPluginRegistry()


def get_assistant_plugin_registry() -> AssistantPluginRegistry:
    """Return the process-wide Assistant plugin registry."""

    return _DEFAULT_ASSISTANT_PLUGIN_REGISTRY


def reload_assistant_plugins(
    directory: str | Path | None = None,
) -> tuple[AssistantPluginManifest, ...]:
    """Rescan the central directory, primarily for settings and tests."""

    global _DEFAULT_ASSISTANT_PLUGIN_REGISTRY
    if directory is not None:
        _DEFAULT_ASSISTANT_PLUGIN_REGISTRY = AssistantPluginRegistry(directory)
    else:
        _DEFAULT_ASSISTANT_PLUGIN_REGISTRY.reload()
    return _DEFAULT_ASSISTANT_PLUGIN_REGISTRY.list_plugins()


def get_assistant_plugin(plugin_id: object) -> AssistantPluginManifest | None:
    return _DEFAULT_ASSISTANT_PLUGIN_REGISTRY.get(plugin_id)


def assistant_plugin_prompt(
    plugin_id: object,
    prompt_name: str,
    fallback: str = "",
) -> str:
    return _DEFAULT_ASSISTANT_PLUGIN_REGISTRY.prompt_for(plugin_id, prompt_name, fallback)


__all__ = [
    "ASSISTANT_PLUGIN_API_VERSION",
    "AssistantPluginDiagnostic",
    "AssistantPluginManifest",
    "AssistantPluginRegistry",
    "assistant_plugin_prompt",
    "default_assistant_plugin_directory",
    "get_assistant_plugin",
    "get_assistant_plugin_registry",
    "reload_assistant_plugins",
]
