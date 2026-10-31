# DORMANT 注记已解除（P1-01 生产接线，2026-08-28）：
# 媒体池多选创建组 + 角度面板/键盘 1-9 实时切换已接入生产
# （组定义挂成员素材 metadata，见 METADATA_ASSET_GROUP_KEY）。
"""Multicam editing support — data model and switch helpers.

A multicam clip groups several synchronized camera angles so an editor can
switch between them in real time; each switch records a hard cut on the
target track (split the clip and point the right half at a different angle).

This module provides the pure data model (no PyQt). The host clip references
a ``MulticamAngleGroup`` via ``clip.metadata['multicam_group_id']``; the group
itself is stored separately (e.g. on the project) so multiple clips can share
one set of angles.

Model
-----
- ``MulticamAngle`` — one camera: ``angle_id``, ``asset_id``, ``sync_offset``
  (frames to add/subtract when mapping timeline → source for this angle),
  ``label``.
- ``MulticamAngleGroup`` — ordered set of angles plus the default angle.

The actual cut operation lives in ``commands.py`` (``SwitchMulticamAngleCommand``)
and reuses the existing ``split_clip`` machinery.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Optional

__all__ = [
    "MulticamAngle",
    "MulticamAngleGroup",
    "METADATA_MULTICAM_KEY",
    "get_multicam_group_id",
    "attach_multicam_group",
]


# Metadata key used on a ClipInstance to reference its multicam group.
METADATA_MULTICAM_KEY = "multicam_group_id"

# F2.5：多机位音频切换策略合法值
MULTICAM_AUDIO_POLICIES = ("fixed_main", "video_only", "follow_angle")


@dataclass(frozen=True)
class MulticamAngle:
    """A single camera angle within a multicam group.

    Attributes:
        angle_id: stable identifier (used as the switch target, e.g. "1","2")
        asset_id: source MediaAsset this angle plays
        sync_offset: frames added to the timeline-derived source frame when
            resolving this angle's source position (clips start-of-action
            sync between cameras). 0 means already in sync.
        label: human-readable label (e.g. "Wide", "Close-up")
    """

    angle_id: str
    asset_id: str
    sync_offset: int = 0
    label: str = ""

    def __post_init__(self) -> None:
        if not self.angle_id:
            raise ValueError("angle_id cannot be empty")
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "angle_id": self.angle_id,
            "asset_id": self.asset_id,
            "sync_offset": self.sync_offset,
            "label": self.label,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MulticamAngle:
        return MulticamAngle(
            angle_id=data.get("angle_id", ""),
            asset_id=data.get("asset_id", ""),
            sync_offset=int(data.get("sync_offset", 0)),
            label=data.get("label", ""),
        )


@dataclass(frozen=True)
class MulticamAngleGroup:
    """An ordered set of synchronized camera angles.

    Attributes:
        group_id: stable identifier referenced from clip metadata
        name: display name
        angles: ordered tuple of MulticamAngle (angle order = switch order)
        default_angle_id: which angle plays before any switch
    """

    group_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    angles: tuple[MulticamAngle, ...] = ()
    default_angle_id: str = ""
    # F2.5：音频切换策略（fixed_main=固定主音轨 / video_only=仅切视频
    # 不带音频 / follow_angle=音频跟随切角）。旧载荷缺省 = fixed_main。
    audio_policy: str = "fixed_main"


    def __post_init__(self) -> None:
        if not self.group_id:
            raise ValueError("group_id cannot be empty")
        # angle_id uniqueness
        ids = [a.angle_id for a in self.angles]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate angle_id in group")
        if self.default_angle_id and self.default_angle_id not in ids:
            raise ValueError(f"default_angle_id {self.default_angle_id} not in angles")

    @property
    def default_angle(self) -> Optional[MulticamAngle]:
        """The default angle, or the first angle if none is marked default."""
        if not self.angles:
            return None
        if self.default_angle_id:
            for a in self.angles:
                if a.angle_id == self.default_angle_id:
                    return a
        return self.angles[0]

    def get_angle(self, angle_id: str) -> Optional[MulticamAngle]:
        for a in self.angles:
            if a.angle_id == angle_id:
                return a
        return None

    def angle_index(self, angle_id: str) -> int:
        """1-based index of an angle (for keyboard 1/2/3… switching)."""
        for i, a in enumerate(self.angles):
            if a.angle_id == angle_id:
                return i + 1
        return -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "name": self.name,
            "angles": [a.to_dict() for a in self.angles],
            "default_angle_id": self.default_angle_id,
            "audio_policy": self.audio_policy,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> MulticamAngleGroup:
        policy = data.get("audio_policy", "fixed_main")
        if policy not in MULTICAM_AUDIO_POLICIES:
            policy = "fixed_main"
        return MulticamAngleGroup(
            group_id=data.get("group_id", str(uuid.uuid4())),
            name=data.get("name", ""),
            angles=tuple(MulticamAngle.from_dict(a) for a in data.get("angles", [])),
            default_angle_id=data.get("default_angle_id", ""),
            audio_policy=policy,
        )


def get_multicam_group_id(clip_metadata: dict[str, Any] | None) -> Optional[str]:
    """Read the multicam group id attached to a clip's metadata, if any."""
    if not clip_metadata:
        return None
    gid = clip_metadata.get(METADATA_MULTICAM_KEY)
    return gid if isinstance(gid, str) and gid else None


def attach_multicam_group(clip_metadata: dict[str, Any], group_id: str) -> dict[str, Any]:
    """Return a copy of clip metadata with the multicam group id attached."""
    new_meta = dict(clip_metadata)
    new_meta[METADATA_MULTICAM_KEY] = group_id
    return new_meta


# ================================================================
# P1-01: 生产接线 —— 组挂在成员素材 metadata 上（asset-carried group）
# ================================================================
#
# 载体设计：多机位组的完整定义序列化进每个成员素材的 metadata
# （METADATA_ASSET_GROUP_KEY），时间线片段的 asset_id 始终指向真实角度
# 素材。这样：
# - 渲染零改动（片段永远指向可解码的真实素材）；
# - split/trim/UpdateClipSource 等所有既有命令自动保持组归属
#   （换源目标本身是携带同一组 metadata 的成员素材）；
# - 保存/重开经素材 metadata 序列化天然持久。

# 成员素材 metadata 中承载完整组定义的 key。
METADATA_ASSET_GROUP_KEY = "multicam_group"


def build_multicam_group_from_assets(
    asset_ids: "list[str] | tuple[str, ...]",
    display_names: "dict[str, str] | None" = None,
    name: str = "",
) -> MulticamAngleGroup:
    """从多个素材 id 构建多机位组（P1-01 创建入口的纯模型步骤）。

    Args:
        asset_ids: 角度素材 id（顺序即 1..N 键位顺序）。
        display_names: asset_id → 显示名（缺省用 asset_id 前 8 位）。
        name: 组名（缺省 "Multicam N"）。
    """
    if len(asset_ids) < 2:
        raise ValueError("A multicam group needs at least 2 angle assets")
    names = display_names or {}
    angles = tuple(
        MulticamAngle(
            angle_id=str(uuid.uuid4()),
            asset_id=aid,
            sync_offset=0,
            label=names.get(aid) or aid[:8],
        )
        for aid in asset_ids
    )
    return MulticamAngleGroup(
        group_id=str(uuid.uuid4()),
        name=name or f"Multicam {len(angles)}",
        angles=angles,
        default_angle_id=angles[0].angle_id,
    )


def stamp_multicam_group_on_assets(
    assets: "list | tuple",
    group: MulticamAngleGroup,
) -> "tuple":
    """把组定义写进每个成员素材的 metadata（返回新的素材元组）。

    输入是 MediaAsset 列表（frozen）；返回同序的替换副本。调用方负责
    把新素材写回 project（asset_id 不变，整条替换）。
    """
    from dataclasses import replace as _dc_replace

    stamped = []
    for asset in assets:
        stamped.append(_dc_replace(
            asset,
            metadata={
                **(asset.metadata or {}),
                METADATA_ASSET_GROUP_KEY: group.to_dict(),
            },
        ))
    return tuple(stamped)


def multicam_group_from_asset(asset) -> Optional[MulticamAngleGroup]:
    """从素材 metadata 读取其所属多机位组（非成员返回 None）。"""
    meta = getattr(asset, "metadata", None) or {}
    raw = meta.get(METADATA_ASSET_GROUP_KEY)
    if not isinstance(raw, dict) or not raw.get("angles"):
        return None
    try:
        return MulticamAngleGroup.from_dict(raw)
    except (TypeError, ValueError):
        return None


def angle_by_asset_id(group: MulticamAngleGroup, asset_id: str) -> Optional[MulticamAngle]:
    """按素材 id 找角度（键盘/面板切换的高亮与偏移查表）。"""
    for angle in group.angles:
        if angle.asset_id == asset_id:
            return angle
    return None


def group_with_sync_offsets(
    group: MulticamAngleGroup,
    offsets: "dict[str, int]",
) -> MulticamAngleGroup:
    """波形同步结果写回组：按 asset_id 更新各角度 sync_offset（纯函数）。"""
    from dataclasses import replace as _dc_replace

    new_angles = tuple(
        _dc_replace(angle, sync_offset=int(offsets.get(angle.asset_id, angle.sync_offset)))
        for angle in group.angles
    )
    return _dc_replace(group, angles=new_angles)


def resolve_angle_source_frame(
    clip_source_start: int,
    clip_timeline_start: int,
    active_sync_offset: int,
    target_sync_offset: int,
    playhead_frame: int,
    total_frames: int,
) -> int:
    """角度网格取帧映射：playhead 时刻目标角度应显示的源帧号。

    当前片段的 source_start 已含"当前角度"的 sync_offset；切到目标角度
    时命令语义是 ``base + target_offset``，网格预览必须与之一致：

        frame = source_start + (playhead - timeline_start)
                + (target_offset - active_offset)

    结果夹在 ``[0, total_frames-1]``（total_frames<1 时返回 0）。
    """
    raw = (
        clip_source_start
        + (playhead_frame - clip_timeline_start)
        + (target_sync_offset - active_sync_offset)
    )
    if total_frames < 1:
        return 0
    return int(max(0, min(raw, total_frames - 1)))


def map_angle_source_frame(
    source_frame: int,
    active_sync_offset: int,
    target_sync_offset: int,
    active_source_fps: Fraction | None = None,
    target_source_fps: Fraction | None = None,
) -> int:
    """Map the already sampled active source into another camera's clock.

    Sync offsets are expressed in each camera's own source frames. Remove
    the active offset before converting the common instant, then apply the
    target camera's offset. Legacy groups with no rates keep their frame base.
    """
    common_frame = Fraction(source_frame - active_sync_offset)
    if active_source_fps is not None and target_source_fps is not None:
        if active_source_fps <= 0 or target_source_fps <= 0:
            raise ValueError("Multicam source frame rates must be positive")
        common_frame *= target_source_fps / active_source_fps
    return max(0, round(common_frame) + target_sync_offset)


@dataclass(frozen=True)
class MulticamInsertPlan:
    """Fully validated group metadata and its prepared timeline insertion."""

    group: MulticamAngleGroup
    assets: tuple[Any, ...]
    command: Any


def plan_multicam_insert(
    project: Any, timeline: Any, asset_ids: list[str], track_id: str,
    playhead: int,
) -> MulticamInsertPlan:
    """Build a pure creation plan; failed insertion never stamps project assets.

    All camera durations are compared in seconds. The initial angle's source
    window carries rational source/sequence FPS into the standard Insert path.
    The command returned by its dry apply contains stable clip IDs/snapshots.
    """
    from dataclasses import replace as _replace
    from fractions import Fraction
    from src.features.edit.core.commands import (
        InsertClipCommand,
        InsertLinkedAssetRippleCommand,
    )
    from src.features.edit.core.media_replacement import source_fps_from_metadata

    ids = list(dict.fromkeys(asset_ids))
    assets = tuple(project.assets[aid] for aid in ids if aid in project.assets
                   and project.assets[aid].media_type == "video")
    if len(assets) < 2:
        raise ValueError("Multicam group needs at least 2 video assets")
    timeline_fps = Fraction(str(float(timeline.fps))).limit_denominator(100_000)
    source_rates = [source_fps_from_metadata(asset.metadata) or timeline_fps for asset in assets]
    seconds = []
    for asset, fps in zip(assets, source_rates):
        frames = asset.metadata.get("total_frames") or asset.metadata.get("duration_frames")
        if frames is not None:
            try:
                count = int(frames)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Camera source frame count is invalid") from exc
            if count > 0:
                seconds.append(Fraction(count, 1) / fps)
    source_frames = max(1, int(min(seconds) * source_rates[0])) if seconds else 250
    group = build_multicam_group_from_assets(
        [asset.asset_id for asset in assets],
        {asset.asset_id: asset.display_name for asset in assets})
    # F2.5：同一批素材已在同一组里（重建组/加机位）时沿用既有音频策略，
    # 不把用户已选的 follow_angle/video_only 静默重置回默认。
    existing = {multicam_group_from_asset(asset) for asset in assets}
    existing.discard(None)
    if len(existing) == 1:
        prior = next(iter(existing))
        if prior.audio_policy in MULTICAM_AUDIO_POLICIES:
            group = _replace(group, audio_policy=prior.audio_policy)
    stamped = stamp_multicam_group_on_assets(assets, group)
    # F2.5 插入侧成对落轨：非 video_only 且主角度（角度 0）素材带音频时
    # 用 A/V 波纹插入（同 playhead 同时长、双向链接、单步 Undo 移除两
    # 片与原子补建的音轨）——fixed_main 的"主音轨"= 初始角度音频；
    # video_only 或无声源保持 video-only 现状。
    main_asset = assets[0]
    main_has_audio = bool(getattr(main_asset, "audio_channels", None))
    if group.audio_policy != "video_only" and main_has_audio:
        audio_track = next(
            (t for t in getattr(timeline, "audio_tracks", ())
             if not t.is_locked), None)
        command = InsertLinkedAssetRippleCommand.create(
            asset_id=main_asset.asset_id, in_frame=0,
            out_frame=source_frames - 1, playhead=playhead,
            video_track_id=track_id,
            audio_track_id=audio_track.track_id if audio_track else None,
            source_fps_num=source_rates[0].numerator,
            source_fps_den=source_rates[0].denominator,
            timeline_fps_num=timeline_fps.numerator,
            timeline_fps_den=timeline_fps.denominator)
    else:
        command = InsertClipCommand.create(
            asset_id=assets[0].asset_id, in_frame=0, out_frame=source_frames - 1,
            playhead=playhead, track_id=track_id,
            source_fps_num=source_rates[0].numerator,
            source_fps_den=source_rates[0].denominator,
            timeline_fps_num=timeline_fps.numerator,
            timeline_fps_den=timeline_fps.denominator)
    _sequence, prepared = command.apply(timeline)
    return MulticamInsertPlan(group, tuple(stamped), prepared)


@dataclass(frozen=True)
class MulticamSyncIdentity:
    """Request source identity, independent of playhead and unrelated edits."""

    project_id: str
    members: tuple[Any, ...]
    signatures: tuple[tuple[int, int], ...]

    @staticmethod
    def capture(project: Any, group: MulticamAngleGroup) -> "MulticamSyncIdentity":
        import copy
        import os
        members = tuple(project.assets.get(angle.asset_id) for angle in group.angles)
        if any(asset is None for asset in members):
            raise ValueError("多机位组包含已删除的素材，请重新创建或修复组后再同步。")
        try:
            stats = [os.stat(asset.path) for asset in members]
        except OSError as exc:
            raise ValueError("无法读取多机位源文件，已取消波形同步。") from exc
        return MulticamSyncIdentity(
            project.project_id, copy.deepcopy(members),
            tuple((stat.st_size, stat.st_mtime_ns) for stat in stats))

    def matches(self, project: Any) -> bool:
        import os
        if project is None or project.project_id != self.project_id:
            return False
        for asset, signature in zip(self.members, self.signatures):
            current = project.assets.get(asset.asset_id)
            # MediaAsset.__eq__ deliberately compares asset_id only; it is not
            # a source-identity comparison for asynchronous work.
            if current is None or vars(current) != vars(asset):
                return False
            try:
                stat = os.stat(asset.path)
            except OSError:
                return False
            if (stat.st_size, stat.st_mtime_ns) != signature:
                return False
        return True


@dataclass(frozen=True)
class UpdateMulticamSyncCommand:
    """A project-history transaction limited to the group's metadata key.

    Asset paths, logs and other feature metadata are read from the live
    project during apply/undo. A conflicting group change is refused before
    touching any member, rather than restoring a stale whole-project snapshot.
    """

    command_id: str
    label: str
    before_groups: dict[str, tuple[bool, Any]]
    after_groups: dict[str, tuple[bool, Any]]

    @staticmethod
    def create(project: Any, group: MulticamAngleGroup) -> "UpdateMulticamSyncCommand":
        import copy
        before: dict[str, tuple[bool, Any]] = {}
        after: dict[str, tuple[bool, Any]] = {}
        for angle in group.angles:
            asset = project.assets.get(angle.asset_id)
            if asset is None:
                raise ValueError("多机位成员已删除，无法应用波形同步结果。")
            metadata = asset.metadata or {}
            before[angle.asset_id] = (
                METADATA_ASSET_GROUP_KEY in metadata,
                copy.deepcopy(metadata.get(METADATA_ASSET_GROUP_KEY)))
            after[angle.asset_id] = (True, copy.deepcopy(group.to_dict()))
        return UpdateMulticamSyncCommand(
            str(uuid.uuid4()), "Apply Multicam Sync", before, after)

    def _patch(
        self, project: Any, expected: dict[str, tuple[bool, Any]],
        desired: dict[str, tuple[bool, Any]],
    ) -> Any:
        import copy
        import time
        from dataclasses import replace

        for asset_id, snapshot in expected.items():
            asset = project.assets.get(asset_id)
            if asset is None:
                raise ValueError("多机位成员已删除，无法撤销或应用同步结果。")
            metadata = asset.metadata or {}
            actual = (METADATA_ASSET_GROUP_KEY in metadata,
                      metadata.get(METADATA_ASSET_GROUP_KEY))
            if actual != snapshot:
                raise ValueError("多机位组已被其他操作修改，请先撤销该操作后重试。")
        assets = dict(project.assets)
        for asset_id, (present, value) in desired.items():
            asset = assets[asset_id]
            metadata = dict(asset.metadata or {})
            if present:
                metadata[METADATA_ASSET_GROUP_KEY] = copy.deepcopy(value)
            else:
                metadata.pop(METADATA_ASSET_GROUP_KEY, None)
            assets[asset_id] = replace(asset, metadata=metadata)
        return replace(project, assets=assets, modified_at=time.time())

    def apply(self, project: Any) -> Any:
        return self._patch(project, self.before_groups, self.after_groups)

    def undo(self, project: Any) -> Any:
        return self._patch(project, self.after_groups, self.before_groups)

    def to_dict(self) -> dict[str, Any]:
        import copy
        return copy.deepcopy({
            "command_id": self.command_id, "command_type": type(self).__name__,
            "label": self.label, "before_groups": self.before_groups,
            "after_groups": self.after_groups,
        })

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "UpdateMulticamSyncCommand":
        import copy
        return UpdateMulticamSyncCommand(
            str(data["command_id"]), str(data["label"]),
            {key: (bool(value[0]), copy.deepcopy(value[1]))
             for key, value in data["before_groups"].items()},
            {key: (bool(value[0]), copy.deepcopy(value[1]))
             for key, value in data["after_groups"].items()})


@dataclass(frozen=True)
class SetMulticamAudioPolicyCommand:
    """F2.5: 设置多机位组的音频切换策略（受限元数据事务，可撤销）。

    与 UpdateMulticamSyncCommand 同模式：只碰组 metadata 键，冲突先拒绝
    不半写；apply/undo 对称。策略影响切角命令的音频联动与插入行为，
    导出按时间线最终音频片段一致渲染。
    """

    command_id: str
    label: str
    before_groups: dict[str, tuple[bool, Any]]
    after_groups: dict[str, tuple[bool, Any]]

    @staticmethod
    def create(project: Any, group: MulticamAngleGroup,
               audio_policy: str) -> "SetMulticamAudioPolicyCommand":
        import copy
        from dataclasses import replace as _replace
        if audio_policy not in MULTICAM_AUDIO_POLICIES:
            raise ValueError(f"unknown audio policy: {audio_policy}")
        new_group = _replace(group, audio_policy=audio_policy)
        before: dict[str, tuple[bool, Any]] = {}
        after: dict[str, tuple[bool, Any]] = {}
        for angle in group.angles:
            asset = project.assets.get(angle.asset_id)
            if asset is None:
                raise ValueError("多机位成员已删除，无法设置音频策略。")
            metadata = asset.metadata or {}
            before[angle.asset_id] = (
                METADATA_ASSET_GROUP_KEY in metadata,
                copy.deepcopy(metadata.get(METADATA_ASSET_GROUP_KEY)))
            after[angle.asset_id] = (True, copy.deepcopy(new_group.to_dict()))
        return SetMulticamAudioPolicyCommand(
            str(uuid.uuid4()), "Set Multicam Audio Policy", before, after)

    def _patch(
        self, project: Any, expected: dict[str, tuple[bool, Any]],
        desired: dict[str, tuple[bool, Any]],
    ) -> Any:
        import copy
        import time
        from dataclasses import replace as _replace

        for asset_id, snapshot in expected.items():
            asset = project.assets.get(asset_id)
            if asset is None:
                raise ValueError("多机位成员已删除，无法撤销或应用音频策略。")
            metadata = asset.metadata or {}
            actual = (METADATA_ASSET_GROUP_KEY in metadata,
                      metadata.get(METADATA_ASSET_GROUP_KEY))
            if actual != snapshot:
                raise ValueError(
                    "多机位组已被其他操作修改，请先撤销该操作后重试。")
        assets = dict(project.assets)
        for asset_id, (present, value) in desired.items():
            asset = assets[asset_id]
            metadata = dict(asset.metadata or {})
            metadata[METADATA_ASSET_GROUP_KEY] = copy.deepcopy(value)
            assets[asset_id] = _replace(asset, metadata=metadata)
        return _replace(project, assets=assets, modified_at=time.time())

    def apply(self, project: Any) -> Any:
        return self._patch(project, self.before_groups, self.after_groups)

    def undo(self, project: Any) -> Any:
        return self._patch(project, self.after_groups, self.before_groups)
