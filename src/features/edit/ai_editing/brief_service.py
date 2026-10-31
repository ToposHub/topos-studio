"""TAEP Brief Service（Phase 1，补齐）。

管理 ``EditorialBrief`` 的 revision、锁定内容、硬/软约束、平台 deliverable
（TAEP §5.3 / §12 P1）。

核心规则（TAEP §5.3 设计规则）：

- 约束分为 ``hard``（违反即拒绝）和 ``soft``（作为排序/解释依据），二者不混。
- 用户可锁定 source range、故事 beat、字幕文案、音乐、品牌素材和已批准 timeline；
  锁定对象永远优先于 AI 重规划。
- brief 的每次变更会产生新 revision，并使受影响的 variant 标为 ``needs_replan``，
  但**不会**删除旧方案。
- 成功度量应可观察：时长、必要 beat 覆盖、字幕可读性、品牌/版权规则、用户选择。

AI 只能**提议** brief patch（``propose_brief_patch``）；用户批准后才版本化保存。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from src.features.edit.ai_editing.editorial_models import (
    ConstraintKind,
    DurationTarget,
    EditorialBrief,
    FrameRange,
    HardConstraint,
    SourceRef,
    SoftPreference,
)


class BriefValidationError(ValueError):
    """brief patch 校验失败（硬约束自相矛盾等）。"""


@dataclass(frozen=True)
class BriefPatch:
    """对 brief 的提议性修改（AI 提议，用户批准后生效）。

    TAEP §5.3：用户为主，AI 只能提议 patch。patch 携带 ``base_revision`` 防止
    把过期的 brief 覆盖掉用户刚做的修改。
    """

    base_revision: int
    changes: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.base_revision < 0:
            raise ValueError(f"base_revision must be >= 0, got {self.base_revision}")


@dataclass(frozen=True)
class LockedSourceRange:
    """用户锁定的 source range（永远优先于 AI 重规划，TAEP §5.3）。"""

    asset_id: str
    source: FrameRange
    label: str = ""

    def to_ref(self) -> SourceRef:
        return SourceRef(self.asset_id, self.source)


@dataclass
class BriefService:
    """brief 的版本化编辑服务。

    纯逻辑层，无 Qt。调用方（controller）在用户批准 patch 后调用 ``apply_patch``。
    """

    def __init__(self) -> None:
        self._briefs: dict[str, EditorialBrief] = {}

    # ---------------- 读取 ---------------- #

    def get(self, brief_id: str) -> EditorialBrief | None:
        return self._briefs.get(brief_id)

    def current_revision(self, brief_id: str) -> int | None:
        b = self._briefs.get(brief_id)
        return b.revision if b else None

    def brand_requirements(self, brief_id: str) -> tuple[dict[str, Any], ...]:
        """取当前 brief 的品牌元素要求，供导出质量门读取。"""
        b = self._briefs.get(brief_id)
        return b.brand_requirements if b is not None else ()

    # ---------------- 写入（用户路径）---------------- #

    def save(self, brief: EditorialBrief) -> EditorialBrief:
        """保存/覆盖一个 brief（用户直接编辑路径，不经过 patch）。"""
        self._briefs[brief.brief_id] = brief
        return brief

    def create_default(self, brief_id: str) -> EditorialBrief:
        """创建一个合理默认 brief 并保存（首次请求无 brief 时调用）。

        幂等：若 ``brief_id`` 已存在则原样返回现有 brief（不覆盖用户的修改）。
        默认值：竖版社交平台、目标 720 帧（30s@24fps，min 648 / max 792）、
        三个必要 beat（hook / product_value / call_to_action）、空 primary_goal
        （由用户在编辑器里填）。
        """
        existing = self._briefs.get(brief_id)
        if existing is not None:
            return existing
        from src.features.edit.ai_editing.editorial_models import Platform

        brief = EditorialBrief(
            brief_id=brief_id,
            revision=0,
            platform=Platform.VERTICAL_SOCIAL,
            target_duration=DurationTarget(720, 648, 792),
            required_beats=("hook", "product_value", "call_to_action"),
        )
        self._briefs[brief_id] = brief
        return brief

    def apply_patch(
        self, brief_id: str, patch: BriefPatch
    ) -> EditorialBrief:
        """应用用户批准的 patch：乐观锁检查 + 字段合并 + revision bump。

        若 ``patch.base_revision`` 与当前 revision 不一致，抛 ``BriefValidationError``。
        """
        current = self._briefs.get(brief_id)
        if current is None:
            raise BriefValidationError(f"brief {brief_id!r} not found")
        if patch.base_revision != current.revision:
            raise BriefValidationError(
                f"brief revision conflict: patch built against {patch.base_revision} "
                f"but current is {current.revision}"
            )
        merged = self._merge(current, patch.changes)
        bumped = merged.with_revision_bumped()
        self._briefs[brief_id] = bumped
        return bumped

    def _merge(self, brief: EditorialBrief, changes: dict[str, Any]) -> EditorialBrief:
        """把 patch.changes 合并到 brief（只覆盖显式提供的字段）。"""
        kwargs: dict[str, Any] = {}
        if "primary_goal" in changes:
            kwargs["primary_goal"] = str(changes["primary_goal"])
        if "platform" in changes:
            kwargs["platform"] = brief.platform if changes["platform"] is None else changes["platform"]
        if "target_duration" in changes and changes["target_duration"] is not None:
            td = changes["target_duration"]
            if isinstance(td, DurationTarget):
                kwargs["target_duration"] = td
            elif isinstance(td, dict):
                kwargs["target_duration"] = DurationTarget(
                    int(td["ideal"]), int(td["min"]), int(td["max"])
                )
        if "required_beats" in changes:
            kwargs["required_beats"] = tuple(changes["required_beats"])
        if "tone" in changes:
            kwargs["tone"] = tuple(changes["tone"])
        if "audience" in changes:
            kwargs["audience"] = tuple(changes["audience"])
        if "subtitle_policy" in changes:
            kwargs["subtitle_policy"] = changes["subtitle_policy"]
        if "language" in changes:
            kwargs["language"] = str(changes["language"])
        if "brand_requirements" in changes:
            raw_requirements = changes["brand_requirements"] or ()
            if isinstance(raw_requirements, Mapping):
                raw_requirements = (raw_requirements,)
            normalized_requirements: list[dict[str, Any]] = []
            for requirement in raw_requirements:
                if hasattr(requirement, "to_dict"):
                    requirement = requirement.to_dict()
                if not isinstance(requirement, Mapping):
                    raise BriefValidationError(
                        "brand_requirements must contain JSON objects"
                    )
                normalized_requirements.append(dict(requirement))
            kwargs["brand_requirements"] = tuple(normalized_requirements)
        return replace(brief, **kwargs)

    # ---------------- 约束管理 ---------------- #

    def add_hard_constraint(
        self, brief_id: str, constraint: HardConstraint
    ) -> EditorialBrief:
        """追加一条硬约束（违反即拒绝提交/导出）。"""
        b = self._require(brief_id)
        updated = replace(
            b,
            hard_constraints=b.hard_constraints + (constraint,),
            revision=b.revision + 1,
        )
        self._briefs[brief_id] = updated
        return updated

    def add_soft_preference(
        self, brief_id: str, pref: SoftPreference
    ) -> EditorialBrief:
        """追加一条软偏好（仅影响 variant 排序）。"""
        b = self._require(brief_id)
        updated = replace(
            b,
            soft_preferences=b.soft_preferences + (pref,),
            revision=b.revision + 1,
        )
        self._briefs[brief_id] = updated
        return updated

    def lock_source_range(
        self, brief_id: str, locked: LockedSourceRange
    ) -> EditorialBrief:
        """锁定一个 source range（作为 hard constraint，TAEP §5.3）。"""
        constraint = HardConstraint(
            kind="locked_source_range",
            spec={
                "asset_id": locked.asset_id,
                "source": locked.source.to_dict(),
                "label": locked.label,
            },
        )
        return self.add_hard_constraint(brief_id, constraint)

    def locked_source_ranges(self, brief_id: str) -> list[LockedSourceRange]:
        """取出当前所有锁定的 source range（供 planner 当硬约束读）。"""
        b = self._briefs.get(brief_id)
        if b is None:
            return []
        out: list[LockedSourceRange] = []
        for c in b.hard_constraints:
            if c.kind == "locked_source_range":
                spec = c.spec
                out.append(LockedSourceRange(
                    asset_id=str(spec.get("asset_id", "")),
                    source=FrameRange(
                        int(spec["source"]["start"]),
                        int(spec["source"]["duration"]),
                    ),
                    label=str(spec.get("label", "")),
                ))
        return out

    def forbidden_labels(self, brief_id: str) -> list[str]:
        """取出禁止的标签（如 competitor_logo / uncleared_music）。"""
        b = self._briefs.get(brief_id)
        if b is None:
            return []
        for c in b.hard_constraints:
            if c.kind == "forbidden_labels":
                return list(c.spec.get("labels", []))
        return []

    def must_include_claim_ids(self, brief_id: str) -> list[str]:
        """取出必须包含的 claim id。"""
        b = self._briefs.get(brief_id)
        if b is None:
            return []
        for c in b.hard_constraints:
            if c.kind == "must_include_claim_ids":
                return list(c.spec.get("claim_ids", []))
        return []

    # ---------------- 约束分类查询 ---------------- #

    def constraints_by_kind(
        self, brief_id: str, kind: ConstraintKind
    ) -> tuple[HardConstraint, ...] | tuple[SoftPreference, ...]:
        """按 hard/soft 分类返回约束。"""
        b = self._briefs.get(brief_id)
        if b is None:
            return ()
        if kind == ConstraintKind.HARD:
            return b.hard_constraints
        return b.soft_preferences

    # ---------------- patch 生成（AI 提议）---------------- #

    def propose_patch_from_text(
        self, brief_id: str, user_text: str
    ) -> BriefPatch:
        """从自然语言需求生成一个**提议性** patch（AI 提议，用户批准后生效）。

        首版是规则启发式（时长/平台关键词），真实意图理解由 provider 在 P9 接入。
        """
        b = self._briefs.get(brief_id)
        base = b.revision if b else 0
        changes: dict[str, Any] = {}

        text_lower = user_text.lower()
        # AUD-13109：关键词匹配统一用小写化文本（英文关键词按小写书写；
        # 中文关键词不受 lower() 影响）——"TikTok"/"Vertical"/"Fast" 等常规
        # 大小写输入不再漏判平台/语气推断。
        # 平台启发式
        if any(k in text_lower for k in ("竖版", "抖音", "竖屏", "vertical", "9:16", "tiktok")):
            from src.features.edit.ai_editing.editorial_models import Platform

            changes["platform"] = Platform.VERTICAL_SOCIAL
        elif any(k in text_lower for k in ("横版", "横屏", "horizontal", "16:9", "youtube")):
            from src.features.edit.ai_editing.editorial_models import Platform

            changes["platform"] = Platform.HORIZONTAL

        # 时长启发式（秒 → 帧，按 24fps 估算）
        import re

        m = re.search(r"(\d+)\s*秒", user_text)
        if m:
            seconds = int(m.group(1))
            frames = seconds * 24
            changes["target_duration"] = DurationTarget(
                frames, int(frames * 0.9), int(frames * 1.1)
            )

        # 语气启发式
        tones: list[str] = []
        if any(k in text_lower for k in ("快节奏", "快", "fast", "节奏")):
            tones.append("fast_paced")
        if any(k in text_lower for k in ("温暖", "warm", "亲切")):
            tones.append("warm")
        if any(k in text_lower for k in ("自信", "confident", "专业")):
            tones.append("confident")
        if tones:
            changes["tone"] = tuple(tones)

        # primary_goal
        if "primary_goal" not in changes and user_text.strip():
            changes["primary_goal"] = user_text.strip()[:200]

        return BriefPatch(base_revision=base, changes=changes, rationale=user_text[:200])

    def _require(self, brief_id: str) -> EditorialBrief:
        b = self._briefs.get(brief_id)
        if b is None:
            raise BriefValidationError(f"brief {brief_id!r} not found")
        return b

    # ---------------- variant 过期判定 ---------------- #

    def variants_need_replan(
        self, variant_brief_revisions: dict[str, int], current_revision: int
    ) -> list[str]:
        """brief 变更后，哪些 variant 需要重规划（TAEP §5.3）。

        Args:
            variant_brief_revisions: {variant_id: 它所基于的 brief_revision}。
            current_revision: 当前 brief revision。
        """
        return [
            vid for vid, rev in variant_brief_revisions.items()
            if rev != current_revision
        ]
