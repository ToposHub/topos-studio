"""Typed Artifact — 创作文档的版本化、可编辑、来源可追溯容器（M6 / §15.1 / 计划 §11.1 M6-1）。

计划 §15.1：为 Brief/脚本/分镜/shot list/交付大纲建立 typed artifact、revision 和编辑卡；
用户修改永远优先于模型结果（§11.1 M6 红线）。

设计（遵循 .clinerules / §0）：

- **版本化 + 不可变快照**：每个 artifact 是 frozen dataclass；变更产生新 revision
  （``with_revision_bumped``）。模型只能 propose patch（``ArtifactPatch``），用户批准后才落地。
- **来源可追溯（provenance）**：每条 artifact 带 ``source``（user / model / skill）+ 模型 id +
  生成时间；``user_edited`` 标记用户手改（用户改过的不被模型结果覆盖）。
- **typed kinds**：``ArtifactKind`` 枚举（brief 已有 EditorialBrief；新增 script/shot_list/
  storyboard/delivery_outline）。各 kind 是 frozen dataclass，带 ``artifact_id``/``revision``/
  ``source``/``user_edited`` + 自身字段。
- **用户优先于模型**：``ArtifactStore.save`` 在 user_edited=True 时拒绝 model-source 的低优先级
  覆盖（§11.1 M6 红线："用户修改永远优先于模型结果"）。
- **不写 timeline**：artifact 是创作文档，不是 timeline 变更；应用 artifact 需经 plan/approve。

与既有结构的关系：``EditorialBrief``（editorial_models.py:209）是"创作合同"，已有自己的
revision/patch 体系（BriefService）；本模块的 ``ScriptArtifact`` 等是它的同级"创作文档"，
复用同样的 revision + provenance 模式但独立存储。brief 不强迁到本体系（向后兼容）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------- #
# ArtifactKind（§15.1 typed kinds）
# ---------------------------------------------------------------------- #


class ArtifactKind(str, Enum):
    """artifact 类型（§15.1）。

    brief 不在本枚举（EditorialBrief 有独立体系）；本枚举覆盖脚本/分镜/大纲/shot list。
    """

    SCRIPT = "script"                # 脚本/文案
    SHOT_LIST = "shot_list"          # 分镜/shot list
    STORYBOARD = "storyboard"        # 分镜板（图文描述）
    DELIVERY_OUTLINE = "delivery_outline"  # 交付大纲


class ArtifactSource(str, Enum):
    """artifact 来源（provenance）。

    ``user`` 永远优先于 ``model``/``skill``（§11.1 M6 红线：用户修改不被模型结果覆盖）。
    """

    USER = "user"
    MODEL = "model"
    SKILL = "skill"


# ---------------------------------------------------------------------- #
# ShotItem（shot_list / storyboard 共用的单条镜头）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShotItem:
    """分镜单条（shot_list / storyboard 的元素；§15.1）。

    ``source_range`` 是建议的素材范围（asset_id + FrameRange）；可为空（纯文案分镜）。
    """

    shot_id: str
    label: str = ""                  # 人类可读名（如"开场特写"）
    description: str = ""            # 描述/拍摄意图
    asset_id: str | None = None      # 关联素材（None = 待补）
    start_frame: int | None = None   # 建议起点（半开区间）
    end_frame: int | None = None     # 建议终点
    duration_frames: int | None = None  # 目标时长
    script_ref: str = ""             # 关联脚本条目（ScriptItem.line_id）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "shot_id": self.shot_id, "label": self.label, "description": self.description,
        }
        if self.asset_id is not None:
            d["asset_id"] = self.asset_id
        if self.start_frame is not None:
            d["start_frame"] = self.start_frame
        if self.end_frame is not None:
            d["end_frame"] = self.end_frame
        if self.duration_frames is not None:
            d["duration_frames"] = self.duration_frames
        if self.script_ref:
            d["script_ref"] = self.script_ref
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ShotItem":
        return ShotItem(
            shot_id=str(data.get("shot_id", "")),
            label=str(data.get("label", "")),
            description=str(data.get("description", "")),
            asset_id=data.get("asset_id"),
            start_frame=data.get("start_frame"),
            end_frame=data.get("end_frame"),
            duration_frames=data.get("duration_frames"),
            script_ref=str(data.get("script_ref", "")),
        )


# ---------------------------------------------------------------------- #
# ScriptArtifact（脚本/文案）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScriptLine:
    """脚本单行（§15.1）。"""

    line_id: str
    speaker: str | None = None       # 匿名 S1/S2 或角色名
    text: str = ""                   # 台词/旁白文本
    note: str = ""                   # 导演标注

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"line_id": self.line_id, "text": self.text}
        if self.speaker is not None:
            d["speaker"] = self.speaker
        if self.note:
            d["note"] = self.note
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ScriptLine":
        return ScriptLine(
            line_id=str(data.get("line_id", "")),
            text=str(data.get("text", "")),
            speaker=data.get("speaker"),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class ScriptArtifact:
    """脚本/文案 artifact（M6 / §15.1）。

    版本化 + 来源可追溯；用户编辑优先于模型（``user_edited`` 置位后模型不能覆盖）。
    """

    artifact_id: str
    kind: ArtifactKind = ArtifactKind.SCRIPT
    revision: int = 0
    title: str = ""
    lines: tuple[ScriptLine, ...] = ()
    source: ArtifactSource = ArtifactSource.USER
    user_edited: bool = False        # 用户改过 → 拒绝模型低优先级覆盖
    model_id: str = ""               # source=MODEL 时记录模型
    created_at: str = ""
    brief_revision: int = 0          # 关联的 brief revision（来源合同）

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id cannot be empty")
        if self.revision < 0:
            raise ValueError(f"revision must be >= 0, got {self.revision}")

    def with_revision_bumped(self) -> "ScriptArtifact":
        return replace(self, revision=self.revision + 1)

    def with_user_edit(self) -> "ScriptArtifact":
        """标记用户手改（之后模型不能覆盖；§11.1 M6 红线）。"""
        return replace(self, user_edited=True, source=ArtifactSource.USER)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "kind": self.kind.value,
            "revision": self.revision,
            "title": self.title,
            "lines": [ln.to_dict() for ln in self.lines],
            "source": self.source.value,
            "user_edited": self.user_edited,
            "brief_revision": self.brief_revision,
        }
        if self.model_id:
            d["model_id"] = self.model_id
        if self.created_at:
            d["created_at"] = self.created_at
        return d


# ---------------------------------------------------------------------- #
# ShotListArtifact / StoryboardArtifact / DeliveryOutlineArtifact
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShotListArtifact:
    """分镜/shot list artifact（M6 / §15.1）。"""

    artifact_id: str
    kind: ArtifactKind = ArtifactKind.SHOT_LIST
    revision: int = 0
    title: str = ""
    shots: tuple[ShotItem, ...] = ()
    source: ArtifactSource = ArtifactSource.USER
    user_edited: bool = False
    model_id: str = ""
    created_at: str = ""
    brief_revision: int = 0

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id cannot be empty")
        if self.revision < 0:
            raise ValueError(f"revision must be >= 0, got {self.revision}")

    def with_revision_bumped(self) -> "ShotListArtifact":
        return replace(self, revision=self.revision + 1)

    def with_user_edit(self) -> "ShotListArtifact":
        return replace(self, user_edited=True, source=ArtifactSource.USER)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "artifact_id": self.artifact_id, "kind": self.kind.value,
            "revision": self.revision, "title": self.title,
            "shots": [s.to_dict() for s in self.shots],
            "source": self.source.value, "user_edited": self.user_edited,
            "brief_revision": self.brief_revision,
        }
        if self.model_id:
            d["model_id"] = self.model_id
        if self.created_at:
            d["created_at"] = self.created_at
        return d


@dataclass(frozen=True)
class StoryboardArtifact:
    """分镜板 artifact（图文描述；M6 / §15.1）。

    复用 ShotItem 作为每格；storyboard 更偏视觉描述（每格一段描述文字）。
    """

    artifact_id: str
    kind: ArtifactKind = ArtifactKind.STORYBOARD
    revision: int = 0
    title: str = ""
    panels: tuple[ShotItem, ...] = ()   # 复用 ShotItem（description = 视觉描述）
    source: ArtifactSource = ArtifactSource.USER
    user_edited: bool = False
    model_id: str = ""
    created_at: str = ""
    brief_revision: int = 0

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id cannot be empty")
        if self.revision < 0:
            raise ValueError(f"revision must be >= 0, got {self.revision}")

    def with_revision_bumped(self) -> "StoryboardArtifact":
        return replace(self, revision=self.revision + 1)

    def with_user_edit(self) -> "StoryboardArtifact":
        return replace(self, user_edited=True, source=ArtifactSource.USER)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "artifact_id": self.artifact_id, "kind": self.kind.value,
            "revision": self.revision, "title": self.title,
            "panels": [p.to_dict() for p in self.panels],
            "source": self.source.value, "user_edited": self.user_edited,
            "brief_revision": self.brief_revision,
        }
        if self.model_id:
            d["model_id"] = self.model_id
        if self.created_at:
            d["created_at"] = self.created_at
        return d


@dataclass(frozen=True)
class DeliveryItem:
    """交付大纲单条（§15.1）。"""

    item_id: str
    label: str = ""
    platform: str = ""               # 目标平台（youtube/douyin/...）
    aspect_ratio: str = ""           # 16:9 / 9:16 / 1:1
    duration_frames: int | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"item_id": self.item_id, "label": self.label,
                             "platform": self.platform, "aspect_ratio": self.aspect_ratio}
        if self.duration_frames is not None:
            d["duration_frames"] = self.duration_frames
        if self.note:
            d["note"] = self.note
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DeliveryItem":
        return DeliveryItem(
            item_id=str(data.get("item_id", "")),
            label=str(data.get("label", "")),
            platform=str(data.get("platform", "")),
            aspect_ratio=str(data.get("aspect_ratio", "")),
            duration_frames=data.get("duration_frames"),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class DeliveryOutlineArtifact:
    """交付大纲 artifact（M6 / §15.1）。"""

    artifact_id: str
    kind: ArtifactKind = ArtifactKind.DELIVERY_OUTLINE
    revision: int = 0
    title: str = ""
    items: tuple[DeliveryItem, ...] = ()
    source: ArtifactSource = ArtifactSource.USER
    user_edited: bool = False
    model_id: str = ""
    created_at: str = ""
    brief_revision: int = 0

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id cannot be empty")
        if self.revision < 0:
            raise ValueError(f"revision must be >= 0, got {self.revision}")

    def with_revision_bumped(self) -> "DeliveryOutlineArtifact":
        return replace(self, revision=self.revision + 1)

    def with_user_edit(self) -> "DeliveryOutlineArtifact":
        return replace(self, user_edited=True, source=ArtifactSource.USER)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "artifact_id": self.artifact_id, "kind": self.kind.value,
            "revision": self.revision, "title": self.title,
            "items": [i.to_dict() for i in self.items],
            "source": self.source.value, "user_edited": self.user_edited,
            "brief_revision": self.brief_revision,
        }
        if self.model_id:
            d["model_id"] = self.model_id
        if self.created_at:
            d["created_at"] = self.created_at
        return d


# 统一 artifact 类型别名（供 ArtifactStore 类型注解）
TypedArtifact = ScriptArtifact | ShotListArtifact | StoryboardArtifact | DeliveryOutlineArtifact


# ---------------------------------------------------------------------- #
# ArtifactPatch（模型只能 propose；用户批准后才落地）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArtifactPatch:
    """artifact patch（模型 propose；用户批准后 apply；§11.1 M6 红线）。

    乐观锁：``base_revision`` 必须匹配当前 artifact 的 revision；否则冲突拒绝。
    """

    artifact_id: str
    base_revision: int
    changes: dict[str, Any]          # 字段级变更（merge 进 artifact）
    rationale: str = ""              # 模型解释为什么这么改
    source: ArtifactSource = ArtifactSource.MODEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "base_revision": self.base_revision,
            "changes": dict(self.changes),
            "rationale": self.rationale,
            "source": self.source.value,
        }


__all__ = [
    "ArtifactKind",
    "ArtifactSource",
    "ShotItem",
    "ScriptLine",
    "ScriptArtifact",
    "ShotListArtifact",
    "StoryboardArtifact",
    "DeliveryItem",
    "DeliveryOutlineArtifact",
    "TypedArtifact",
    "ArtifactPatch",
]
