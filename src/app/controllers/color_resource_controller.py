"""R03: 生产资源命令适配器（浏览器/拖放意图 → typed target + 图命令）。

与 :class:`ColorResourcePreview`（显式开发 preview 模式）互斥装配——同一
组浏览器信号只接一个适配器，禁止同时写草稿和正式 service。

契约（docs/color_resources_backend_implementation_plan.md R03）：

- **typed target 在意图接收瞬间捕获**：`(session_id, generation, scope,
  scope_owner_id, stack_id, compound_path, node_id)` + 意图时刻的
  ``expected_graph_revision``；不在异步完成后重新读活动节点。菜单添加使用
  活动 Corrector；拖放使用落点的 node_id 或 edge_id。
- **提交**：经 ``ColorGraphIntentNormalizer`` 规范化为 GraphIntent，
  走 ``bridge.submit`` → ApplyColorGraphCommand →
  工程 command 栈（一次操作一次 undo）；reject 不入历史、不递增 revision。
- **水合**：ack 后从 ``coordinator.last_ack``（权威投影）派生浏览器状态；
  reject 后按权威投影恢复（不留 UI 成功状态）。
"""
from __future__ import annotations

import logging
import time
from copy import deepcopy
from typing import Any, Mapping
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from src.app.controllers.color_graph_controller import (
    ColorGraphIntentNormalizer,
    EnvelopeError,
)
from src.shared.i18n.i18n import I18N


def _tr(key: str, fallback: str) -> str:
    return I18N.tr('resource_browser.' + key, fallback)


#: 浏览器 draft 信号 → (intent action, 参数映射)。
_EFFECT_ACTIONS = {
    'set_parameter': ('set_effect_parameter',
                      lambda p: {'name': p.get('name'), 'value': p.get('value')}),
    'set_enabled': ('set_effect_enabled',
                    lambda p: {'enabled': bool(p.get('enabled'))}),
    'reset_instance': ('reset_effect', lambda _p: {}),
    'remove_instance': ('remove_effect', lambda _p: {}),
    'move_instance': ('move_effect',
                      lambda p: {'index': int(p.get('index', 0))}),
}


class ColorResourceController(QObject):
    """正式资源命令适配器（R03 主干：effects/LUT → 图命令 + ack 水合）。"""

    #: 拒绝诊断：{action, code, message, target}（UI 只显示可翻译码）。
    resource_rejected = pyqtSignal(object)
    import_feedback = pyqtSignal(object)

    def __init__(self, host: Any, *, catalog_provider: Any = None) -> None:
        super().__init__(host)
        self.host = host
        self.graph_host = host.color_graph_host
        self.panel = host.color_node_panel
        self.effects = host.color_effects_browser
        self.luts = host.color_lut_browser
        self.catalog_provider = catalog_provider or host.color_resource_catalog_provider
        self._target_node_id = ''
        self._active_compound_path: tuple[str, ...] = ()
        # R04：参数手势事务（一次连续拖动 = 一次 undo；取消 = 零命令）。
        self._gesture: dict[str, Any] | None = None
        self._thumbnails: Any = None
        self._lut_source_array: Any = None
        self._lut_source_generation = 0
        self._refreshing_thumbnails = False
        # 浏览器信号（与 preview 适配器互斥——main_window 只装一个）。
        self.effects.resource_activated.connect(self.activate_effect)
        self.luts.resource_activated.connect(self.activate_resource)
        self.effects.draft_edit_requested.connect(self.edit)
        self.luts.draft_edit_requested.connect(self.edit)
        self.luts.import_requested.connect(self.import_luts)
        self.luts.refresh_requested.connect(self.refresh_catalog)
        self.luts.visible_entries_changed.connect(self._refresh_lut_thumbnails)
        self.panel.active_node_changed.connect(lambda _id: self.refresh_target())
        self.panel.scope_changed.connect(lambda _s: self.refresh_target())
        self.panel.view.scene_refreshed.connect(lambda: self.refresh_target())
        self.panel.view.resource_drop_requested.connect(self.drop)
        self.panel.view.effect_wire_drop_requested.connect(self.drop_effect_on_wire)
        self.panel.view.effect_wire_drop_enabled = True
        self.import_feedback.connect(self._on_import_feedback)
        source_preview = getattr(host, 'color_node_source_preview', None)
        source_signal = getattr(source_preview, 'source_thumbnail_changed', None)
        if source_signal is not None:
            source_signal.connect(self._on_source_thumbnail_changed)
        source_pixmap = getattr(self.panel.view, '_source_thumbnail', None)
        if source_pixmap is not None:
            self._on_source_thumbnail_changed(source_pixmap)
        catalog_signal = getattr(self.catalog_provider, 'catalog_changed', None)
        if catalog_signal is not None:
            catalog_signal.connect(self.refresh_catalog)
        self.refresh_catalog()
        self.refresh_target()

    # ── typed target（意图瞬间捕获） ───────────────────────────────
    def _coordinator(self) -> Any:
        return getattr(self.graph_host, '_coordinator', None)

    def _capture_target(self, node_id: str) -> dict[str, Any] | None:
        """意图接收瞬间的 typed target + expected revision（不延后读取）。"""
        coordinator = self._coordinator()
        if coordinator is None:
            return None
        return {
            'session_id': coordinator.session_id,
            'generation': coordinator.generation,
            'scope': self.panel.scope,
            'scope_owner_id': coordinator.scope_owner_id,
            'stack_id': getattr(self.graph_host, '_bound_stack_id', ''),
            'compound_path': tuple(item[1] for item in getattr(self.panel, '_parents', ())),
            'node_id': node_id,
            'expected_graph_revision': (
                coordinator.last_ack.graph_revision
                if coordinator.last_ack else None),
        }

    def _submit(self, action: str, node_id: str,
                arguments: Mapping[str, Any]) -> bool:
        """捕获 target → 规范化 intent → bridge 提交；reject 走权威恢复。"""
        target = self._capture_target(node_id)
        if target is None:
            self.resource_rejected.emit({'action': action, 'code': 'no_target',
                                         'message': '无可用图会话或目标', 'target': None})
            return False
        coordinator = self._coordinator()
        normalizer = ColorGraphIntentNormalizer(
            session_id=target['session_id'],
            scope_owner_id=target['scope_owner_id'],
            stack_id=target['stack_id'],
            expected_graph_revision=target['expected_graph_revision'],
            session_generation=target['generation'])
        args = dict(arguments)
        if node_id:
            args['node_id'] = node_id
        envelope = {
            'scope': target['scope'],
            'compound_path': list(target['compound_path']),
            'payload': {'arguments': args},
        }
        try:
            intent = normalizer.normalize(action, envelope)
        except EnvelopeError as exc:
            self.resource_rejected.emit({'action': action, 'code': 'envelope_error',
                                         'message': str(exc), 'target': target})
            return False
        started = time.monotonic()
        if not self.graph_host.bridge.submit(intent):
            # reject：coordinator 已回滚到最近 ack；按权威投影恢复 UI。
            pending = getattr(coordinator, 'pending', None)
            code = 'rejected'
            message = ''
            if pending is not None:
                code = 'stale_session'
                message = '会话已切换或提交被拒绝'
            payload = {'action': action, 'code': code,
                       'message': message, 'target': target,
                       'duration_ms': round((time.monotonic() - started) * 1000, 1)}
            _LOGGER.warning('资源命令拒绝: %s (%s) target=%s revision=%s %.1fms',
                            action, code, (target or {}).get('node_id'),
                            (target or {}).get('expected_graph_revision'),
                            payload['duration_ms'])
            self.resource_rejected.emit(payload)
            self.refresh_target()
            return False
        self.refresh_target()
        return True

    # ── 浏览器意图入口 ─────────────────────────────────────────────
    def _active_effectable_node(self) -> str:
        """活动 Corrector 家族节点（多选时只取 active，不批量应用）。"""
        node = self._authoritative_node(self._target_node_id,
                                        self._active_compound_path)
        if node is not None and node.get('node_type') in (
                'corrector', 'shared_corrector'):
            return self._target_node_id
        return ''

    def activate_effect(self, entry: object) -> None:
        if not isinstance(entry, dict) or entry.get('kind', 'effect') != 'effect':
            return
        node_id = self._active_effectable_node()
        if not node_id:
            self.resource_rejected.emit({'action': 'add_effect', 'code': 'no_target',
                                         'message': _tr('writable_corrector',
                                                        'Select an unlocked corrector node'),
                                         'target': None})
            return
        if not entry.get('available', True):
            self.resource_rejected.emit({'action': 'add_effect', 'code': 'resource_unavailable',
                                         'message': entry.get('disabled_reason', ''),
                                         'target': self._capture_target(node_id)})
            return
        if self._submit('add_effect', node_id, {'resource_id': entry.get('id', '')}):
            self.effects.show_settings()

    def activate_resource(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        if entry.get('kind') == 'lut':
            self._apply_lut(entry)
        else:
            self.activate_effect(entry)

    def _apply_lut(self, entry: dict[str, Any]) -> None:
        node_id = self._active_effectable_node()
        if not node_id:
            self.resource_rejected.emit({'action': 'set_lut', 'code': 'no_target',
                                         'message': _tr('writable_corrector',
                                                        'Select an unlocked corrector node'),
                                         'target': None})
            return
        lut_ref = entry.get('resource_uri') or entry.get('id', '')
        if not lut_ref.startswith('topos-res://'):
            # 会话外/旧路径条目不可作为正式 LUT 引用（R02 注册目录才有 uri）。
            self.resource_rejected.emit({'action': 'set_lut', 'code': 'resource_unavailable',
                                         'message': 'LUT 未注册为工程资源',
                                         'target': self._capture_target(node_id)})
            return
        self._submit('set_lut', node_id, {'lut_ref': lut_ref, 'strength': 1.0})

    def drop(self, node_id: str, token: object) -> None:
        """拖放落点即目标（不读活动节点）。"""
        if not isinstance(token, dict) or not self.panel.view.resource_drop_enabled:
            return
        self._end_gesture()
        path = tuple(item[1] for item in self.panel._parents)
        node = self._authoritative_node(node_id, path)
        if node is None or node.get('node_type') not in ('corrector', 'shared_corrector'):
            self.resource_rejected.emit({'action': 'drop', 'code': 'node_not_effectable',
                                         'message': '落点不是 Corrector', 'target': None})
            return
        if node.get('params', {}).get('locked'):
            self.resource_rejected.emit({'action': 'drop', 'code': 'node_locked',
                                         'message': '节点已锁定', 'target': None})
            return
        kind = token.get('kind')
        resource_id = token.get('resource_id', '')
        if kind == 'effect':
            entry = self._effect_drop_entry(resource_id, node_id)
            if entry is not None and self._submit(
                'add_effect', node_id, {'resource_id': resource_id, 'replace_existing': True},
            ):
                self.effects.show_settings()
            return
        if kind != 'lut':
            return
        entry = self._catalog_entry('lut', resource_id)
        if entry is None:
            self.resource_rejected.emit({'action': 'set_lut', 'code': 'resource_unknown',
                                         'message': f'未知 LUT 资源: {resource_id}',
                                         'target': self._capture_target(node_id)})
            return
        # 落点命中后同步选中（与 preview 行为一致，供面板/底栏显示）。
        view = self.panel.view
        view._syncing = True
        try:
            view.scene().clearSelection()
            if node_id in view.cards:
                view.cards[node_id].setSelected(True)
        finally:
            view._syncing = False
        view._selection_changed()
        self._target_node_id = node_id
        self._apply_lut(entry)

    def drop_effect_on_wire(self, edge_id: str, token: object,
                            position: tuple[float, float]) -> None:
        if (not self.panel.view.resource_drop_enabled or not isinstance(token, dict)
                or token.get('kind') != 'effect'):
            return
        self._end_gesture()
        entry = self._effect_drop_entry(token.get('resource_id', ''), '')
        if entry is not None and self._submit('insert_effect_on_edge', '', {
            'edge_id': edge_id, 'resource_id': entry['id'],
            'label': str(entry.get('name') or entry['id']), 'position': position,
        }):
            self.effects.show_settings()

    def _effect_drop_entry(self, resource_id: str, node_id: str) -> dict[str, Any] | None:
        entry = self._catalog_entry('effect', resource_id)
        if entry is not None and entry.get('available', True):
            return entry
        self.resource_rejected.emit({
            'action': 'drop', 'code': 'resource_unknown' if entry is None else 'resource_unavailable',
            'message': resource_id if entry is None else entry.get('disabled_reason', ''),
            'target': self._capture_target(node_id),
        })
        return None

    def edit(self, action: str, payload: object) -> None:
        """浏览器实例编辑信号 → 资源命令（实例归属由 service 验证）。"""
        if not isinstance(payload, dict):
            return
        node_id = self._active_effectable_node()
        if not node_id:
            self.resource_rejected.emit({'action': action, 'code': 'no_target',
                                         'message': '无可编辑目标', 'target': None})
            return
        instance_id = payload.get('instance_id')
        if action == 'set_parameter':
            self._edit_parameter_phase(node_id, instance_id, payload)
            return
        if action in _EFFECT_ACTIONS:
            intent_action, mapper = _EFFECT_ACTIONS[action]
            arguments = {'instance_id': instance_id}
            arguments.update(mapper(payload))
            self._submit(intent_action, node_id, arguments)
            return
        if action == 'set_lut_strength':
            phase = payload.get('phase', 'commit')
            self._gestured_submit(
                node_id, 'set_node_lut_strength', 'set_lut_strength',
                {'strength': float(payload.get('strength', 1.0))}, phase)
        elif action == 'reset_lut_strength':
            self._submit('set_lut_strength', node_id, {'strength': 1.0})
        elif action == 'remove_lut':
            self._submit('remove_lut', node_id, {})
        # 未知 action 忽略（不产生半命令）。

    # ── R04：参数手势事务 ──────────────────────────────────────────
    def _edit_parameter_phase(self, node_id: str, instance_id: Any,
                              payload: Mapping[str, Any]) -> None:
        """set_parameter 的 start/change/commit/cancel 相位机。

        - start：begin_transaction（在途手势先按落定策略 flush）；
        - change：缓冲提交（transient 与 pending 图一致，不产生 command）；
        - commit：末次缓冲 + flush → 一个 command 一次 undo；
        - cancel：discard（零 command，浏览器按权威投影恢复）；
        - 无 start 的离散 change（spin/键入）：立即单发命令。
        """
        name = payload.get('name')
        value = payload.get('value')
        phase = payload.get('phase', 'commit')
        if phase == 'start':
            self._begin_gesture(node_id, instance_id, name)
            return
        if phase == 'cancel':
            self._cancel_gesture()
            return
        arguments = {'instance_id': instance_id, 'name': name, 'value': value}
        if self._gesture is None:
            self._begin_gesture(node_id, instance_id, name)
            phase = 'commit'
        if phase == 'change' and self._gesture is not None:
            gesture = self._gesture
            if (gesture['node_id'] != node_id
                    or gesture['instance_id'] != instance_id):
                # 目标漂移（非信号驱动路径）：先落定旧手势再拒绝本帧。
                self._end_gesture()
                self.resource_rejected.emit({'action': 'set_parameter',
                                             'code': 'gesture_target_changed',
                                             'message': '手势目标已切换', 'target': None})
                return
            ok = self.graph_host.bridge.submit_buffered(
                'set_node_effect_parameter', dict(arguments, node_id=node_id),
                gesture['transaction'],
                compound_path=self._active_compound_path)
            if not ok:
                # 中间帧被 schema 拒绝（越界等）：丢弃该帧，手势保持。
                self.resource_rejected.emit({'action': 'set_parameter',
                                             'code': 'param_out_of_range',
                                             'message': '中间值被拒绝', 'target': None})
            else:
                observer = getattr(self.graph_host, 'observe_effect_parameter', None)
                if callable(observer):
                    observer(node_id, instance_id, name, value, self._active_compound_path)
            return
        # commit（或无手势的离散 change）：末次进入缓冲后 flush。
        self._gestured_submit(node_id, 'set_node_effect_parameter',
                              'set_effect_parameter', arguments, phase)

    def _gestured_submit(self, node_id: str, service_operation: str,
                         intent_action: str,
                         arguments: Mapping[str, Any], phase: str) -> None:
        """手势化提交：service 操作名与 intent action 名分开传。

        - 手势在途（change/commit）：缓冲到 pending 图；commit 落定一次
          command；
        - 无手势/离散：直接单发 intent 命令（一次 command）。
        """
        if phase == 'start':
            self._begin_gesture(node_id)
            return
        if self._gesture is not None and phase in ('change', 'commit'):
            gesture = self._gesture
            ok = self.graph_host.bridge.submit_buffered(
                service_operation, dict(arguments, node_id=node_id),
                gesture['transaction'],
                compound_path=self._active_compound_path)
            if not ok:
                self.resource_rejected.emit({'action': intent_action,
                                             'code': 'rejected',
                                             'message': '手势提交被拒绝', 'target': None})
                self._cancel_gesture()
                self.refresh_target()
                return
            if service_operation == 'set_node_effect_parameter':
                observer = getattr(self.graph_host, 'observe_effect_parameter', None)
                if callable(observer):
                    observer(node_id, str(arguments['instance_id']), str(arguments['name']),
                             arguments['value'], self._active_compound_path)
            if phase == 'commit':
                self._end_gesture()
                self.refresh_target()
            return
        # 离散操作（下拉/bool/确认颜色/键入值）：一次 command。
        self._submit(intent_action, node_id, arguments)

    def _begin_gesture(self, node_id: str, instance_id: Any = None,
                       name: Any = None) -> None:
        """开启参数手势事务（在途手势先落定）。"""
        self._end_gesture()
        transaction = uuid4().hex
        self.graph_host.bridge.begin_transaction(transaction)
        self._gesture = {'transaction': transaction, 'node_id': node_id,
                         'instance_id': instance_id, 'name': name}

    def _end_gesture(self) -> None:
        """落定在途手势（flush → 一个 command）；无手势时无操作。"""
        if self._gesture is None:
            return
        transaction = self._gesture['transaction']
        self._gesture = None
        flush = getattr(self.graph_host, 'flush_keyframe_recording', None)
        if callable(flush):
            flush(transaction)
        self.graph_host.bridge.flush_transaction()

    def _cancel_gesture(self) -> None:
        """取消在途手势（零 command；浏览器按权威投影恢复）。"""
        if self._gesture is None:
            return
        self._gesture = None
        cancel = getattr(self.graph_host, 'cancel_keyframe_recording', None)
        if callable(cancel):
            cancel()
        self.graph_host.bridge.cancel_transaction()
        self.refresh_target()

    # ── R04：AI/离线异步结果守卫 ───────────────────────────────────
    def issue_async_token(self, instance_id: str) -> dict[str, Any] | None:
        """异步任务发放令牌（目标 + 意图时刻 revision；不延后捕获）。"""
        node_id = self._active_effectable_node()
        target = self._capture_target(node_id) if node_id else None
        if target is None:
            return None
        return {'target': target, 'instance_id': instance_id}

    def verify_async_token(self, token: Any) -> bool:
        """晚到结果守卫：会话/目标/实例任一失配即丢弃（不写新目标）。"""
        if not isinstance(token, dict):
            return False
        target = token.get('target') or {}
        coordinator = self._coordinator()
        if coordinator is None:
            return False
        if (target.get('session_id') != coordinator.session_id
                or target.get('generation') != coordinator.generation
                or target.get('stack_id') != getattr(
                    self.graph_host, '_bound_stack_id', '')):
            return False
        node = self._authoritative_node(target.get('node_id', ''),
                                         tuple(target.get('compound_path') or ()))
        if node is None or node.get('node_type') not in ('corrector',
                                                         'shared_corrector'):
            return False
        plugins = (node.get('legacy_payload') or {}).get('plugins') or []
        return any(isinstance(p, Mapping)
                   and p.get('instance_id') == token.get('instance_id')
                   for p in plugins)

    # ── 权威水合 ───────────────────────────────────────────────────
    def _authoritative_graph(self) -> Mapping[str, Any] | None:
        coordinator = self._coordinator()
        ack = coordinator.last_ack if coordinator else None
        graph = ack.graph if ack else None
        return graph if isinstance(graph, Mapping) else None

    def _authoritative_node(self, node_id: str,
                            compound_path: tuple[str, ...] = ()) -> Mapping[str, Any] | None:
        graph = self._authoritative_graph()
        if graph is None or not node_id:
            return None

        def find(container: Mapping[str, Any], wanted: str) -> Mapping[str, Any] | None:
            for node in container.get('nodes') or ():
                if isinstance(node, Mapping) and node.get('node_id') == wanted:
                    return node
            return None

        container: Mapping[str, Any] = graph
        for compound_id in compound_path:
            compound = find(container, compound_id)
            children = compound.get('compound_children') if compound else None
            if not isinstance(children, Mapping):
                return None
            container = children
        return find(container, node_id)

    def refresh_target(self, *_args: Any) -> None:
        """从权威投影水合浏览器（ack/reject 后统一入口）。"""
        # R04 确定性结束策略：切节点/clip/scope 触发的水合先把在途参数
        # 手势落定（flush，不丢已拖数据）；显式 cancel 才走零命令路径。
        self._end_gesture()
        self._refresh_lut_thumbnails()
        self._target_node_id = self.panel.view.state.graph.active_id
        self._active_compound_path = tuple(
            item[1] for item in getattr(self.panel, '_parents', ()))
        node = self._authoritative_node(self._target_node_id,
                                        self._active_compound_path)
        coordinator = self._coordinator()
        drop_enabled = bool(coordinator and coordinator.last_ack
                            and coordinator.last_ack.scope == self.panel.scope
                            and getattr(self.graph_host, '_bound_stack_id', '')
                            and self.panel.scope != 'timeline')
        editable = bool(node is not None
                        and node.get('node_type') in ('corrector', 'shared_corrector')
                        and not node.get('params', {}).get('locked')
                        and drop_enabled)
        self.panel.view.resource_drop_enabled = drop_enabled
        label = ''
        if node is not None:
            label = f"{node.get('number', 0):02d}" if node.get('number') else ''
            if node.get('label'):
                label = (label + ' · ' if label else '') + str(node['label'])
        reason = '' if editable else _tr('writable_corrector',
                                         'Select an unlocked corrector node')
        self.effects.set_target(self._target_node_id if node else '', label,
                                editable, reason)
        self.luts.set_target(self._target_node_id if node else '', label,
                             editable, reason)
        self.effects.set_instances(self._project_instances(node))
        resources = (node.get('params', {}) or {}).get('resources', {}) \
            if node is not None else {}
        lut_ref = resources.get('lut_ref') if isinstance(resources, Mapping) else None
        strength = resources.get('lut_strength', 1.0) \
            if isinstance(resources, Mapping) else 1.0
        self.luts.set_current_lut(lut_ref if isinstance(lut_ref, str) else None,
                                  float(strength) if isinstance(strength, (int, float)) else 1.0)

    def _project_instances(self, node: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """权威节点实例 → 浏览器投影（schema 来自 catalog，缺资源保留配置）。

        R11：每实例带 status（R06 预检码：resource_unknown/ai_model_missing/
        shader_missing）与 disabled_reason（可翻译诊断码）；ack 后随权威
        投影刷新，reject 不留成功状态。
        """
        if node is None:
            return []
        issues = self._resource_issues_for(node)
        payload = node.get('legacy_payload')
        plugins = payload.get('plugins') if isinstance(payload, Mapping) else None
        instances: list[dict[str, Any]] = []
        for plugin in plugins if isinstance(plugins, (list, tuple)) else ():
            if not isinstance(plugin, Mapping):
                continue
            resource_id = str(plugin.get('plugin_id', ''))
            instance_id = str(plugin.get('instance_id', ''))
            entry = self._catalog_entry('effect', resource_id) or {}
            issue = next((i for i in issues
                          if i.get('instance_id') == instance_id), None)
            status = issue.get('code', 'ok') if issue else 'ok'
            instances.append({
                'instance_id': instance_id,
                'resource_id': resource_id,
                'name': entry.get('name', resource_id),
                'enabled': bool(plugin.get('enabled', True)),
                'params': deepcopy(dict(plugin.get('params') or {})),
                'parameters': deepcopy(entry.get('parameters', [])),
                'available': not issue,
                'status': status,
                'disabled_reason': status if status != 'ok' else '',
            })
        return instances

    def _resource_issues_for(self, node: Mapping[str, Any]) -> list[dict[str, Any]]:
        """R06 预检（节点域子集；注册表缺席时无诊断，不阻塞投影）。"""
        try:
            from src.features.color.core.color_graph_preflight import (
                preflight_graph_resources,
            )
            from src.shared.project.color_graph import ColorGraphData
            graph = ColorGraphData.from_dict({
                'nodes': [dict(node)], 'edges': [],
                'source_node_id': 'source', 'output_node_id': 'output'})
            issues = preflight_graph_resources(graph)
        except Exception:  # noqa: BLE001
            return []
        return [i.to_dict() for i in issues if i.node_id == node.get('node_id')]

    def _catalog_entry(self, kind: str, resource_id: str) -> dict[str, Any] | None:
        provider = self.catalog_provider
        entries = provider.entries() if provider is not None else []
        for entry in entries:
            if entry.get('kind') == kind and entry.get('id') == resource_id:
                return entry
        return None

    # ── R07：LUT 候选缩略图（后台评估 + GUI 投递） ──────────────────
    def _on_source_thumbnail_changed(self, pixmap: Any) -> None:
        """Snapshot Qt pixels on the GUI thread before a LUT worker reads them."""
        import numpy as np
        from PyQt6.QtGui import QImage, QPixmap

        self._lut_source_generation += 1
        self._lut_source_array = None
        self.luts.clear_thumbnails()
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            image = pixmap.toImage()
            if image.width() > 128 or image.height() > 72:
                image = image.scaled(
                    128, 72, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            image = image.convertToFormat(QImage.Format.Format_RGB888)
            ptr = image.constBits()
            ptr.setsize(image.sizeInBytes())
            rows = np.frombuffer(ptr, dtype=np.uint8).reshape(
                image.height(), image.bytesPerLine())
            rgb = rows[:, :image.width() * 3].reshape(
                image.height(), image.width(), 3)
            source = rgb.astype(np.float32) / 255.0
            source.setflags(write=False)
            self._lut_source_array = source
        self._refresh_lut_thumbnails()

    def _ensure_thumbnail_service(self) -> Any:
        if self._thumbnails is not None:
            return self._thumbnails
        from src.app.controllers.lut_thumbnail_service import LutThumbnailService
        service = LutThumbnailService(
            source_provider=lambda: self._lut_source_array)
        service.thumbnail_ready.connect(self._on_lut_thumbnail_ready)
        service.thumbnail_failed.connect(self._on_lut_thumbnail_failed)
        self._thumbnails = service
        return service

    def _on_lut_thumbnail_ready(self, entry_id: str, image: Any,
                                generation: int) -> None:
        service = self._thumbnails
        if (service is not None and self._lut_source_array is not None
                and generation == service.stats()['generation']):
            self.luts.set_thumbnail(entry_id, image)
            self._refresh_lut_thumbnails()

    def _on_lut_thumbnail_failed(self, entry_id: str, code: str,
                                 generation: int) -> None:
        service = self._thumbnails
        if (service is not None and self._lut_source_array is not None
                and generation == service.stats()['generation']):
            self.luts.set_thumbnail_failed(entry_id, code)
            self._refresh_lut_thumbnails()

    def _refresh_lut_thumbnails(self) -> None:
        """按当前会话世代请求可见 LUT 条目的缩略图（只处理可见资源）。"""
        if self._refreshing_thumbnails:
            return
        self._refreshing_thumbnails = True
        try:
            self._request_visible_lut_thumbnails()
        finally:
            self._refreshing_thumbnails = False

    def _request_visible_lut_thumbnails(self) -> None:
        coordinator = self._coordinator()
        if self._lut_source_array is None and self._thumbnails is None:
            return
        service = self._ensure_thumbnail_service()
        revision = (coordinator.last_ack.graph_revision
                    if coordinator and coordinator.last_ack else 0)
        service.set_source_identity(
            coordinator.session_id if coordinator else "",
            getattr(self.graph_host, "_bound_stack_id", "") or "",
            0, revision, self._lut_source_generation)
        if self._lut_source_array is None:
            return
        for entry_id in self.luts.visible_entry_ids():
            entry = next((e for k, e in self.luts._entries.items()
                          if e.get("id") == entry_id), None)
            if entry is None or 'thumbnail' in entry or 'thumbnail_error' in entry:
                continue
            lut_ref = entry.get("resource_uri") or entry.get("source_path")
            if isinstance(lut_ref, str) and lut_ref:
                service.request(entry_id, lut_ref)

    def shutdown(self) -> None:
        service = self._thumbnails
        self._thumbnails = None
        if service is not None:
            service.shutdown()

    # ── 目录/导入（与 preview 同源；导入是工程资源域，不经图命令） ──
    def _lut_session(self) -> Any:
        from src.shared.color.lut_resource_registry import get_lut_resource_session
        return get_lut_resource_session()

    def import_luts(self, paths: object) -> None:
        if not isinstance(paths, list):
            return
        from pathlib import Path
        candidates = [p for p in paths if isinstance(p, str)
                      and Path(p).suffix.lower() in ('.cube', '.3dl', '.look')
                      and Path(p).is_file()]
        self.import_feedback.emit({'status': 'started', 'total': len(candidates)})
        import threading
        threading.Thread(target=self._import_worker, args=(candidates,),
                         name='lut-resource-import', daemon=True).start()

    def _import_worker(self, paths: list[str]) -> None:
        try:
            results = self._lut_session().import_files(paths)
        except Exception as exc:  # noqa: BLE001
            self.import_feedback.emit({'status': 'failed', 'error': str(exc)})
            return
        self.import_feedback.emit({'status': 'finished', 'results': [
            {'path': r.path, 'status': r.status,
             'error_code': r.error.code.value if r.error else ''}
            for r in results]})

    def _on_import_feedback(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get('status') == 'started':
            self.luts.status_label.setText(
                _tr('import_started', 'Importing {count} LUT(s)…').format(
                    count=int(payload.get('total', 0))))
        elif payload.get('status') == 'finished':
            results = payload.get('results', [])
            imported = sum(1 for r in results if r.get('status') == 'imported')
            duplicates = sum(1 for r in results if r.get('status') == 'duplicate')
            failed = sum(1 for r in results if r.get('status') == 'failed')
            self.luts.status_label.setText(
                _tr('import_finished',
                    'Imported {imported} · duplicate {duplicates} · failed {failed}')
                .format(imported=imported, duplicates=duplicates, failed=failed))
            self.refresh_catalog()
        elif payload.get('status') == 'failed':
            self.luts.status_label.setText(
                _tr('import_failed', 'LUT import failed: {error}').format(
                    error=payload.get('error', '')))

    def refresh_catalog(self) -> None:
        """重投影目录到浏览器（效果 provider + LUT 会话；D2 后旧画廊
        投影来源已删除——LUT 条目唯一来源是资源会话目录）。"""
        combined: dict[tuple[str, str], dict[str, Any]] = {}
        if self.catalog_provider is not None:
            for entry in self.catalog_provider.entries():
                combined[(entry['kind'], entry['id'])] = entry
        try:
            for entry in self._lut_session().catalog():
                combined[(entry['kind'], entry['id'])] = entry
        except Exception as exc:  # noqa: BLE001 — 目录失败需明确呈现
            _LOGGER.exception('LUT resource catalog refresh failed')
            self.resource_rejected.emit({
                'action': 'refresh_catalog', 'code': 'lut_catalog_failed',
                'message': str(exc), 'target': None,
            })
            self.luts.more_button.setToolTip(
                _tr('lut_catalog_failed', 'LUT catalog unavailable') + ': ' + str(exc))
        else:
            self.luts.more_button.setToolTip(
                _tr('lut_options', 'LUT options'))
        self.effects.set_resources([e for (kind, _), e in combined.items()
                                    if kind == 'effect'])
        self.luts.set_resources([e for (kind, _), e in combined.items()
                                 if kind == 'lut'])
        self.refresh_target()
