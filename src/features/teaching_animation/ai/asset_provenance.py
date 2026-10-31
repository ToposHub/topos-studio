"""Asset provenance and license boundary (MG-14).

MG-14 gives every external resource (generated image, downloaded font, stock
clip, TTS audio) a single :class:`AssetProvenance` record that tracks:

* provider + model + version that produced it;
* license / consent scope (where it may be used);
* cost paid for it;
* content hash for tamper detection;
* import approval state — only assets the user explicitly imported may enter
  a commercial export.

The :class:`AssetProvenanceStore` is append-only and JSON-safe.  The export
quality gate (MG-17/MG-18) queries it to refuse "this clip uses an asset
without a known license" before writing the final file.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import threading
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# License / approval states
# --------------------------------------------------------------------------- #


LICENSE_OK = "ok"
LICENSE_MISSING = "missing"
LICENSE_RESTRICTED = "restricted"
LICENSE_UNKNOWN = "unknown"
LICENSE_PENDING_REVIEW = "pending_review"

ALL_LICENSE_STATES: frozenset[str] = frozenset({
    LICENSE_OK, LICENSE_MISSING, LICENSE_RESTRICTED, LICENSE_UNKNOWN, LICENSE_PENDING_REVIEW,
})

IMPORT_PENDING = "pending"
IMPORT_APPROVED = "approved"
IMPORT_REJECTED = "rejected"

ALL_IMPORT_STATES: frozenset[str] = frozenset({IMPORT_PENDING, IMPORT_APPROVED, IMPORT_REJECTED})


# --------------------------------------------------------------------------- #
# Provenance record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AssetProvenance:
    """One external resource's provenance + license record."""

    asset_id: str
    source_kind: str                   # generated / downloaded / imported / builtin
    provider: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    prompt_hash: str | None = None     # sha256 of the generation prompt (not the prompt)
    content_hash: str | None = None    # sha256 of the bytes (tamper detection)
    cost_units: float = 0.0
    license_state: str = LICENSE_UNKNOWN
    license_scope: tuple[str, ...] = ()  # e.g. ("commercial", "web")
    license_url: str | None = None
    consent_scope: tuple[str, ...] = ()
    created_at: float = 0.0
    import_state: str = IMPORT_PENDING
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.asset_id:
            raise ValueError("AssetProvenance.asset_id is required")
        if self.license_state not in ALL_LICENSE_STATES:
            raise ValueError(f"Unknown license_state: {self.license_state!r}")
        if self.import_state not in ALL_IMPORT_STATES:
            raise ValueError(f"Unknown import_state: {self.import_state!r}")
        if self.cost_units < 0:
            raise ValueError("cost_units cannot be negative")

    @property
    def may_enter_commercial_export(self) -> bool:
        return (
            self.license_state == LICENSE_OK
            and self.import_state == IMPORT_APPROVED
            and "commercial" in self.license_scope
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "source_kind": self.source_kind,
            "provider": self.provider,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_hash": self.prompt_hash,
            "content_hash": self.content_hash,
            "cost_units": self.cost_units,
            "license_state": self.license_state,
            "license_scope": list(self.license_scope),
            "license_url": self.license_url,
            "consent_scope": list(self.consent_scope),
            "created_at": self.created_at,
            "import_state": self.import_state,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AssetProvenance":
        return cls(
            asset_id=str(data.get("asset_id", "")),
            source_kind=str(data.get("source_kind", "")),
            provider=data.get("provider"),
            model_id=data.get("model_id"),
            model_version=data.get("model_version"),
            prompt_hash=data.get("prompt_hash"),
            content_hash=data.get("content_hash"),
            cost_units=float(data.get("cost_units", 0.0) or 0.0),
            license_state=str(data.get("license_state", LICENSE_UNKNOWN)),
            license_scope=tuple(str(item) for item in data.get("license_scope", [])),
            license_url=data.get("license_url"),
            consent_scope=tuple(str(item) for item in data.get("consent_scope", [])),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            import_state=str(data.get("import_state", IMPORT_PENDING)),
            notes=str(data.get("notes", "")),
        )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class AssetProvenanceStore:
    """Thread-safe append-only provenance store.

    Records are immutable; updating license / import state creates a new
    record with the same ``asset_id`` (the old one is kept for audit).  The
    :meth:`latest` method returns the newest record per asset id.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: list[AssetProvenance] = []

    def record(self, provenance: AssetProvenance) -> None:
        with self._lock:
            self._records.append(provenance)

    def latest(self, asset_id: str) -> AssetProvenance | None:
        with self._lock:
            for record in reversed(self._records):
                if record.asset_id == asset_id:
                    return record
            return None

    def history(self, asset_id: str) -> tuple[AssetProvenance, ...]:
        with self._lock:
            return tuple(r for r in self._records if r.asset_id == asset_id)

    def all_records(self) -> tuple[AssetProvenance, ...]:
        with self._lock:
            return tuple(self._records)

    # ---------------- mutations ---------------- #

    def set_license_state(
        self,
        asset_id: str,
        *,
        license_state: str,
        license_scope: Iterable[str] = (),
        license_url: str | None = None,
    ) -> AssetProvenance:
        with self._lock:
            current = self.latest(asset_id)
            if current is None:
                raise KeyError(f"Unknown asset: {asset_id!r}")
            updated = AssetProvenance(
                asset_id=current.asset_id,
                source_kind=current.source_kind,
                provider=current.provider,
                model_id=current.model_id,
                model_version=current.model_version,
                prompt_hash=current.prompt_hash,
                content_hash=current.content_hash,
                cost_units=current.cost_units,
                license_state=license_state,
                license_scope=tuple(license_scope) or current.license_scope,
                license_url=license_url or current.license_url,
                consent_scope=current.consent_scope,
                created_at=current.created_at,
                import_state=current.import_state,
                notes=current.notes,
            )
            self._records.append(updated)
            return updated

    def set_import_state(
        self,
        asset_id: str,
        *,
        import_state: str,
    ) -> AssetProvenance:
        with self._lock:
            current = self.latest(asset_id)
            if current is None:
                raise KeyError(f"Unknown asset: {asset_id!r}")
            updated = AssetProvenance(
                asset_id=current.asset_id,
                source_kind=current.source_kind,
                provider=current.provider,
                model_id=current.model_id,
                model_version=current.model_version,
                prompt_hash=current.prompt_hash,
                content_hash=current.content_hash,
                cost_units=current.cost_units,
                license_state=current.license_state,
                license_scope=current.license_scope,
                license_url=current.license_url,
                consent_scope=current.consent_scope,
                created_at=current.created_at,
                import_state=import_state,
                notes=current.notes,
            )
            self._records.append(updated)
            return updated

    # ---------------- queries for the export gate ---------------- #

    def blocking_for_export(self, asset_ids: Iterable[str]) -> tuple[AssetProvenance, ...]:
        """Return assets that block a commercial export (missing license/import)."""
        blocking: list[AssetProvenance] = []
        for asset_id in asset_ids:
            record = self.latest(asset_id)
            if record is None or not record.may_enter_commercial_export:
                blocking.append(record or AssetProvenance(asset_id=asset_id, source_kind="unknown"))
        return tuple(blocking)

    def total_cost(self) -> float:
        with self._lock:
            seen: set[str] = set()
            total = 0.0
            for record in reversed(self._records):
                if record.asset_id in seen:
                    continue
                seen.add(record.asset_id)
                total += record.cost_units
            return total


# --------------------------------------------------------------------------- #
# Hash helpers
# --------------------------------------------------------------------------- #


def hash_prompt(prompt: str) -> str:
    """Stable sha256 of a generation prompt (never store the prompt itself)."""
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def hash_content(bytes_payload: bytes) -> str:
    return hashlib.sha256(bytes_payload).hexdigest()


__all__ = [
    "AssetProvenance",
    "AssetProvenanceStore",
    "ALL_LICENSE_STATES",
    "ALL_IMPORT_STATES",
    "LICENSE_OK",
    "LICENSE_MISSING",
    "LICENSE_RESTRICTED",
    "LICENSE_UNKNOWN",
    "LICENSE_PENDING_REVIEW",
    "IMPORT_PENDING",
    "IMPORT_APPROVED",
    "IMPORT_REJECTED",
    "hash_prompt",
    "hash_content",
]
