"""F4.1 · 场景/机位调色组——组前/片段/组后处理链的组实体与领域命令。

模型（对齐调研结论与计划 F4.1）：
- ``ColorGroupData``：工程级组表条目（成员 = timeline_id+clip_id 引用，
  跨素材/跨时间线；携带 ``pre_group_stack_id`` / ``post_group_stack_id``
  两段组处理）。组 look 与 input/output transform 分开——组栈只含调色
  参数，IDT/ODT 是管线级语义（F5.1 已收口），不在组栈内。
- 处理顺序（``color_chain.resolve_color_chain`` 的权威序）：
  **pre-group → clip 主栈 → clip 微调栈（trim）→ post-group**。
  "共同改 look"= 改组栈（或共享主栈）；"各镜头微调保留" = trim 栈独立
  于组栈存在，组编辑不触碰。
- 领域命令：``SetColorGroupsCommand``（受限事务，冲突先拒绝不半写，
  apply/undo 对称）+ 建组/加成员/移成员/移组四个工厂。全部经
  ``project.with_color_groups`` 单点写回；序列化走
  ``edit_project_io`` 的 settings 键（旧工程无键 = 空组表）。
- 缓存失效：组栈 id 必须进 ``render_cache.build_render_cache_fingerprint``
  的 stack_ids 集合（否则出现"绿缓存"——该函数注释明示此风险）。
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class ColorGroupMember:
    """组成员引用（时间线片段身份；split/复制产生的新片段不自动入组）。"""

    timeline_id: str
    clip_id: str

    def to_dict(self) -> dict:
        return {"timeline_id": self.timeline_id, "clip_id": self.clip_id}

    @staticmethod
    def from_dict(data: Any) -> "ColorGroupMember":
        return ColorGroupMember(
            timeline_id=str(data.get("timeline_id", "") or ""),
            clip_id=str(data.get("clip_id", "") or ""),
        )


@dataclass(frozen=True)
class ColorGroupData:
    """场景/机位调色组（工程级；成员可跨素材、跨时间线）。"""

    group_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Scene Group"
    #: 机位/类别标签（"两类机位" = 两组或组内标签；空 = 未分类）
    camera: str = ""
    members: Tuple[ColorGroupMember, ...] = ()
    #: 组前处理（先于 clip 主栈应用；None = 无）
    pre_group_stack_id: Optional[str] = None
    #: 组后处理（后于 clip 微调栈应用；None = 无）
    post_group_stack_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "name": self.name,
            "camera": self.camera,
            "members": [m.to_dict() for m in self.members],
            "pre_group_stack_id": self.pre_group_stack_id,
            "post_group_stack_id": self.post_group_stack_id,
        }

    @staticmethod
    def from_dict(data: Any) -> "ColorGroupData":
        if not isinstance(data, dict):
            raise ValueError("color group payload must be a dict")
        members = tuple(
            ColorGroupMember.from_dict(m)
            for m in data.get("members", ()) or ())
        return ColorGroupData(
            group_id=str(data.get("group_id") or uuid.uuid4()),
            name=str(data.get("name", "") or "Scene Group"),
            camera=str(data.get("camera", "") or ""),
            members=members,
            pre_group_stack_id=data.get("pre_group_stack_id") or None,
            post_group_stack_id=data.get("post_group_stack_id") or None,
        )


# ── 查询 ─────────────────────────────────────────────────────────────


def color_groups_of(project: Any) -> dict:
    """工程组表（缺字段/旧工程 → 空表；只读视图）。"""
    return dict(getattr(project, "color_groups", None) or {})


def group_for_clip(
    project: Any, timeline_id: str, clip_id: str,
) -> Optional[ColorGroupData]:
    """查片段所属组（一个片段至多一个组——命令层强制唯一归属）。"""
    for group in color_groups_of(project).values():
        for member in group.members:
            if member.clip_id == clip_id and (
                    not member.timeline_id or member.timeline_id == timeline_id
                    or not timeline_id):
                return group
    return None


# ── 领域命令（受限事务：冲突先拒绝，不半写） ────────────────────────


@dataclass(frozen=True)
class SetColorGroupsCommand:
    """组表整体 before/after 事务（AUD-2708 单命令栈同模式）。

    apply 校验工程当前组表与 before 一致（被其他操作改过则整体拒绝），
    再原子替换为 after；undo 对称。子操作（建组/加/移/解组）经工厂
    生成 before/after 对，全部走同一事务路径。
    """

    command_id: str
    label: str
    before: dict[str, dict]
    after: dict[str, dict]

    def apply(self, project: Any) -> Any:
        current = {
            gid: copy.deepcopy(g.to_dict())
            for gid, g in color_groups_of(project).items()}
        if current != self.before:
            raise ValueError(
                "调色组已被其他操作修改，请先撤销该操作后重试。")
        groups = {
            gid: ColorGroupData.from_dict(payload)
            for gid, payload in self.after.items()}
        return project.with_color_groups(groups)

    def undo(self, project: Any) -> Any:
        current = {
            gid: copy.deepcopy(g.to_dict())
            for gid, g in color_groups_of(project).items()}
        if current != self.after:
            raise ValueError(
                "调色组已被其他操作修改，无法撤销。")
        groups = {
            gid: ColorGroupData.from_dict(payload)
            for gid, payload in self.before.items()}
        return project.with_color_groups(groups)

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "command_type": "SetColorGroupsCommand",
            "label": self.label,
            "before": copy.deepcopy(self.before),
            "after": copy.deepcopy(self.after),
        }

    @staticmethod
    def from_dict(data: dict) -> "SetColorGroupsCommand":
        return SetColorGroupsCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Color Groups"),
            before=dict(data.get("before", {}) or {}),
            after=dict(data.get("after", {}) or {}),
        )

    # ── 工厂 ─────────────────────────────────────────────────────────

    @staticmethod
    def _snapshot(project: Any) -> dict:
        return {
            gid: copy.deepcopy(g.to_dict())
            for gid, g in color_groups_of(project).items()}

    @staticmethod
    def create_group(
        project: Any,
        name: str,
        members: Tuple[Tuple[str, str], ...] = (),
        *,
        camera: str = "",
        pre_group_stack_id: Optional[str] = None,
        post_group_stack_id: Optional[str] = None,
    ) -> "SetColorGroupsCommand":
        """建组（成员为 (timeline_id, clip_id) 对）。

        成员必须存在且不属任何既有组（唯一归属）；违反则整体拒绝、
        零写入。
        """
        before = SetColorGroupsCommand._snapshot(project)
        member_objs = tuple(
            ColorGroupMember(t, c) for t, c in members)
        if len({m.clip_id for m in member_objs}) != len(member_objs):
            raise ValueError("建组成员含重复片段。")
        existing = color_groups_of(project)
        for member in member_objs:
            for group in existing.values():
                if any(m.clip_id == member.clip_id for m in group.members):
                    raise ValueError(
                        f"片段 {member.clip_id} 已属组「{group.name}」，"
                        "先移出再入组（唯一归属）。")
        group = ColorGroupData(
            name=name, camera=camera, members=member_objs,
            pre_group_stack_id=pre_group_stack_id,
            post_group_stack_id=post_group_stack_id)
        after = copy.deepcopy(before)
        after[group.group_id] = group.to_dict()
        return SetColorGroupsCommand(
            str(uuid.uuid4()), f"Create Color Group「{name}」",
            before, after)

    @staticmethod
    def add_clips(
        project: Any, group_id: str,
        members: Tuple[Tuple[str, str], ...],
    ) -> "SetColorGroupsCommand":
        """加成员（唯一归属校验同建组）。"""
        before = SetColorGroupsCommand._snapshot(project)
        group = color_groups_of(project).get(group_id)
        if group is None:
            raise ValueError(f"调色组不存在：{group_id}")
        incoming = {c for _t, c in members}
        existing_members = {m.clip_id for m in group.members}
        for other in color_groups_of(project).values():
            if other.group_id == group_id:
                continue
            clash = incoming & {m.clip_id for m in other.members}
            if clash:
                raise ValueError(
                    f"片段 {sorted(clash)[0]} 已属组「{other.name}」"
                    "（唯一归属）。")
        if incoming & existing_members:
            raise ValueError("待加入片段已在组内。")
        new_group = replace(
            group,
            members=group.members + tuple(
                ColorGroupMember(t, c) for t, c in members))
        after = copy.deepcopy(before)
        after[group_id] = new_group.to_dict()
        return SetColorGroupsCommand(
            str(uuid.uuid4()), f"Add {len(members)} clips to「{group.name}」",
            before, after)

    @staticmethod
    def remove_clips(
        project: Any, group_id: str,
        clip_ids: Tuple[str, ...],
    ) -> "SetColorGroupsCommand":
        """移成员（片段保留自身的栈绑定不动）。"""
        before = SetColorGroupsCommand._snapshot(project)
        group = color_groups_of(project).get(group_id)
        if group is None:
            raise ValueError(f"调色组不存在：{group_id}")
        remaining = tuple(
            m for m in group.members if m.clip_id not in set(clip_ids))
        if len(remaining) == len(group.members):
            raise ValueError("待移除片段不在组内。")
        new_group = replace(group, members=remaining)
        after = copy.deepcopy(before)
        after[group_id] = new_group.to_dict()
        return SetColorGroupsCommand(
            str(uuid.uuid4()), f"Remove {len(clip_ids)} clips from「{group.name}」",
            before, after)

    @staticmethod
    def dissolve_group(project: Any, group_id: str) -> "SetColorGroupsCommand":
        """解组（删组实体；成员片段与栈绑定不受影响）。"""
        before = SetColorGroupsCommand._snapshot(project)
        group = color_groups_of(project).get(group_id)
        if group is None:
            raise ValueError(f"调色组不存在：{group_id}")
        after = copy.deepcopy(before)
        after.pop(group_id, None)
        return SetColorGroupsCommand(
            str(uuid.uuid4()), f"Dissolve Color Group「{group.name}」",
            before, after)


__all__ = [
    "ColorGroupData",
    "ColorGroupMember",
    "SetColorGroupsCommand",
    "color_groups_of",
    "group_for_clip",
]


# ── M05.3：组成员编辑策略（split/复制/跨时间线成员——纯函数策略层） ──

def member_policy_for_split(
    group: "ColorGroupData",
    clip_id: str,
    new_clip_ids: "tuple[str, ...]",
) -> "dict[str, Any]":
    """split 产生的新片段入组策略（M05.3 计划句：'split/复制产生的新
    片段不自动入组'——本函数把该契约显式化并给出可审计决策）。

    Returns:
        {"in_group": bool,           # 原 clip 是否在组内
         "new_members_adopted": [],  # 显式为空——split 新片段不自动入组
         "policy": "manual",
         "reason": str}
    """
    in_group = any(m.clip_id == clip_id for m in group.members)
    if not in_group:
        return {"in_group": False, "new_members_adopted": [],
                "policy": "n/a", "reason": "原片段不在组内"}
    return {
        "in_group": True,
        "new_members_adopted": [],  # 契约：不自动入组（人工经 add_clips）
        "policy": "manual",
        "reason": "split/复制产生的新片段不自动入组——人工经"
                  " add_clips 决策（防 split 风暴把组撑爆）",
    }


def member_policy_for_cross_timeline(
    group: "ColorGroupData",
    timeline_id: str,
) -> "dict[str, Any]":
    """跨时间线成员策略：组成员引用 (timeline_id, clip_id) 对——
    组表本身支持跨时间线（ColorGroupMember 带 timeline_id）；本函数
    汇总组的跨时间线分布供编辑入口呈现。"""
    by_timeline: "dict[str, int]" = {}
    for m in group.members:
        by_timeline[m.timeline_id] = by_timeline.get(m.timeline_id, 0) + 1
    return {
        "group_id": group.group_id,
        "total_members": len(group.members),
        "timelines": by_timeline,
        "cross_timeline": len(by_timeline) > 1,
        "querying_timeline": timeline_id,
        "members_in_querying": by_timeline.get(timeline_id, 0),
    }


def group_stack_entry_assignment(
    group: "ColorGroupData",
    scope: str,
    stack_id: "str | None",
) -> "dict[str, Any]":
    """组链段（pre/post）赋值入口的校验视图。

    Args:
        scope: "pre" 或 "post"。
        stack_id: 要绑定的栈 id（None = 解绑）。
    """
    if scope not in ("pre", "post"):
        raise ValueError(f"未知链段：{scope}（支持 pre/post）")
    current = (group.pre_group_stack_id if scope == "pre"
               else group.post_group_stack_id)
    return {
        "group_id": group.group_id,
        "scope": scope,
        "current_stack_id": current,
        "new_stack_id": stack_id,
        "changed": current != stack_id,
    }


__all__ = __all__ + [
    "member_policy_for_split",
    "member_policy_for_cross_timeline",
    "group_stack_entry_assignment",
]
