"""Versioned Beta/release handoff manifest (P10/P11)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from src.shared.feature_flags import COMMERCIAL_AUTOMATION_FLAGS, DEFAULT_FEATURE_FLAGS


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    channel: str
    supported_providers: tuple[str, ...] = ()
    default_flags: dict[str, bool] | None = None
    test_report_id: str = ""
    quality_gate_passed: bool = False
    rollback_version: str = ""
    known_issues: tuple[str, ...] = ()
    test_matrix_passed: bool = False
    commercial_audit_passed: bool = False
    commercial_audit_report_id: str = ""
    commercial_acceptance_passed: bool = False
    commercial_acceptance_report_id: str = ""

    def __post_init__(self) -> None:
        if self.channel not in {"alpha", "beta", "stable"}:
            raise ValueError("channel must be alpha, beta or stable")
        if not self.version:
            raise ValueError("version is required")
        if self.default_flags is None:
            object.__setattr__(
                self,
                "default_flags",
                {name: DEFAULT_FEATURE_FLAGS[name] for name in COMMERCIAL_AUTOMATION_FLAGS},
            )

    @property
    def releasable(self) -> bool:
        """Return whether all release evidence is present and passed.

        A quality gate alone is intentionally insufficient.  Every channel
        must carry a traceable test report, a complete matrix result, a
        commercial provenance audit and a panel-only commercial acceptance
        report.  Stable additionally requires rollback.
        """
        return not self.release_blockers

    @property
    def release_blockers(self) -> tuple[str, ...]:
        """Stable, privacy-safe reasons why this manifest cannot be released."""
        blockers: list[str] = []
        if not self.quality_gate_passed:
            blockers.append("quality_gate_failed")
        if not self.test_report_id:
            blockers.append("test_report_missing")
        if not self.test_matrix_passed:
            blockers.append("test_matrix_failed_or_missing")
        if not self.commercial_audit_passed:
            blockers.append("commercial_audit_failed_or_missing")
        if not self.commercial_audit_report_id:
            blockers.append("commercial_audit_report_missing")
        if not self.commercial_acceptance_passed:
            blockers.append("commercial_panel_acceptance_failed_or_missing")
        if not self.commercial_acceptance_report_id:
            blockers.append("commercial_panel_acceptance_report_missing")
        if self.channel == "stable" and not self.rollback_version:
            blockers.append("rollback_version_missing")
        return tuple(blockers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "channel": self.channel,
            "supported_providers": list(self.supported_providers),
            "default_flags": dict(self.default_flags or {}),
            "test_report_id": self.test_report_id,
            "quality_gate_passed": self.quality_gate_passed,
            "rollback_version": self.rollback_version,
            "known_issues": list(self.known_issues),
            "test_matrix_passed": self.test_matrix_passed,
            "commercial_audit_passed": self.commercial_audit_passed,
            "commercial_audit_report_id": self.commercial_audit_report_id,
            "commercial_acceptance_passed": self.commercial_acceptance_passed,
            "commercial_acceptance_report_id": self.commercial_acceptance_report_id,
            "release_blockers": list(self.release_blockers),
            "releasable": self.releasable,
        }


def build_release_manifest(
    *,
    version: str,
    channel: str,
    providers: Iterable[Any] = (),
    feature_flags: Any | None = None,
    test_report_id: str = "",
    quality_gate_passed: bool = False,
    rollback_version: str = "",
    known_issues: Iterable[str] = (),
    test_matrix_passed: bool = False,
    commercial_audit_passed: bool = False,
    commercial_audit_report_id: str = "",
    commercial_acceptance_passed: bool = False,
    commercial_acceptance_report_id: str = "",
) -> ReleaseManifest:
    """Build a release manifest from providers, flags and release evidence."""
    provider_ids = tuple(
        sorted({str(getattr(provider, "provider_id", "")) for provider in providers if getattr(provider, "provider_id", "")})
    )
    flags = {
        name: bool(
            feature_flags.is_enabled(name) if feature_flags is not None
            and callable(getattr(feature_flags, "is_enabled", None))
            else DEFAULT_FEATURE_FLAGS[name]
        )
        for name in COMMERCIAL_AUTOMATION_FLAGS
    }
    return ReleaseManifest(
        version=version,
        channel=channel,
        supported_providers=provider_ids,
        default_flags=flags,
        test_report_id=test_report_id,
        quality_gate_passed=quality_gate_passed,
        rollback_version=rollback_version,
        known_issues=tuple(str(issue)[:300] for issue in known_issues),
        test_matrix_passed=test_matrix_passed,
        commercial_audit_passed=commercial_audit_passed,
        commercial_audit_report_id=str(commercial_audit_report_id)[:200],
        commercial_acceptance_passed=commercial_acceptance_passed,
        commercial_acceptance_report_id=str(commercial_acceptance_report_id)[:200],
    )


__all__ = ["ReleaseManifest", "build_release_manifest"]
