"""FH-09: promote a verified HTML motion layer into a reusable template.

Covers the pure promotion/apply engine (no browser, no Qt):
- the public node readers (text + void-element asset),
- ``promote_html_layer`` (slot/token parameterization, graceful skips,
  provenance, motion-CSS preserved),
- ``apply_html_template`` (round-trip, overrides, required-param gate,
  re-sanitize defence, single-pass substitution),
- the :class:`HtmlTemplate` dataclass contract + serialization, and
- :class:`HtmlTemplateRegistry` (versioning + ``schema_for_ai``).
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.html_layer import (
    FreeformHtmlMotionLayer, HtmlEditSlot, build_html_layer,
)
from src.features.teaching_animation.core.html_template import (
    HtmlTemplate, HtmlTemplateRegistry,
)
from src.features.teaching_animation.core.executable_template import ParamSpec
from src.features.teaching_animation.render.html_layer import (
    apply_html_template, promote_html_layer, read_node_asset_ref,
    read_node_inner_text,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _html(*, with_token_def: bool = True, with_brand_use: bool = True) -> str:
    brand_def = ":root{--brand:#00e676}" if with_token_def else ""
    brand_use = ";background:var(--brand)" if with_brand_use else ""
    return (
        "<style>"
        f"{brand_def}.t{{color:#fff{brand_use}}}"
        "@keyframes slide{0%{left:0}100%{left:200px}}"
        ".bar{animation:slide 1s linear infinite;transition:opacity .3s ease}"
        "</style>"
        '<div class="bar" data-topos-node-id="title">Hello</div>'
        '<img src="topos-asset://logo" data-topos-node-id="logo">'
    )


def _layer(
    *, editable_slots=(HtmlEditSlot(node_id="title", kind="text"),
                       HtmlEditSlot(node_id="logo", kind="asset")),
    editable_tokens=("brand",),
) -> FreeformHtmlMotionLayer:
    html = _html()
    return build_html_layer(
        layer_id="L", sanitized_html=html, authored_source=html,
        viewport_width=320, viewport_height=180, fps_num=24, fps_den=1,
        duration_frames=48, bound_asset_ids=("logo",),
        editable_slots=tuple(editable_slots), editable_tokens=tuple(editable_tokens),
        sanitizer_version="test-1.0",
    )


# --------------------------------------------------------------------------- #
# Node readers
# --------------------------------------------------------------------------- #

class TestNodeReaders:
    def test_read_inner_text(self) -> None:
        html = '<div data-topos-node-id="t">  Hello World  </div>'
        assert read_node_inner_text(html, "t") == "Hello World"

    def test_read_inner_text_void_returns_empty(self) -> None:
        html = '<img src="topos-asset://x" data-topos-node-id="i">'
        assert read_node_inner_text(html, "i") == ""

    def test_read_inner_text_missing_node(self) -> None:
        assert read_node_inner_text("<div>x</div>", "nope") is None

    def test_read_asset_ref_on_void_img(self) -> None:
        # The _locate_element void-fix is what makes this work.
        html = '<img src="topos-asset://logo" data-topos-node-id="logo">'
        assert read_node_asset_ref(html, "logo") == "logo"

    def test_read_asset_ref_href_fallback(self) -> None:
        html = '<a href="topos-asset://doc" data-topos-node-id="lnk">link</a>'
        assert read_node_asset_ref(html, "lnk") == "doc"

    def test_read_asset_ref_non_topos_returns_none(self) -> None:
        html = '<img src="https://evil/x.png" data-topos-node-id="i">'
        assert read_node_asset_ref(html, "i") is None

    def test_read_asset_ref_missing_node(self) -> None:
        assert read_node_asset_ref("<img>", "nope") is None


# --------------------------------------------------------------------------- #
# promote_html_layer
# --------------------------------------------------------------------------- #

class TestPromote:
    def test_text_slot_becomes_string_param_with_marker(self) -> None:
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        spec = next(p for p in tpl.params_schema if p.name == "title")
        assert spec.type == "string" and spec.default == "Hello"
        assert "{{slot:title}}" in tpl.html_template

    def test_asset_slot_on_img_becomes_asset_id_param(self) -> None:
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        spec = next(p for p in tpl.params_schema if p.name == "logo")
        assert spec.type == "asset_id" and spec.default == "logo"
        assert "{{asset:logo}}" in tpl.html_template

    def test_color_token_becomes_color_param(self) -> None:
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        spec = next(p for p in tpl.params_schema if p.name == "brand")
        assert spec.type == "color" and spec.default == "#00e676"
        assert "{{token:brand}}" in tpl.html_template

    def test_non_color_token_classified_string(self) -> None:
        html = "<style>:root{--gap:12px}</style><div data-topos-node-id='x'>x</div>"
        layer = build_html_layer(layer_id="L", sanitized_html=html, authored_source=html,
                                 viewport_width=10, viewport_height=10,
                                 editable_tokens=("gap",))
        tpl = promote_html_layer(layer, template_id="t", display_name="T")
        spec = next(p for p in tpl.params_schema if p.name == "gap")
        assert spec.type == "string" and spec.default == "12px"

    def test_unitless_numeric_token_becomes_numeric_control(self) -> None:
        html = "<style>:root{--density:24}</style><div data-topos-node-id='x'>x</div>"
        layer = build_html_layer(layer_id="L", sanitized_html=html, authored_source=html,
                                 viewport_width=10, viewport_height=10,
                                 editable_tokens=("density",))
        tpl = promote_html_layer(layer, template_id="t", display_name="T")
        spec = next(p for p in tpl.params_schema if p.name == "density")
        assert spec.type == "integer" and spec.default == 24

    def test_undefined_token_is_skipped(self) -> None:
        # Token used via var() but never defined inline → no value to capture.
        html = "<style>.t{color:var(--brand)}</style><div data-topos-node-id='x'>x</div>"
        layer = build_html_layer(layer_id="L", sanitized_html=html, authored_source=html,
                                 viewport_width=10, viewport_height=10,
                                 editable_tokens=("brand",))
        tpl = promote_html_layer(layer, template_id="t", display_name="T")
        assert not any(p.name == "brand" for p in tpl.params_schema)

    def test_slot_node_missing_in_html_skipped(self) -> None:
        layer = _layer(editable_slots=(HtmlEditSlot(node_id="ghost", kind="text"),))
        tpl = promote_html_layer(layer, template_id="t", display_name="T")
        assert not any(p.name == "ghost" for p in tpl.params_schema)

    def test_duplicate_name_second_occurrence_skipped(self) -> None:
        # node_id and token name collide → only the first wins (avoids the
        # HtmlTemplate "param names must be unique" guard firing).
        html = ("<style>:root{--dup:#fff}</style>"
                "<div data-topos-node-id='dup'>x</div>")
        layer = build_html_layer(layer_id="L", sanitized_html=html, authored_source=html,
                                 viewport_width=10, viewport_height=10,
                                 editable_slots=(HtmlEditSlot(node_id="dup", kind="text"),),
                                 editable_tokens=("dup",))
        tpl = promote_html_layer(layer, template_id="t", display_name="T")
        assert sum(1 for p in tpl.params_schema if p.name == "dup") == 1

    def test_motion_css_preserved_in_template(self) -> None:
        # Regression guard for the animation/transition sanitizer fix: the
        # promoted template must still carry the animation + keyframes.
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        assert "animation:slide" in tpl.html_template
        assert "transition:opacity" in tpl.html_template
        assert "@keyframes slide" in tpl.html_template

    def test_provenance_and_geometry_preserved(self) -> None:
        layer = _layer()
        tpl = promote_html_layer(layer, template_id="t", display_name="T",
                                 description="desc", version=3)
        assert tpl.source_hash == layer.sanitizer_hash
        assert tpl.provenance_layer_revision == layer.revision
        assert tpl.viewport_width == 320 and tpl.viewport_height == 180
        assert tpl.duration_frames == 48 and tpl.fps_num == 24
        assert tpl.bound_asset_ids == ("logo",)
        assert tpl.editable_slots == layer.editable_slots
        assert tpl.editable_tokens == layer.editable_tokens
        assert tpl.description == "desc" and tpl.version == 3

    def test_source_layer_not_mutated(self) -> None:
        layer = _layer()
        before = layer.sanitized_html
        promote_html_layer(layer, template_id="t", display_name="T")
        assert layer.sanitized_html == before  # pure transform


# --------------------------------------------------------------------------- #
# apply_html_template
# --------------------------------------------------------------------------- #

class TestApply:
    def test_defaults_round_trip_content(self) -> None:
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        back = apply_html_template(tpl, layer_id="L2")
        assert "Hello" in back.sanitized_html
        assert "topos-asset://logo" in back.sanitized_html
        assert "#00e676" in back.sanitized_html
        assert back.layer_id == "L2"

    def test_overrides_substituted(self) -> None:
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        out = apply_html_template(
            tpl, {"title": "World", "logo": "photo", "brand": "#1122ff"},
            layer_id="L3")
        assert "World" in out.sanitized_html and "Hello" not in out.sanitized_html
        assert "topos-asset://photo" in out.sanitized_html
        assert "#1122ff" in out.sanitized_html

    def test_required_param_missing_raises(self) -> None:
        tpl = HtmlTemplate(
            template_id="t", display_name="T", html_template="{{slot:x}}",
            params_schema=(ParamSpec(name="x", type="string", required=True),),
            viewport_width=1, viewport_height=1,
        )
        with pytest.raises(ValueError):
            apply_html_template(tpl, layer_id="L")

    def test_script_in_param_stripped_by_resanitize(self) -> None:
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        out = apply_html_template(tpl, {"title": "<script>alert(1)</script>X"}, layer_id="L")
        assert "<script" not in out.sanitized_html
        assert "alert(1)" not in out.sanitized_html

    def test_marker_inside_value_not_resubstituted(self) -> None:
        # Single-pass substitution: a value containing a marker substring is
        # not re-read (prevents injection cascades).
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        out = apply_html_template(tpl, {"title": "{{slot:title}}"}, layer_id="L")
        # The literal marker text should appear verbatim, not collapse/cascade.
        assert "{{slot:title}}" in out.sanitized_html

    def test_geometry_carried_to_new_layer(self) -> None:
        tpl = promote_html_layer(_layer(), template_id="t", display_name="T")
        out = apply_html_template(tpl, layer_id="L9")
        assert out.viewport_width == 320 and out.viewport_height == 180
        assert out.duration_frames == 48
        assert out.bound_asset_ids == ("logo",)
        assert out.editable_slots == tpl.editable_slots


# --------------------------------------------------------------------------- #
# HtmlTemplate dataclass contract
# --------------------------------------------------------------------------- #

class TestHtmlTemplateContract:
    def _base_kwargs(self) -> dict:
        return dict(template_id="t", display_name="T", html_template="x",
                    params_schema=(), viewport_width=1, viewport_height=1)

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            HtmlTemplate(**{**self._base_kwargs(), "template_id": ""})

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            HtmlTemplate(**{**self._base_kwargs(), "display_name": ""})

    def test_nonpositive_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            HtmlTemplate(**{**self._base_kwargs(), "version": 0})

    def test_duplicate_param_names_rejected(self) -> None:
        schema = (ParamSpec(name="a", type="string"), ParamSpec(name="a", type="string"))
        with pytest.raises(ValueError):
            HtmlTemplate(**{**self._base_kwargs(), "params_schema": schema})

    def test_validate_params_mirrors_executable_template(self) -> None:
        schema = (ParamSpec(name="n", type="integer", required=True),
                  ParamSpec(name="c", type="color", enum=("#000", "#fff")))
        tpl = HtmlTemplate(**{**self._base_kwargs(), "params_schema": schema})
        assert tpl.validate_params({"n": 5, "c": "#000"}) == []
        assert any("missing required" in e for e in tpl.validate_params({"c": "#000"}))
        assert any("must be an integer" in e for e in tpl.validate_params({"n": "x", "c": "#000"}))
        assert any("must be one of" in e for e in tpl.validate_params({"n": 1, "c": "#f00"}))

    def test_to_dict_from_dict_round_trip(self) -> None:
        original = promote_html_layer(_layer(), template_id="t", display_name="T",
                                      description="d", version=2)
        restored = HtmlTemplate.from_dict(original.to_dict())
        assert restored.template_id == original.template_id
        assert restored.display_name == original.display_name
        assert restored.html_template == original.html_template
        assert restored.version == original.version
        assert restored.params_schema == original.params_schema
        assert restored.editable_slots == original.editable_slots
        assert restored.editable_tokens == original.editable_tokens
        assert restored.source_hash == original.source_hash
        assert restored.viewport_width == original.viewport_width


# --------------------------------------------------------------------------- #
# HtmlTemplateRegistry
# --------------------------------------------------------------------------- #

class TestRegistry:
    def test_register_get_latest_and_specific_version(self) -> None:
        reg = HtmlTemplateRegistry()
        v1 = promote_html_layer(_layer(), template_id="t", display_name="T", version=1)
        v2 = promote_html_layer(_layer(), template_id="t", display_name="T", version=2)
        reg.register(v1)
        reg.register(v2)
        assert reg.get("t").version == 2          # latest
        assert reg.get("t", version=1).version == 1

    def test_list_templates_returns_latest_only(self) -> None:
        reg = HtmlTemplateRegistry()
        a = promote_html_layer(_layer(), template_id="a", display_name="A")
        b = promote_html_layer(_layer(), template_id="b", display_name="B")
        reg.register(a)
        reg.register(b)
        listed = {t.template_id: t.version for t in reg.list_templates()}
        assert listed == {"a": 1, "b": 1}

    def test_get_unknown_returns_none(self) -> None:
        assert HtmlTemplateRegistry().get("nope") is None

    def test_schema_for_ai_shape(self) -> None:
        reg = HtmlTemplateRegistry()
        reg.register(promote_html_layer(_layer(), template_id="t", display_name="T",
                                        description="d"))
        schema = reg.schema_for_ai("t")
        assert schema is not None
        assert schema["kind"] == "html"
        assert schema["template_id"] == "t"
        assert {p["name"] for p in schema["params"]} == {"title", "logo", "brand"}

    def test_schema_for_ai_unknown_returns_none(self) -> None:
        assert HtmlTemplateRegistry().schema_for_ai("nope") is None
