"""General-purpose structured feedback store (OS-07).

OS-07 closes the gap between "用户说不满意" and "model knows what to change".
The MG-specific FeedbackPatch in ``teaching_animation/ai/revision.py`` proved
the shape; this module generalizes it to all domains (editorial / color /
composite / delivery) with a closed 11-category vocabulary.

The store is deliberately separate from ``decision_service.DecisionStore``:
that one records *decisions* (accept / reject / lock); this one records
*structured natural-language feedback* (which aspect is the user commenting
on, what exactly do they want changed, linked to which artifact).

Design rules (OS-07 acceptance):
* closed category vocabulary so feedback routes to the right planner adapter;
* every feedback patch links to a proposal / plan / artifact so it is
  traceable, never orphaned;
* feedback is scoped to a project + run so it cannot leak across contexts;
* no raw prompts / secrets / absolute paths — feedback text is sanitized
  through the existing ``sanitize_for_storage`` boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import threading
import time
import uuid
from typing import Any, Iterable, Mapping

from src.features.ai_harness.invocation_models import sanitize_for_storage


# --------------------------------------------------------------------------- #
# Feedback categories (OS-07: 11 dimensions)
# --------------------------------------------------------------------------- #

CAT_GOAL = "goal"             # 目标 / 整体意图
CAT_PACING = "pacing"         # 节奏 / 速度
CAT_SHOT = "shot"             # 镜头 / 素材选择
CAT_COPY = "copy"             # 文案 / 字幕文本
CAT_COLOR = "color"           # 颜色 / 调色
CAT_AUDIO = "audio"           # 音频 / 音乐 / 配音
CAT_BRAND = "brand"           # 品牌 / Logo / 规范
CAT_PLATFORM = "platform"     # 平台 / 画幅 / 交付规格
CAT_COST = "cost"             # 成本 / 预算
CAT_QUALITY = "quality"       # 质量 / 可读性 / 技术问题
CAT_RISK = "risk"             # 风险 / 安全 / 许可

SUPPORTED_FEEDBACK_CATEGORIES: frozenset[str] = frozenset({
    CAT_GOAL, CAT_PACING, CAT_SHOT, CAT_COPY, CAT_COLOR,
    CAT_AUDIO, CAT_BRAND, CAT_PLATFORM, CAT_COST, CAT_QUALITY, CAT_RISK,
})


# --------------------------------------------------------------------------- #
# Domain scope
# --------------------------------------------------------------------------- #

DOMAIN_EDITORIAL = "editorial"
DOMAIN_COLOR = "color"
DOMAIN_COMPOSITE = "composite"
DOMAIN_DELIVERY = "delivery"
DOMAIN_GENERAL = "general"

SUPPORTED_DOMAINS: frozenset[str] = frozenset({
    DOMAIN_EDITORIAL, DOMAIN_COLOR, DOMAIN_COMPOSITE, DOMAIN_DELIVERY, DOMAIN_GENERAL,
})


# --------------------------------------------------------------------------- #
# Feedback patch
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GeneralFeedbackPatch:
    """One structured piece of user feedback, generalized across domains.

    Attributes:
        patch_id: Stable identifier.
        project_id / run_id: Scope so feedback does not leak across projects
            or runs.
        domain: Which domain the feedback targets (editorial / color /
            composite / delivery / general).
        categories: Subset of :data:`SUPPORTED_FEEDBACK_CATEGORIES` touched.
        requested_changes: Mapping of concrete change requests (kept loose so
            each category adapts without a schema break).
        artifact_refs: Typed ids of the proposal / plan / element / clip /
            node the feedback is about, so it is always traceable.
        note: Short user note, already sanitized.
        created_at: Wall clock for ordering.
    """

    patch_id: str
    project_id: str
    run_id: str | None
    domain: str
    categories: tuple[str, ...]
    requested_changes: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    note: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.patch_id:
            raise ValueError("GeneralFeedbackPatch.patch_id is required")
        if not self.project_id:
            raise ValueError("GeneralFeedbackPatch.project_id is required")
        if self.domain not in SUPPORTED_DOMAINS:
            raise ValueError(
                f"Unsupported domain: {self.domain!r}; "
                f"expected one of {sorted(SUPPORTED_DOMAINS)}"
            )
        if not self.categories:
            raise ValueError("categories cannot be empty")
        unknown = set(self.categories) - SUPPORTED_FEEDBACK_CATEGORIES
        if unknown:
            raise ValueError(
                f"Unsupported feedback categories: {sorted(unknown)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "domain": self.domain,
            "categories": list(self.categories),
            "requested_changes": sanitize_for_storage(dict(self.requested_changes)),
            "artifact_refs": list(self.artifact_refs),
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GeneralFeedbackPatch":
        cats = data.get("categories", [])
        refs = data.get("artifact_refs", [])
        return cls(
            patch_id=str(data.get("patch_id", "")),
            project_id=str(data.get("project_id", "")),
            run_id=data.get("run_id"),
            domain=str(data.get("domain", DOMAIN_GENERAL)),
            categories=tuple(str(c) for c in cats) if isinstance(cats, (list, tuple)) else (),
            requested_changes=dict(data.get("requested_changes", {})),
            artifact_refs=tuple(str(r) for r in refs) if isinstance(refs, (list, tuple)) else (),
            note=str(data.get("note", "")),
            created_at=float(data.get("created_at", 0.0) or 0.0),
        )

    def hash(self) -> str:
        payload = {
            "project_id": self.project_id,
            "domain": self.domain,
            "categories": sorted(self.categories),
            "requested_changes": dict(self.requested_changes),
            "artifact_refs": sorted(self.artifact_refs),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class FeedbackStore:
    """Thread-safe append-only store of general feedback patches.

    The store answers two questions the planner / replan policy need:

    * "what did the user say about this artifact?" — :meth:`for_artifact`
    * "what categories has the user commented on in this run?" —
      :meth:`categories_covered` (drives "don't ask about aspects the user
      already vetoed").
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._patches: list[GeneralFeedbackPatch] = []

    def add(self, patch: GeneralFeedbackPatch) -> GeneralFeedbackPatch:
        with self._lock:
            self._patches.append(patch)
            return patch

    def create(
        self,
        *,
        project_id: str,
        run_id: str | None = None,
        domain: str,
        categories: Iterable[str],
        requested_changes: Mapping[str, Any] | None = None,
        artifact_refs: Iterable[str] | None = None,
        note: str = "",
        created_at: float | None = None,
    ) -> GeneralFeedbackPatch:
        patch = GeneralFeedbackPatch(
            patch_id=f"feedback-{uuid.uuid4().hex}",
            project_id=project_id,
            run_id=run_id,
            domain=domain,
            categories=tuple(categories),
            requested_changes=dict(requested_changes or {}),
            artifact_refs=tuple(artifact_refs or ()),
            # ``note`` may later be serialized into a project-scoped artifact
            # memory.  Sanitize at its creation boundary as well as in
            # ``to_dict`` so callers cannot accidentally reuse an unsanitized
            # in-memory patch as persisted context.
            note=str(sanitize_for_storage(str(note or "")))[:500],
            created_at=created_at if created_at is not None else time.time(),
        )
        return self.add(patch)

    # ---------------- queries ---------------- #

    def all_patches(self) -> tuple[GeneralFeedbackPatch, ...]:
        with self._lock:
            return tuple(self._patches)

    def for_project(self, project_id: str) -> tuple[GeneralFeedbackPatch, ...]:
        with self._lock:
            return tuple(p for p in self._patches if p.project_id == project_id)

    def for_run(self, run_id: str) -> tuple[GeneralFeedbackPatch, ...]:
        with self._lock:
            return tuple(p for p in self._patches if p.run_id == run_id)

    def for_artifact(self, artifact_ref: str) -> tuple[GeneralFeedbackPatch, ...]:
        with self._lock:
            return tuple(p for p in self._patches if artifact_ref in p.artifact_refs)

    def for_domain(self, domain: str, *, project_id: str | None = None) -> tuple[GeneralFeedbackPatch, ...]:
        with self._lock:
            return tuple(
                p for p in self._patches
                if p.domain == domain and (project_id is None or p.project_id == project_id)
            )

    def categories_covered(self, *, run_id: str | None = None, project_id: str | None = None) -> frozenset[str]:
        with self._lock:
            scoped = [
                p for p in self._patches
                if (run_id is None or p.run_id == run_id)
                and (project_id is None or p.project_id == project_id)
            ]
        return frozenset(cat for p in scoped for cat in p.categories)

    def latest(self, *, run_id: str | None = None, project_id: str | None = None) -> GeneralFeedbackPatch | None:
        with self._lock:
            scoped = [
                p for p in self._patches
                if (run_id is None or p.run_id == run_id)
                and (project_id is None or p.project_id == project_id)
            ]
            return scoped[-1] if scoped else None

    def clear_for_project(self, project_id: str) -> int:
        with self._lock:
            before = len(self._patches)
            self._patches = [p for p in self._patches if p.project_id != project_id]
            return before - len(self._patches)


__all__ = [
    "FeedbackStore",
    "GeneralFeedbackPatch",
    "SUPPORTED_FEEDBACK_CATEGORIES",
    "SUPPORTED_DOMAINS",
    "CAT_GOAL",
    "CAT_PACING",
    "CAT_SHOT",
    "CAT_COPY",
    "CAT_COLOR",
    "CAT_AUDIO",
    "CAT_BRAND",
    "CAT_PLATFORM",
    "CAT_COST",
    "CAT_QUALITY",
    "CAT_RISK",
    "DOMAIN_EDITORIAL",
    "DOMAIN_COLOR",
    "DOMAIN_COMPOSITE",
    "DOMAIN_DELIVERY",
    "DOMAIN_GENERAL",
]
