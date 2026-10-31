"""Side-effect-free plugin discovery and trust decisions.

Discovery accepts already parsed manifests only.  It never imports plugin
code, executes entry points or enables a plugin.  Installation is an explicit
host action and always starts from the disabled state; activation remains the
responsibility of the lifecycle/approval boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.features.ai_harness.plugin_runtime import (
    PluginLifecycle,
    PluginManifest,
    PluginRegistry,
    PluginSource,
    PluginState,
)
from src.features.ai_harness.plugin_trust import PluginTrustPolicy


class PluginDiscoveryState(str, Enum):
    READY = "ready"
    UNVERIFIED = "unverified"
    PENDING_ENABLE = "pending_enable"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PluginDiscoveryDiagnostic:
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message[:500],
        }


@dataclass(frozen=True)
class PluginDiscoveryCandidate:
    manifest: PluginManifest | None
    state: PluginDiscoveryState
    installable: bool
    requires_explicit_enable: bool
    high_risk: bool
    diagnostics: tuple[PluginDiscoveryDiagnostic, ...] = ()

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id if self.manifest is not None else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "state": self.state.value,
            "installable": self.installable,
            "requires_explicit_enable": self.requires_explicit_enable,
            "high_risk": self.high_risk,
            "manifest": self.manifest.to_dict() if self.manifest is not None else None,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


@dataclass(frozen=True)
class PluginDiscoveryReport:
    candidates: tuple[PluginDiscoveryCandidate, ...] = ()

    @property
    def installable(self) -> tuple[PluginDiscoveryCandidate, ...]:
        return tuple(item for item in self.candidates if item.installable)

    @property
    def rejected(self) -> tuple[PluginDiscoveryCandidate, ...]:
        return tuple(item for item in self.candidates if not item.installable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [item.to_dict() for item in self.candidates],
            "installable_count": len(self.installable),
            "rejected_count": len(self.rejected),
        }


@dataclass(frozen=True)
class PluginInstallResult:
    accepted: bool
    state: str
    diagnostics: tuple[PluginDiscoveryDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "state": self.state,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


class PluginDiscovery:
    """Validate and stage plugin manifests without executing plugin code."""

    @staticmethod
    def inspect(
        manifest: object,
        *,
        trust_policy: PluginTrustPolicy | None = None,
    ) -> PluginDiscoveryCandidate:
        if not isinstance(manifest, PluginManifest):
            return PluginDiscoveryCandidate(
                manifest=None,
                state=PluginDiscoveryState.REJECTED,
                installable=False,
                requires_explicit_enable=True,
                high_risk=True,
                diagnostics=(PluginDiscoveryDiagnostic(
                    "PLUGIN_MANIFEST_INVALID",
                    "error",
                    "discovery accepts only a validated PluginManifest",
                ),),
            )

        high_risk = any(
            capability.permission in {"write", "file_write", "expensive"}
            for capability in manifest.capabilities
        )
        if manifest.declared_hash and not manifest.verify_declared_hash():
            return PluginDiscoveryCandidate(
                manifest=manifest,
                state=PluginDiscoveryState.REJECTED,
                installable=False,
                requires_explicit_enable=True,
                high_risk=high_risk,
                diagnostics=(PluginDiscoveryDiagnostic(
                    "PLUGIN_INTEGRITY_HASH_MISMATCH",
                    "error",
                    "plugin manifest integrity hash does not match content",
                ),),
            )
        if manifest.source == PluginSource.LOCAL_DEV:
            return PluginDiscoveryCandidate(
                manifest=manifest,
                state=PluginDiscoveryState.UNVERIFIED,
                installable=True,
                requires_explicit_enable=True,
                high_risk=high_risk,
                diagnostics=(PluginDiscoveryDiagnostic(
                    "PLUGIN_UNVERIFIED_SOURCE",
                    "warning",
                    "local development plugin requires explicit enable",
                ),),
            )

        if manifest.source == PluginSource.VERIFIED_THIRD_PARTY:
            if not manifest.declared_hash:
                return PluginDiscoveryCandidate(
                    manifest=manifest,
                    state=PluginDiscoveryState.REJECTED,
                    installable=False,
                    requires_explicit_enable=True,
                    high_risk=high_risk,
                    diagnostics=(PluginDiscoveryDiagnostic(
                        "PLUGIN_INTEGRITY_HASH_MISSING",
                        "error",
                        "verified third-party plugin must declare an integrity hash",
                    ),),
                )
            if not manifest.verify_declared_hash():
                return PluginDiscoveryCandidate(
                    manifest=manifest,
                    state=PluginDiscoveryState.REJECTED,
                    installable=False,
                    requires_explicit_enable=True,
                    high_risk=high_risk,
                    diagnostics=(PluginDiscoveryDiagnostic(
                        "PLUGIN_INTEGRITY_HASH_MISMATCH",
                        "error",
                        "plugin manifest integrity hash does not match content",
                    ),),
                )
            trusted, trust_code = (trust_policy or PluginTrustPolicy()).verify(manifest)
            if not trusted:
                messages = {
                    "PLUGIN_SIGNATURE_MISSING": "verified third-party plugin must carry a detached signature",
                    "PLUGIN_SIGNATURE_VERIFIER_UNAVAILABLE": "host has no verifier for the plugin signature",
                    "PLUGIN_SIGNATURE_INVALID": "plugin detached signature is invalid",
                }
                return PluginDiscoveryCandidate(
                    manifest=manifest,
                    state=PluginDiscoveryState.REJECTED,
                    installable=False,
                    requires_explicit_enable=True,
                    high_risk=high_risk,
                    diagnostics=(PluginDiscoveryDiagnostic(
                        trust_code,
                        "error",
                        messages.get(trust_code, "plugin trust verification failed"),
                    ),),
                )
            return PluginDiscoveryCandidate(
                manifest=manifest,
                state=PluginDiscoveryState.PENDING_ENABLE,
                installable=True,
                requires_explicit_enable=True,
                high_risk=high_risk,
                diagnostics=(PluginDiscoveryDiagnostic(
                    "PLUGIN_PENDING_ENABLE",
                    "info",
                    "verified third-party plugin is staged but not enabled",
                ),),
            )

        return PluginDiscoveryCandidate(
            manifest=manifest,
            state=PluginDiscoveryState.READY,
            installable=True,
            requires_explicit_enable=False,
            high_risk=high_risk,
        )

    @classmethod
    def discover(
        cls,
        manifests: Iterable[object],
        *,
        trust_policy: PluginTrustPolicy | None = None,
    ) -> PluginDiscoveryReport:
        """Inspect manifests in stable order and reject duplicate IDs."""
        candidates: list[PluginDiscoveryCandidate] = []
        seen_ids: set[str] = set()
        for manifest in manifests:
            candidate = cls.inspect(manifest, trust_policy=trust_policy)
            if candidate.plugin_id and candidate.plugin_id in seen_ids:
                candidate = PluginDiscoveryCandidate(
                    manifest=candidate.manifest,
                    state=PluginDiscoveryState.REJECTED,
                    installable=False,
                    requires_explicit_enable=True,
                    high_risk=candidate.high_risk,
                    diagnostics=candidate.diagnostics + (PluginDiscoveryDiagnostic(
                        "PLUGIN_DUPLICATE_ID",
                        "error",
                        f"plugin id {candidate.plugin_id!r} was discovered more than once",
                    ),),
                )
            elif candidate.plugin_id:
                seen_ids.add(candidate.plugin_id)
            candidates.append(candidate)
        return PluginDiscoveryReport(tuple(candidates))

    @staticmethod
    def install(
        registry: PluginRegistry,
        candidate: PluginDiscoveryCandidate,
        plugin: PluginLifecycle | None = None,
    ) -> bool:
        """Stage a discovered plugin as disabled; never auto-enable it."""
        if not candidate.installable or candidate.manifest is None:
            return False
        return PluginDiscovery.install_with_report(registry, candidate, plugin).accepted

    @staticmethod
    def install_with_report(
        registry: PluginRegistry,
        candidate: PluginDiscoveryCandidate,
        plugin: PluginLifecycle | None = None,
    ) -> PluginInstallResult:
        """Stage a candidate and expose a structured, disabled-only result."""
        if not candidate.installable or candidate.manifest is None:
            return PluginInstallResult(
                accepted=False,
                state=PluginState.DISABLED.value,
                diagnostics=candidate.diagnostics or (PluginDiscoveryDiagnostic(
                    "PLUGIN_INSTALL_REJECTED",
                    "error",
                    "plugin candidate is not installable",
                ),),
            )
        accepted = registry.register(candidate.manifest, plugin, enabled=False)
        if not accepted:
            return PluginInstallResult(
                accepted=False,
                state=PluginState.DISABLED.value,
                diagnostics=(PluginDiscoveryDiagnostic(
                    "PLUGIN_INSTALL_CONFLICT",
                    "error",
                    "plugin registration was rejected by the host registry",
                ),),
            )
        return PluginInstallResult(
            accepted=True,
            state=PluginState.DISABLED.value,
            diagnostics=(PluginDiscoveryDiagnostic(
                "PLUGIN_INSTALLED_DISABLED",
                "info",
                "plugin staged successfully and requires explicit enable",
            ),),
        )


__all__ = [
    "PluginDiscovery",
    "PluginDiscoveryCandidate",
    "PluginDiscoveryDiagnostic",
    "PluginDiscoveryReport",
    "PluginDiscoveryState",
    "PluginInstallResult",
]
