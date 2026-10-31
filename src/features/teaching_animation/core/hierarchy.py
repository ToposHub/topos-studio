"""SG-01: real scene hierarchy — groups, parents, world transforms, solo/lock.

A flat element list cannot express "move this group and all its children" or
"inherit a transform/visibility".  This module adds a real hierarchy WITHOUT a
schema-version bump: a ``Hierarchy`` (parent links + lock/solo/hide flags) is
stored in ``scene.metadata`` under a versioned key, and resolves world-space
transforms and effective visibility by walking parent chains.

Invariants enforced (plan §SG-01 / §3.1):
* no cycles (a child can never become its own ancestor);
* every parent_id references an existing element in the same scene (no
  cross-scene references — instances use ``SceneInstance`` instead);
* world transform = parent_world ∘ local, accumulated along the chain;
* visibility: a hidden ancestor hides all descendants; ``solo`` isolates the
  soloed subtree (only soloed elements + their descendants are visible).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .model import SceneTransform


_HIERARCHY_KEY = "hierarchy"
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Affine2d:
    """A 2D affine transform [a c e; b d f; 0 0 1] for transform composition."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    @classmethod
    def from_transform(cls, t: SceneTransform) -> "Affine2d":
        """translate(x,y) ∘ rotate(r) ∘ scale(sx,sy), origin at (x,y)."""
        import math
        cos = math.cos(math.radians(t.rotation))
        sin = math.sin(math.radians(t.rotation))
        sx, sy = t.scale_x, t.scale_y
        # M = T(x,y) * R * S  (column-vector convention)
        a = cos * sx
        b = sin * sx
        c = -sin * sy
        d = cos * sy
        return cls(a=a, b=b, c=c, d=d, e=t.x, f=t.y)

    def compose(self, other: "Affine2d") -> "Affine2d":
        """Return self ∘ other (other applied first)."""
        return Affine2d(
            a=self.a * other.a + self.c * other.b,
            b=self.b * other.a + self.d * other.b,
            c=self.a * other.c + self.c * other.d,
            d=self.b * other.c + self.d * other.d,
            e=self.a * other.e + self.c * other.f + self.e,
            f=self.b * other.e + self.d * other.f + self.f,
        )

    def transform_point(self, x: float, y: float) -> tuple[float, float]:
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)


@dataclass(frozen=True)
class Hierarchy:
    """Parent/flag map for a scene's elements."""

    parents: Mapping[str, str] = field(default_factory=dict)   # child_id -> parent_id
    locked: frozenset[str] = field(default_factory=frozenset)
    solo: frozenset[str] = field(default_factory=frozenset)
    hidden: frozenset[str] = field(default_factory=frozenset)
    schema_version: int = _SCHEMA_VERSION

    def parent_of(self, element_id: str) -> str | None:
        return self.parents.get(element_id)

    def children_of(self, element_id: str) -> tuple[str, ...]:
        return tuple(child for child, parent in self.parents.items() if parent == element_id)

    def ancestors(self, element_id: str) -> tuple[str, ...]:
        chain: list[str] = []
        current = self.parents.get(element_id)
        seen: set[str] = set()
        while current is not None and current not in seen:
            chain.append(current)
            seen.add(current)
            current = self.parents.get(current)
        return tuple(chain)

    def is_ancestor(self, candidate: str, element_id: str) -> bool:
        return candidate in self.ancestors(element_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "parents": dict(self.parents),
            "locked": sorted(self.locked), "solo": sorted(self.solo),
            "hidden": sorted(self.hidden),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> "Hierarchy":
        data = data or {}
        return cls(
            parents={str(k): str(v) for k, v in (data.get("parents") or {}).items()},  # type: ignore[union-attr]
            locked=frozenset(str(x) for x in (data.get("locked") or [])),
            solo=frozenset(str(x) for x in (data.get("solo") or [])),
            hidden=frozenset(str(x) for x in (data.get("hidden") or [])),
        )


@dataclass(frozen=True)
class HierarchyIssue:
    element_id: str
    code: str        # missing_parent | cycle | duplicate_parent
    message: str


def validate_hierarchy(element_ids: Iterable[str], hierarchy: Hierarchy) -> tuple[HierarchyIssue, ...]:
    """Check references exist and no cycles.  Empty tuple == valid."""
    known = set(element_ids)
    issues: list[HierarchyIssue] = []
    seen_children: set[str] = set()
    for child, parent in hierarchy.parents.items():
        if child in seen_children:
            issues.append(HierarchyIssue(child, "duplicate_parent", "element has multiple parents"))
        seen_children.add(child)
        if child not in known:
            issues.append(HierarchyIssue(child, "missing_parent", "child not in scene"))
            continue
        if parent not in known:
            issues.append(HierarchyIssue(child, "missing_parent", f"parent {parent} not in scene"))
        if hierarchy.is_ancestor(child, child) or _creates_cycle(child, parent, hierarchy):
            issues.append(HierarchyIssue(child, "cycle", f"{child} would create a cycle via {parent}"))
    return tuple(issues)


def _creates_cycle(child: str, parent: str, hierarchy: Hierarchy) -> bool:
    # Walking up from parent, if we reach child, it's a cycle.
    current = parent
    seen: set[str] = set()
    while current is not None and current not in seen:
        if current == child:
            return True
        seen.add(current)
        current = hierarchy.parents.get(current)
    return False


def world_affine(element_id: str, transforms: Mapping[str, SceneTransform],
                 hierarchy: Hierarchy) -> Affine2d:
    """Compose local + ancestor transforms into world space (parent applied first)."""
    local = Affine2d.from_transform(transforms[element_id])
    result = local
    for ancestor in hierarchy.ancestors(element_id):
        if ancestor not in transforms:
            continue
        result = Affine2d.from_transform(transforms[ancestor]).compose(result)
    return result


def effective_visibility(element_id: str, hierarchy: Hierarchy, *,
                         local_visible: bool = True) -> bool:
    """Resolve visibility honouring hidden ancestors + solo isolation."""
    if not local_visible or element_id in hierarchy.hidden:
        return False
    for ancestor in hierarchy.ancestors(element_id):
        if ancestor in hierarchy.hidden:
            return False
    if hierarchy.solo:
        # When anything is soloed, only soloed subtrees are visible.
        if element_id in hierarchy.solo:
            return True
        return any(a in hierarchy.solo for a in hierarchy.ancestors(element_id))
    return True


def read_hierarchy(metadata: Mapping[str, object]) -> Hierarchy:
    raw = metadata.get(_HIERARCHY_KEY) if isinstance(metadata, Mapping) else None
    return Hierarchy.from_dict(raw if isinstance(raw, Mapping) else None)  # type: ignore[arg-type]


def write_hierarchy_into(metadata: Mapping[str, object], hierarchy: Hierarchy) -> dict[str, object]:
    """Return a new metadata dict with the hierarchy stored under its versioned key."""
    new = dict(metadata) if isinstance(metadata, Mapping) else {}
    new[_HIERARCHY_KEY] = hierarchy.to_dict()
    return new


def sort_for_paint(
    elements: Any,
    parents: Mapping[str, str],
    *,
    id_of: Any = None,
    z_of: Any = None,
) -> list:
    """Order ``elements`` for painting with ancestors strictly before descendants.

    ``z_index`` stays the primary key among unrelated layers and siblings —
    exactly like the DOM overlay, where a frame's background plate paints
    below the layers nested inside it.  A flat z sort instead hid every child
    behind its own frame whenever the frame was authored later (higher z),
    making the canvas/export disagree with the live browser preview.
    ``id_of`` / ``z_of`` adapt non-SceneElement inputs (e.g. EvaluatedElement).
    """
    get_id = id_of or (lambda element: element.element_id)
    get_z = z_of or (lambda element: element.z_index)
    z_by_id = {get_id(element): get_z(element) for element in elements}

    def sort_key(element: Any) -> tuple[int, ...]:
        chain: list[int] = []
        seen: set[str] = set()
        current: str | None = get_id(element)
        while current is not None and current in z_by_id and current not in seen:
            seen.add(current)
            chain.append(int(z_by_id[current]))
            current = parents.get(current)
        chain.reverse()  # root-first so a parent's tuple prefixes its children's
        return tuple(chain)

    return sorted(elements, key=sort_key)


__all__ = [
    "Affine2d", "Hierarchy", "HierarchyIssue",
    "effective_visibility", "read_hierarchy", "sort_for_paint", "validate_hierarchy",
    "world_affine", "write_hierarchy_into",
]
