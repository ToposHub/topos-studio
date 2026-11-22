"""Resource browser adapter for the image workflow's real Layer model.

There is no graph or MediaPool dependency. Layer mutation uses the existing
engine/resource loaders and the image session's history, render and save hooks.
Slider changes are buffered until release; cancellation never mutates a layer.
"""
from __future__ import annotations

from copy import deepcopy
import logging
import math
from pathlib import Path
import threading
from typing import Any, Callable, Mapping

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


def _tr(key: str, fallback: str) -> str:
    return I18N.tr("resource_browser." + key, fallback)


class ColorLayerResourceController(QObject):
    """Project catalog resources onto an active image layer, not hidden nodes."""

    resource_rejected = pyqtSignal(object)
    import_feedback = pyqtSignal(object)

    def __init__(self, host: Any, *, catalog_provider: Any = None) -> None:
        super().__init__(host)
        self.host = host
        self.effects = host.color_effects_browser
        self.luts = host.color_lut_browser
        self.catalog_provider = catalog_provider or host.color_resource_catalog_provider
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}
        self._gesture: dict[str, Any] | None = None
        self._cancelled_gestures: set[tuple[Any, ...]] = set()
        self._projection: Any = None
        self._last_target: dict[str, Any] | None = None
        self._closed = False
        self._imports: list[threading.Event] = []
        self._thumbnails: Any = None
        self._source_key: Any = None
        self._source_array: Any = None
        self._source_generation = 0
        self.effects.resource_activated.connect(self.activate_effect)
        self.luts.resource_activated.connect(self.activate_resource)
        self.effects.draft_edit_requested.connect(self.edit)
        self.luts.draft_edit_requested.connect(self.edit)
        self.luts.import_requested.connect(self.import_luts)
        self.luts.refresh_requested.connect(self.refresh_catalog)
        self.luts.visible_entries_changed.connect(self._refresh_lut_thumbnails)
        self.import_feedback.connect(self._on_import_feedback)
        host.layer_panel.layer_selected.connect(self.refresh_target)
        changed = getattr(self.catalog_provider, "catalog_changed", None)
        if changed is not None:
            changed.connect(self.refresh_catalog)
        # Source loads and undo may replace Layer objects without a selection
        # signal. Poll identities only; all LUT evaluation stays in its service.
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self.refresh_target)
        self._timer.start()
        self.refresh_catalog()

    def _target(self) -> dict[str, Any] | None:
        engine = self.host.engine
        if self._closed or engine.img_full_original is None:
            return None
        layer = engine.get_active_layer()
        if layer is None:
            return None
        return {"layer": layer, "layer_id": layer.id,
                "source": str(getattr(self.host, "current_file_path", "") or ""),
                "epoch": getattr(engine, "_source_epoch", 0),
                "project_id": getattr(getattr(self.host, "_current_edit_project", None), "project_id", "")}

    @staticmethod
    def _same_target(first: Any, second: Any) -> bool:
        if first is None and second is None:
            return True
        return bool(isinstance(first, dict) and isinstance(second, dict)
                    and first.get("layer") is second.get("layer")
                    and all(first.get(key) == second.get(key)
                            for key in ("layer_id", "source", "epoch", "project_id")))

    def _reject(self, action: str, code: str, message: str) -> None:
        self.resource_rejected.emit({"action": action, "code": code,
                                     "message": message, "target": None})
        status = getattr(self.host, "status_bar", None)
        if status is not None:
            status.showMessage(message, 4000)

    def _plugin_manager(self) -> Any:
        from src.features.color.core.plugin_manager import get_plugin_manager
        return get_plugin_manager()

    def _lut_session(self) -> Any:
        from src.shared.color.lut_resource_registry import get_lut_resource_session
        return get_lut_resource_session()

    def _effect_entry(self, resource_id: str) -> dict[str, Any] | None:
        return self._entries.get(("effect", resource_id))

    def _effect_current(self, resource_id: str, entry: Mapping[str, Any]) -> bool:
        """A catalog snapshot cannot authorize a changed plugin implementation."""
        metadata = self._plugin_manager().get_plugin(resource_id)
        return bool(metadata and metadata.get("type", "dctl") == "dctl"
                    and metadata.get("shader_code")
                    and (not entry.get("content_hash") or entry["content_hash"] == metadata.get("content_hash"))
                    and list(metadata.get("params") or ()) == list(entry.get("parameters") or ()))

    def _commit(self, target: dict[str, Any], operation: Callable[[], None]) -> bool:
        if not self._same_target(target, self._target()):
            self._reject("edit", "stale_target", _tr("layer_changed", "Image or layer changed; retry the operation"))
            return False
        if getattr(target["layer"], "is_baked", False):
            self._reject("edit", "layer_baked", _tr("unbake_layer", "Unbake this layer before editing resources"))
            return False
        self.host._push_layer_history()
        operation()
        target["layer"].is_dirty = True
        self.host.edit_project_dirty = True
        self.host.layer_panel.update_layers(self.host.engine.layers, self.host.engine.active_layer_index)
        self.host.request_render()
        self.host._trigger_autosave()
        self._projection = None
        self.refresh_target()
        return True

    def activate_resource(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        if entry.get("kind") != "lut":
            self.activate_effect(entry)
            return
        target = self._target()
        if target is None:
            self._reject("set_lut", "no_target", _tr("select_layer", "Select an image layer"))
            return
        reference = str(entry.get("resource_uri") or entry.get("id") or "")
        if not reference.startswith("topos-res://") or self._lut_session().status_of(reference) != "registered":
            self._reject("set_lut", "resource_unavailable", _tr("lut_unavailable", "LUT resource is missing or unavailable"))
            return
        # Registry membership is not proof the staged/project file still exists.
        # Use the same resolver as the renderer before changing history/model.
        from src.shared.color.lut_loader import resolve_lut_path
        try:
            local = resolve_lut_path(reference)
            if not Path(local).is_file():
                raise FileNotFoundError(local)
        except (OSError, ValueError, RuntimeError) as exc:
            self._reject("set_lut", "resource_unavailable", str(exc))
            return
        layer = target["layer"]
        if layer.active_lut_path == reference:
            return
        self._gesture = None
        self._commit(target, lambda: self.host.engine.apply_lut(reference))

    def activate_effect(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        target = self._target()
        if target is None:
            self._reject("add_effect", "no_target", _tr("select_layer", "Select an image layer"))
            return
        resource_id = str(entry.get("id") or "")
        authoritative = self._effect_entry(resource_id)
        if authoritative is None or not authoritative.get("available", False) \
                or not self._effect_current(resource_id, authoritative):
            self._reject("add_effect", "resource_unavailable", str(
                (authoritative or {}).get("disabled_reason") or "Effect is unavailable for image layers"))
            return
        defaults = {param["var_name"]: deepcopy(param.get("default", 0.0))
                    for param in authoritative.get("parameters", []) if isinstance(param, dict) and param.get("var_name")}
        self._gesture = None
        if self._commit(target, lambda: target["layer"].add_plugin(resource_id, defaults)):
            self.effects.show_settings()

    def _parameter_value(self, plugin: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[str, Any]:
        entry = self._effect_entry(str(plugin.get("plugin_id") or ""))
        if entry is None or not entry.get("available") or not self._effect_current(str(plugin.get("plugin_id") or ""), entry):
            raise ValueError("resource_unavailable")
        name = payload.get("name")
        schema = next((p for p in entry.get("parameters", [])
                       if isinstance(p, dict) and p.get("var_name") == name), None)
        if schema is None:
            raise ValueError("parameter_unknown")
        value = deepcopy(payload.get("value"))
        if schema.get("type", "slider") not in {
            "slider", "float", "int", "checkbox", "bool", "boolean", "select", "enum", "choice", "color"}:
            raise ValueError("parameter_type_unsupported")
        # Reuse the existing schema rule without constructing a graph/session.
        from src.features.color.core.color_graph_service import ColorGraphService
        try:
            error = ColorGraphService._validate_effect_param(schema, value)
        except (ValueError, TypeError, OverflowError):
            error = "param_type_mismatch"
        if error:
            raise ValueError(error)
        return str(name), value

    def edit(self, action: str, payload: object) -> None:
        if not isinstance(payload, dict) or self._closed:
            return
        target = self._target()
        if target is None:
            self._reject(action, "no_target", _tr("select_layer", "Select an image layer"))
            return
        layer = target["layer"]
        phase = payload.get("phase", "commit")
        if phase == "cancel":
            if self._gesture is not None:
                self._cancelled_gestures.add(self._gesture["identity"])
            self._gesture = None
            self._projection = None
            self.refresh_target()
            return
        if action in ("set_parameter", "set_lut_strength"):
            try:
                if action == "set_parameter":
                    plugin = layer.get_plugin_instance(payload.get("instance_id"))
                    if plugin is None:
                        raise ValueError("instance_unknown")
                    name, value = self._parameter_value(plugin, payload)
                    old = plugin["params"].get(name)
                    operation = lambda: plugin["params"].__setitem__(name, value)
                    identity = (action, payload.get("instance_id"), name)
                else:
                    value = payload.get("strength", 1.0)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) \
                            or not math.isfinite(value) or not 0.0 <= value <= 1.0:
                        raise ValueError("param_out_of_range")
                    if not layer.active_lut_path:
                        raise ValueError("lut_not_applied")
                    old = layer.lut_intensity
                    operation = lambda: self.host.engine.set_lut_intensity(float(value))
                    identity = (action, layer.active_lut_path)
            except ValueError as exc:
                self._reject(action, str(exc), _tr("invalid_parameter", "Resource parameter is invalid"))
                return
            if phase == "start":
                self._cancelled_gestures.discard(identity)
                self._gesture = {"target": target, "identity": identity}
                return
            if identity in self._cancelled_gestures:
                if phase == "commit":
                    self._cancelled_gestures.discard(identity)
                self._reject(action, "gesture_target_changed", _tr("layer_changed", "Image or layer changed; retry the operation"))
                return
            if self._gesture is not None:
                gesture = self._gesture
                if not self._same_target(gesture["target"], target) or gesture["identity"] != identity:
                    self._gesture = None
                    self._reject(action, "gesture_target_changed", _tr("layer_changed", "Image or layer changed; retry the operation"))
                    return
                if phase == "change":
                    gesture.update(operation=operation, old=old, value=value)
                    return
            if phase not in ("change", "commit"):
                self._reject(action, "phase_unknown", "Unknown resource edit phase")
                return
            self._gesture = None
            if old != value:
                self._commit(target, operation)
            return
        self._gesture = None
        if action == "remove_lut":
            if layer.active_lut_path:
                self._commit(target, self.host.engine.reset_lut)
            return
        if action == "reset_lut_strength":
            if layer.active_lut_path and layer.lut_intensity != 1.0:
                self._commit(target, lambda: self.host.engine.set_lut_intensity(1.0))
            return
        plugin = layer.get_plugin_instance(payload.get("instance_id"))
        if plugin is None:
            self._reject(action, "instance_unknown", _tr("effect_missing", "Effect instance no longer exists"))
            return
        instance_id = str(plugin["instance_id"])
        if action == "set_enabled":
            enabled = payload.get("enabled")
            if type(enabled) is not bool:
                self._reject(action, "param_type_mismatch", "Effect enabled state must be boolean")
            elif bool(plugin.get("enabled", True)) != enabled:
                self._commit(target, lambda: layer.set_plugin_enabled(instance_id, enabled))
        elif action == "remove_instance":
            self._commit(target, lambda: self.host.engine.remove_plugin(self.host.engine.active_layer_index, instance_id))
        elif action == "reset_instance":
            entry = self._effect_entry(str(plugin.get("plugin_id") or ""))
            if entry is None or not entry.get("available") or not self._effect_current(str(plugin.get("plugin_id") or ""), entry):
                self._reject(action, "resource_unavailable", "Effect resource is unavailable")
                return
            defaults = {p["var_name"]: deepcopy(p.get("default", 0.0)) for p in entry.get("parameters", [])}
            if plugin.get("params") != defaults:
                self._commit(target, lambda: plugin.__setitem__("params", defaults))
        elif action == "move_instance":
            index = payload.get("index")
            if type(index) is not int or not 0 <= index < len(layer.plugins):
                self._reject(action, "param_out_of_range", "Effect order is out of range")
                return
            previous = layer.plugins.index(plugin)
            if previous != index:
                def move() -> None:
                    layer.plugins.insert(index, layer.plugins.pop(previous))
                self._commit(target, move)
        else:
            self._reject(action, "unsupported_action", "Operation is unavailable for image layers")

    def issue_async_token(self, instance_id: str) -> dict[str, Any] | None:
        target = self._target()
        if target is None or target["layer"].get_plugin_instance(instance_id) is None:
            return None
        return {"target": target, "instance_id": instance_id}

    def verify_async_token(self, token: Any) -> bool:
        return bool(isinstance(token, dict) and isinstance(token.get("target"), dict)
                    and self._same_target(token.get("target"), self._target())
                    and token["target"]["layer"].get_plugin_instance(token.get("instance_id")) is not None)

    def refresh_catalog(self) -> None:
        if self._closed:
            return
        entries = []
        manager = self._plugin_manager()
        for raw in self.catalog_provider.entries():
            entry = deepcopy(raw)
            if entry.get("kind") == "effect":
                metadata = manager.get_plugin(str(entry.get("id") or ""))
                if entry.get("source") == "builtin" or metadata is None:
                    entry.update(available=False, disabled_reason="image_backend_not_wired")
                elif metadata.get("type", "dctl") != "dctl":
                    entry.update(available=False, disabled_reason="image_bake_not_wired")
                elif not metadata.get("shader_code") or (entry.get("content_hash") and
                        entry["content_hash"] != metadata.get("content_hash")):
                    entry.update(available=False, disabled_reason="plugin_identity_mismatch")
                entries.append(entry)
        try:
            entries.extend(self._lut_session().catalog())
        except (OSError, ValueError, RuntimeError) as exc:
            self._reject("refresh_catalog", "lut_catalog_failed", str(exc))
        self._entries = {(str(e["kind"]), str(e["id"])): e for e in entries}
        self.effects.set_resources([e for e in entries if e["kind"] == "effect"])
        self.luts.set_resources([e for e in entries if e["kind"] == "lut"])
        self._projection = None
        self.refresh_target()

    def finish_transaction(self) -> None:
        """Commit a still-active slider once when its owning UI ends a gesture."""
        gesture, self._gesture = self._gesture, None
        if gesture is not None and "operation" in gesture and gesture["old"] != gesture["value"]:
            self._commit(gesture["target"], gesture["operation"])

    def cancel_transaction(self) -> None:
        if self._gesture is not None:
            self._cancelled_gestures.add(self._gesture["identity"])
        self._gesture = None
        self._projection = None
        self.refresh_target()

    def refresh_target(self, *_args: Any) -> None:
        if self._closed:
            return
        target = self._target()
        if not self._same_target(self._last_target, target):
            for cancel in self._imports:
                cancel.set()
            self._last_target = target
        if self._gesture is not None and not self._same_target(self._gesture["target"], target):
            if len(self._cancelled_gestures) >= 64:
                self._cancelled_gestures.clear()
            self._cancelled_gestures.add(self._gesture["identity"])
            self._gesture = None
            for cancel in self._imports:
                cancel.set()
        layer = target["layer"] if target else None
        identity = ((id(layer), target["source"], target["epoch"], target["project_id"])
                    if target else None)
        projection = (identity, deepcopy(layer.plugins) if layer else [],
                      layer.active_lut_path if layer else None,
                      layer.lut_intensity if layer else 1.0,
                      (layer.name, bool(getattr(layer, "is_baked", False))) if layer else None)
        if projection != self._projection:
            self._projection = projection
            layer_id = str(layer.id) if layer else ""
            label = layer.name if layer else ""
            editable = bool(layer and not getattr(layer, "is_baked", False))
            reason = (_tr("unbake_layer", "Unbake this layer before editing resources")
                      if layer and not editable else "" if layer else _tr("select_layer", "Select an image layer"))
            self.effects.set_target(layer_id, label, editable, reason)
            self.luts.set_target(layer_id, label, editable, reason)
            self.luts.target_label.setText(_tr("target_layer", "Target layer") + ": " + (label or reason))
            instances = []
            for plugin in layer.plugins if layer else []:
                entry = self._effect_entry(plugin["plugin_id"]) or {}
                available = bool(entry.get("available", False))
                instances.append({"instance_id": plugin["instance_id"], "resource_id": plugin["plugin_id"],
                    "name": entry.get("name", plugin["plugin_id"]), "enabled": bool(plugin.get("enabled", True)),
                    "params": deepcopy(plugin.get("params", {})), "parameters": deepcopy(entry.get("parameters", [])),
                    "available": available, "status": "ok" if available else "resource_unavailable",
                    "disabled_reason": entry.get("disabled_reason", "resource_unknown") if not available else ""})
            self.effects.set_instances(instances)
            self.luts.set_current_lut(layer.active_lut_path if layer else None, layer.lut_intensity if layer else 1.0)
        self._refresh_source(target)

    def _refresh_source(self, target: dict[str, Any] | None) -> None:
        import cv2
        import numpy as np
        engine = self.host.engine
        source = getattr(engine, "img_low", None)
        key = (target["source"], target["epoch"], id(source)) if target else None
        if key != self._source_key:
            self._source_key = key
            self._source_generation += 1
            self._source_array = None
            self.luts.clear_thumbnails()
            if target and isinstance(source, np.ndarray) and source.ndim == 3 and source.shape[2] >= 3:
                height, width = source.shape[:2]
                scale = min(1.0, 128 / width, 72 / height)
                small = cv2.resize(source[:, :, :3], (max(1, round(width * scale)), max(1, round(height * scale))))
                rgb = small[:, :, ::-1].astype(np.float32)
                if np.issubdtype(source.dtype, np.integer):
                    rgb /= float(np.iinfo(source.dtype).max)
                rgb.setflags(write=False)
                self._source_array = rgb
            if self._thumbnails is not None:
                self._thumbnails.cancel_all()
        self._refresh_lut_thumbnails()

    def _refresh_lut_thumbnails(self) -> None:
        if self._closed or self._source_array is None:
            return
        if self._thumbnails is None:
            from src.app.controllers.lut_thumbnail_service import LutThumbnailService
            self._thumbnails = LutThumbnailService(source_provider=lambda: self._source_array)
            self._thumbnails.thumbnail_ready.connect(self._thumbnail_ready)
            self._thumbnails.thumbnail_failed.connect(self._thumbnail_failed)
        self._thumbnails.set_source_identity(str(self._source_key), "image-layer", 0, 0, self._source_generation)
        for entry_id in self.luts.visible_entry_ids():
            entry = self.luts._entries.get(entry_id)
            if entry and "thumbnail" not in entry and "thumbnail_error" not in entry:
                self._thumbnails.request(entry_id, entry.get("resource_uri") or entry_id)

    def _thumbnail_ready(self, entry_id: str, image: Any, generation: int) -> None:
        if not self._closed and self._thumbnails and generation == self._thumbnails.stats()["generation"]:
            self.luts.set_thumbnail(entry_id, image)

    def _thumbnail_failed(self, entry_id: str, code: str, generation: int) -> None:
        if not self._closed and self._thumbnails and generation == self._thumbnails.stats()["generation"]:
            self.luts.set_thumbnail_failed(entry_id, code)

    def import_luts(self, paths: object) -> None:
        if not isinstance(paths, list) or self._closed:
            return
        candidates = [path for path in paths if isinstance(path, str)
                      and Path(path).suffix.lower() in (".cube", ".3dl", ".look")]
        if not candidates:
            return
        target = self._target()
        cancel = threading.Event()
        self._imports.append(cancel)
        session = self._lut_session()
        self.import_feedback.emit({"status": "started", "total": len(candidates)})

        def work() -> None:
            try:
                results = session.import_files(candidates, cancel_check=cancel.is_set)
                rows = [{"path": r.path, "status": r.status,
                         "error_code": r.error.code.value if r.error else "",
                         "message": r.error.message if r.error else ""} for r in results]
                successful = any(row["status"] in ("imported", "duplicate") for row in rows)
                status = ("failed" if rows and not successful and any(row["status"] == "failed" for row in rows)
                          else "cancelled" if rows and not successful
                          else "finished")
                payload = {"status": status, "target": target, "cancel": cancel, "results": rows}
            except (OSError, ValueError, RuntimeError) as exc:
                payload = {"status": "failed", "error": str(exc), "cancel": cancel}
            if not self._closed:
                try:
                    self.import_feedback.emit(payload)
                except RuntimeError:
                    logger.debug("LUT import completed after adapter deletion")
        threading.Thread(target=work, name="image-lut-import", daemon=True).start()

    def _on_import_feedback(self, payload: object) -> None:
        if not isinstance(payload, dict) or self._closed:
            return
        status = payload.get("status")
        if status == "started":
            self.luts.status_label.setText(_tr("import_started", "Importing LUTs…"))
            return
        cancel = payload.get("cancel")
        if cancel in self._imports:
            self._imports.remove(cancel)
        if status in ("finished", "failed", "cancelled"):
            rows = payload.get("results") or []
            imported = sum(row.get("status") == "imported" for row in rows)
            duplicates = sum(row.get("status") == "duplicate" for row in rows)
            failed = [row for row in rows if row.get("status") == "failed"]
            cancelled = sum(row.get("status") == "cancelled" for row in rows)
            error = str(payload.get("error") or "")
            details = "; ".join(f"{row.get('path', '')}: {row.get('message') or row.get('error_code', '')}"
                                for row in failed)
            self.refresh_catalog()
            summary = _tr("import_summary", "Imported {imported} · duplicate {duplicates} · failed {failed} · cancelled {cancelled}").format(
                imported=imported, duplicates=duplicates, failed=len(failed) or int(bool(error)), cancelled=cancelled)
            if error or failed:
                self._reject("import_luts", "lut_import_failed", details or error)
                summary += "\n" + (details or error)
            self.luts.status_label.setText(summary)

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._gesture = None
        self._cancelled_gestures.clear()
        for cancel in self._imports:
            cancel.set()
        self._imports.clear()
        if self._thumbnails is not None:
            self._thumbnails.shutdown()
            self._thumbnails = None
