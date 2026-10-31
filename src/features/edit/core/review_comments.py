"""F8.2 · 带版本锚点的审片评论与 marker——纯模型层。

语义（对齐计划）：
- **评论身份**：稳定 id + 作者 + 状态（open/resolved/rejected）+
  **版本锚点**（payload_hash——F8.1 快照哈希；无快照时用当前工程
  payload 哈希）；
- **精确锚点**：时间基（timeline frame）+ 源帧（asset_id+source_frame）
  + 片段锚（clip_id）三重锚——剪辑变更后重定位按源帧锚优先；
- **重定位置信度**：改 fps/重套/删镜头后给可解释映射或**标失效**——
  失效锚点绝不静默挪到别的镜头（source_frame/asset 双匹配才迁移）；
- **导入导出**：JSON 文件往返；**重复导入幂等**（稳定 id 去重）；
- **评论→marker**：resolved↔marker 状态同步（open→红/resolved→绿）；
- **隐私清理**：导出可剥离作者字段（privacy strip）；
- Unicode 安全（payload ensure_ascii=False）。

不默认部署云服务（计划原文）——文件交换即接口。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

COMMENT_STATUSES = ("open", "resolved", "rejected")

_RELOCATION_CONFIDENCE = {
    "exact": 1.0,       # 源帧+素材双匹配
    "timeline_only": 0.5,  # 仅时间线帧匹配（fps 变化后近似）
    "stale": 0.0,       # 素材锚缺失/片段已删 → 失效
}


@dataclass(frozen=True)
class ReviewComment:
    """一条带版本锚点的审片评论。"""

    comment_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    author: str = ""
    body: str = ""                       # Unicode 自由文本
    status: str = "open"                 # open / resolved / rejected
    created_at: float = 0.0
    # 版本锚点（F8.1 快照 payload_hash；None=未锚定）
    version_hash: Optional[str] = None
    # 精确时间基锚
    timeline_id: str = ""
    timeline_frame: int = 0
    # 源帧锚（重定位优先依据）
    asset_id: str = ""
    source_frame: int = 0
    # 片段锚（可选）
    clip_id: Optional[str] = None
    replies: Tuple[str, ...] = ()        # 回复 comment_id 列表（处理/回复）

    def __post_init__(self) -> None:
        if self.status not in COMMENT_STATUSES:
            raise ValueError(f"unknown comment status: {self.status}")

    def to_dict(self) -> dict:
        return {
            "comment_id": self.comment_id, "author": self.author,
            "body": self.body, "status": self.status,
            "created_at": self.created_at,
            "version_hash": self.version_hash,
            "timeline_id": self.timeline_id,
            "timeline_frame": self.timeline_frame,
            "asset_id": self.asset_id, "source_frame": self.source_frame,
            "clip_id": self.clip_id, "replies": list(self.replies),
        }

    @staticmethod
    def from_dict(data: dict) -> "ReviewComment":
        return ReviewComment(
            comment_id=str(data.get("comment_id") or uuid.uuid4()),
            author=str(data.get("author", "") or ""),
            body=str(data.get("body", "") or ""),
            status=str(data.get("status", "open") or "open"),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            version_hash=data.get("version_hash"),
            timeline_id=str(data.get("timeline_id", "") or ""),
            timeline_frame=int(data.get("timeline_frame", 0) or 0),
            asset_id=str(data.get("asset_id", "") or ""),
            source_frame=int(data.get("source_frame", 0) or 0),
            clip_id=data.get("clip_id"),
            replies=tuple(data.get("replies", ()) or ()),
        )


def export_comments(
    comments: List[ReviewComment],
    path: str,
    *,
    strip_authors: bool = False,
) -> bool:
    """导出 JSON（Unicode 安全；strip_authors 隐私清理）。"""
    payload = {
        "schema": "topos.review_comments/1",
        "comments": [
            {**c.to_dict(),
             **({"author": ""} if strip_authors else {})}
            for c in comments
        ],
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def import_comments(
    path: str,
    existing: Optional[List[ReviewComment]] = None,
) -> Tuple[List[ReviewComment], int]:
    """导入 JSON（重复导入幂等：稳定 comment_id 去重）。

    Returns:
        (合并后的全量评论, 新增数)
    """
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if payload.get("schema") != "topos.review_comments/1":
        raise ValueError("未知评论文件 schema")
    incoming = [ReviewComment.from_dict(c)
                for c in payload.get("comments", [])]
    known = {c.comment_id for c in (existing or [])}
    merged = list(existing or [])
    added = 0
    for comment in incoming:
        if comment.comment_id in known:
            continue  # 幂等：重复导入不重复
        merged.append(comment)
        known.add(comment.comment_id)
        added += 1
    return merged, added


def comment_to_marker(comment: ReviewComment) -> Optional[dict]:
    """评论 → marker 载荷（open→红/resolved→绿/rejected→灰）。"""
    if not comment.body:
        return None
    color = {"open": "#ff4444",
             "resolved": "#44cc44"}.get(comment.status, "#888888")
    return {
        "frame": comment.timeline_frame,
        "name": (comment.body[:40] + "…") if len(comment.body) > 40
        else comment.body,
        "color": color,
        "comment": comment.body,
        "clip_id": comment.clip_id,
    }


@dataclass(frozen=True)
class RelocationResult:
    """评论重定位结果（剪辑变更后）。"""

    comment: ReviewComment
    confidence: float
    reason: str   # exact / timeline_only / stale:*


def relocate_comment(
    comment: ReviewComment,
    new_timeline: Any,
    *,
    clip_lookup: Optional[Any] = None,
) -> RelocationResult:
    """剪辑变更后的重定位——失效锚点绝不静默挪到别的镜头。

    策略（置信度从高到低）：
    1. **exact**（1.0）：素材+源帧锚在新时间线找到同 asset 片段且
       source 窗覆盖该源帧 → 迁移到对应 timeline_frame；
    2. **timeline_only**（0.5）：锚点素材不存在于新时间线，但时间线
       帧仍在范围内（fps 变化近似）→ 保留原帧并声明近似；
    3. **stale**（0.0）：素材被删/源帧出窗 → 标失效（不挪到别的镜头）。
    """
    if not comment.asset_id:
        # 无源锚：仅时间线锚（帧界取轨道末帧——duration 属性不可靠）
        last_frame = 0
        for track in getattr(new_timeline, "video_tracks", ()) or ():
            for clip in getattr(track, "clips", ()) or ():
                last_frame = max(last_frame, clip.timeline_start
                                 + clip.timeline_duration)
        if 0 <= comment.timeline_frame < last_frame:
            return RelocationResult(
                comment, _RELOCATION_CONFIDENCE["timeline_only"],
                "timeline_only")
        return RelocationResult(
            comment, _RELOCATION_CONFIDENCE["stale"], "stale:no_anchor")

    # M15.2（R10）：同源素材可能多次出现在时间线（复制/分机位引用）。
    # 收集**全部**覆盖该源帧的候选片段——优先原 clip_id 命中（exact）；
    # 多候选且原 clip_id 不在其中 → ambiguous（置信 0.3，用户裁决），
    # 绝不静默取第一个同源片段冒充 exact（评论会跳到别的 occurrence）。
    candidates: List[Tuple[Any, int]] = []  # (clip, mapped_frame)
    for track in getattr(new_timeline, "video_tracks", ()) or ():
        for clip in getattr(track, "clips", ()) or ():
            if clip.asset_id != comment.asset_id:
                continue
            try:
                # 源窗内？timeline_to_source_frame 在窗外会抛
                # ValueError —— 捕获后试下一片段
                for f in range(clip.timeline_start,
                               clip.timeline_start
                               + clip.timeline_duration):
                    if clip.timeline_to_source_frame(f) == (
                            comment.source_frame):
                        candidates.append((clip, f))
                        break
            except (ValueError, AttributeError):
                continue

    if candidates:
        preferred = [c for c in candidates
                     if getattr(c[0], "clip_id", None) == comment.clip_id]
        if len(preferred) == 1:
            clip, frame = preferred[0]
        elif len(candidates) == 1:
            clip, frame = candidates[0]
        else:
            # 多个同源 occurrence 且原 clip_id 不在候选中——歧义，交由
            # 用户裁决；保留原评论（不挪动）
            return RelocationResult(
                comment, 0.3,
                "ambiguous:multiple_occurrences:"
                + ",".join(str(c[0].clip_id) for c in candidates))
        updated = ReviewComment(
            **{**comment.to_dict(),
               "timeline_frame": frame,
               "clip_id": clip.clip_id})
        return RelocationResult(
            updated, _RELOCATION_CONFIDENCE["exact"], "exact")

    # 素材锚存在但失败（素材被删/源帧被修剪）：**标失效**——绝不静默
    # 挪到别的镜头（计划原文）。timeline_only 兜底只对无源锚的纯
    # 时间线评论开放。
    return RelocationResult(
        comment, _RELOCATION_CONFIDENCE["stale"],
        "stale:asset_or_frame_gone")


__all__ = [
    "ReviewComment",
    "RelocationResult",
    "COMMENT_STATUSES",
    "export_comments",
    "import_comments",
    "comment_to_marker",
    "relocate_comment",
]
