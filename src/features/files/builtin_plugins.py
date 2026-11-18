"""Native builtin declarations, with no legacy loader or server dependency."""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Any, Sequence

from src.features.files.plugin_contracts import FilesPluginInfo
from src.shared.plugins.discovery import DesiredStateStore, current_host_facts
from src.shared.plugins.dispatch import DispatchAuthority
from src.shared.plugins.manifest import parse_manifest_file, validate_manifest_set

BUILTIN_IDS = frozenset({"format_factory", "image_factory", "shutdown_timer",
    "decode_benchmark", "code_editor", "image_editor", "ai_library_browser",
    "audio_pro", "video_pro", "topos_image"})
_STATES_LOCK = threading.Lock()
_STATES: dict[str, tuple[DispatchAuthority, dict[str, bool]]] = {}


class NativePluginCatalog:
    """Discover declarations only; builtin code is explicitly hosted by the app."""

    native_only = True

    def __init__(self, plugins_dir: Path | None = None, *,
                 desired_store: DesiredStateStore | None = None) -> None:
        self.plugins_dir = plugins_dir or Path(__file__).with_name("builtin_manifests")
        self._desired_store = desired_store
        with _STATES_LOCK:
            key = str(desired_store._path.absolute()) if desired_store else f"instance:{id(self)}"
            if key not in _STATES:
                _STATES[key] = (DispatchAuthority(), desired_store.load() if desired_store else {})
            self.authority, self._desired = _STATES[key]

    def is_enabled(self, plugin_id: str) -> bool:
        return self._desired.get(f"files.{plugin_id.removeprefix('files.')}", True)

    def snapshot(self) -> tuple[FilesPluginInfo, ...]:
        desired = dict(self._desired)
        rows: list[FilesPluginInfo] = []
        for name in sorted(BUILTIN_IDS):
            folder = self.plugins_dir / name
            manifest, notes = parse_manifest_file(folder / "topos.plugin.json")
            if manifest is None:
                continue
            findings = (*notes, *validate_manifest_set((manifest,), **current_host_facts())[manifest.plugin_id])
            enabled = desired.get(manifest.plugin_id, True)
            blocked = any(note.severity == "error" for note in findings)
            command = any(c.kind == "command" and c.available for c in manifest.capabilities)
            reason = ("声明校验失败" if blocked else "插件已停用" if not enabled
                      else "格式能力随素材使用，无独立窗口" if not command else "")
            rows.append(FilesPluginInfo(
                plugin_id=name, name=manifest.display_name, version=manifest.version,
                author=manifest.author, description=manifest.description,
                api_version=1, category=manifest.category,
                capabilities=tuple(c.title or c.capability_id for c in manifest.capabilities),
                runtime_loaded=enabled and not blocked, desired_enabled=enabled,
                self_disabled=False, needs_restart=False, error="",
                launch_path=f"native://{name}" if command else "",
                launch_payload_kind="paths", launch_unavailable_reason=reason,
                path=folder, diagnostics=tuple(note.message for note in findings),
                command_key=name, runtime_version=manifest.version))
        return tuple(rows)

    def set_enabled(self, plugin_id: str, enabled: bool) -> FilesPluginInfo:
        name = plugin_id.removeprefix("files.")
        if name not in BUILTIN_IDS:
            raise ValueError("未知插件")
        def update() -> None:
            desired = dict(self._desired)
            desired[f"files.{name}"] = bool(enabled)
            if self._desired_store:
                self._desired_store.save(desired)
            self._desired.clear()
            self._desired.update(desired)
        self.authority.mutate(f"enabled:{name}", update)
        return next(row for row in self.snapshot() if row.plugin_id == name)

    def launch_request(self, plugin_id: str, paths: Sequence[str]) -> tuple[str, dict[str, Any]]:
        row = next((row for row in self.snapshot() if row.plugin_id == plugin_id), None)
        if row is None or not row.can_launch:
            raise ValueError(row.launch_unavailable_reason if row else "未知插件")
        return row.launch_path, {"paths": list(paths)}
