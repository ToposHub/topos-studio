# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Layered test matrix definition for Motion Graphic pipeline (MG-25).

MG-25 makes the testing contract explicit: every P0/P1 capability must have
focused tests at the layers that matter to it (unit / integration / UI / fault
injection / real environment / human acceptance — a capability declares which
layers count as its coverage via ``TestRequirement.any_layer_of``), and
real-provider evidence must never be confused with mock evidence.

This module is the *catalog*, not the tests themselves.  It defines a typed
:class:`TestLayer` vocabulary, a :class:`TestRequirement` per capability, and
a :class:`TestCoverageReport` that the host (and CI) can compute from the
actual test collection.  Keeping the catalog next to the code means a new
capability forces a deliberate decision about what layer of test it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


# --------------------------------------------------------------------------- #
# Layers
# --------------------------------------------------------------------------- #


LAYER_UNIT = "unit"
LAYER_INTEGRATION = "integration"
LAYER_UI = "ui"
LAYER_FAULT_INJECTION = "fault_injection"
LAYER_REAL_ENVIRONMENT = "real_environment"
LAYER_HUMAN_ACCEPTANCE = "human_acceptance"

ALL_LAYERS: tuple[str, ...] = (
    LAYER_UNIT,
    LAYER_INTEGRATION,
    LAYER_UI,
    LAYER_FAULT_INJECTION,
    LAYER_REAL_ENVIRONMENT,
    LAYER_HUMAN_ACCEPTANCE,
)

#: Layers that must never depend on a paid API key (CI-runnable).
CI_LAYERS: frozenset[str] = frozenset({
    LAYER_UNIT, LAYER_INTEGRATION, LAYER_UI, LAYER_FAULT_INJECTION,
})

#: Layers whose evidence is only valid when recorded against a real provider.
REAL_ONLY_LAYERS: frozenset[str] = frozenset({LAYER_REAL_ENVIRONMENT})


# --------------------------------------------------------------------------- #
# Requirement
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TestRequirement:
    """What a capability must be covered by.

    ``any_layer_of`` declares the layers that count as coverage: the
    capability counts as covered when *at least one* of these layers has a
    recorded test.  AUD-27114 renamed the field from ``required_layers`` so
    the name matches the any-of semantics instead of implying that every
    listed layer is mandatory.
    """

    capability_id: str            # e.g. "MG-04"
    description: str
    any_layer_of: tuple[str, ...]
    real_provider_only: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        for layer in self.any_layer_of:
            if layer not in ALL_LAYERS:
                raise ValueError(f"Unknown test layer: {layer!r}")

    def to_dict(self) -> dict[str, str | bool | list[str]]:
        return {
            "capability_id": self.capability_id,
            "description": self.description,
            "any_layer_of": list(self.any_layer_of),
            "real_provider_only": self.real_provider_only,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# Matrix (MG-25 acceptance: every P0/P1 covered)
# --------------------------------------------------------------------------- #


DEFAULT_MATRIX: tuple[TestRequirement, ...] = (
    TestRequirement(
        "MG-01", "Structured brief with revision history",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    ),
    TestRequirement(
        "MG-02", "Typed proposal card view-model",
        any_layer_of=(LAYER_UNIT, LAYER_UI),
    ),
    TestRequirement(
        "MG-03", "Playable preview + storyboard + diff",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION, LAYER_UI),
    ),
    TestRequirement(
        "MG-04", "Three-stage brief → proposal → plan",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
        real_provider_only=False,
    ),
    TestRequirement(
        "MG-05", "Explicit fallback state machine",
        any_layer_of=(LAYER_UNIT, LAYER_FAULT_INJECTION),
    ),
    TestRequirement(
        "MG-06", "Approval scope + one-shot grant + stale detection",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION, LAYER_FAULT_INJECTION),
    ),
    TestRequirement(
        "MG-07", "Controlled Job DAG",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    ),
    TestRequirement(
        "MG-09", "Extended command catalog",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    ),
    TestRequirement(
        "MG-10", "Design / render / accessibility validator",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    ),
    TestRequirement(
        "MG-11", "Delivery profile + responsive layout",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    ),
    TestRequirement(
        "MG-14", "Asset provenance + license boundary",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    ),
    TestRequirement(
        "MG-16", "Run lifecycle + cancel + resume + snapshot",
        any_layer_of=(LAYER_UNIT, LAYER_FAULT_INJECTION),
    ),
    TestRequirement(
        "MG-17", "Background export + atomic output + postflight",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION, LAYER_FAULT_INJECTION),
    ),
    TestRequirement(
        "MG-18", "Motion Graphic quality gate",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    ),
    TestRequirement(
        "MG-20", "Persistent session store + grant invalidation",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION, LAYER_FAULT_INJECTION),
    ),
    TestRequirement(
        "MG-21", "Scheduler lanes + concurrency + budget",
        any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    ),
    TestRequirement(
        "MG-22", "Action permission matrix",
        any_layer_of=(LAYER_UNIT,),
    ),
    TestRequirement(
        "MG-23", "Stable error codes + audit redaction",
        any_layer_of=(LAYER_UNIT,),
    ),
    TestRequirement(
        "E2E", "End-to-end Motion Graphic creation (real provider)",
        any_layer_of=(LAYER_REAL_ENVIRONMENT, LAYER_HUMAN_ACCEPTANCE),
        real_provider_only=True,
        notes="Must not be replaced by mock evidence in release sign-off.",
    ),
)


# --------------------------------------------------------------------------- #
# Coverage report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TestCoverageReport:
    """Result of mapping collected tests onto the matrix."""

    covered: tuple[str, ...]
    missing: tuple[str, ...]
    real_evidence_recorded: tuple[str, ...]
    mock_only: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:  # type: ignore[name-defined]
        return {
            "covered": list(self.covered),
            "missing": list(self.missing),
            "real_evidence_recorded": list(self.real_evidence_recorded),
            "mock_only": list(self.mock_only),
            "is_complete": self.is_complete,
        }


def compute_coverage(
    matrix: Iterable[TestRequirement],
    *,
    collected_by_capability: Mapping[str, tuple[str, ...]],
    real_evidence_capabilities: Iterable[str] = (),
) -> TestCoverageReport:
    """Compute coverage from what tests were actually collected.

    ``collected_by_capability`` maps a capability id (``"MG-04"``) to the
    layers that have at least one recorded test.  Capabilities absent from
    the mapping are treated as missing.  A capability counts as covered when
    at least one of its ``any_layer_of`` layers is present — the any-of
    semantics the field name documents (AUD-27114).
    """

    covered: list[str] = []
    missing: list[str] = []
    real_set = set(real_evidence_capabilities)
    real_recorded: list[str] = []
    mock_only: list[str] = []
    for requirement in matrix:
        layers_present = collected_by_capability.get(requirement.capability_id, ())
        if layers_present and any(layer in layers_present for layer in requirement.any_layer_of):
            covered.append(requirement.capability_id)
            if requirement.real_provider_only:
                if requirement.capability_id in real_set:
                    real_recorded.append(requirement.capability_id)
                else:
                    mock_only.append(requirement.capability_id)
        else:
            missing.append(requirement.capability_id)
    return TestCoverageReport(
        covered=tuple(covered),
        missing=tuple(missing),
        real_evidence_recorded=tuple(real_recorded),
        mock_only=tuple(mock_only),
    )


# --------------------------------------------------------------------------- #
# Typing helper used in dataclass above
# --------------------------------------------------------------------------- #


from typing import Any  # noqa: E402


__all__ = [
    "TestRequirement",
    "TestCoverageReport",
    "DEFAULT_MATRIX",
    "ALL_LAYERS",
    "CI_LAYERS",
    "REAL_ONLY_LAYERS",
    "LAYER_UNIT",
    "LAYER_INTEGRATION",
    "LAYER_UI",
    "LAYER_FAULT_INJECTION",
    "LAYER_REAL_ENVIRONMENT",
    "LAYER_HUMAN_ACCEPTANCE",
    "compute_coverage",
]
