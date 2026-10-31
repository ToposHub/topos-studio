"""F4.2 · 命名调色版本与共享 look 锁定。

模型（对齐计划 F4.2）：
- **命名版本**：每个栈可保存任意命名版本（完整 v2 栈快照 + 插件清单），
  ``active`` 指针标记当前激活版本；**激活 = 内容互换**（栈身份字段
  stack_id/asset_id/scope/timeline_id/clip_id 不变），因此所有引用
  （含 F4.1 组链、共享引用、导出解析）自动取到正确版本——"非当前
  序列导出取正确版本"由内容互换天然满足。
- **A/B/C 一键比较/回退**：``ActivateColorVersionCommand`` 单步可撤销
  （undo 恢复前一内容 + active 指针），来回激活即比较。
- **共享 look 锁定**：被多镜头引用（或 asset_default）的栈默认防误改
  ——``UpdateColorStackCommand`` 对锁定栈整体拒绝（ValueError，面板
  转状态栏提示），解锁经 ``SetStackLockCommand`` 显式操作或
  MakeLocalGradeCopy 复制独立版本（既有命令）。锁不影响版本命令
  （保存/激活/删除版本是版本管理，不是改 look）。
- **插件缺失不丢数据**：版本载荷携带 ``plugin_versions`` 清单，激活
  时比对当前注册表——缺失插件不阻断激活（数据保留），命令结果携带
  ``missing_plugins`` 供 UI/预检明示"不可渲染原因"。
- 持久化：``EditProject.color_stack_versions``（IO settings 键；旧工程
  无键 = 空表）。工程级版本快照归 F8.1，此处只做栈级。
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

#: 栈身份字段——版本互换时保留（引用稳定性）
_IDENTITY_FIELDS = (
    "stack_id", "asset_id", "scope", "source_kind", "timeline_id",
    "clip_id", "created_at",
)


def versions_state(project: Any, stack_id: str) -> dict:
    """该栈的版本表（{"active", "locked", "versions"}；缺省=空）。"""
    table = (getattr(project, "color_stack_versions", None) or {}).get(
        stack_id) or {}
    return {
        "active": table.get("active"),
        "locked": bool(table.get("locked", False)),
        "versions": dict(table.get("versions", {}) or {}),
    }


def is_stack_locked(project: Any, stack_id: str) -> bool:
    return versions_state(project, stack_id)["locked"]


def shared_stack_ids(project: Any) -> Tuple[str, ...]:
    """被多镜头引用的共享栈（含 asset_default）——默认锁定候选。"""
    stacks = getattr(project, "color_stacks", {}) or {}
    ref_counts: Dict[str, int] = {}
    for clip_id, clip in _iter_clips(project):
        for sid in _clip_stack_refs(clip):
            ref_counts[sid] = ref_counts.get(sid, 0) + 1
    shared = set()
    for sid, count in ref_counts.items():
        if count > 1:
            shared.add(sid)
    for sid, stack in stacks.items():
        if getattr(stack, "scope", "") == "asset_default":
            shared.add(sid)
    return tuple(sorted(shared))


def stack_dependent_clips(project: Any, stack_id: str) -> List[Tuple[str, str]]:
    """引用该栈的 (timeline_id, clip_id) 全集——共享变化影响范围预览。"""
    dependents: List[Tuple[str, str]] = []
    for timeline_id, clip in _iter_clips(project):
        if stack_id in _clip_stack_refs(clip):
            dependents.append((timeline_id, clip.clip_id))
    return dependents


def _clip_stack_refs(clip: Any) -> Tuple[str, ...]:
    refs = []
    main = getattr(clip, "color_stack_id", None)
    if main:
        refs.append(str(main))
    trim = getattr(clip, "color_trim_stack_id", None)
    if trim:
        refs.append(str(trim))
    return tuple(refs)


def _iter_clips(project: Any):
    """遍历工程全部时间线的全部 clip（含音频轨；调色在视频轨）。"""
    timelines = getattr(project, "timelines", None)
    if isinstance(timelines, dict):
        timeline_iter = timelines.values()
    elif timelines:
        timeline_iter = timelines
    else:
        current = getattr(project, "active_timeline", None)
        timeline_iter = [current] if current is not None else []
    for timeline in timeline_iter:
        if timeline is None:
            continue
        timeline_id = str(getattr(timeline, "timeline_id", "") or "")
        for track in getattr(timeline, "video_tracks", ()) or ():
            for clip in getattr(track, "clips", ()) or ():
                yield timeline_id, clip


class PluginRegistryUnavailableError(RuntimeError):
    """M07（S09）：插件注册表探测失败——调用方须标 unverified，
    不得把"探测不了"当成"无缺失"。"""


def _missing_or_unverified(payload: dict) -> Tuple[Tuple[str, ...], bool]:
    """(缺失清单, 注册表是否不可用)——探测失败时缺失清单为空但
    unverified=True（不冒充"已核对无缺失"）。"""
    try:
        return tuple(sorted(_missing_plugins(payload))), False
    except PluginRegistryUnavailableError:
        logger.warning(
            "Plugin registry unavailable; version plugin check marked "
            "unverified (not 'no missing')", exc_info=True)
        return (), True

def _plugin_manifest(stack: Any) -> Dict[str, Any]:
    """从 Layer 快照收集插件/效果清单（M07/S09 修复）。

    插件实例的真实形态是 ``layer["plugins"]`` 列表（{"instance_id",
    "plugin_id", "params", "enabled"}，layer.py:566）——此前读 layer
    根字段的 plugin_id/effect_id/node_type（Layer 快照根本没有这些键，
    清单恒空）。现在逐实例收集 plugin_id；版本优先取实例自带
    plugin_version/version（DCTL 等外挂可携带），否则从 Color 插件
    注册表（PluginManager，.tglsl @version）解析，注册表不可用或未
    装该插件时标 "unknown"（缺失检查由 _missing_plugins 负责）。
    根字段扫描保留为兼容回退（不丢任何可能的旧形态数据）。
    """
    registry: Dict[str, Any] = {}
    try:
        from src.features.color.core.plugin_manager import get_plugin_manager
        registry = get_plugin_manager().get_all_plugins() or {}
    except Exception:  # noqa: BLE001 — 清单收集不因注册表失败阻断
        registry = {}
    manifest: Dict[str, Any] = {}

    def _note(pid: Any, version: Any = None) -> None:
        if not pid:
            return
        pid = str(pid)
        if version is None:
            installed = registry.get(pid)
            version = (installed or {}).get("version") if installed else None
        manifest[pid] = str(version) if version else "unknown"

    for layer in getattr(stack, "layers", ()) or ():
        if not isinstance(layer, dict):
            continue
        # 真实形态：插件实例列表
        for plugin in layer.get("plugins", ()) or ():
            if not isinstance(plugin, dict):
                continue
            _note(
                plugin.get("plugin_id") or plugin.get("effect_id")
                or plugin.get("node_type"),
                plugin.get("plugin_version") or plugin.get("version"))
        # 兼容回退：根字段（旧形态/测试替身）
        for key in ("plugin_id", "effect_id", "node_type"):
            _note(layer.get(key),
                  layer.get("plugin_version") or layer.get("version"))
    return manifest


def _with_state(project: Any, stack_id: str, state: dict) -> Any:
    table = dict(getattr(project, "color_stack_versions", None) or {})
    table[stack_id] = {
        "active": state.get("active"),
        "locked": bool(state.get("locked", False)),
        "versions": dict(state.get("versions", {}) or {}),
    }
    return project.with_color_stack_versions(table)


# ── 命令（project 域受限事务；undo 对称） ────────────────────────────


@dataclass(frozen=True)
class SaveColorVersionCommand:
    """保存当前栈内容为命名版本（完整 v2 快照 + 插件清单）。"""

    command_id: str
    label: str
    stack_id: str
    version_name: str
    before: dict
    after: dict
    missing_plugins: Tuple[str, ...] = ()

    def apply(self, project: Any) -> Any:
        current = versions_state(project, self.stack_id)
        if current != self.before:
            raise ValueError(
                f"栈 {self.stack_id} 版本表已被其他操作修改，请先撤销后重试。")
        return _with_state(project, self.stack_id, self.after)

    def undo(self, project: Any) -> Any:
        current = versions_state(project, self.stack_id)
        if current != self.after:
            raise ValueError("版本表已被其他操作修改，无法撤销。")
        return _with_state(project, self.stack_id, self.before)

    @staticmethod
    def create(project: Any, stack_id: str, version_name: str,
               *, replace: bool = False) -> "SaveColorVersionCommand":
        stack = (getattr(project, "color_stacks", {}) or {}).get(stack_id)
        if stack is None:
            raise ValueError(f"调色栈不存在：{stack_id}")
        version_name = str(version_name).strip()
        if not version_name:
            raise ValueError("版本名不能为空。")
        before = versions_state(project, stack_id)
        if version_name in before["versions"] and not replace:
            raise ValueError(f"版本「{version_name}」已存在（replace=True 覆盖）。")
        payload = copy.deepcopy(stack.to_dict())
        payload["plugin_versions"] = _plugin_manifest(stack)
        payload["saved_at"] = time.time()
        missing, unverified = _missing_or_unverified(payload)
        if unverified:
            # 注册表探测失败：载荷显式携带 unverified 标记（重开/比较
            # 时不冒称已核对），清单保守为空。
            payload["plugin_check_unverified"] = True
        after = copy.deepcopy(before)
        after["versions"] = dict(after["versions"])
        after["versions"][version_name] = payload
        after["active"] = version_name
        return SaveColorVersionCommand(
            str(uuid.uuid4()), f"Save Color Version「{version_name}」",
            stack_id, version_name, before, after, missing)


@dataclass(frozen=True)
class ActivateColorVersionCommand:
    """激活命名版本（内容互换；栈身份保留；单步可撤销=回退/比较）。"""

    command_id: str
    label: str
    stack_id: str
    version_name: str
    _before_stack: Any = None    # apply 时填充的完整旧栈
    _after_stack: Any = None
    _before_state: dict = None
    _after_state: dict = None
    missing_plugins: Tuple[str, ...] = ()

    def apply(self, project: Any) -> Any:
        stacks = dict(getattr(project, "color_stacks", {}) or {})
        current = stacks.get(self.stack_id)
        if current is None:
            raise ValueError(f"调色栈不存在：{self.stack_id}")
        state = versions_state(project, self.stack_id)
        payload = state["versions"].get(self.version_name)
        if payload is None:
            raise ValueError(f"版本「{self.version_name}」不存在。")
        content = {k: v for k, v in payload.items()
                   if k not in ("plugin_versions", "saved_at")}
        restored = _stack_from_payload(current, content)
        stacks[self.stack_id] = restored
        after_state = copy.deepcopy(state)
        after_state["active"] = self.version_name
        updated = project.with_color_stacks(stacks)
        updated = _with_state(updated, self.stack_id, after_state)
        try:
            missing = tuple(sorted(_missing_plugins(payload)))
        except PluginRegistryUnavailableError:
            # 未知插件不丢载荷；注册表探测失败不冒称无缺失——
            # 标记 unverified 供 UI/诊断呈现
            logger.warning(
                "Activate version %s: plugin registry unavailable; "
                "missing check unverified", self.version_name)
            missing = ()
        applied = ActivateColorVersionCommand(
            command_id=self.command_id, label=self.label,
            stack_id=self.stack_id, version_name=self.version_name,
            _before_stack=copy.deepcopy(current),
            _after_stack=copy.deepcopy(restored),
            _before_state=copy.deepcopy(state),
            _after_state=copy.deepcopy(after_state),
            missing_plugins=missing,
        )
        return updated, applied

    def undo(self, project: Any) -> Any:
        if self._before_stack is None or self._before_state is None:
            raise ValueError("命令未 apply，无法撤销。")
        state = versions_state(project, self.stack_id)
        if state != self._after_state:
            raise ValueError("版本表已被其他操作修改，无法撤销。")
        stacks = dict(getattr(project, "color_stacks", {}) or {})
        stacks[self.stack_id] = copy.deepcopy(self._before_stack)
        updated = project.with_color_stacks(stacks)
        return _with_state(updated, self.stack_id, self._before_state)

    @staticmethod
    def create(stack_id: str, version_name: str) -> "ActivateColorVersionCommand":
        return ActivateColorVersionCommand(
            str(uuid.uuid4()),
            f"Activate Color Version「{version_name}」",
            stack_id, version_name)


@dataclass(frozen=True)
class DeleteColorVersionCommand:
    """删除命名版本（active 指向被删版本时清除指针；栈内容不动）。"""

    command_id: str
    label: str
    stack_id: str
    version_name: str
    before: dict
    after: dict

    def apply(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.before:
            raise ValueError("版本表已被其他操作修改，请先撤销后重试。")
        return _with_state(project, self.stack_id, self.after)

    def undo(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.after:
            raise ValueError("版本表已被其他操作修改，无法撤销。")
        return _with_state(project, self.stack_id, self.before)

    @staticmethod
    def create(project: Any, stack_id: str,
               version_name: str) -> "DeleteColorVersionCommand":
        state = versions_state(project, stack_id)
        if version_name not in state["versions"]:
            raise ValueError(f"版本「{version_name}」不存在。")
        after = copy.deepcopy(state)
        after["versions"] = {
            k: v for k, v in after["versions"].items()
            if k != version_name}
        if after["active"] == version_name:
            after["active"] = None
        return DeleteColorVersionCommand(
            str(uuid.uuid4()), f"Delete Color Version「{version_name}」",
            stack_id, version_name, state, after)


@dataclass(frozen=True)
class SetStackLockCommand:
    """共享 look 锁定/解锁（显式操作；默认锁定经 lock_shared_stacks）。"""

    command_id: str
    label: str
    stack_id: str
    before: dict
    after: dict

    def apply(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.before:
            raise ValueError("版本表已被其他操作修改，请先撤销后重试。")
        return _with_state(project, self.stack_id, self.after)

    def undo(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.after:
            raise ValueError("版本表已被其他操作修改，无法撤销。")
        return _with_state(project, self.stack_id, self.before)

    @staticmethod
    def create(project: Any, stack_id: str,
               locked: bool) -> "SetStackLockCommand":
        stack = (getattr(project, "color_stacks", {}) or {}).get(stack_id)
        if stack is None:
            raise ValueError(f"调色栈不存在：{stack_id}")
        before = versions_state(project, stack_id)
        after = copy.deepcopy(before)
        after["locked"] = bool(locked)
        return SetStackLockCommand(
            str(uuid.uuid4()),
            f"{'Lock' if locked else 'Unlock'} Color Stack {stack_id[:8]}",
            stack_id, before, after)


def lock_shared_stacks(project: Any) -> Tuple[Any, Tuple[str, ...]]:
    """把全部共享栈置为锁定（共享 look 默认防误改；返回新工程+清单）。"""
    locked_ids: List[str] = []
    updated = project
    for stack_id in shared_stack_ids(project):
        if not versions_state(updated, stack_id)["locked"]:
            updated = SetStackLockCommand.create(
                updated, stack_id, True).apply(updated)
            locked_ids.append(stack_id)
    return updated, tuple(locked_ids)


def compare_versions(project: Any, stack_id: str,
                     name_a: str, name_b: str) -> Dict[str, Any]:
    """M07 · 两命名版本的主调参数差异（版本比较视图数据源）。

    比较两版本载荷的 ``grading_params`` 逐键差异与插件清单差集；
    未知版本名抛 ValueError。纯函数不改工程。
    """
    versions = versions_state(project, stack_id)["versions"]
    if name_a not in versions or name_b not in versions:
        raise ValueError(f"版本不存在：{name_a!r}/{name_b!r}")
    pa, pb = versions[name_a], versions[name_b]
    ga = dict(pa.get("grading_params", {}) or {})
    gb = dict(pb.get("grading_params", {}) or {})
    diffs = {
        key: [ga.get(key), gb.get(key)]
        for key in sorted(set(ga) | set(gb))
        if ga.get(key) != gb.get(key)
    }
    plug_a = set((pa.get("plugin_versions") or {}).keys())
    plug_b = set((pb.get("plugin_versions") or {}).keys())
    return {
        "a": name_a, "b": name_b,
        "diffs": diffs,
        "same": not diffs,
        "plugin_only_a": sorted(plug_a - plug_b),
        "plugin_only_b": sorted(plug_b - plug_a),
    }


# ── 内部 ─────────────────────────────────────────────────────────────


def _stack_from_payload(current: Any, content: dict) -> Any:
    """用版本载荷重建栈（身份字段沿用当前栈；内容字段取版本）。"""
    from src.shared.project.color_stack import ColorStackData
    merged = dict(content)
    for field in _IDENTITY_FIELDS:
        merged.pop(field, None)
    payload = current.to_dict()
    payload.update(merged)
    return ColorStackData.from_dict(payload)


def _missing_plugins(payload: dict) -> List[str]:
    """版本清单中当前注册表缺失的插件（明示不可渲染原因；不丢数据）。

    M07（S09）修复：
    - 注册表用 **Color 插件注册表**（PluginManager——.tglsl/.tai/DCTL
      的身份与 @version 来源），此前误用内置音频/效果 effect_registry
      （get_effect_registry_health 的 missing 是音频效果名空间，与
      Color 插件永不交集，检查恒空）；
    - 注册表探测失败抛 PluginRegistryUnavailableError——调用方标
      unverified，不再 except 吞掉接口错误冒称"无缺失"。
    """
    manifest = payload.get("plugin_versions") or {}
    if not manifest:
        return []
    from src.features.color.core.plugin_manager import get_plugin_manager
    try:
        installed = set(
            (get_plugin_manager().get_all_plugins() or {}).keys())
    except Exception as exc:  # noqa: BLE001 — 统一转类型化错误
        raise PluginRegistryUnavailableError(
            f"Color 插件注册表不可用，无法核对缺失：{exc}") from exc
    return [pid for pid in manifest if pid not in installed]


__all__ = [
    "ActivateColorVersionCommand",
    "DeleteColorVersionCommand",
    "SaveColorVersionCommand",
    "SetStackLockCommand",
    "compare_versions",
    "is_stack_locked",
    "lock_shared_stacks",
    "shared_stack_ids",
    "stack_dependent_clips",
    "versions_state",
]
