"""BE-03 · 调色版本 local/remote 域解析服务（纯读侧，无 PyQt 依赖）。

语义决策（对齐达芬奇 Local/Remote Versions 的本项目映射，记录于
docs/plans/2026-11-01-color-clip-versions/IMPLEMENTATION_PLAN.md §3.2）：

**域与 ColorStackData.scope 是不同概念，字段不得混用。**
- 版本域（本模块的 ``local`` / ``remote``）是**片段视角**的版本组织：
  LOCAL = 该时间线片段私有栈的版本表；REMOTE = 该片段素材的共享栈
  （asset_default）的版本表。域只存在于解析结果中，不写入工程 schema。
- ``ColorStackData.scope``（clip_local / asset_default / …）是**栈视角**
  的属主标记。域解析**以被引用栈的 scope 为权威依据**：clip 的
  ``color_stack_id`` 解引用后 scope==asset_default ⇒ 片段正在用 remote
  域；否则（clip_local 等）⇒ local 域。``clip.color_shared`` 仅作交叉
  校验（存在 UpdateClipColorStackCommand 可独立改两值的空隙），二者
  矛盾时以栈 scope 为准。
- 版本表 ``color_stack_versions`` 按 stack_id 键控，天然分域：
  LOCAL 域版本 = 私有栈的表；REMOTE 域版本 = 共享栈的表。**不新增
  第二套存储、不给版本表加域字段**（旧工程无键 = 空表，零迁移可读）。

**换域 = 切换片段引用，不是改 scope 字段。** 加载另一域的版本时，
先用既有域切换入口（Use Shared Grade / Make Local Grade Copy 所在的
controller 路径：``UpdateClipColorStackCommand`` 时间线命令 + 项目层
建栈命令）把片段切到该域的栈，再在该栈上执行版本命令。禁止只改
UI 显示或直接改栈的 scope 盖章。片段微调栈 ``color_trim_stack_id``
在任何域操作中保持不动。

**域栈解析规则：**
- LOCAL：① clip 当前引用的栈若 scope==clip_local 即为本地域栈
  且非空属主盖章与当前 timeline/clip 匹配（legacy 无盖章按引用归属）；
  split 继承其他片段私有栈时不能借用原片段版本；② 当前引用不适用时，
  按 ownership 盖章（scope==clip_local 且 timeline_id+clip_id 属主匹配）
  找回其遗留私有栈；均无 ⇒ 空域（CREATE 时由宿主按策略建栈）。
- REMOTE：素材共享栈 = scope==asset_default 且 asset_id 匹配的栈
  （线性扫描第一个）。**不得用** ``EditProject.get_color_stack_for_asset``
  ——它不查 scope，会误中同素材的 clip_local 栈。无共享栈 ⇒ 空域。

服务只做解析与判定，不执行命令、不改工程；写入路径由 controller
（WIRE-01）组合本模块解析结果与既有命令完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Tuple

from src.features.edit.core.color_versions import versions_state

#: 与 ColorStackData.scope 的 asset_default 值一致（schema_v1_spec 枚举）
_ASSET_DEFAULT_SCOPE = "asset_default"
_CLIP_LOCAL_SCOPE = "clip_local"

#: live/快照内容比对时排除的键：身份字段（激活互换不变）、时间戳
#: （参数未变时不计入"已修改"）、版本表附属清单与诊断快照
_CONTENT_COMPARE_EXCLUDE = frozenset({
    "stack_id", "asset_id", "scope", "source_kind", "timeline_id",
    "clip_id", "created_at", "modified_at", "schema_version",
    "plugin_versions", "saved_at", "plugin_check_unverified",
    "plugin_diagnostics",
})


class VersionDomain(str, Enum):
    """片段视角的版本域（与 ColorStackData.scope 分离；值为 UI 域值）。"""

    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True)
class ClipStackBinding:
    """片段当前调色绑定（解析结果快照，不含版本表内容）。"""

    timeline_id: str
    clip_id: str
    asset_id: str
    stack_id: str | None          # 片段主栈引用（trim 栈不在此列）
    stack_scope: str              # 被引用栈的 scope；无栈为 ""
    color_shared: bool            # 交叉校验用标志（非权威）
    domain: VersionDomain | None  # 权威域判定；无栈/未知 scope 为 None
    trim_stack_id: str | None     # 微调栈（域操作不得触碰，仅供审计）


def find_clip_ref(project: Any, timeline_id: str,
                  clip_id: str) -> Tuple[str, Any] | None:
    """(track_id, clip) 定位；不存在返回 None。"""
    timelines = getattr(project, "timelines", None) or {}
    timeline = timelines.get(str(timeline_id)) if isinstance(
        timelines, dict) else None
    if timeline is None:
        return None
    for track in getattr(timeline, "video_tracks", ()) or ():
        for clip in getattr(track, "clips", ()) or ():
            if str(getattr(clip, "clip_id", "")) == str(clip_id):
                return str(getattr(track, "track_id", "")), clip
    return None


def resolve_clip_binding(project: Any, timeline_id: str,
                         clip_id: str) -> ClipStackBinding | None:
    """解析片段当前绑定与权威域判定（scope 优先，color_shared 交叉）。"""
    ref = find_clip_ref(project, timeline_id, clip_id)
    if ref is None:
        return None
    _track_id, clip = ref
    stack_id = getattr(clip, "color_stack_id", None) or None
    stack = (getattr(project, "color_stacks", {}) or {}).get(
        stack_id) if stack_id else None
    stack_scope = str(getattr(stack, "scope", "") or "") if stack else ""
    if not stack:
        domain = None
    elif stack_scope == _ASSET_DEFAULT_SCOPE:
        domain = VersionDomain.REMOTE
    else:
        # clip_local（含 legacy 无盖章裸栈与孤立 asset 盖章媒体池栈）
        domain = VersionDomain.LOCAL
    return ClipStackBinding(
        timeline_id=str(timeline_id),
        clip_id=str(clip_id),
        asset_id=str(getattr(clip, "asset_id", "") or ""),
        stack_id=str(stack_id) if stack_id else None,
        stack_scope=stack_scope,
        color_shared=bool(getattr(clip, "color_shared", False)),
        domain=domain,
        trim_stack_id=(str(getattr(clip, "color_trim_stack_id", None) or "")
                       or None),
    )


def _trim_stack_ids(project: Any) -> set:
    """全部片段微调栈引用集合（域解析不得命中——trim 栈是主栈之上的
    单镜头微调层，不属于任何版本域）。"""
    ids = set()
    for _timeline_id, clip in _iter_project_clips(project):
        trim = getattr(clip, "color_trim_stack_id", None)
        if trim:
            ids.add(str(trim))
    return ids


def _iter_project_clips(project: Any):
    timelines = getattr(project, "timelines", None) or {}
    timeline_iter = (timelines.values() if isinstance(timelines, dict)
                     else ([] if not timelines else list(timelines)))
    for timeline in timeline_iter:
        if timeline is None:
            continue
        for track in getattr(timeline, "video_tracks", ()) or ():
            for clip in getattr(track, "clips", ()) or ():
                yield str(getattr(timeline, "timeline_id", "")), clip


def resolve_local_stack_id(project: Any, timeline_id: str,
                           clip_id: str) -> str | None:
    """LOCAL 域栈解析：当前引用的属主匹配栈 → 属主盖章遗留栈 → 无。

    微调栈（被任何片段的 color_trim_stack_id 引用）即使在属主盖章上
    匹配也不得命中——trim 栈不属于版本域。
    """
    trim_ids = _trim_stack_ids(project)
    binding = resolve_clip_binding(project, timeline_id, clip_id)
    if (binding is not None and binding.stack_scope == _CLIP_LOCAL_SCOPE
            and binding.stack_id not in trim_ids):
        stack = (getattr(project, "color_stacks", {}) or {}).get(binding.stack_id)
        owner_clip = str(getattr(stack, "clip_id", "") or "")
        owner_timeline = str(getattr(stack, "timeline_id", "") or "")
        # Split/duplicate can inherit a private stack reference. It remains
        # owned by the source clip until this clip gets its own local stack.
        # Legacy unstamped references continue to resolve by reference.
        if ((not owner_clip or owner_clip == str(clip_id))
                and (not owner_timeline or owner_timeline == str(timeline_id))):
            return binding.stack_id
    # 片段已切共享栈：按 ownership 盖章找回遗留私有栈
    for stack in (getattr(project, "color_stacks", {}) or {}).values():
        if str(getattr(stack, "stack_id", "")) in trim_ids:
            continue
        if (str(getattr(stack, "scope", "") or "") == _CLIP_LOCAL_SCOPE
                and str(getattr(stack, "timeline_id", "") or "")
                == str(timeline_id)
                and str(getattr(stack, "clip_id", "") or "")
                == str(clip_id)):
            return str(stack.stack_id)
    return None


def resolve_remote_stack_id(project: Any, asset_id: str) -> str | None:
    """REMOTE 域栈解析：素材共享栈（scope 校验；不用
    get_color_stack_for_asset——它不查 scope 会误中 clip_local 栈）。"""
    for stack in (getattr(project, "color_stacks", {}) or {}).values():
        if (str(getattr(stack, "scope", "") or "") == _ASSET_DEFAULT_SCOPE
                and str(getattr(stack, "asset_id", "") or "")
                == str(asset_id)):
            return str(stack.stack_id)
    return None


def resolve_domain_stack_id(project: Any, timeline_id: str, clip_id: str,
                            asset_id: str,
                            domain: VersionDomain) -> str | None:
    """按域解析栈 id（空域返回 None；宿主决定建栈或保持空域）。"""
    if domain == VersionDomain.LOCAL:
        return resolve_local_stack_id(project, timeline_id, clip_id)
    return resolve_remote_stack_id(project, asset_id)


def domain_in_use(project: Any, timeline_id: str, clip_id: str,
                  domain: VersionDomain) -> bool:
    """该域是否为片段当前实际使用域（✓ 标记依据；两域各自记住 active，
    仅 in_use 域显示 ✓）。"""
    binding = resolve_clip_binding(project, timeline_id, clip_id)
    if binding is None or binding.domain != domain:
        return False
    resolved = resolve_domain_stack_id(
        project, timeline_id, clip_id, binding.asset_id, domain)
    return resolved is not None and binding.stack_id == resolved


def shared_binding_conflict(project: Any, timeline_id: str,
                            clip_id: str) -> str:
    """color_shared 标志与栈 scope 矛盾时的诊断（不改变判定，仅供
    unavailable_reason/日志呈现；空串=无矛盾）。"""
    binding = resolve_clip_binding(project, timeline_id, clip_id)
    if binding is None or not binding.stack_id:
        return ""
    if binding.color_shared and binding.domain == VersionDomain.LOCAL:
        return "color_shared=True 但被引用栈 scope 非 asset_default（以栈 scope 为准）"
    if (not binding.color_shared
            and binding.domain == VersionDomain.REMOTE):
        return "color_shared=False 但被引用栈为 asset_default（以栈 scope 为准）"
    return ""


def _content_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """排除身份/时间戳/清单后的内容视图（live ↔ 快照比对基准）。"""
    return {k: v for k, v in (payload or {}).items()
            if k not in _CONTENT_COMPARE_EXCLUDE}


def live_matches_active_version(project: Any, stack_id: str) -> bool:
    """live 栈内容是否与 active 版本快照一致（live_modified 的宿主计算）。

    - 无版本表/无 active：False（live 内容不属于任何已保存版本，
      UI 呈现 live_modified=True 语义——保存/切换前须确认）。
    - 比对排除身份字段与时间戳：激活互换不动身份；仅 modified_at 推进
      而参数未变不视为修改（避免误报）。
    """
    state = versions_state(project, stack_id)
    active = state["active"]
    payload = state["versions"].get(active) if active else None
    stack = (getattr(project, "color_stacks", {}) or {}).get(stack_id)
    if payload is None or stack is None:
        return False
    return _content_view(payload) == _content_view(stack.to_dict())


def domain_snapshot(project: Any, timeline_id: str, clip_id: str,
                    asset_id: str,
                    domain: VersionDomain) -> Dict[str, Any]:
    """域解析汇总（宿主组装 ClipVersionCollection 的数据源）。

    返回 {stack_id, stack_exists, domain_in_use, versions_state,
    live_matches_active, shared_binding_conflict}。纯读；无栈时
    versions_state 为空表结构。
    """
    stack_id = resolve_domain_stack_id(
        project, timeline_id, clip_id, asset_id, domain)
    return {
        "domain": domain,
        "stack_id": stack_id,
        "stack_exists": stack_id is not None,
        "domain_in_use": domain_in_use(project, timeline_id, clip_id, domain),
        "versions_state": (versions_state(project, stack_id)
                           if stack_id else {"active": None,
                                             "locked": False,
                                             "versions": {},
                                             "default_version": None}),
        "live_matches_active": (live_matches_active_version(project, stack_id)
                                if stack_id else False),
        "shared_binding_conflict": shared_binding_conflict(
            project, timeline_id, clip_id),
    }


__all__ = [
    "ClipStackBinding",
    "VersionDomain",
    "domain_in_use",
    "domain_snapshot",
    "find_clip_ref",
    "live_matches_active_version",
    "resolve_clip_binding",
    "resolve_domain_stack_id",
    "resolve_local_stack_id",
    "resolve_remote_stack_id",
    "shared_binding_conflict",
]
