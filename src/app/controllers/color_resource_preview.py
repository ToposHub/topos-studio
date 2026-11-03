"""UI-only resource interaction session. No engine/project mutation or rendering.

Replace this presenter with an acknowledged command adapter when implementing
``docs/color_resources_backend_implementation_plan.md``. Drafts are intentionally
not saved in a .topos file and never advertise that pixels have changed.

R02：LUT 导入经 ``LutResourceSession`` 真实注册（验证/去重/暂存/随包保存），
反馈经 ``import_feedback``；但应用到画面仍属 R03+ 的命令路径。
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from src.app.controllers.color_resource_catalog import EffectsResourceCatalog
from src.features.color.ui.node_editor_state import NodeKind
from src.shared.i18n.i18n import I18N


def tr(key: str, fallback: str) -> str:
    return I18N.tr('resource_browser.' + key, fallback)


class ColorResourcePreview(QObject):
    """Keep resource drafts isolated by session, scope, compound and target."""

    resource_intent = pyqtSignal(object)
    import_feedback = pyqtSignal(object)

    def __init__(self, host: Any, *, resources: list[dict[str, Any]] | None = None,
                 catalog_provider: Any = None) -> None:
        super().__init__(host)
        self.host = host
        self.panel = host.color_node_panel
        self.effects = host.color_effects_browser
        self.luts = host.color_lut_browser
        # R01: 效果条目来自正式 catalog presenter（只读权威投影）；LUT 条目
        # 仍是会话级临时路径，待 R02 资源注册替换。
        owns_provider = catalog_provider is None
        self.catalog_provider = catalog_provider or EffectsResourceCatalog()
        self.catalog: dict[tuple[str, str], dict[str, Any]] = {}
        self._drafts: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._active_key: tuple[Any, ...] | None = None
        self._target_id = ''
        self._editable = False
        self.set_catalog(resources if resources is not None else self._existing_catalog())
        self.catalog_provider.catalog_changed.connect(self._on_catalog_changed)
        if owns_provider and resources is None:
            # 生产装配：后台预热冷目录（唯一磁盘扫描步骤），完成后重投影。
            self.catalog_provider.warm_background()
        self.effects.resource_activated.connect(lambda entry: self.activate(entry))
        self.luts.resource_activated.connect(lambda entry: self.activate(entry))
        self.effects.draft_edit_requested.connect(self.edit)
        self.luts.draft_edit_requested.connect(self.edit)
        self.luts.import_requested.connect(self.import_luts)
        self.luts.refresh_requested.connect(self.refresh_catalog)
        self.import_feedback.connect(self._on_import_feedback)
        self.panel.active_node_changed.connect(self.refresh_target)
        self.panel.scope_changed.connect(self.refresh_target)
        self.panel.view.scene_refreshed.connect(self.refresh_target)
        self.panel.view.resource_drop_requested.connect(self.drop)
        self.refresh_target()

    def _lut_session(self) -> Any:
        """R02：LUT 资源会话（工程索引 + 导入；无 Qt，线程安全）。"""
        from src.shared.color.lut_resource_registry import get_lut_resource_session
        return get_lut_resource_session()

    def _lut_gallery_entries(self) -> list[dict[str, Any]]:
        """旧 LUT 面板画廊只读投影（库目录浏览；正式 id 见会话目录）。"""
        entries: list[dict[str, Any]] = []
        gallery = getattr(getattr(self.host, 'lut_panel', None), 'lut_gallery', None)
        if gallery is not None:
            for index in range(gallery.list_widget.count()):
                item = gallery.list_widget.item(index)
                raw_path = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(raw_path, str):
                    entries.append(self._lut_entry(raw_path))
        return entries

    def _rebuild_catalog(self) -> None:
        """合并目录：效果投影 + LUT 会话（工程+导入）+ 旧画廊只读条目。"""
        combined: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self.catalog_provider.entries():
            combined[(entry['kind'], entry['id'])] = entry
        try:
            for entry in self._lut_session().catalog():
                combined[(entry['kind'], entry['id'])] = entry
        except Exception:  # noqa: BLE001 — 会话失败不清空效果目录
            pass
        for entry in self._lut_gallery_entries():
            combined.setdefault((entry['kind'], entry['id']), entry)
        self.set_catalog(list(combined.values()))

    def _existing_catalog(self) -> list[dict[str, Any]]:
        """Read metadata already loaded by the app; never compile/decode here."""
        entries: list[dict[str, Any]] = list(self.catalog_provider.entries())
        try:
            entries.extend(self._lut_session().catalog())
        except Exception:  # noqa: BLE001
            pass
        entries.extend(self._lut_gallery_entries())
        return entries

    def _on_catalog_changed(self) -> None:
        """权威目录刷新（后台预热/手动刷新）：重建全部条目。"""
        self._rebuild_catalog()

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
        # 用户显式刷新：重投影权威（内存读 + stat 级模型探测，无扫描），
        # LUT 会话与旧画廊条目一并重建；本调用不安装资源。
        self.catalog_provider.refresh()
        self._rebuild_catalog()

    def import_luts(self, paths: object) -> None:
        """R02 正式导入入口：后台验证+注册，反馈经 import_feedback 回传。

        取代旧 ``import_preview`` 的会话假导入；失败/取消不产生条目，
        成功后经会话目录重投影（含工程加载条目）。
        """
        if not isinstance(paths, list):
            return
        candidates = [path for path in paths if isinstance(path, str)
                      and Path(path).suffix.lower() in ('.cube', '.3dl', '.look')
                      and Path(path).is_file()]
        rejected = len(paths) - len(candidates)
        self.import_feedback.emit({'status': 'started', 'total': len(candidates),
                                   'rejected': rejected})
        import threading
        threading.Thread(target=self._import_lut_worker, args=(candidates,),
                         name='lut-resource-import', daemon=True).start()

    def _import_lut_worker(self, paths: list[str]) -> None:
        try:
            results = self._lut_session().import_files(paths)
        except Exception as exc:  # noqa: BLE001 — worker 内异常必须回传
            self.import_feedback.emit({'status': 'failed', 'error': str(exc)})
            return
        self.import_feedback.emit({'status': 'finished', 'results': [
            {'path': result.path, 'status': result.status,
             'error_code': result.error.code.value if result.error else '',
             'resource_uri': result.entry.uri if result.entry else ''}
            for result in results]})

    def _on_import_feedback(self, payload: object) -> None:
        """导入反馈（GUI 线程，经排队信号回传）：更新状态与目录树。"""
        if not isinstance(payload, dict):
            return
        status = payload.get('status')
        if status == 'started':
            total = int(payload.get('total', 0))
            self.luts.status_label.setText(
                tr('import_started', 'Importing {count} LUT(s)…').format(count=total))
            return
        if status == 'finished':
            self._rebuild_catalog()
            results = payload.get('results', [])
            imported = sum(1 for r in results if r.get('status') == 'imported')
            duplicates = sum(1 for r in results if r.get('status') == 'duplicate')
            failed = sum(1 for r in results if r.get('status') == 'failed')
            self.luts.status_label.setText(
                tr('import_finished',
                   'Imported {imported} · duplicate {duplicates} · failed {failed}')
                .format(imported=imported, duplicates=duplicates, failed=failed))
            for result in results:
                if result.get('status') == 'failed' and result.get('error_code'):
                    self._emit('import_rejected', {'error_code': result['error_code'],
                                                   'path': result.get('path', '')})
            return
        if status == 'failed':
            self.luts.status_label.setText(
                tr('import_failed', 'LUT import failed: {error}').format(
                    error=payload.get('error', '')))

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
