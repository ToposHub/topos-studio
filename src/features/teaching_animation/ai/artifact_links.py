"""Typed bidirectional references between Motion Graphic artifacts (MG-19).

MG-19 connects the result card to the objects it talks about: a scene, a
clip, a timeline command, a preview, an export, a run.  Today these are
scattered strings (``asset_id = "teaching_animation:{scene_id}"``); the card
cannot reliably answer "open this in the scene editor" or "jump to this clip
on the timeline" because it has no typed linkage.

This module defines a single :class:`MotionGraphicArtifactLink` graph node
plus a small in-memory :class:`ArtifactLinkIndex`.  Every artifact the
pipeline produces is registered with typed refs; the card / inspector / run
drawer query the index to navigate.

Design rules:

* refs are typed (kind + id), never bare strings;
* the index is append-only and Qt-free;
* no secret / path / prompt is stored — refs only carry ids and a short,
  pre-sanitized label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import uuid
from typing import Iterable, Mapping


# --------------------------------------------------------------------------- #
# Reference kinds
# --------------------------------------------------------------------------- #


class ArtifactRefKind(str):
    """What kind of object a reference points at."""


REF_BRIEF = "brief"
REF_PROPOSAL = "proposal"
REF_PLAN = "plan"
REF_REVISION = "revision"
REF_SCENE = "scene"
REF_ELEMENT = "element"
REF_CLIP = "clip"
REF_TIMELINE_COMMAND = "timeline_command"
REF_TRACK = "track"
REF_PREVIEW = "preview"
REF_STORYBOARD_FRAME = "storyboard_frame"
REF_QUALITY_REPORT = "quality_report"
REF_EXPORT = "export"
REF_RUN = "run"
REF_RUN_NODE = "run_node"
REF_AUDIT_EVENT = "audit_event"

ALL_REF_KINDS: frozenset[str] = frozenset({
    REF_BRIEF, REF_PROPOSAL, REF_PLAN, REF_REVISION, REF_SCENE, REF_ELEMENT,
    REF_CLIP, REF_TIMELINE_COMMAND, REF_TRACK, REF_PREVIEW,
    REF_STORYBOARD_FRAME, REF_QUALITY_REPORT, REF_EXPORT, REF_RUN,
    REF_RUN_NODE, REF_AUDIT_EVENT,
})


@dataclass(frozen=True)
class ArtifactRef:
    """A typed pointer to one object in the Motion Graphic pipeline."""

    kind: str
    ref_id: str
    label: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ALL_REF_KINDS:
            raise ValueError(f"Unknown artifact ref kind: {self.kind!r}")
        if not self.ref_id:
            raise ValueError("ArtifactRef.ref_id is required")

    @classmethod
    def brief(cls, brief_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_BRIEF, brief_id, label=label)

    @classmethod
    def proposal(cls, proposal_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_PROPOSAL, proposal_id, label=label)

    @classmethod
    def plan(cls, plan_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_PLAN, plan_id, label=label)

    @classmethod
    def revision(cls, revision_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_REVISION, revision_id, label=label)

    @classmethod
    def scene(cls, scene_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_SCENE, scene_id, label=label)

    @classmethod
    def element(cls, scene_id: str, element_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_ELEMENT, f"{scene_id}#{element_id}", label=label)

    @classmethod
    def clip(cls, clip_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_CLIP, clip_id, label=label)

    @classmethod
    def timeline_command(cls, command_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_TIMELINE_COMMAND, command_id, label=label)

    @classmethod
    def track(cls, track_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_TRACK, track_id, label=label)

    @classmethod
    def preview(cls, preview_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_PREVIEW, preview_id, label=label)

    @classmethod
    def storyboard_frame(cls, preview_id: str, frame_index: int, *, label: str = "") -> "ArtifactRef":
        return cls(REF_STORYBOARD_FRAME, f"{preview_id}#{frame_index}", label=label)

    @classmethod
    def quality_report(cls, report_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_QUALITY_REPORT, report_id, label=label)

    @classmethod
    def export_ref(cls, export_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_EXPORT, export_id, label=label)

    @classmethod
    def run(cls, run_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_RUN, run_id, label=label)

    @classmethod
    def run_node(cls, run_id: str, node_id: str, *, label: str = "") -> "ArtifactRef":
        return cls(REF_RUN_NODE, f"{run_id}#{node_id}", label=label)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref_id": self.ref_id, "label": self.label}


# --------------------------------------------------------------------------- #
# Link graph node
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicArtifactLink:
    """One node in the artifact link graph plus its relationships.

    ``source`` is the artifact this link describes; ``targets`` are the
    artifacts it points at (e.g. a clip → its scene, a scene → its elements,
    a run node → the preview it produced).
    """

    link_id: str
    source: ArtifactRef
    targets: tuple[ArtifactRef, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "link_id": self.link_id,
            "source": self.source.to_dict(),
            "targets": [target.to_dict() for target in self.targets],
            "metadata": dict(self.metadata),
        }


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #


class ArtifactLinkIndex:
    """Thread-safe append-only index of artifact links.

    The index supports:

    * :meth:`link` — register a source ref → many target refs;
    * :meth:`targets_of` — what does this ref point at?;
    * :meth:`sources_of` — what points at this ref? (the "open the clip from
      the scene editor" navigation);
    * :meth:`reached_from` — transitive reachability for "find me everything
      produced by this run".
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._outgoing: dict[tuple[str, str], set[ArtifactRef]] = {}
        self._incoming: dict[tuple[str, str], set[ArtifactRef]] = {}
        self._links: dict[str, MotionGraphicArtifactLink] = {}

    def link(
        self,
        source: ArtifactRef,
        targets: Iterable[ArtifactRef],
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> MotionGraphicArtifactLink:
        link = MotionGraphicArtifactLink(
            link_id=f"mg-link-{uuid.uuid4().hex}",
            source=source,
            targets=tuple(targets),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._links[link.link_id] = link
            source_key = (source.kind, source.ref_id)
            outgoing = self._outgoing.setdefault(source_key, set())
            for target in link.targets:
                outgoing.add(target)
                target_key = (target.kind, target.ref_id)
                self._incoming.setdefault(target_key, set()).add(source)
            return link

    def targets_of(self, ref: ArtifactRef) -> tuple[ArtifactRef, ...]:
        with self._lock:
            return tuple(self._outgoing.get((ref.kind, ref.ref_id), set()))

    def sources_of(self, ref: ArtifactRef) -> tuple[ArtifactRef, ...]:
        with self._lock:
            return tuple(self._incoming.get((ref.kind, ref.ref_id), set()))

    def reached_from(self, ref: ArtifactRef, *, max_depth: int = 8) -> tuple[ArtifactRef, ...]:
        """Transitive forward reach (what can I get to from here?)."""
        visited: set[tuple[str, str]] = set()
        ordered: list[ArtifactRef] = []
        with self._lock:
            self._walk(ref, visited, ordered, depth=0, max_depth=max_depth)
        return tuple(ordered)

    def _walk(
        self,
        ref: ArtifactRef,
        visited: set[tuple[str, str]],
        ordered: list[ArtifactRef],
        *,
        depth: int,
        max_depth: int,
    ) -> None:
        key = (ref.kind, ref.ref_id)
        if key in visited or depth > max_depth:
            return
        visited.add(key)
        ordered.append(ref)
        for target in self._outgoing.get(key, set()):
            self._walk(target, visited, ordered, depth=depth + 1, max_depth=max_depth)

    def all_links(self) -> tuple[MotionGraphicArtifactLink, ...]:
        with self._lock:
            return tuple(self._links.values())

    def clear(self) -> None:
        with self._lock:
            self._outgoing.clear()
            self._incoming.clear()
            self._links.clear()


__all__ = [
    "ArtifactRef",
    "ArtifactRefKind",
    "MotionGraphicArtifactLink",
    "ArtifactLinkIndex",
    "ALL_REF_KINDS",
    "REF_BRIEF",
    "REF_PROPOSAL",
    "REF_PLAN",
    "REF_REVISION",
    "REF_SCENE",
    "REF_ELEMENT",
    "REF_CLIP",
    "REF_TIMELINE_COMMAND",
    "REF_TRACK",
    "REF_PREVIEW",
    "REF_STORYBOARD_FRAME",
    "REF_QUALITY_REPORT",
    "REF_EXPORT",
    "REF_RUN",
    "REF_RUN_NODE",
    "REF_AUDIT_EVENT",
]
