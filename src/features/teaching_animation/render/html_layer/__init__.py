"""W2-prep: browser-independent HTML layer infrastructure (gated from scene schema).

This package implements the FH-02/FH-10/FH-13 machinery the W2 ``HtmlRendererService``
will use, WITHOUT attaching ``FreeformHtmlMotionLayer`` to the scene schema.
Per the W0-A gate (``docs/adr/0001-...``) the HTML layer cannot enter the project
format until a capture backend reports available AND passes the determinism gate.
These modules are the validated, ready-to-activate layer for that day.
"""

from __future__ import annotations

from .html_patch import (
    DeclaredNode,
    HtmlLayerState,
    HtmlPatch,
    HtmlPatchError,
    HtmlPatchErrorCode,
    HtmlPatchOperation,
    validate_patch,
)
from .html_patch_apply import (
    apply_html_patch,
    read_node_asset_ref,
    read_node_inner_text,
)
from .node_identity import extract_declared_nodes, extract_slots, extract_tokens
from .sanitizer import ALLOWED_TAGS, HtmlSanitizer, SanitizeResult, SanitizerReport
from .budget import (
    BudgetReport, DomStats, HtmlRenderEvidence, enforce_budget, measure_dom,
)
from .template_promote import apply_html_template, promote_html_layer

__all__ = [
    "ALLOWED_TAGS",
    "BudgetReport",
    "DeclaredNode",
    "DomStats",
    "HtmlLayerState",
    "HtmlPatch",
    "HtmlPatchError",
    "HtmlPatchErrorCode",
    "HtmlPatchOperation",
    "HtmlRenderEvidence",
    "HtmlSanitizer",
    "SanitizeResult",
    "SanitizerReport",
    "apply_html_patch",
    "apply_html_template",
    "enforce_budget",
    "extract_declared_nodes",
    "extract_slots",
    "extract_tokens",
    "measure_dom",
    "promote_html_layer",
    "read_node_asset_ref",
    "read_node_inner_text",
    "validate_patch",
]
