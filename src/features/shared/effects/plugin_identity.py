"""FX-M07: Stable plugin identity, versioning, and failure recovery.

Problem
-------
Effect plugins (.tglsl / .tai) are referenced in project files solely by
their ``effect_type`` string (e.g. ``"plugin:Film_Halation"``). When a
plugin file is moved, renamed, updated to a new version, or its parameter
schema changes, the project has no way to detect the mismatch — the effect
silently becomes a no-op or renders with wrong parameters.

Solution
--------
This module defines:

* ``PluginIdentity`` — an immutable snapshot of a plugin's stable identity
  (plugin_id, version, content_hash, source_uri, parameter_schema_hash)
  that is serialized alongside each ``EffectInstance`` in the project file.

* ``PluginStatus`` — an enum of resolution states the loader can produce
  when comparing a stored identity against the currently-available catalog.

* ``PluginStatusReport`` — a per-instance diagnostic produced by validation.

* Hash utilities — ``compute_content_hash`` and
  ``compute_parameter_schema_hash`` for deterministic identity fingerprinting.

* ``resolve_plugin_status`` — the core comparison logic used at project load
  and at export preflight.

* ``validate_effect_instances`` — batch validation for a clip's effect stack.

* ``migrate_legacy_identity`` — backfill identity for old projects that
  predate this system (marked LEGACY until the user confirms).

Design constraints:
  - Pure data, no GL/Qt imports (unit-testable headlessly).
  - Never mutates EffectInstance.params — identity is metadata only.
  - Content hash is SHA-256 of the concatenated shader source strings.
  - Parameter schema hash is SHA-256 of the canonical JSON of param_defs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.features.shared.effects.effect_spec import EffectSpec

logger = logging.getLogger(__name__)


# =============================================================================
# PluginStatus enum
# =============================================================================

class PluginStatus(str, Enum):
    """Resolution state of a plugin effect instance.

    Values are strings for clean serialization and UI badge rendering.
    """

    ACTIVE = "active"
    # Plugin found, version and schema match — normal operation.

    MISSING = "missing"
    # Plugin file not found on this machine / not in catalog.

    VERSION_MISMATCH = "version_mismatch"
    # Plugin found by ID but its declared version differs from the stored one.

    SCHEMA_MISMATCH = "schema_mismatch"
    # Plugin found, version matches (or absent), but parameter schema changed.

    CONTENT_CHANGED = "content_changed"
    # Plugin found, version/schema match, but shader source hash differs
    # (e.g. a bugfix that didn't bump version). Non-blocking but worth noting.

    COMPILE_ERROR = "compile_error"
    # Plugin source is present but the shader failed to compile on this GPU.

    UNSUPPORTED_BACKEND = "unsupported_backend"
    # Plugin requires a backend capability this machine lacks (e.g. compute
    # shader on a GL 3.3 context).

    LEGACY = "legacy"
    # Project predates the identity system; no stored identity to compare.
    # The plugin may work fine, but we cannot guarantee it's the same one.


# =============================================================================
# PluginIdentity — serialized identity snapshot
# =============================================================================

@dataclass(frozen=True)
class PluginIdentity:
    """Immutable snapshot of a plugin's identity at the time it was applied.

    Serialized into ``EffectInstance.plugin_identity`` (as a dict) so the
    project file carries enough information to detect drift.

    Attributes:
        plugin_id: Stable identifier (filename without extension for .tglsl).
        version: Semantic version string declared by the plugin author
            (``@version`` directive). Empty string if undeclared.
        content_hash: SHA-256 hex digest of the concatenated shader source(s).
        source_uri: Original file path or URI the plugin was loaded from.
            Informational only — never used for resolution (the catalog
            resolves by plugin_id).
        parameter_schema_hash: SHA-256 hex digest of the canonical JSON
            serialization of the plugin's param_defs. Detects schema drift
            even when the shader source is unchanged.
    """

    plugin_id: str
    version: str = ""
    content_hash: str = ""
    source_uri: str = ""
    parameter_schema_hash: str = ""
    capabilities: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict for embedding in EffectInstance.to_dict()."""
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "source_uri": self.source_uri,
            "parameter_schema_hash": self.parameter_schema_hash,
            "capabilities": list(self.capabilities),
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "PluginIdentity":
        """Reconstruct from a serialized dict.

        AUD-26408: the payload comes from project files (external data) and
        feeds ``resolve_plugin_status``'s "never raises" contract, so every
        field is defensively coerced instead of trusted: a non-dict payload
        or wrong-typed members degrade to an empty/legacy identity (which
        the status matrix reports as MISSING/LEGACY) rather than raising
        AttributeError/TypeError up the project-load / export-preflight
        path.
        """
        if not isinstance(data, dict):
            return PluginIdentity(plugin_id="")

        def _text(key: str) -> str:
            value = data.get(key, "")
            if isinstance(value, str):
                return value
            return "" if value is None else str(value)

        raw_caps = data.get("capabilities", ()) or ()
        if isinstance(raw_caps, (list, tuple, set, frozenset)):
            capabilities: Tuple[str, ...] = tuple(
                cap for cap in raw_caps if isinstance(cap, str)
            )
        else:
            # A scalar (or bare string) cannot be tuple()-ed meaningfully;
            # treat as absent.
            capabilities = ()

        return PluginIdentity(
            plugin_id=_text("plugin_id"),
            version=_text("version"),
            content_hash=_text("content_hash"),
            source_uri=_text("source_uri"),
            parameter_schema_hash=_text("parameter_schema_hash"),
            capabilities=capabilities,
        )


# =============================================================================
# PluginStatusReport — per-instance diagnostic
# =============================================================================

@dataclass(frozen=True)
class PluginStatusReport:
    """Result of validating one effect instance against the live catalog.

    The UI badges from ``status``; export preflight blocks on
    ``export_blocked``; the detail string is human-readable.
    """

    effect_id: str
    effect_type: str
    status: PluginStatus
    detail: str = ""
    recoverable: bool = False
    export_blocked: bool = False

    @property
    def ok(self) -> bool:
        """Whether the effect is safe to render and export."""
        return self.status in (PluginStatus.ACTIVE, PluginStatus.CONTENT_CHANGED)


# =============================================================================
# Hash utilities
# =============================================================================

def compute_content_hash(shader_sources: Sequence[str]) -> str:
    """SHA-256 hex digest of concatenated shader source strings.

    The hash is computed over the UTF-8 encoding of all sources joined by
    a NUL separator (to avoid ambiguity when source boundaries shift).
    Deterministic across platforms and Python versions.
    """
    h = hashlib.sha256()
    for i, src in enumerate(shader_sources):
        if i > 0:
            h.update(b"\x00")
        h.update(src.encode("utf-8"))
    return h.hexdigest()


def compute_parameter_schema_hash(param_defs: Sequence[Any]) -> str:
    """SHA-256 hex digest of the canonical JSON of param_defs.

    Each ParamDef is serialized to a stable dict (name, ptype, default,
    labels, min_val, max_val) and the list is JSON-dumped with sorted keys
    to guarantee determinism. This detects schema drift (added/removed/
    renamed parameters, changed ranges) even when the shader source is
    unchanged.
    """
    canonical: List[Dict[str, Any]] = []
    for pdef in param_defs:
        entry: Dict[str, Any] = {
            "name": getattr(pdef, "name", ""),
            "ptype": getattr(pdef, "ptype", ""),
            "default": getattr(pdef, "default", None),
            "labels": list(getattr(pdef, "labels", None) or []),
            "min_val": getattr(pdef, "min_val", None),
            "max_val": getattr(pdef, "max_val", None),
        }
        canonical.append(entry)
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# =============================================================================
# Identity construction
# =============================================================================

def build_plugin_identity(
    spec: "EffectSpec",
    *,
    source_uri: Optional[str] = None,
    version: Optional[str] = None,
) -> PluginIdentity:
    """Build a PluginIdentity snapshot from a live EffectSpec.

    Called when a plugin effect is first applied to a clip (AddEffectCommand
    path) and when the catalog refreshes and the user confirms re-binding.

    Args:
        spec: The resolved EffectSpec from the catalog.
        source_uri: The file path the plugin was loaded from (informational).
        version: The plugin's declared version string (from @version directive).

    Returns:
        A frozen PluginIdentity ready for serialization.
    """
    return PluginIdentity(
        plugin_id=getattr(spec, "plugin_id", "") or spec.effect_type,
        version=(getattr(spec, "plugin_version", "") if version is None else version),
        content_hash=(
            getattr(spec, "plugin_content_hash", "")
            or compute_content_hash(spec.shader_sources)
        ),
        source_uri=(
            getattr(spec, "plugin_source_uri", "")
            if source_uri is None else source_uri
        ),
        parameter_schema_hash=(
            getattr(spec, "plugin_parameter_schema_hash", "")
            or compute_parameter_schema_hash(spec.param_defs)
        ),
        capabilities=tuple(getattr(spec, "plugin_capabilities", ()) or ()),
    )


# =============================================================================
# Status resolution
# =============================================================================

def resolve_plugin_status(
    stored_identity: Optional[Dict[str, Any]],
    effect_type: str,
    *,
    catalog_spec: Optional["EffectSpec"],
    compile_ok: bool = True,
    backend_supported: bool = True,
) -> PluginStatusReport:
    """Compare a stored identity dict against the live catalog state.

    This is the core resolution logic used at project load, catalog refresh,
    and export preflight. It never raises — all failure modes produce a
    report with the appropriate status.

    Args:
        stored_identity: The dict from EffectInstance.plugin_identity, or
            None if the project predates the identity system.
        effect_type: The EffectInstance.effect_type (used for lookup and
            reporting).
        catalog_spec: The currently-resolved EffectSpec from the catalog,
            or None if the plugin is not found.
        compile_ok: Whether the shader compiled successfully on this GPU.
            Pass False when the renderer reports a compile failure.
        backend_supported: Whether the required backend capability is
            available (e.g. compute shader support).

    Returns:
        A PluginStatusReport with the resolved status and human-readable
        detail.
    """
    effect_id = ""  # filled by caller if needed; here we use effect_type

    # ── No catalog entry → MISSING ──
    if catalog_spec is None:
        return PluginStatusReport(
            effect_id=effect_id,
            effect_type=effect_type,
            status=PluginStatus.MISSING,
            detail=f"Plugin '{effect_type}' not found in catalog",
            recoverable=True,  # user can reinstall / relink
            export_blocked=True,
        )

    # ── Backend not supported ──
    if not backend_supported:
        return PluginStatusReport(
            effect_id=effect_id,
            effect_type=effect_type,
            status=PluginStatus.UNSUPPORTED_BACKEND,
            detail="Required GPU backend capability unavailable",
            recoverable=False,
            export_blocked=True,
        )

    # ── Compile failure ──
    if not compile_ok:
        return PluginStatusReport(
            effect_id=effect_id,
            effect_type=effect_type,
            status=PluginStatus.COMPILE_ERROR,
            detail="Shader failed to compile on this GPU",
            recoverable=True,  # may work after driver update / plugin fix
            export_blocked=True,
        )

    # ── No stored identity → LEGACY ──
    if stored_identity is None:
        return PluginStatusReport(
            effect_id=effect_id,
            effect_type=effect_type,
            status=PluginStatus.LEGACY,
            detail="Project predates plugin identity; cannot verify consistency",
            recoverable=True,  # user can confirm to backfill
            export_blocked=False,  # legacy plugins still export (best-effort)
        )

    identity = PluginIdentity.from_dict(stored_identity)

    if identity.plugin_id and identity.plugin_id != effect_type:
        return PluginStatusReport(
            effect_id=effect_id,
            effect_type=effect_type,
            status=PluginStatus.MISSING,
            detail=(
                f"Stored plugin id '{identity.plugin_id}' does not match "
                f"effect type '{effect_type}'"
            ),
            recoverable=True,
            export_blocked=True,
        )

    # ── Version mismatch ──
    if identity.version and catalog_spec is not None:
        current_version = (
            getattr(catalog_spec, "plugin_version", "")
            or getattr(catalog_spec, "_plugin_version", "")
        )
        if current_version and identity.version != current_version:
            return PluginStatusReport(
                effect_id=effect_id,
                effect_type=effect_type,
                status=PluginStatus.VERSION_MISMATCH,
                detail=(
                    f"Stored version {identity.version} != "
                    f"current {current_version}"
                ),
                recoverable=True,
                export_blocked=False,  # version bump may be compatible
            )

    # ── Schema mismatch ──
    if identity.parameter_schema_hash:
        current_schema_hash = compute_parameter_schema_hash(catalog_spec.param_defs)
        if identity.parameter_schema_hash != current_schema_hash:
            return PluginStatusReport(
                effect_id=effect_id,
                effect_type=effect_type,
                status=PluginStatus.SCHEMA_MISMATCH,
                detail="Parameter schema changed; saved values may be invalid",
                recoverable=True,
                export_blocked=True,  # params may map to wrong uniforms
            )

    # ── Content changed (non-blocking) ──
    if identity.content_hash:
        current_content_hash = compute_content_hash(catalog_spec.shader_sources)
        if identity.content_hash != current_content_hash:
            return PluginStatusReport(
                effect_id=effect_id,
                effect_type=effect_type,
                status=PluginStatus.CONTENT_CHANGED,
                detail="Shader source changed since this effect was applied",
                recoverable=True,
                export_blocked=False,  # visual difference but structurally safe
            )

    # ── All checks passed ──
    return PluginStatusReport(
        effect_id=effect_id,
        effect_type=effect_type,
        status=PluginStatus.ACTIVE,
        detail="",
        recoverable=False,
        export_blocked=False,
    )


# =============================================================================
# Batch validation
# =============================================================================

def validate_effect_instances(
    instances: Sequence[Any],
    *,
    catalog_lookup: Any,
    compile_failures: Optional[set] = None,
) -> List[PluginStatusReport]:
    """Validate a sequence of EffectInstance objects against the catalog.

    Only plugin effects (effect_type starting with ``"plugin:"``) are
    checked; built-in effects always produce ACTIVE.

    Args:
        instances: Sequence of EffectInstance (or duck-typed objects with
            .effect_id, .effect_type, .plugin_identity attributes).
        catalog_lookup: Callable(effect_type) -> Optional[EffectSpec].
            Typically ``EffectCatalog.get_spec``.
        compile_failures: Optional set of effect_types that failed to
            compile on the current GPU (fed by the renderer's diagnostics).

    Returns:
        List of PluginStatusReport, one per plugin effect. Built-in effects
        are omitted from the result (they don't need identity validation).
    """
    if compile_failures is None:
        compile_failures = set()

    reports: List[PluginStatusReport] = []
    for inst in instances:
        etype = getattr(inst, "effect_type", "")
        if not etype.startswith("plugin:"):
            continue  # built-in effects don't need identity checks

        stored = getattr(inst, "plugin_identity", None)
        spec = catalog_lookup(etype)
        compile_ok = etype not in compile_failures

        report = resolve_plugin_status(
            stored,
            etype,
            catalog_spec=spec,
            compile_ok=compile_ok,
        )
        # Patch in the real effect_id for per-instance reporting.
        reports.append(PluginStatusReport(
            effect_id=getattr(inst, "effect_id", ""),
            effect_type=report.effect_type,
            status=report.status,
            detail=report.detail,
            recoverable=report.recoverable,
            export_blocked=report.export_blocked,
        ))
    return reports


# =============================================================================
# Legacy migration
# =============================================================================

def migrate_legacy_identity(
    effect_type: str,
    catalog_spec: Optional["EffectSpec"],
    *,
    source_uri: str = "",
    version: str = "",
) -> Optional[Dict[str, Any]]:
    """Backfill a plugin_identity dict for a legacy effect instance.

    Called when the user confirms migration of an old project. Returns the
    identity dict to store, or None if the plugin is not in the catalog
    (cannot backfill what we cannot find).

    This function does NOT mutate any EffectInstance — the caller is
    responsible for creating a new instance with the returned identity
    (via a command, so it's undoable).
    """
    if catalog_spec is None:
        logger.warning(
            "FX-M07: cannot backfill identity for '%s' — not in catalog",
            effect_type,
        )
        return None

    identity = build_plugin_identity(
        catalog_spec, source_uri=source_uri, version=version,
    )
    return identity.to_dict()


# =============================================================================
# Export preflight
# =============================================================================

def export_preflight_check(
    instances: Sequence[Any],
    *,
    catalog_lookup: Any,
    compile_failures: Optional[set] = None,
) -> List[PluginStatusReport]:
    """Pre-export validation: return only reports that block export.

    A convenience wrapper around validate_effect_instances that filters
    to export_blocked=True. The export dialog calls this to list specific
    effect instances and their blocking reasons before the user commits.
    """
    all_reports = validate_effect_instances(
        instances,
        catalog_lookup=catalog_lookup,
        compile_failures=compile_failures,
    )
    return [r for r in all_reports if r.export_blocked]
