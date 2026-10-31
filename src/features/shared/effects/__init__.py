"""Cross-page effect abstractions.

This package hosts the unified ``EffectSpec`` description shared by the Edit
page (clip video filters), the Composite page (node graph) and the Color page
(``.tglsl`` DCTL plugins). Each renderer compiles and executes the same spec
on its own moderngl context — no program/FBO objects are shared across
contexts (they cannot be, per the OpenGL share-group rules), only the
*description* (shader source + parameter schema) is reused.
"""

from src.features.shared.effects.effect_catalog import (
    EffectCatalog,
    get_effect_catalog,
)
from src.features.shared.effects.effect_capability import (
    CPU_PREVIEW_EFFECT_TYPES,
    EXPORT_EFFECT_TYPES,
    GPU_EXPORT_EFFECT_TYPES,
    EffectCapability,
    EffectDiagnostic,
    EffectDiagnosticState,
    check_backend_support,
    classify_for_export,
    compute_effect_status,
    decide_export_policy,
    decide_preview_policy,
    parse_backend_declaration,
    resolve_capability,
    resolve_execution_capability,
)
from src.features.shared.effects.effect_spec import EffectSpec, ShaderKind
from src.features.shared.effects.preview_policy import (
    DegradationDecl,
    DegradationMode,
    PolicyContext,
    PreviewPolicyDecision,
    PreviewPolicyEngine,
    get_degradation_decl,
    register_degradation_decl,
    resolve_params_for_decision,
)
from src.features.shared.effects.plugin_identity import (
    PluginIdentity,
    PluginStatus,
    PluginStatusReport,
    build_plugin_identity,
    compute_content_hash,
    compute_parameter_schema_hash,
    export_preflight_check,
    migrate_legacy_identity,
    resolve_plugin_status,
    validate_effect_instances,
)

__all__ = [
    "EffectCatalog",
    "EffectCapability",
    "EffectDiagnostic",
    "EffectDiagnosticState",
    "EffectSpec",
    "ShaderKind",
    "CPU_PREVIEW_EFFECT_TYPES",
    "EXPORT_EFFECT_TYPES",
    "GPU_EXPORT_EFFECT_TYPES",
    "classify_for_export",
    "compute_effect_status",
    "decide_export_policy",
    "decide_preview_policy",
    "get_effect_catalog",
    "resolve_capability",
    "resolve_execution_capability",
    # Phase 7: backend axes (P7-06/07/08)
    "check_backend_support",
    "parse_backend_declaration",
    # FX-M04: Preview Quality Policy
    "DegradationDecl",
    "DegradationMode",
    "PolicyContext",
    "PreviewPolicyDecision",
    "PreviewPolicyEngine",
    "get_degradation_decl",
    "register_degradation_decl",
    "resolve_params_for_decision",
    # FX-M07: Plugin Identity
    "PluginIdentity",
    "PluginStatus",
    "PluginStatusReport",
    "build_plugin_identity",
    "compute_content_hash",
    "compute_parameter_schema_hash",
    "export_preflight_check",
    "migrate_legacy_identity",
    "resolve_plugin_status",
    "validate_effect_instances",
]
