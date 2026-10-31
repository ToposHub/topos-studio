"""Evidence citation contracts for planner and commercial workflows (P4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class EvidenceValidation:
    valid: bool
    missing_refs: tuple[str, ...] = ()
    unknown_refs: tuple[str, ...] = ()
    unscoped_refs: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        parts: list[str] = []
        if self.missing_refs:
            parts.append(f"missing evidence refs: {list(self.missing_refs)}")
        if self.unknown_refs:
            parts.append(f"unknown evidence refs: {list(self.unknown_refs)}")
        if self.unscoped_refs:
            parts.append(f"evidence outside selected assets: {list(self.unscoped_refs)}")
        return "; ".join(parts)


def validate_evidence_selection(
    refs: Iterable[str],
    known_refs: Iterable[str] | Mapping[str, str],
    *,
    required_count: int = 1,
    allowed_asset_ids: Iterable[str] = (),
) -> EvidenceValidation:
    """Validate non-empty, known and selection-scoped evidence references.

    ``known_refs`` can be a set/sequence of IDs or ``segment_id -> asset_id``.
    Only IDs and asset IDs are returned in diagnostics; no transcript or prompt
    text is included.
    """
    selected = tuple(str(ref).strip() for ref in refs if str(ref).strip())
    required = max(0, int(required_count))
    missing = ("evidence_ref",) if len(selected) < required else ()
    if isinstance(known_refs, Mapping):
        known = set(known_refs)
        asset_by_ref = {
            str(key): str(getattr(value, "asset_id", value))
            for key, value in known_refs.items()
        }
    else:
        known = {str(ref) for ref in known_refs}
        asset_by_ref = {}
    unknown = tuple(sorted(set(selected) - known))
    allowed = {str(asset_id) for asset_id in allowed_asset_ids}
    unscoped = tuple(sorted(
        ref for ref in selected
        if allowed and ref in asset_by_ref and asset_by_ref[ref] not in allowed
    ))
    return EvidenceValidation(
        valid=not missing and not unknown and not unscoped,
        missing_refs=missing,
        unknown_refs=unknown,
        unscoped_refs=unscoped,
    )


__all__ = ["EvidenceValidation", "validate_evidence_selection"]
