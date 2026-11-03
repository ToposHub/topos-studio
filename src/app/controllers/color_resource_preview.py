"""UI-only resource interaction session. No engine/project mutation or rendering.

Replace this presenter with an acknowledged command adapter when implementing
``docs/color_resources_backend_implementation_plan.md``. Drafts are intentionally
not saved in a .topos file and never advertise that pixels have changed.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from src.features.color.ui.node_editor_state import NodeKind
from src.shared.i18n.i18n import I18N


def tr(key: str, fallback: str) -> str:
    return I18N.tr('resource_browser.' + key, fallback)


class ColorResourcePreview(QObject):
    """Keep resource drafts isolated by session, scope, compound and target."""

    resource_intent = pyqtSignal(object)

    def __init__(self, host: Any, *, resources: list[dict[str, Any]] | None = None) -> None:
        super().__init__(host)
        self.host = host
        self.panel = host.color_node_panel
        self.effects = host.color_effects_browser
        self.luts = host.color_lut_browser
        self.catalog: dict[tuple[str, str], dict[str, Any]] = {}
        self._drafts: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._active_key: tuple[Any, ...] | None = None
        self._target_id = ''
        self._editable = False
        self.set_catalog(resources if resources is not None else self._existing_catalog())
        self.effects.resource_activated.connect(lambda entry: self.activate(entry))
        self.luts.resource_activated.connect(lambda entry: self.activate(entry))
        self.effects.draft_edit_requested.connect(self.edit)
        self.luts.draft_edit_requested.connect(self.edit)
        self.luts.import_requested.connect(self.import_preview)
        self.luts.refresh_requested.connect(self.refresh_catalog)
        self.panel.active_node_changed.connect(self.refresh_target)
        self.panel.scope_changed.connect(self.refresh_target)
        self.panel.view.scene_refreshed.connect(self.refresh_target)
        self.panel.view.resource_drop_requested.connect(self.drop)
        self.refresh_target()

    def _existing_catalog(self) -> list[dict[str, Any]]:
        """Read metadata already loaded by the app; never compile/decode here."""
        from src.features.color.core.plugin_manager import get_plugin_manager
        entries: list[dict[str, Any]] = []
        for resource_id, metadata in get_plugin_manager().get_all_plugins().items():
            entries.append({'id': resource_id, 'kind': 'effect',
                            'name': metadata.get('name', resource_id),
                            'category': metadata.get('category') or ('AI' if metadata.get('type') == 'ai' else 'Topos FX'),
                            'description': metadata.get('description', ''),
                            'parameters': deepcopy(metadata.get('params', []))})
        gallery = getattr(getattr(self.host, 'lut_panel', None), 'lut_gallery', None)
        if gallery is not None:
            for index in range(gallery.list_widget.count()):
                item = gallery.list_widget.item(index)
                raw_path = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(raw_path, str):
                    entry = self._lut_entry(raw_path)
                    # Legacy icons are evaluated on a generic test image, not this clip.
                    # Do not pass them off as current-frame node previews.
                    entries.append(entry)
        return entries

    @staticmethod
    def _lut_entry(raw_path: str) -> dict[str, Any]:
        path = Path(raw_path).expanduser().resolve()
        return {'id': 'lut:' + sha256(str(path).encode()).hexdigest(), 'kind': 'lut',
                'name': path.stem, 'category': path.parent.name,
                'description': tr('lut_resource_hint', 'LUT resource · current-frame preview is not connected'),
                'source_path': str(path)}

    def set_catalog(self, entries: list[dict[str, Any]]) -> None:
        self.catalog = {(entry['kind'], entry['id']): dict(entry) for entry in entries
                        if isinstance(entry, dict) and entry.get('kind') in ('effect', 'lut')
                        and isinstance(entry.get('id'), str) and entry['id']}
        self.effects.set_resources([entry for (kind, _), entry in self.catalog.items() if kind == 'effect'])
        self.luts.set_resources([entry for (kind, _), entry in self.catalog.items() if kind == 'lut'])

    def refresh_catalog(self) -> None:
        # Keep imported session-only entries; this does not install resources.
        combined = {(e['kind'], e['id']): e for e in self._existing_catalog()}
        combined.update(self.catalog)
        self.set_catalog(list(combined.values()))

    def import_preview(self, paths: object) -> None:
        if not isinstance(paths, list):
            return
        additions = [self._lut_entry(path) for path in paths if isinstance(path, str)
                     and Path(path).suffix.lower() in ('.cube', '.3dl', '.look') and Path(path).is_file()]
        self.set_catalog(list(self.catalog.values()) + additions)
        self.luts.status_label.setText(tr('import_preview', 'Added to session library · resource import is not connected'))

    def _identity(self, node_id: str) -> tuple[Any, ...]:
        graph_host = getattr(self.host, 'color_graph_host', None)
        coordinator = getattr(graph_host, '_coordinator', None)
        path = tuple(item[1] for item in getattr(self.panel, '_parents', ()))
        return (getattr(coordinator, 'session_id', 'ui-session'),
                getattr(coordinator, 'generation', 0),
                getattr(coordinator, 'scope_owner_id', None),
                getattr(graph_host, '_bound_stack_id', ''), self.panel.scope, path, node_id)

    def _seed(self, node: Any) -> dict[str, Any]:
        raw_payload = node.backend_payload.get('legacy_payload')
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        instances = []
        raw_plugins = payload.get('plugins')
        for plugin in raw_plugins if isinstance(raw_plugins, list) else []:
            if not isinstance(plugin, dict):
                continue
            resource_id = str(plugin.get('plugin_id', ''))
            entry = self.catalog.get(('effect', resource_id), {})
            instances.append({'instance_id': str(plugin.get('instance_id') or uuid4().hex),
                              'resource_id': resource_id, 'name': entry.get('name', resource_id),
                              'enabled': bool(plugin.get('enabled', True)),
                              'params': deepcopy(plugin.get('params', {})),
                              'parameters': deepcopy(entry.get('parameters', []))})
        lut_path = payload.get('active_lut_path')
        lut_id = self._lut_entry(lut_path)['id'] if isinstance(lut_path, str) and lut_path else None
        return {'instances': instances, 'lut_id': lut_id, 'strength': payload.get('lut_intensity', 1.0)}

    def refresh_target(self, *_args: Any) -> None:
        node_id = self.panel.view.state.graph.active_id
        node = self.panel.view.state.graph.nodes.get(node_id)
        self._target_id = node_id if node else ''
        self.panel.view.resource_drop_enabled = self.panel.scope != 'timeline'
        self._editable = bool(node and node.kind == NodeKind.CORRECTOR and not node.locked
                              and self.panel.scope != 'timeline')
        label = (f'{node.number:02d}' + (f' · {node.label}' if node.label else '')) if node else ''
        reason = '' if self._editable else tr('writable_corrector', 'Select an unlocked corrector node')
        self.effects.set_target(self._target_id, label, self._editable, reason)
        self.luts.set_target(self._target_id, label, self._editable, reason)
        self._active_key = self._identity(node_id) if node else None
        if self._active_key is not None and self._active_key not in self._drafts:
            if len(self._drafts) >= 256:
                self._drafts.pop(next(iter(self._drafts)))
            self._drafts[self._active_key] = self._seed(node)
        draft = self._drafts.get(self._active_key, {})
        self.effects.set_instances(draft.get('instances', []))
        self.luts.set_current_lut(draft.get('lut_id'), draft.get('strength', 1.0))

    def _current_draft(self) -> dict[str, Any] | None:
        # Recheck identity and lock: a queued gesture may outlive a clip switch.
        node = self.panel.view.state.graph.nodes.get(self._target_id)
        if (not self._editable or node is None or node.locked or node.kind != NodeKind.CORRECTOR
                or self._active_key != self._identity(self._target_id)):
            return None
        return self._drafts.get(self._active_key)

    def drop(self, node_id: str, token: object) -> None:
        if not isinstance(token, dict):
            return
        entry = self.catalog.get((token.get('kind'), token.get('resource_id')))
        node = self.panel.view.state.graph.nodes.get(node_id)
        if entry is None or node is None or node.kind != NodeKind.CORRECTOR or node.locked:
            return
        view = self.panel.view
        view._syncing = True
        try:
            view.scene().clearSelection()
            view.cards[node_id].setSelected(True)
        finally:
            view._syncing = False
        view._selection_changed()
        self.refresh_target()
        self.activate(entry)

    def activate(self, entry: object) -> None:
        draft = self._current_draft()
        if draft is None or not isinstance(entry, dict):
            return
        resource = self.catalog.get((entry.get('kind'), entry.get('id')))
        if resource is None or not resource.get('available', True):
            return
        if resource['kind'] == 'effect':
            metadata = deepcopy(resource.get('parameters', []))
            draft['instances'].append({'instance_id': uuid4().hex, 'resource_id': resource['id'],
                                       'name': resource['name'], 'enabled': True,
                                       'parameters': metadata,
                                       'params': {p['var_name']: deepcopy(p.get('default')) for p in metadata
                                                  if isinstance(p, dict) and 'var_name' in p}})
            self.effects.set_instances(draft['instances'])
            self.effects.tabs.setCurrentIndex(1)
        else:
            draft['lut_id'] = resource['id']
            draft['strength'] = 1.0
            self.luts.set_current_lut(resource['id'], 1.0)
        self._emit('add_effect' if resource['kind'] == 'effect' else 'set_lut', {'resource_id': resource['id']})

    def _emit(self, action: str, payload: dict[str, Any]) -> None:
        self.resource_intent.emit({'ui_preview_only': True, 'target': self._active_key,
                                   'action': action, 'payload': deepcopy(payload)})

    def edit(self, action: str, payload: object) -> None:
        draft = self._current_draft()
        if draft is None or not isinstance(payload, dict):
            return
        instances = draft['instances']
        instance = next((item for item in instances if item['instance_id'] == payload.get('instance_id')), None)
        rebuild = False
        if action == 'set_lut_strength':
            draft['strength'] = payload['strength']
        elif action == 'reset_lut_strength':
            draft['strength'] = 1.0
        elif action == 'remove_lut':
            draft['lut_id'] = None
            draft['strength'] = 1.0
        elif instance is None:
            return
        elif action == 'set_parameter':
            instance['params'][payload['name']] = deepcopy(payload['value'])
        elif action == 'set_enabled':
            instance['enabled'] = bool(payload['enabled'])
        elif action == 'reset_instance':
            instance['params'] = {p['var_name']: deepcopy(p.get('default')) for p in instance['parameters']}
            rebuild = True
        elif action == 'remove_instance':
            instances.remove(instance)
            rebuild = True
        elif action == 'move_instance':
            instances.remove(instance)
            instances.insert(max(0, min(len(instances), int(payload['index']))), instance)
            rebuild = True
        else:
            return
        if rebuild:
            self.effects.set_instances(instances)
        self._emit(action, payload)
