"""AUD-30017 regression tests: parameter values must never inject active content.

``apply_html_template`` substitutes external (user/AI-provided) parameter
values into the template HTML and rebuilds a layer through
``build_html_layer_from_source``, whose freeform Motion Graphic profile
deliberately keeps ``on*`` event handlers / inline scripts authored by the
template author.  These tests pin the AUD-30017 fix: parameter values are
treated as *data* per substitution context (text slot / asset id / CSS
token), so payloads like ``<svg onload=...>``, ``x" onerror="...`` or
``red}</style><img onerror=...>`` can never survive as active markup.

An ``on*=`` sequence appearing inside escaped text or inside a quoted
attribute *value* is inert — the assertions below therefore check for real
markup/attribute boundaries, not bare substring matches.

Run: .venv/bin/python -m pytest tests/teaching_animation/test_fix_w6g7_aud30017.py
"""

from __future__ import annotations

import re

from src.features.teaching_animation.core.executable_template import ParamSpec
from src.features.teaching_animation.core.html_template import HtmlTemplate
from src.features.teaching_animation.render.html_layer import apply_html_template
from src.features.teaching_animation.render.html_layer.template_promote import (
    _prepare_param_value,
)

# A real event-handler attribute needs a quote (or unquoted value followed
# by a tag/attribute boundary); escaped text never forms one.
_ON_ATTR_BOUNDARY_RE = re.compile(r"\son[a-z]+\s*=\s*[\"']", re.IGNORECASE)
# Real open tags (payload text is entity-escaped, so it never matches).
_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")


def _on_attr_in_real_tags(html: str) -> re.Match | None:
    """Search for an event-handler attribute inside actual open tags only.

    An ``on*=`` sequence that appears in escaped text content or inside a
    quoted attribute *value* is inert; only attributes on real tags fire.
    """
    for tag in _TAG_RE.findall(html):
        m = _ON_ATTR_BOUNDARY_RE.search(tag)
        if m:
            return m
    return None


def _make_template() -> HtmlTemplate:
    """One template with a text slot, an asset slot and a CSS token."""
    return HtmlTemplate(
        template_id="t",
        display_name="T",
        html_template=(
            "<style>:root{--brand:{{token:brand}}}"
            ".t{color:#fff;background:var(--brand)}</style>"
            '<div class="t" data-topos-node-id="title">{{slot:title}}</div>'
            '<img src="topos-asset://{{asset:logo}}" data-topos-node-id="logo">'
        ),
        params_schema=(
            ParamSpec(name="title", type="string", default="Hello"),
            ParamSpec(name="logo", type="asset_id", default="logo"),
            ParamSpec(name="brand", type="color", default="#00e676"),
        ),
        viewport_width=320,
        viewport_height=180,
        # Non-empty metadata routes the rebuild through
        # build_html_layer_from_source → freeform profile with
        # allow_event_handlers=True — the exact path from the audit.
        metadata={"render_profile": "motion_graphic_freeform"},
    )


def _after_style(html: str) -> str:
    """The markup portion after the (last) style block."""
    return html.rsplit("</style>", 1)[-1]


class TestSlotTextPayloadsNeutralized:
    """Text-slot values are HTML text, never markup."""

    def test_svg_onload_payload_not_active(self) -> None:
        out = apply_html_template(
            _make_template(),
            {"title": "<svg onload=alert(1)>"},
            layer_id="L",
        )
        html = out.sanitized_html
        assert "<svg" not in html
        # The payload survives as visible literal text, not markup.
        assert "&lt;svg onload=alert(1)&gt;" in html
        # No event-handler attribute boundary is formed anywhere.
        assert _on_attr_in_real_tags(html) is None

    def test_img_onerror_payload_not_active(self) -> None:
        out = apply_html_template(
            _make_template(),
            {"title": '<img src=x onerror="alert(1)">'},
            layer_id="L",
        )
        html = out.sanitized_html
        assert _on_attr_in_real_tags(html) is None
        # No *injected* img: the only img is the template's own logo node.
        assert html.count("<img") == 1

    def test_onclick_anchor_payload_not_active(self) -> None:
        out = apply_html_template(
            _make_template(),
            {"title": "<a onclick=alert(1)>click</a>"},
            layer_id="L",
        )
        html = out.sanitized_html
        assert "<a" not in html
        assert _on_attr_in_real_tags(html) is None

    def test_javascript_url_in_text_is_inert_text(self) -> None:
        out = apply_html_template(
            _make_template(),
            {"title": "javascript:alert(1)"},
            layer_id="L",
        )
        html = out.sanitized_html
        # It remains literal text inside the div — no attribute context is
        # formed around it, so it cannot execute.
        assert "javascript:alert(1)" in html
        assert 'href="javascript:' not in html
        assert 'src="javascript:' not in html

    def test_script_block_still_stripped(self) -> None:
        out = apply_html_template(
            _make_template(),
            {"title": "<script>alert(1)</script>X"},
            layer_id="L",
        )
        assert "<script" not in out.sanitized_html
        assert "alert(1)" not in out.sanitized_html


class TestAssetPayloadsNeutralized:
    """Asset ids stay inside the quoted topos-asset:// attribute."""

    def test_attribute_breakout_blocked(self) -> None:
        out = apply_html_template(
            _make_template(),
            {'logo': 'x" onerror="alert(1)'},
            layer_id="L",
        )
        html = out.sanitized_html
        # The quote is neutralised (&quot;) so no new attribute can open.
        assert ' onerror="' not in html
        assert _on_attr_in_real_tags(html) is None
        # Exactly one img — the template's own node.
        assert html.count("<img") == 1

    def test_javascript_scheme_cannot_replace_topos_scheme(self) -> None:
        out = apply_html_template(
            _make_template(),
            {"logo": "javascript:alert(1)"},
            layer_id="L",
        )
        # The substitution site fixes the topos-asset:// prefix; the value
        # can only be the id part, never a scheme override.
        assert 'src="topos-asset://javascript:alert(1)"' in out.sanitized_html


class TestTokenPayloadsNeutralized:
    """Token values stay a single CSS declaration value."""

    def test_style_block_breakout_blocked(self) -> None:
        out = apply_html_template(
            _make_template(),
            {"brand": "red}</style><img src=x onerror=alert(1)>"},
            layer_id="L",
        )
        html = out.sanitized_html
        # The </style> breakout is gone: only the template's own img and
        # style block exist, and no event-handler attribute is formed in
        # the markup portion.
        assert "</style><img" not in html
        assert html.count("<img") == 1
        assert html.count("<style>") == 1
        assert _on_attr_in_real_tags(_after_style(html)) is None

    def test_rule_restructure_metacharacters_stripped(self) -> None:
        out = apply_html_template(
            _make_template(),
            {"brand": "red} *{position:fixed;inset:0"},
            layer_id="L",
        )
        style = out.sanitized_html.split("</style>", 1)[0]
        # The injected selector never forms a new rule: every structural
        # metacharacter is stripped, so the whole payload flattens into the
        # (inert) declaration value.
        assert "*{" not in style
        assert "position:fixed;" not in style
        assert "--brand:red *position:fixedinset:0}" in style

    def test_unit_metacharacters_stripped(self) -> None:
        assert _prepare_param_value("token", "a}b{c;d<e>f\\g") == "abcdefg"

    def test_legit_token_values_pass_through(self) -> None:
        for legit in ("#00e676", "#1122ff", "1.5em", "rgba(0,0,0,.5)", "dark-blue"):
            assert _prepare_param_value("token", legit) == legit


class TestUnitNeutralisation:
    """Direct unit checks for _prepare_param_value per context."""

    def test_slot_escapes_markup(self) -> None:
        assert _prepare_param_value("slot", '<b onclick="x">') == (
            "&lt;b onclick=&quot;x&quot;&gt;"
        )

    def test_asset_escapes_quotes(self) -> None:
        assert _prepare_param_value("asset", 'a"b') == "a&quot;b"

    def test_unknown_kind_left_as_is(self) -> None:
        assert _prepare_param_value("other", "x") == "x"
