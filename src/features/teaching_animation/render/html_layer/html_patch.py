"""FH-10: HtmlPatch — the versioned, schema-validated edit contract for HTML layers.

Per the W0-A gate this does NOT attach ``FreeformHtmlMotionLayer`` to the scene
schema yet.  It defines the *edit contract* the W2 inspector/canvas will speak:
every HTML edit is a typed ``HtmlPatch`` with a precondition (base revision +
source hash), validated against the layer's declared nodes/slots/tokens.  There
is deliberately NO generic ``innerHTML``/``stylesheet`` string write — patches
can only act on declared, addressable targets, so an edit can never partially
write or bypass the sanitizer.

Patch operations (FH-10): replace_text | set_asset | set_token | set_slot_attr |
transform_node | set_animation_timing.  Conflict, stale-revision and illegal-
target cases all produce a structured ``HtmlPatchError`` rather than a partial
write.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class HtmlPatchOperation(str, Enum):
    REPLACE_TEXT = "replace_text"
    SET_ASSET = "set_asset"
    SET_TOKEN = "set_token"
    SET_SLOT_ATTR = "set_slot_attr"
    TRANSFORM_NODE = "transform_node"
    SET_ANIMATION_TIMING = "set_animation_timing"


class HtmlPatchErrorCode(str, Enum):
    UNKNOWN_NODE = "unknown_node"
    UNKNOWN_TOKEN = "unknown_token"
    UNKNOWN_SLOT = "unknown_slot"
    STALE_REVISION = "stale_revision"
    PRECONDITION_MISMATCH = "precondition_mismatch"
    ILLEGAL_VALUE = "illegal_value"
    EDIT_MODE_FORBIDS = "edit_mode_forbids"
    UNKNOWN_OPERATION = "unknown_operation"


class HtmlPatchError(ValueError):
    def __init__(self, code: HtmlPatchErrorCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DeclaredNode:
    """A node the AI/author marked editable via ``data-topos-node-id``."""

    node_id: str
    edit_mode: str = "content"   # content | absolute_transform | token_only
    text_slot: bool = True
    asset_slot: bool = False

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("node_id cannot be empty")
        if self.edit_mode not in {"content", "absolute_transform", "token_only"}:
            raise ValueError(f"unknown edit_mode: {self.edit_mode}")


@dataclass(frozen=True)
class HtmlLayerState:
    """The addressable targets a patch may touch (W2-prep standalone model).

    This is the data ``FreeformHtmlMotionLayer`` will hold once the W0-A gate
    passes; for now it is the validation context for patches.
    """

    revision: int
    source_hash: str                       # sha256 of the sanitized HTML
    declared_nodes: Mapping[str, DeclaredNode] = field(default_factory=dict)
    declared_tokens: frozenset[str] = frozenset()       # CSS custom properties
    declared_slots: frozenset[str] = frozenset()        # data-topos-editable slot ids
    bound_asset_ids: frozenset[str] = frozenset()

    @classmethod
    def for_source(cls, html: str, *, declared_nodes: Mapping[str, DeclaredNode] | None = None,
                   declared_tokens: frozenset[str] | None = None,
                   declared_slots: frozenset[str] | None = None,
                   bound_asset_ids: frozenset[str] | None = None) -> "HtmlLayerState":
        return cls(
            revision=1,
            source_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            declared_nodes=dict(declared_nodes or {}),
            declared_tokens=declared_tokens or frozenset(),
            declared_slots=declared_slots or frozenset(),
            bound_asset_ids=bound_asset_ids or frozenset(),
        )

    def with_revision(self, revision: int) -> "HtmlLayerState":
        from dataclasses import replace
        return replace(self, revision=revision)


@dataclass(frozen=True)
class HtmlPatch:
    """One typed, precondition-checked edit against an HTML layer."""

    operation: HtmlPatchOperation
    target: str                              # node_id / token name / slot id
    value: Any
    base_revision: int
    precondition_hash: str                   # source_hash the patch was authored against
    sub_target: str = ""                     # e.g. attribute name for set_slot_attr

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value, "target": self.target, "value": self.value,
            "base_revision": self.base_revision, "precondition_hash": self.precondition_hash,
            "sub_target": self.sub_target,
        }


def validate_patch(patch: HtmlPatch, state: HtmlLayerState) -> None:
    """Raise ``HtmlPatchError`` if the patch cannot be applied to ``state``.

    Validation never mutates state; the caller applies the validated patch to
    produce a new revision.  This is the fail-before-write boundary the plan
    requires (FH-10: "冲突、过期 revision、非法属性可诊断且不会部分写入").
    """
    if patch.base_revision != state.revision:
        raise HtmlPatchError(HtmlPatchErrorCode.STALE_REVISION,
                             f"patch base_revision {patch.base_revision} != state revision {state.revision}")
    if patch.precondition_hash != state.source_hash:
        raise HtmlPatchError(HtmlPatchErrorCode.PRECONDITION_MISMATCH,
                             "precondition hash does not match current source hash")
    op = patch.operation
    if op == HtmlPatchOperation.REPLACE_TEXT:
        node = _require_node(patch.target, state)
        if not node.text_slot:
            raise HtmlPatchError(HtmlPatchErrorCode.EDIT_MODE_FORBIDS, f"{patch.target} has no text slot")
        # AUD-30005: FH-13 declares ``token_only`` = "only CSS-token edits, no
        # content/transform".  The text_slot flag alone could not enforce that
        # (every declared node currently reports a text slot), so the edit mode
        # itself must forbid a content rewrite.
        if node.edit_mode == "token_only":
            raise HtmlPatchError(
                HtmlPatchErrorCode.EDIT_MODE_FORBIDS,
                f"{patch.target} edit_mode=token_only forbids replace_text")
        if not isinstance(patch.value, str):
            raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE, "replace_text value must be a string")
    elif op == HtmlPatchOperation.SET_ASSET:
        node = _require_node(patch.target, state)
        if not node.asset_slot:
            raise HtmlPatchError(HtmlPatchErrorCode.EDIT_MODE_FORBIDS, f"{patch.target} has no asset slot")
        if patch.value not in state.bound_asset_ids:
            raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE,
                                 f"asset {patch.value} is not bound to this layer")
    elif op == HtmlPatchOperation.SET_TOKEN:
        if patch.target not in state.declared_tokens:
            raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_TOKEN, f"token {patch.target} not declared")
        if not isinstance(patch.value, str):
            raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE, "set_token value must be a string")
    elif op == HtmlPatchOperation.SET_SLOT_ATTR:
        if patch.target not in state.declared_slots:
            raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_SLOT, f"slot {patch.target} not declared")
        if not patch.sub_target or not isinstance(patch.value, str):
            raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE, "set_slot_attr needs sub_target + string value")
    elif op == HtmlPatchOperation.TRANSFORM_NODE:
        node = _require_node(patch.target, state)
        if node.edit_mode != "absolute_transform":
            raise HtmlPatchError(HtmlPatchErrorCode.EDIT_MODE_FORBIDS,
                                 f"{patch.target} edit_mode={node.edit_mode} forbids transform_node")
        if not _is_transform_value(patch.value):
            raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE, "transform_node value must be a finite transform mapping")
    elif op == HtmlPatchOperation.SET_ANIMATION_TIMING:
        _require_node(patch.target, state)
        if not isinstance(patch.value, Mapping) or "duration_ms" not in patch.value:
            raise HtmlPatchError(HtmlPatchErrorCode.ILLEGAL_VALUE,
                                 "set_animation_timing value needs duration_ms")
        # AUD-30006: patch values are external/AI input.  A non-int-able or
        # negative duration used to escape as a bare ValueError/TypeError from
        # the apply side's ``int()`` (HtmlPatchError is a ValueError subclass,
        # so callers that only catch HtmlPatchError missed it) or silently emit
        # invalid CSS like ``animation: -0.5s``.  Validate the value here so
        # the structured-error contract holds end to end.
        duration = patch.value["duration_ms"]
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise HtmlPatchError(
                HtmlPatchErrorCode.ILLEGAL_VALUE,
                "set_animation_timing duration_ms must be a non-negative integer (ms)")
    else:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_OPERATION, str(patch.operation))


def _require_node(node_id: str, state: HtmlLayerState) -> DeclaredNode:
    node = state.declared_nodes.get(node_id)
    if node is None:
        raise HtmlPatchError(HtmlPatchErrorCode.UNKNOWN_NODE, f"node {node_id} not declared")
    return node


def _is_transform_value(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key in ("x", "y", "scale", "rotation"):
        if key in value:
            v = value[key]
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return False
    return True


__all__ = [
    "DeclaredNode",
    "HtmlLayerState",
    "HtmlPatch",
    "HtmlPatchError",
    "HtmlPatchErrorCode",
    "HtmlPatchOperation",
    "validate_patch",
]
