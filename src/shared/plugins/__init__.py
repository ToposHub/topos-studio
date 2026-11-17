"""Cross-domain plugin contracts shared by GLSL/AI/Files hosts.

Stage A (PL-01/02/03/07) of docs/plans/2026-11-17-files-plugin-system-rebuild-plan.md.
Pure-Python, Qt-free; hosts and feature packages import these, never reverse.
"""
from src.shared.plugins.freeze import deep_freeze, deep_thaw
from src.shared.plugins.inventory import (
    FAILURE_PHASES,
    INSTALL_STATES,
    RUNTIME_STATES,
    InventorySnapshot,
    PluginInventory,
    PluginRecord,
    default_desired_for,
    legacy_files_records,
)
from src.shared.plugins.lifecycle import (
    CancelToken,
    ContributionSet,
    InvocationCancelled,
    InvocationContext,
    PluginJobs,
    PluginLifecycleController,
    check_command_applicability,
    make_invocation,
)
from src.shared.plugins.manifest import (
    CONTRIBUTION_KINDS,
    FORMAT_PROVIDES,
    ManifestDiagnostic,
    PluginManifest,
    validate_manifest_set,
)
from src.shared.plugins.services import (
    CommandRejected,
    ItemResult,
    PreparedInvocation,
    ServiceResult,
)

__all__ = [
    "CONTRIBUTION_KINDS", "FORMAT_PROVIDES", "ManifestDiagnostic", "PluginManifest",
    "validate_manifest_set", "INSTALL_STATES", "RUNTIME_STATES", "FAILURE_PHASES",
    "PluginRecord", "InventorySnapshot", "PluginInventory", "legacy_files_records",
    "default_desired_for", "CancelToken", "InvocationCancelled", "InvocationContext",
    "make_invocation", "ContributionSet", "PluginJobs", "PluginLifecycleController",
    "check_command_applicability", "ItemResult", "ServiceResult", "CommandRejected",
    "PreparedInvocation", "deep_freeze", "deep_thaw",
]
