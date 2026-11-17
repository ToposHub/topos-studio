"""Read-only plugin inventory and restart-bound settings for the legacy Files host.

Inventory never imports plugin code. Launch descriptors are an explicit bridge
for the four existing HTTP launch routes, not a general plugin execution API.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.features.files.plugin_contracts import FilesPluginInfo
from src.shared.plugins.manifest import PluginManifest, parse_manifest_file


_HOST_API_VERSION = 1
_MAX_METADATA_BYTES = 1024 * 1024
_METADATA_KEYS = frozenset({"NAME", "VERSION", "AUTHOR", "DESCRIPTION", "API_VERSION"})
# category, capabilities, launch endpoint payload kind (empty = no HTTP launcher)
_BUILTINS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "ai_library_browser": ("搜索与整理", ("语义搜索", "素材库状态"), ""),
    "audio_pro": ("音频", ("音频预览", "波形"), ""),
    "code_editor": ("编辑工具", ("代码编辑", "文本文件关联"), ""),
    "decode_benchmark": ("开发工具", ("离线解码跑分", "Edit 时间线跑分"), "paths"),
    "format_factory": ("转档与压缩", ("视频转码", "封装转换"), "path"),
    "image_editor": ("图片", ("图片编辑", "图片文件关联"), ""),
    "image_factory": ("图片", ("图片批处理",), "none"),
    "shutdown_timer": ("效率工具", ("定时关机",), "none"),
    "topos_image": ("格式扩展", (".toos 缩略图", ".toos 元数据"), ""),
    "video_pro": ("视频", ("视频预览", ".topos 缩略图", ".topos 元数据"), ""),
}


class FileExplorerPluginCatalog:
    """Project existing Files runtime state without creating or reloading it."""

    def __init__(
        self,
        plugins_dir: Path | None,
        manager: Any = None,
        load_config: Callable[[], Mapping[str, Any]] | None = None,
        save_config: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        # plugins_dir=None 表示只按 manager 投影运行时清单（桥接模块没有
        # 可定位的插件目录时使用）；绝不按 CWD 猜目录。
        self.plugins_dir = Path(plugins_dir).resolve() if plugins_dir is not None else None
        self._manager = manager
        self._load_config = load_config
        self._save_config = save_config

    def _config(self) -> dict[str, Any]:
        raw = self._load_config() if self._load_config is not None else {}
        if not isinstance(raw, Mapping):
            raise ValueError("Files 插件配置必须是对象")
        config = dict(raw)
        disabled = config.get("disabled_plugins", [])
        if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
            raise ValueError("disabled_plugins 必须是字符串列表")
        return config

    @staticmethod
    def _read_text(path: Path) -> str:
        if path.stat().st_size > _MAX_METADATA_BYTES:
            raise ValueError("插件元数据文件超过 1 MiB")
        return path.read_text(encoding="utf-8")

    def _metadata(self, folder: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        constants: dict[str, Any] = {}
        config: dict[str, Any] = {}
        diagnostics: list[str] = []
        try:
            tree = ast.parse(self._read_text(folder / "__init__.py"))
            for statement in tree.body:
                if isinstance(statement, ast.Assign):
                    names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
                    value = statement.value
                elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    names = [statement.target.id]
                    value = statement.value
                else:
                    continue
                for name in names:
                    if name in _METADATA_KEYS and value is not None:
                        try:
                            literal = ast.literal_eval(value)
                        except (ValueError, TypeError, SyntaxError):
                            diagnostics.append(f"{name} 不是静态常量，未执行插件代码")
                            continue
                        if isinstance(literal, (str, int)) and not isinstance(literal, bool):
                            constants[name] = literal
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as exc:
            diagnostics.append(f"无法读取插件声明：{exc}")
        config_path = folder / "config.json"
        if config_path.exists():
            try:
                if not config_path.resolve().is_relative_to(self.plugins_dir):
                    raise ValueError("插件配置位于插件目录之外")
                raw = json.loads(self._read_text(config_path))
                if not isinstance(raw, dict):
                    raise ValueError("config.json 必须是对象")
                config = raw
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                diagnostics.append(f"无法读取插件配置：{exc}")
        return constants, config, diagnostics

    def _declarative(self, folder: Path, plugin_id: str) -> tuple[PluginManifest | None, list[str], bool]:
        """Read topos.plugin.json when present; identity must match the folder."""
        path = folder / "topos.plugin.json"
        if not path.exists() and not path.is_symlink():
            return None, [], False
        if not path.resolve().is_relative_to(folder.resolve()):
            return None, ["manifest 位于插件目录之外，未读取"], True
        manifest, diagnostics = parse_manifest_file(path)
        notes = [f"manifest：{item.message}" for item in diagnostics]
        if manifest is not None and manifest.plugin_id != f"files.{plugin_id}":
            notes.append(f"manifest 身份 {manifest.plugin_id} 与目录不符，已忽略")
            return None, notes, True
        return manifest, notes, manifest is None or any(item.severity == "error" for item in diagnostics)

    @staticmethod
    def _payload_kind(manifest: PluginManifest | None) -> str:
        if manifest is None:
            return ""
        for capability in manifest.capabilities:
            if capability.kind == "command" and capability.available:
                return {"single": "path", "many": "paths"}.get(capability.selection_mode, "none")
        return ""

    def snapshot(self) -> tuple[FilesPluginInfo, ...]:
        config = self._config()
        disabled = set(config.get("disabled_plugins", []))
        active: dict[str, Mapping[str, Any]] = {}
        failed: dict[str, str] = {}
        for entry in getattr(self._manager, "plugins", ()):
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                active[entry["name"]] = entry
        for entry in getattr(self._manager, "failed_plugins", ()):
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                failed[entry["name"]] = str(entry.get("error", "插件加载失败"))
        folders = {
            folder.name: folder for folder in self.plugins_dir.iterdir()
            if folder.is_dir() and (folder / "__init__.py").is_file()
            and folder.resolve().is_relative_to(self.plugins_dir)
            and (folder / "__init__.py").resolve().is_relative_to(self.plugins_dir)
        } if self.plugins_dir is not None and self.plugins_dir.is_dir() else {}
        rows: list[FilesPluginInfo] = []
        for plugin_id in sorted(folders.keys() | active.keys() | failed.keys()):
            folder = folders.get(plugin_id) or (
                self.plugins_dir / plugin_id if self.plugins_dir is not None
                else Path(plugin_id))
            constants, local, diagnostics = self._metadata(folder) if plugin_id in folders else ({}, {}, ["运行时插件目录已不存在"])
            manifest, manifest_notes, manifest_blocked = self._declarative(folder, plugin_id) if plugin_id in folders else (None, [], False)
            diagnostics.extend(manifest_notes)
            runtime_info = active.get(plugin_id, {}).get("info", {})
            if not isinstance(runtime_info, Mapping):
                runtime_info = {}

            declared_fields = {"NAME": "display_name", "VERSION": "version",
                               "AUTHOR": "author", "DESCRIPTION": "description"}

            def text(key: str, fallback: str) -> str:
                if manifest is not None:
                    declared = getattr(manifest, declared_fields[key], "")
                    if isinstance(declared, str) and declared:
                        return declared
                value = runtime_info.get(key.lower(), constants.get(key, local.get(key.lower(), fallback)))
                return str(value) if isinstance(value, (str, int)) else fallback

            if manifest is not None:
                api_version = manifest.host_api[0]
                api_raw = f"{manifest.host_api[0]}–{manifest.host_api[1]}"
                api_compatible = manifest.host_api[0] <= _HOST_API_VERSION <= manifest.host_api[1]
            else:
                api_raw = constants.get("API_VERSION", 1)
                try:
                    api_version = int(api_raw)
                except (ValueError, TypeError, OverflowError):
                    api_version = None
                api_compatible = api_version is not None and api_version <= _HOST_API_VERSION
            if not api_compatible:
                diagnostics.append(f"宿主 API 不兼容：插件 {api_raw}，宿主 {_HOST_API_VERSION}")
            self_disabled = bool(local.get("disabled"))
            desired_enabled = plugin_id not in disabled and not self_disabled
            runtime_loaded = plugin_id in active
            fallback_category, fallback_capabilities, _ = _BUILTINS.get(plugin_id, ("其他", (), ""))
            if manifest is not None:
                category = manifest.category or fallback_category
                capabilities = tuple(
                    (capability.title or capability.capability_id)
                    for capability in manifest.capabilities if capability.available
                ) or fallback_capabilities
                payload_kind = self._payload_kind(manifest)
            else:
                category, capabilities = fallback_category, fallback_capabilities
                payload_kind = _BUILTINS.get(plugin_id, ("其他", (), ""))[2]
            # A declaration cannot create a legacy HTTP route or change its
            # payload contract. Only the verified builtin adapters may launch.
            expected_kind = _BUILTINS.get(plugin_id, ("其他", (), ""))[2]
            payload_mismatch = bool(payload_kind and expected_kind and payload_kind != expected_kind)
            if payload_mismatch:
                diagnostics.append("声明的选择规则与旧版启动接口不匹配")
            launch_path = f"/api/plugin/{plugin_id}/launch" if payload_kind and expected_kind and not payload_mismatch else ""
            error = failed.get(plugin_id, "")
            if manifest_blocked:
                unavailable = "插件声明无效或不兼容，详情中可查看原因"
            elif error:
                unavailable = "插件加载失败，详情中可查看原因"
            elif self_disabled:
                unavailable = "插件自身配置已停用，请先修改插件 config.json"
            elif not desired_enabled:
                unavailable = "插件已停用，重启后生效" if runtime_loaded else "插件已停用"
            elif not api_compatible:
                unavailable = "插件 API 与宿主不兼容"
            elif payload_mismatch:
                unavailable = "声明的选择规则与旧版启动接口不匹配"
            elif not runtime_loaded:
                unavailable = "插件尚未加载，启用后请重启应用"
            elif not launch_path:
                unavailable = "格式能力随文件使用，无独立窗口" if category == "格式扩展" or plugin_id == "video_pro" else "此插件使用旧版 Files 内嵌交互，尚无原生启动入口"
            else:
                unavailable = ""
            rows.append(FilesPluginInfo(
                plugin_id=plugin_id, name=text("NAME", plugin_id), version=text("VERSION", "0.0.1"),
                author=text("AUTHOR", "Unknown"), description=text("DESCRIPTION", ""),
                api_version=api_version, category=category, capabilities=capabilities,
                runtime_loaded=runtime_loaded, desired_enabled=desired_enabled,
                self_disabled=self_disabled, needs_restart=runtime_loaded != desired_enabled,
                error=error, launch_path=launch_path, launch_payload_kind=payload_kind,
                launch_unavailable_reason=unavailable, path=folder, diagnostics=tuple(diagnostics),
            ))
        return tuple(rows)

    def launch_request(self, plugin_id: str, paths: Sequence[str]) -> tuple[str, dict[str, Any]]:
        """Validate current capability and build the existing HTTP request."""
        info = next((item for item in self.snapshot() if item.plugin_id == plugin_id), None)
        if info is None:
            raise ValueError(f"未知插件：{plugin_id}")
        if not info.can_launch:
            raise ValueError(info.launch_unavailable_reason)
        if isinstance(paths, (str, bytes)) or any(not isinstance(path, str) for path in paths):
            raise ValueError("所选路径必须是字符串列表")
        selected = [path for path in paths if path.strip()]
        if info.launch_payload_kind == "path":
            if len(selected) > 1:
                raise ValueError("格式工厂的现有启动接口一次只能接收一个文件，请只选择一个素材")
            payload: dict[str, Any] = {"path": selected[0] if selected else ""}
        elif info.launch_payload_kind == "paths":
            payload = {"paths": selected}
        else:
            payload = {}
        return info.launch_path, payload

    def set_enabled(self, plugin_id: str, enabled: bool) -> FilesPluginInfo:
        """Persist next-start preference; do not unload active Python/Qt code."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        info = next((item for item in self.snapshot() if item.plugin_id == plugin_id), None)
        if info is None:
            raise ValueError(f"未知插件：{plugin_id}")
        if enabled and info.self_disabled:
            raise ValueError("插件自身 config.json 已停用，宿主不能覆盖该设置")
        if self._load_config is None or self._save_config is None:
            raise RuntimeError("Files 插件配置服务不可用")
        config = self._config()
        disabled = set(config.get("disabled_plugins", []))
        if enabled:
            disabled.discard(plugin_id)
        else:
            disabled.add(plugin_id)
        config["disabled_plugins"] = sorted(disabled)
        self._save_config(config)
        # The legacy writer logs I/O errors instead of raising: verify its result.
        persisted = self._config()
        if (plugin_id not in persisted.get("disabled_plugins", [])) != enabled:
            raise OSError("插件启停设置未能保存，请检查配置目录权限")
        return next(item for item in self.snapshot() if item.plugin_id == plugin_id)


__all__ = ["FileExplorerPluginCatalog", "FilesPluginInfo"]
