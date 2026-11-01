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

from src.features.edit.core.commands import ProjectTransactionResult

logger = logging.getLogger(__name__)

#: 栈身份字段——版本互换时保留（引用稳定性）
_IDENTITY_FIELDS = (
    "stack_id", "asset_id", "scope", "source_kind", "timeline_id",
    "clip_id", "created_at",
)

# These describe a named snapshot, never live ColorStack content.
_VERSION_METADATA_FIELDS = frozenset({
    "plugin_versions", "saved_at", "plugin_check_unverified",
    "plugin_diagnostics",
})

#: 版本名长度上限（与片段版本 UI 的 128 字符策略统一；后端强制执行，
#: 不依赖前端校验）
_MAX_VERSION_NAME_LEN = 128


def validate_version_name(name: Any) -> str:
    """后端统一版本名校验（BE-02）：去首尾空白、非空、≤128 字符、
    禁控制字符。返回规范化名称；非法抛 ValueError。

    与 clip_version_ui 的前端约束一致，但保存/改名/复制命令各自再执行
    一次——前端校验不是事务边界。
    """
    normalized = str(name).strip()
    if not normalized:
        raise ValueError("版本名不能为空。")
    if len(normalized) > _MAX_VERSION_NAME_LEN:
        raise ValueError(
            f"版本名不能超过 {_MAX_VERSION_NAME_LEN} 个字符。")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        raise ValueError("版本名不能包含控制字符。")
    return normalized


#: BE-04 · 默认版本名（空域初始化时创建的受保护版本；非本地化——
#: 与 UI 的 "Version {number}" 建议名一致，Resolve 语义的 Default 位）
DEFAULT_VERSION_NAME = "Version 1"


def versions_state(project: Any, stack_id: str) -> dict:
    """该栈的版本表（{"active", "locked", "versions", "default_version"}；
    缺省=空表。``default_version`` 为 BE-04 新增键：指向初始化时创建的
    受保护版本（DELETE_ALL 保留它）；旧工程/未初始化表为 None——此时
    宿主不得开放 DELETE_ALL（先迁移，见 promote_default_version）。"""
    table = (getattr(project, "color_stack_versions", None) or {}).get(
        stack_id) or {}
    state = copy.deepcopy(table)
    state.update({
        "active": table.get("active"),
        "locked": bool(table.get("locked", False)),
        "versions": dict(state.get("versions", {}) or {}),
        "default_version": table.get("default_version"),
    })
    return state


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


def version_plugin_diagnostics(payload: dict) -> Dict[str, Any]:
    """DIAG-01 · 统一插件诊断字段：``{"missing": [...], "unverified": bool}``。

    消费方（片段版本宿主、M07 旧面板）统一调用本函数，不再各自猜
    ``missing_plugins_hint`` 之类的无来源键。保存时的 plugin_diagnostics
    是历史快照；插件可能已安装/卸载，因此每次读取均核对当前注册表，
    不修改保存载荷。注册表不可用时 ``unverified=True``（明确"未核对"，
    不把历史结果冒充为当前可渲染性）。
    """
    missing, unverified = _missing_or_unverified(payload or {})
    return {"missing": list(missing), "unverified": bool(unverified)}

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
    table[stack_id] = copy.deepcopy(state)
    table[stack_id].update({
        "active": state.get("active"),
        "locked": bool(state.get("locked", False)),
        "versions": dict(table[stack_id].get("versions", {}) or {}),
        "default_version": state.get("default_version"),
    })
    return project.with_color_stack_versions(table)


def _restore_version_entry(project: Any, stack_id: str,
                           entry: dict | None) -> Any:
    """Undo restores the original table shape, including an absent entry."""
    table = dict(getattr(project, "color_stack_versions", None) or {})
    if entry is None:
        table.pop(stack_id, None)
    else:
        table[stack_id] = copy.deepcopy(entry)
    return project.with_color_stack_versions(table)


def _raw_version_entry(project: Any, stack_id: str) -> dict | None:
    return copy.deepcopy((getattr(project, "color_stack_versions", None) or {}).get(stack_id))


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
    _before_entry: dict | None = None

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
        return _restore_version_entry(project, self.stack_id, self._before_entry)

    @staticmethod
    def create(project: Any, stack_id: str, version_name: str,
               *, replace: bool = False) -> "SaveColorVersionCommand":
        stack = (getattr(project, "color_stacks", {}) or {}).get(stack_id)
        if stack is None:
            raise ValueError(f"调色栈不存在：{stack_id}")
        version_name = validate_version_name(version_name)
        before = versions_state(project, stack_id)
        if version_name in before["versions"] and not replace:
            raise ValueError(f"版本「{version_name}」已存在（replace=True 覆盖）。")
        payload = copy.deepcopy(stack.to_dict())
        payload["plugin_versions"] = _plugin_manifest(stack)
        payload["saved_at"] = time.time()
        missing, unverified = _missing_or_unverified(payload)
        # DIAG-01：统一诊断字段随载荷持久化（保存时快照；读取方可经
        # version_plugin_diagnostics 现算覆盖）；unverified 不冒称无缺失。
        payload["plugin_diagnostics"] = {
            "missing": list(missing), "unverified": bool(unverified)}
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
            stack_id, version_name, before, after, missing,
            _raw_version_entry(project, stack_id))


@dataclass(frozen=True)
class ActivateColorVersionCommand:
    """激活命名版本（内容互换；栈身份保留；单步可撤销=回退/比较）。

    BE-01 协议修复：``apply`` 返回项目栈正典 ``ProjectTransactionResult``
    （project=新工程，updated_command=含激活前/后快照的命令实例）。
    此前返回裸 ``(new_project, applied)`` 二元组，ProjectCommandStack
    只识别 Result，把二元组交给 dataclasses.replace 直接 TypeError
    （2026-11-01 实测阻塞）；面板 fake executor 手工拆包不能证明真实
    栈可激活。undo 栈必须持有 applied command——激活前内容与 active
    指针都在其中。
    """

    command_id: str
    label: str
    stack_id: str
    version_name: str
    _before_stack: Any = None    # apply 时填充的完整旧栈
    _after_stack: Any = None
    _before_state: dict = None
    _after_state: dict = None
    missing_plugins: Tuple[str, ...] = ()

    def apply(self, project: Any) -> "ProjectTransactionResult":
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
        return ProjectTransactionResult(
            project=updated, updated_command=applied)

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
class RenameColorVersionCommand:
    """BE-02 · 原子重命名版本表键。

    同一版本表内换名：payload、插件清单与 ``saved_at`` 逐字节保留，
    版本顺序不变（键在原位换名），实际调色与 live 栈不受影响；
    active/default_version 指向旧名时同步改为新名。**不是**
    "保存 live 栈再删旧名"——那会改错快照内容（live ≠ 旧版本）。
    创建时快照 before/after，apply 期校验当前表，stale 对话框不得
    覆盖新修改。
    """

    command_id: str
    label: str
    stack_id: str
    old_name: str
    new_name: str
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
    def create(project: Any, stack_id: str, old_name: str,
               new_name: str) -> "RenameColorVersionCommand":
        old_name = str(old_name).strip()
        before = versions_state(project, stack_id)
        if old_name not in before["versions"]:
            raise ValueError(f"版本「{old_name}」不存在。")
        new_name = validate_version_name(new_name)
        if new_name == old_name:
            raise ValueError("新版本名与原名称相同。")
        if new_name in before["versions"]:
            raise ValueError(f"版本「{new_name}」已存在。")
        # 原位换名保序：旧键位置放入新键，payload 引用不变
        versions = {}
        for key, payload in before["versions"].items():
            versions[new_name if key == old_name else key] = payload
        after = copy.deepcopy(before)
        after.update({
            "active": (new_name if before["active"] == old_name
                       else before["active"]),
            "locked": before["locked"],
            "versions": versions,
            "default_version": (
                new_name if before["default_version"] == old_name
                else before["default_version"]),
        })
        return RenameColorVersionCommand(
            str(uuid.uuid4()),
            f"Rename Color Version「{old_name}」→「{new_name}」",
            stack_id, old_name, new_name, before, after)


@dataclass(frozen=True)
class DuplicateColorVersionCommand:
    """BE-02 · 深拷贝指定版本快照为新名。

    复制**指定快照**（可复制非当前版本；``SaveColorVersionCommand``
    保存的是 live 栈，不能替代）。副本为源 payload 的深拷贝（含插件
    清单与 saved_at 溯源），追加到表尾；保留原 active 与当前 live 栈，
    **不自动激活副本**（UI/后端约定，见计划 §4.5 DUPLICATE）。
    """

    command_id: str
    label: str
    stack_id: str
    source_name: str
    new_name: str
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
    def create(project: Any, stack_id: str, source_name: str,
               new_name: str) -> "DuplicateColorVersionCommand":
        before = versions_state(project, stack_id)
        source_name = str(source_name).strip()
        if source_name not in before["versions"]:
            raise ValueError(f"版本「{source_name}」不存在。")
        new_name = validate_version_name(new_name)
        if new_name in before["versions"]:
            raise ValueError(f"版本「{new_name}」已存在。")
        after = copy.deepcopy(before)
        after["versions"][new_name] = copy.deepcopy(
            before["versions"][source_name])
        return DuplicateColorVersionCommand(
            str(uuid.uuid4()),
            f"Duplicate Color Version「{source_name}」→「{new_name}」",
            stack_id, source_name, new_name, before, after)


@dataclass(frozen=True)
class CopyColorVersionCommand:
    """WIRE-01 · 跨栈复制版本快照（COPY_TO_SCOPE 核心）。

    从 ``source_stack_id`` 的 ``source_name`` 深拷贝 payload 到
    ``dest_stack_id`` 的新名——两栈各自的版本表独立（LOCAL/REMOTE 域
    即不同栈）。保留双方 active、默认标记与 live 栈，不切换使用域、
    不自动激活副本（§4.5 COPY_TO_SCOPE）。apply 期双表 stale 校验：
    源表只读（快照在 create 期取得），目标表原子替换。undo 只还原
    目标表（源表从未被写）。
    """

    command_id: str
    label: str
    source_stack_id: str
    dest_stack_id: str
    source_name: str
    new_name: str
    source_before: dict
    dest_before: dict
    dest_after: dict

    def apply(self, project: Any) -> Any:
        if versions_state(project, self.source_stack_id) != \
                self.source_before:
            raise ValueError("源版本表已被其他操作修改，请先撤销后重试。")
        if versions_state(project, self.dest_stack_id) != self.dest_before:
            raise ValueError("目标版本表已被其他操作修改，请先撤销后重试。")
        return _with_state(project, self.dest_stack_id, self.dest_after)

    def undo(self, project: Any) -> Any:
        if versions_state(project, self.dest_stack_id) != self.dest_after:
            raise ValueError("目标版本表已被其他操作修改，无法撤销。")
        return _with_state(project, self.dest_stack_id, self.dest_before)

    @staticmethod
    def create(project: Any, source_stack_id: str, source_name: str,
               dest_stack_id: str,
               new_name: str) -> "CopyColorVersionCommand":
        source_before = versions_state(project, source_stack_id)
        if source_name not in source_before["versions"]:
            raise ValueError(f"版本「{source_name}」不存在。")
        dest_before = versions_state(project, dest_stack_id)
        new_name = validate_version_name(new_name)
        if new_name in dest_before["versions"]:
            raise ValueError(f"版本「{new_name}」已存在。")
        dest_after = copy.deepcopy(dest_before)
        dest_after["versions"][new_name] = copy.deepcopy(
            source_before["versions"][source_name])
        return CopyColorVersionCommand(
            str(uuid.uuid4()),
            f"Copy Color Version「{source_name}」→"
            f"[{dest_stack_id[:8]}]「{new_name}」",
            source_stack_id, dest_stack_id, source_name, new_name,
            source_before, dest_before, dest_after)


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
class InitializeDefaultVersionCommand:
    """BE-04 · 空域初始化默认版本（表级原子命令）。

    初始快照策略（已记录于计划 §4.5 CREATE）：默认版本捕获**初始化时
    该栈的当前 live 内容**（与 Resolve 语义一致——首个版本即起始调色，
    不是"未调色重置"）。表为空（无 versions）才可执行；产出
    ``default_version=DEFAULT_VERSION_NAME`` 受保护键。旧工程零迁移：
    未初始化表 default_version=None，宿主对无默认版本的域不得开放
    DELETE_ALL（先经本命令或 promote_default_version 迁移）。
    """

    command_id: str
    label: str
    stack_id: str
    before: dict
    after: dict
    _before_entry: dict | None = None

    def apply(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.before:
            raise ValueError("版本表已被其他操作修改，请先撤销后重试。")
        return _with_state(project, self.stack_id, self.after)

    def undo(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.after:
            raise ValueError("版本表已被其他操作修改，无法撤销。")
        return _restore_version_entry(project, self.stack_id, self._before_entry)

    @staticmethod
    def create(project: Any, stack_id: str,
               default_name: str = DEFAULT_VERSION_NAME
               ) -> "InitializeDefaultVersionCommand":
        stack = (getattr(project, "color_stacks", {}) or {}).get(stack_id)
        if stack is None:
            raise ValueError(f"调色栈不存在：{stack_id}")
        before = versions_state(project, stack_id)
        if before["versions"]:
            raise ValueError("版本表非空，无需初始化默认版本。")
        default_name = validate_version_name(default_name)
        payload = copy.deepcopy(stack.to_dict())
        payload["plugin_versions"] = _plugin_manifest(stack)
        payload["saved_at"] = time.time()
        missing, unverified = _missing_or_unverified(payload)
        payload["plugin_diagnostics"] = {
            "missing": list(missing), "unverified": bool(unverified)}
        if unverified:
            payload["plugin_check_unverified"] = True
        after = copy.deepcopy(before)
        after.update({
            "active": default_name,
            "locked": before["locked"],
            "versions": {default_name: payload},
            "default_version": default_name,
        })
        return InitializeDefaultVersionCommand(
            str(uuid.uuid4()),
            f"Initialize Default Color Version「{default_name}」",
            stack_id, before, after, _raw_version_entry(project, stack_id))


@dataclass(frozen=True)
class DeleteAllVersionsCommand:
    """BE-04 · 删除全部非默认版本并加载默认版本（单原子事务）。

    表级 + 栈级一起变更，现有逐个 Delete 不足以表达（计划 §4.5
    DELETE_ALL）：保留 ``default_version`` 指向的快照，删除其余全部，
    active 指向默认版本，并把默认版本内容**换入 live 栈**（内容互换，
    栈身份保留——与 ActivateColorVersionCommand 同语义）。undo 整体
    恢复版本表与 live 栈。apply 返回 ``ProjectTransactionResult``
    （undo 栈必须持有含激活前快照的命令实例）。无默认版本的旧表
    在 create 即拒绝（宿主先迁移）。
    """

    command_id: str
    label: str
    stack_id: str
    default_name: str
    before: dict                    # create 期快照（apply 期 stale 校验）
    after: dict
    _before_stack: Any = None       # apply 期填充
    _after_stack: Any = None

    def apply(self, project: Any) -> "ProjectTransactionResult":
        current_state = versions_state(project, self.stack_id)
        if current_state != self.before:
            raise ValueError("版本表已被其他操作修改，请先撤销后重试。")
        stacks = dict(getattr(project, "color_stacks", {}) or {})
        current = stacks.get(self.stack_id)
        if current is None:
            raise ValueError(f"调色栈不存在：{self.stack_id}")
        payload = self.after["versions"][self.default_name]
        content = {k: v for k, v in payload.items()
                   if k not in ("plugin_versions", "saved_at")}
        restored = _stack_from_payload(current, content)
        stacks[self.stack_id] = restored
        updated = project.with_color_stacks(stacks)
        updated = _with_state(updated, self.stack_id, self.after)
        applied = DeleteAllVersionsCommand(
            command_id=self.command_id, label=self.label,
            stack_id=self.stack_id, default_name=self.default_name,
            before=self.before, after=self.after,
            _before_stack=copy.deepcopy(current),
            _after_stack=copy.deepcopy(restored))
        return ProjectTransactionResult(
            project=updated, updated_command=applied)

    def undo(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.after:
            raise ValueError("版本表已被其他操作修改，无法撤销。")
        if self._before_stack is None:
            raise ValueError("命令未 apply，无法撤销。")
        stacks = dict(getattr(project, "color_stacks", {}) or {})
        stacks[self.stack_id] = copy.deepcopy(self._before_stack)
        updated = project.with_color_stacks(stacks)
        return _with_state(updated, self.stack_id, self.before)

    @staticmethod
    def create(project: Any,
               stack_id: str) -> "DeleteAllVersionsCommand":
        before = versions_state(project, stack_id)
        default_name = before["default_version"]
        if not default_name or default_name not in before["versions"]:
            raise ValueError(
                "该栈没有受保护的默认版本（旧数据需先迁移），"
                "无法执行删除全部。")
        if len(before["versions"]) <= 1:
            raise ValueError("没有可删除的其他版本。")
        payload = before["versions"][default_name]
        after = copy.deepcopy(before)
        after.update({
            "active": default_name,
            "locked": before["locked"],
            "versions": {default_name: payload},
            "default_version": default_name,
        })
        return DeleteAllVersionsCommand(
            str(uuid.uuid4()),
            f"Delete All Other Color Versions（keep「{default_name}」）",
            stack_id, default_name, before, after)


def promote_default_version(project: Any, stack_id: str) -> Tuple[
        Any, "InitializeDefaultVersionCommand | _ReplaceDefaultMarkerCommand"
        " | None"]:
    """BE-04 · 旧数据迁移：无 default_version 键的表补默认版本标记。

    迁移策略（计划 §6 BE-04"初始化策略可迁移"）：优先提升**当前 active**
    版本为默认（用户当前所见的调色保持受保护，DELETE_ALL 后加载的
    即当前内容）；无 active 时按 ``saved_at`` 最早者（最接近"起始版本"）。
    空表则经 ``InitializeDefaultVersionCommand`` 以 live 内容初始化。
    返回 (new_project, command)；已迁移表返回原样 (project, None)。
    command 需由调用方经真实命令栈提交（本函数不直接执行）。
    """
    state = versions_state(project, stack_id)
    if state["default_version"]:
        return project, None
    if not state["versions"]:
        cmd = InitializeDefaultVersionCommand.create(project, stack_id)
        return cmd.apply(project), cmd
    candidates = state["versions"]
    chosen = state["active"]
    if not chosen or chosen not in candidates:
        chosen = min(
            candidates,
            key=lambda name: (candidates[name].get("saved_at") or 0.0,
                              name))
    after = dict(state)
    after["default_version"] = chosen
    after["versions"] = dict(state["versions"])
    cmd = _ReplaceDefaultMarkerCommand.create(stack_id, state, after, chosen)
    return cmd.apply(project), cmd


@dataclass(frozen=True)
class _ReplaceDefaultMarkerCommand:
    """内部 · 仅改 default_version 标记的表级命令（迁移用）。"""

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
    def create(stack_id: str, before: dict, after: dict,
               chosen: str) -> "_ReplaceDefaultMarkerCommand":
        return _ReplaceDefaultMarkerCommand(
            str(uuid.uuid4()),
            f"Promote Default Color Version「{chosen}」",
            stack_id, before, after)


def create_version_with_default(project: Any, stack_id: str,
                                version_name: str
                                ) -> "ProjectMacroCommand":
    """BE-04/WIRE-01 · 空域首个 CREATE：同一事务初始化默认版本 + 保存
    用户命名版本（计划 §4.5 CREATE："若为空域，在同一事务初始化默认
    版本"）。

    子命令在**中间态工程**上构建（init 先纯函数 apply 得中间态，再在
    中间态上 create Save——保证 Save 的 before 快照与宏 apply 顺序一致，
    不触发 stale 校验冲突）。非空域请直接用 SaveColorVersionCommand。
    """
    from src.features.edit.core.commands import ProjectMacroCommand

    init = InitializeDefaultVersionCommand.create(project, stack_id)
    intermediate = init.apply(project)
    save = SaveColorVersionCommand.create(
        intermediate, stack_id, version_name)
    return ProjectMacroCommand.create(
        f"Create Color Version「{version_name}」", [init, save])


@dataclass(frozen=True)
class SetStackLockCommand:
    """共享 look 锁定/解锁（显式操作；默认锁定经 lock_shared_stacks）。"""

    command_id: str
    label: str
    stack_id: str
    before: dict
    after: dict
    _before_entry: dict | None = None

    def apply(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.before:
            raise ValueError("版本表已被其他操作修改，请先撤销后重试。")
        return _with_state(project, self.stack_id, self.after)

    def undo(self, project: Any) -> Any:
        if versions_state(project, self.stack_id) != self.after:
            raise ValueError("版本表已被其他操作修改，无法撤销。")
        return _restore_version_entry(project, self.stack_id, self._before_entry)

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
            stack_id, before, after, _raw_version_entry(project, stack_id))


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


def version_restorable(project: Any, stack_id: str, version_name: str) -> bool:
    """WIRE-01 · 版本载荷可重建性预校验（跨域 LOAD 前置，审查 S3）。

    用与激活一致的重建路径（身份字段保留 + 内容字段覆盖）试跑一次；
    载荷畸形（from_dict 校验失败等）返回 False——宿主在切换片段引用
    **之前**调用，避免"引用已切、内容加载失败"的半提交。
    """
    stack = (getattr(project, "color_stacks", {}) or {}).get(stack_id)
    payload = versions_state(project, stack_id)["versions"].get(version_name)
    if stack is None or payload is None:
        return False
    content = {k: v for k, v in payload.items()
               if k not in ("plugin_versions", "saved_at")}
    try:
        _stack_from_payload(stack, content)
    except Exception:  # noqa: BLE001 — 任何重建失败都视为不可加载
        logger.warning("Version %s/%s payload not restorable",
                       stack_id, version_name, exc_info=True)
        return False
    return True


# ── 内部 ─────────────────────────────────────────────────────────────


def _stack_from_payload(current: Any, content: dict) -> Any:
    """用版本载荷重建栈（身份字段沿用当前栈；内容字段取版本）。"""
    from src.shared.project.color_stack import ColorStackData
    # Optional empty fields are omitted by to_dict(). Building on the current
    # stack would retain the other version's layers, RAW/denoise and curves.
    # Restore snapshot content in full; from_dict supplies schema defaults.
    payload = copy.deepcopy({
        key: value for key, value in content.items()
        if key not in _VERSION_METADATA_FIELDS
    })
    current_payload = current.to_dict()
    for field in _IDENTITY_FIELDS:
        payload[field] = copy.deepcopy(current_payload.get(field))
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
    "DEFAULT_VERSION_NAME",
    "ActivateColorVersionCommand",
    "CopyColorVersionCommand",
    "DeleteAllVersionsCommand",
    "DeleteColorVersionCommand",
    "DuplicateColorVersionCommand",
    "InitializeDefaultVersionCommand",
    "RenameColorVersionCommand",
    "SaveColorVersionCommand",
    "SetStackLockCommand",
    "compare_versions",
    "create_version_with_default",
    "is_stack_locked",
    "lock_shared_stacks",
    "promote_default_version",
    "shared_stack_ids",
    "stack_dependent_clips",
    "validate_version_name",
    "version_plugin_diagnostics",
    "version_restorable",
    "versions_state",
]
