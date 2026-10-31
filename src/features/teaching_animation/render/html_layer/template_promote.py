"""FH-09 promote/apply engine for HTML motion templates.

``promote_html_layer`` distills a verified :class:`FreeformHtmlMotionLayer`
into a :class:`HtmlTemplate` by reading each editable slot/token's current
value out of the sanitized HTML and replacing it with a placeholder marker.
``apply_html_template`` is the inverse: it substitutes parameters back into
the markers and **re-sanitizes** before rebuilding a layer — so a parameter
value can never bypass the allowlist (the same defence-in-depth contract as
``apply_html_patch``).

Both functions are pure: promotion reads, application writes a *new* layer;
neither touches the source scene, so the original layer remains the audited
fallback (FH-09).

Placeholder grammar (single-pass, regex-substituted so values are not
re-scanned for markers):

- ``{{slot:NODE_ID}}`` — inner text of a content node (kind ``text``)
- ``{{asset:NODE_ID}}`` — the id in a node's ``topos-asset://`` ref (kind ``asset``)
- ``{{token:NAME}}`` — the value of a ``--NAME`` CSS custom property
"""

from __future__ import annotations

import re
from html import escape as _html_escape
from typing import Any, Mapping

from ...core.executable_template import ParamSpec
from ...core.html_layer import FreeformHtmlMotionLayer, build_html_layer
from ...core.html_template import HtmlTemplate
from .html_patch_apply import _locate_element, read_node_asset_ref, read_node_inner_text
from .sanitizer import HtmlSanitizer


# Matches the three placeholder kinds in one pass; group(1)=kind, group(2)=name.
_MARKER_RE = re.compile(r"\{\{(slot|asset|token):([^}]+)\}\}")
_SCRIPT_BLOCK_RE = re.compile(
    r"<script\b[^>]*>.*?</script\s*>|</?script\b[^>]*>?",
    re.IGNORECASE | re.DOTALL,
)
# CSS structural metacharacters that must never appear in a token value:
# ``<`` can terminate a ``<style>`` block (``</style><img onerror=...>``),
# ``}``/``;`` close/restructure rules, ``{`` opens rogue blocks, backslash
# enables CSS-escape reconstruction of any of the above.
_CSS_META_RE = re.compile(r"[<>{};\\]")
# A hex / rgb() / rgba() colour literal — classifies a token as ``color``.
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", re.IGNORECASE)
_INTEGER_RE = re.compile(r"^-?\d+$")
_NUMBER_RE = re.compile(r"^-?(?:\d+\.\d+|\d+\.\d*|\.\d+)$")


def _token_value(html: str, token: str) -> str | None:
    # Reads ``--NAME: VALUE`` from a <style> block, bounded so it stops at
    # ``}``/``;``/newline.  Built by concatenation to avoid ``str.format``
    # brace hazards with the literal ``}`` in the character class.
    pattern = re.compile(r"--" + re.escape(token) + r"\s*:\s*([^;}\n]+)")
    m = pattern.search(html)
    if m is None:
        return None
    return m.group(1).strip()


def _replace_inner_text(html: str, node_id: str, new_text: str) -> str | None:
    """Substitute the inner text of ``node_id``; return new html or None."""
    span = _locate_element(html, node_id)
    if span is None or span.is_void:
        return None
    return html[:span.inner_start] + new_text + html[span.inner_end:]


def _replace_asset_ref(html: str, node_id: str, new_id: str) -> str | None:
    """Substitute the topos-asset id on ``node_id``; return new html or None."""
    span = _locate_element(html, node_id)
    if span is None:
        return None
    current = read_node_asset_ref(html, node_id)
    if current is None:
        return None
    # Replace only the id, keep the scheme + surrounding quotes intact.
    needle = f"topos-asset://{current}"
    replacement = f"topos-asset://{new_id}"
    open_tag = html[span.open_start:span.open_end]
    new_open = open_tag.replace(needle, replacement, 1)
    if new_open == open_tag:
        return None
    return html[:span.open_start] + new_open + html[span.open_end:]


def _replace_token_value(html: str, token: str, new_value: str) -> str | None:
    """Substitute the value of ``--token``; return new html or None."""
    pattern = re.compile(r"(--" + re.escape(token) + r"\s*:\s*)([^;}\n]+)")
    new, n = pattern.subn(lambda m: f"{m.group(1)}{new_value}", html, count=1)
    if n == 0:
        return None
    return new


def _classify_token(value: str) -> str:
    value = value.strip()
    if _COLOR_RE.search(value):
        return "color"
    if _INTEGER_RE.fullmatch(value):
        return "integer"
    if _NUMBER_RE.fullmatch(value):
        return "number"
    return "string"


def _prepare_param_value(kind: str, value: str) -> str:
    """Neutralise one parameter value for its substitution context.

    AUD-30017: parameter values are external input (user/AI-provided at
    template instantiation), so they are treated as *data*, never markup.
    The rebuild path re-sanitizes with the freeform Motion Graphic profile
    (``allow_event_handlers=True`` etc. — by design for the template
    *author's* own HTML), which means the sanitizer will happily keep an
    ``on*`` attribute that arrived inside a parameter value.  Context-aware
    neutralisation at substitution time closes that hole:

    - ``slot`` (inner text of a content node): HTML-escaped, so payloads
      like ``<svg onload=alert(1)>`` or ``<img src=x onerror=...>`` render
      as literal text instead of parsing into an element with an event
      handler.  The value round-trips: the sanitizer unescapes it as text
      data and re-escapes it identically.
    - ``asset`` (the id inside a quoted ``topos-asset://`` attribute):
      quote-escaped so the value can never break out of the attribute
      (``x" onclick="alert(1)`` stays part of the URL text).  The scheme
      prefix is fixed by the substitution site, so a ``javascript:`` URL
      cannot be introduced here.
    - ``token`` (a ``--NAME`` CSS custom-property value inside ``<style>``):
      CSS structural metacharacters are stripped so the value remains a
      single declaration and cannot close the rule (``}``/``;``) or
      terminate the style block (``</style>`` → markup injection).

    Any ``<script>`` blocks were already stripped from all values before
    this is called (defence in depth, kept from the original fix).
    """
    if kind == "slot":
        return _html_escape(value, quote=True)
    if kind == "asset":
        return _html_escape(value, quote=True)
    if kind == "token":
        return _CSS_META_RE.sub("", value)
    return value


def promote_html_layer(
    layer: FreeformHtmlMotionLayer,
    *,
    template_id: str,
    display_name: str,
    description: str = "",
    version: int = 1,
) -> HtmlTemplate:
    """Freeze ``layer`` into a reusable, parameterized :class:`HtmlTemplate`.

    Iterates the layer's declared ``editable_slots`` (text/asset kinds) and
    ``editable_tokens``; for each, reads the current value from
    ``sanitized_html`` and replaces it with a placeholder, and records a
    :class:`ParamSpec` whose default is that current value.  Slots/tokens
    whose node/value is absent from the HTML are skipped (the edit contract
    and the page have drifted; promotion stays faithful to what is actually
    on the canvas).

    The source layer is not modified — it remains the fallback (FH-09).
    """
    # ``FreeformHtmlMotionLayer.__post_init__`` guarantees non-empty HTML, so
    # no empty-input guard is needed here.
    html = layer.sanitized_html
    params: list[ParamSpec] = []
    used_names: set[str] = set()

    for slot in layer.editable_slots:
        node_id = slot.node_id
        if node_id in used_names:
            continue  # avoid duplicate param names (validated in __post_init__)
        if slot.kind == "text":
            current = read_node_inner_text(html, node_id)
            if current is None:
                continue
            updated = _replace_inner_text(html, node_id, f"{{{{slot:{node_id}}}}}")
            if updated is None:
                continue
            html = updated
            params.append(ParamSpec(
                name=node_id, type="string", default=current,
                description=slot.label or f"text content of {node_id}",
            ))
            used_names.add(node_id)
        elif slot.kind == "asset":
            current = read_node_asset_ref(html, node_id)
            if current is None:
                continue
            updated = _replace_asset_ref(html, node_id, f"{{{{asset:{node_id}}}}}")
            if updated is None:
                continue
            html = updated
            params.append(ParamSpec(
                name=node_id, type="asset_id", default=current,
                description=slot.label or f"asset for {node_id}",
            ))
            used_names.add(node_id)
        # Other slot kinds (token / slot_attr / animation_timing) are
        # preserved on the template's editable_slots but don't become scalar
        # params here — token values are parameterized separately below.

    for token in layer.editable_tokens:
        if token in used_names:
            continue
        current = _token_value(html, token)
        if current is None:
            continue
        updated = _replace_token_value(html, token, f"{{{{token:{token}}}}}")
        if updated is None:
            continue
        html = updated
        param_type = _classify_token(current)
        typed_default: Any = current
        if param_type == "integer":
            typed_default = int(current)
        elif param_type == "number":
            typed_default = float(current)
        params.append(ParamSpec(
            name=token, type=param_type, default=typed_default,
            description=f"design token --{token}",
        ))
        used_names.add(token)

    return HtmlTemplate(
        template_id=template_id,
        display_name=display_name,
        description=description,
        html_template=html,
        params_schema=tuple(params),
        viewport_width=layer.viewport_width,
        viewport_height=layer.viewport_height,
        version=version,
        fps_num=layer.fps_num,
        fps_den=layer.fps_den,
        duration_frames=layer.duration_frames,
        bound_asset_ids=layer.bound_asset_ids,
        editable_slots=layer.editable_slots,
        editable_tokens=layer.editable_tokens,
        sanitizer_version=layer.sanitizer_version,
        metadata=dict(layer.metadata),
        source_hash=layer.sanitizer_hash or layer.source_hash,
        provenance_layer_revision=layer.revision,
    )


def apply_html_template(
    template: HtmlTemplate,
    params: Mapping[str, Any] | None = None,
    *,
    layer_id: str,
    sanitizer: HtmlSanitizer | None = None,
) -> FreeformHtmlMotionLayer:
    """Reconstitute a :class:`FreeformHtmlMotionLayer` from ``template``.

    Merges ``params`` over the template defaults, substitutes the markers in
    a single pass (so a value containing a marker substring is not re-read),
    then runs the sanitizer again before rebuilding the layer.  Any required
    param left without a default+value raises ``ValueError``.
    """
    merged = dict(template.default_params())
    if params:
        merged.update(params)
    validation_errors = template.validate_params(merged)
    if validation_errors:
        raise ValueError(
            f"invalid params for template {template.template_id!r}: "
            + "; ".join(validation_errors)
        )
    # Missing required params with no default → fail loudly before substituting.
    for spec in template.params_schema:
        if spec.required and merged.get(spec.name) is None:
            raise ValueError(f"missing required parameter: {spec.name}")

    values = {name: str(val) for name, val in merged.items() if val is not None}
    # Defence in depth: parameter values are user-editable strings.  Strip
    # script blocks before substitution regardless of the layer's browser
    # profile — a freeform template author's inline JS is allowed, but a
    # parameter value must never inject one.
    values = {name: _SCRIPT_BLOCK_RE.sub("", value) for name, value in values.items()}

    def _sub(match: re.Match) -> str:
        kind, name = match.group(1), match.group(2)
        value = values.get(name)
        if value is None:
            return match.group(0)
        # AUD-30017: neutralise per substitution context (text/attr/CSS)
        # so active content (on* handlers, markup, style breakout) can
        # never survive the subsequent freeform-profile re-sanitization.
        return _prepare_param_value(kind, value)

    substituted = _MARKER_RE.sub(_sub, template.html_template)

    # Motion Graphic generator templates carry the same freeform browser
    # profile as their source layer.  Rebuild through the shared source
    # importer so inline scripts, page markers, CSP and timing metadata remain
    # consistent with direct HTML import.  Legacy templates without metadata
    # retain the conservative sanitizer path below.
    #
    # AUD-30018: the routing condition used to read ``if layer_metadata:``
    # *after* the schema key below was injected unconditionally, so it was
    # always true — the conservative fallback was dead code and every
    # instantiation (even of a legacy, metadata-less template) received the
    # freeform browser profile.  Route on the template's own metadata, which
    # is what "generator vs legacy" actually means, before any key injection.
    source_metadata = template.metadata
    layer_metadata = dict(source_metadata)
    # Keep the template's declared parameter types with the instantiated
    # layer.  The HTML itself only contains CSS strings, so without this the
    # Motion Graphic inspector cannot distinguish a colour token from a
    # numeric token after a clip has been dropped on the timeline.
    layer_metadata["motion_graphic_parameter_schema"] = [
        spec.to_dict() for spec in template.params_schema
    ]
    if source_metadata:
        from ...core.html_layer_commands import build_html_layer_from_source

        return build_html_layer_from_source(
            substituted,
            layer_id=layer_id,
            authored_source=substituted,
            viewport_width=template.viewport_width,
            viewport_height=template.viewport_height,
            duration_frames=template.duration_frames,
            fps_num=template.fps_num,
            fps_den=template.fps_den,
            bound_asset_ids=template.bound_asset_ids,
            metadata=layer_metadata,
            editable_slots=template.editable_slots,
        )

    # Defence in depth: a parameter value must not bypass the allowlist.
    san = sanitizer or HtmlSanitizer()
    result = san.sanitize(substituted)

    return build_html_layer(
        layer_id=layer_id,
        sanitized_html=result.html,
        authored_source=substituted,
        viewport_width=template.viewport_width,
        viewport_height=template.viewport_height,
        fps_num=template.fps_num,
        fps_den=template.fps_den,
        duration_frames=template.duration_frames,
        bound_asset_ids=template.bound_asset_ids,
        editable_slots=template.editable_slots,
        editable_tokens=template.editable_tokens,
        sanitizer_version=template.sanitizer_version,
        metadata=layer_metadata,
    )


__all__ = [
    "promote_html_layer",
    "apply_html_template",
]
