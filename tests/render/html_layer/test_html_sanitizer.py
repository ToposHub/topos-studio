"""W2-prep sanitizer: allowlist + attack corpus (FH-02)."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.render.html_layer import (
    HtmlSanitizer, extract_declared_nodes, extract_slots, extract_tokens,
)
from src.features.teaching_animation.render.html_renderer_spike import ATTACK_FIXTURES


@pytest.fixture
def sanitizer() -> HtmlSanitizer:
    return HtmlSanitizer()


class TestAllowlist:
    def test_safe_html_passes_through(self, sanitizer: HtmlSanitizer) -> None:
        html = '<div class="card" data-topos-node-id="c1"><h1>Title</h1><p>Body</p></div>'
        result = sanitizer.sanitize(html)
        assert result.ok
        assert "data-topos-node-id" in result.html
        assert "Title" in result.html and "Body" in result.html
        assert result.report.removed_tags == ()

    def test_safe_css_kept(self, sanitizer: HtmlSanitizer) -> None:
        html = '<style>.card{color:#fff;font-size:20px;transform:translateX(10px)}</style>'
        result = sanitizer.sanitize(html)
        assert "color:#fff" in result.html
        assert "transform:translatex(10px)" in result.html or "translateX(10px)" in result.html

    def test_inline_layout_css_is_kept(self, sanitizer: HtmlSanitizer) -> None:
        """Inline styles must retain the model's actual canvas geometry."""
        html = (
            '<div style="position:absolute;top:120px;left:240px;'
            'width:480px;height:260px;background:#123456;color:#fff">Card</div>'
        )
        result = sanitizer.sanitize(html)
        assert 'style="' in result.html
        assert "position:absolute" in result.html
        assert "top:120px" in result.html and "left:240px" in result.html
        assert "width:480px" in result.html and "height:260px" in result.html
        assert "background:#123456" in result.html
        assert result.report.removed_attrs == ()

    def test_keyframes_kept(self, sanitizer: HtmlSanitizer) -> None:
        html = '<style>@keyframes slide{from{left:0}to{left:100px}}</style>'
        result = sanitizer.sanitize(html)
        assert "@keyframes slide" in result.html
        assert "left:0" in result.html
        assert "},to{" not in result.html

    def test_animation_and_transition_kept_and_idempotent(self, sanitizer: HtmlSanitizer) -> None:
        # CSS time-based motion is essential to the HTML motion layer and has
        # no script/network/file surface.  Both the shorthand and the longhand
        # family must survive, and the sanitizer must be a fixed point on a
        # realistic motion-graphics blob (re-scan removes nothing) — otherwise
        # the export quality gate (check_unsafe_content) rejects animated HTML.
        html = ("<style>"
                ".bar{animation:slide 1s linear infinite;animation-delay:.2s}"
                ".t{transition:opacity .3s ease;transition-duration:.5s}"
                "@keyframes slide{0%{left:0}100%{left:200px}}"
                "</style>")
        first = sanitizer.sanitize(html)
        assert "animation:slide" in first.html
        assert "animation-delay:.2s" in first.html
        assert "transition:opacity" in first.html
        assert "transition-duration:.5s" in first.html
        # Fixed point: a second pass over the already-sanitized HTML must not
        # remove any further tokens (this is exactly the export-gate check).
        second = sanitizer.sanitize(first.html)
        residual = (second.report.removed_tags + second.report.removed_attrs
                    + second.report.removed_css)
        assert residual == (), f"sanitizer not idempotent on motion CSS: {residual}"

    def test_responsive_rules_and_web_composition_properties_survive(self, sanitizer: HtmlSanitizer) -> None:
        html = ("<style>"
                ".hero{display:grid;grid-template-columns:1fr 2fr;order:2;"
                "clip-path:inset(0 round 24px);mask-image:linear-gradient(#000,#0000);"
                "outline:1px solid #fff;vertical-align:middle}"
                "@media (max-width:800px){.hero{grid-template-columns:1fr;row-gap:24px}}"
                "@supports (backdrop-filter:blur(1px)){.hero{backdrop-filter:blur(12px)}}"
                "</style>")
        result = sanitizer.sanitize(html)
        assert "clip-path:inset(0 round 24px)" in result.html
        assert "mask-image:linear-gradient(#000,#0000)" in result.html
        assert "@media (max-width:800px)" in result.html
        assert "@supports (backdrop-filter:blur(1px))" in result.html
        second = sanitizer.sanitize(result.html)
        assert second.report.removed_css == ()

    def test_same_document_css_fragments_are_safe(self, sanitizer: HtmlSanitizer) -> None:
        result = sanitizer.sanitize(
            "<style>.hero{clip-path:url(#hero-clip);mask-image:url('#hero-mask')}"
            "</style>"
        )
        assert "clip-path:url(#hero-clip)" in result.html
        assert "mask-image:url('#hero-mask')" in result.html
        assert result.report.removed_css == ()

    def test_font_face_topos_asset_src_kept(self, sanitizer: HtmlSanitizer) -> None:
        html = ("<style>@font-face{font-family:'X';"
                "src:url(topos-asset://font/dejavu-sans) format('truetype')}</style>")
        result = sanitizer.sanitize(html)
        assert result.ok
        assert "@font-face" in result.html
        assert "topos-asset://font/dejavu-sans" in result.html
        assert "font-family:" in result.html

    def test_font_face_remote_src_stripped(self, sanitizer: HtmlSanitizer) -> None:
        # A remote @font-face src is an external-font escape; it must be dropped.
        html = ("<style>@font-face{font-family:'Evil';"
                "src:url(https://evil.example/font.woff2)}</style>")
        result = sanitizer.sanitize(html)
        assert "https://evil.example" not in result.html
        # The whole @font-face block is removed once its only src is stripped.
        assert "@font-face" not in result.html

    def test_topos_asset_src_kept(self, sanitizer: HtmlSanitizer) -> None:
        html = '<img src="topos-asset://logo" alt="logo">'
        result = sanitizer.sanitize(html)
        assert "topos-asset://logo" in result.html

    def test_static_svg_visuals_and_text_survive(self, sanitizer: HtmlSanitizer) -> None:
        """Model-authored SVG art must not collapse into a blank canvas."""
        html = (
            '<svg viewBox="0 0 100 60" xmlns="http://www.w3.org/2000/svg">'
            '<defs><linearGradient id="g"><stop offset="0%" stop-color="#06b6d4"/>'
            '<stop offset="100%" stop-color="#8b5cf6"/></linearGradient></defs>'
            '<rect x="2" y="2" width="96" height="56" rx="8" fill="url(#g)"/>'
            '<path d="M10 40 L50 8 L90 40" fill="none" stroke="#fff" stroke-width="2"/>'
            '<text x="50" y="34" text-anchor="middle" fill="#fff">HTML5</text>'
            '</svg>'
        )
        result = sanitizer.sanitize(html)
        assert result.ok
        assert '<svg' in result.html and '<rect' in result.html
        assert '<rect x="2" y="2" width="96" height="56" rx="8" fill="url(#g)"/>' in result.html
        assert '<lineargradient' in result.html and '<path' in result.html
        assert '<text' in result.html and 'HTML5' in result.html
        assert 'url(#g)' in result.html
        assert result.report.removed_tags == ()
        second = sanitizer.sanitize(result.html)
        assert second.report.removed_tags == ()
        assert second.report.removed_attrs == ()

    def test_svg_symbol_reuse_and_foreign_object_survive_safely(self, sanitizer: HtmlSanitizer) -> None:
        html = (
            '<svg viewBox="0 0 100 60"><defs><circle id="dot" r="4" fill="#fff"/>'
            '</defs><use href="#dot" x="20" y="20"/>'
            '<foreignObject x="0" y="0" width="100" height="20">'
            '<div style="font-size:12px;color:#fff">HTML</div></foreignObject></svg>'
        )
        result = sanitizer.sanitize(html)
        assert '<use href="#dot"' in result.html
        assert '<foreignobject' in result.html
        assert "HTML" in result.html
        assert result.report.removed_tags == ()
        assert result.report.removed_attrs == ()

        remote = sanitizer.sanitize('<svg><use href="https://evil.example/icon"/></svg>')
        assert "https://evil.example" not in remote.html

    def test_pseudo_element_composition_survives(self, sanitizer: HtmlSanitizer) -> None:
        """Generated decks may put their full-screen background in ::before."""
        html = (
            '<style>body{margin:0;background:transparent} '
            'body::before{content:"";position:absolute;inset:0;'
            'background:linear-gradient(135deg,#101827,#4c1d95)}</style>'
            '<body><h1 data-topos-node-id="title">Deck</h1></body>'
        )
        result = sanitizer.sanitize(html)
        assert 'content:""' in result.html
        assert 'inset:0' in result.html
        assert result.report.removed_css == ()


class TestMotionCssCoverage:
    """Coverage matrix for CSS a real motion-graphics HTML page uses.

    Living documentation of the allowlist's motion-CSS coverage, locked in
    so a regression (silent stripping of essential motion CSS — the same
    class of bug as the animation/transition fix) cannot recur unnoticed.
    Tier-1 properties must survive AND be sanitizer fixed-points; tier-2
    properties are intentionally pending and listed here so a future
    addition is a deliberate, code-reviewed change rather than drift.
    """

    # Essential motion / CJK compositing — must be kept + fixed-point.
    TIER1_KEPT = (
        "transform", "transform-origin",
        "perspective", "perspective-origin", "backface-visibility",
        "animation", "animation-name", "animation-duration",
        "transition", "transition-property",
        "mix-blend-mode",
        "writing-mode", "text-orientation",
        "filter", "opacity",
        "content", "inset", "inset-block", "inset-inline",
        "clip-path", "mask", "mask-image", "will-change", "pointer-events",
        "contain", "content-visibility", "cursor", "user-select", "tab-size",
        "-webkit-background-clip", "-webkit-text-fill-color",
        "-webkit-backdrop-filter", "-webkit-mask-image",
        "row-gap", "column-gap",
    )
    # Useful but deliberately not yet allowed (url()-policy / vendor-prefix /
    # lower-value).  A failing assertion here means someone added one — make
    # sure that was intended and security-reviewed.
    TIER2_INTENTIONALLY_PENDING = ()

    @pytest.fixture
    def sanitizer(self) -> HtmlSanitizer:
        return HtmlSanitizer()

    @pytest.mark.parametrize("prop", TIER1_KEPT)
    def test_tier1_property_survives_and_is_fixed_point(self, sanitizer, prop) -> None:
        html = f"<style>.x{{{prop}:testvalue}}</style>"
        first = sanitizer.sanitize(html)
        assert not any(prop in r for r in first.report.removed_css), (
            f"{prop} was stripped on first sanitize: {first.report.removed_css}")
        second = sanitizer.sanitize(first.html)
        residual = (second.report.removed_tags + second.report.removed_attrs
                    + second.report.removed_css)
        assert residual == (), f"{prop} not a sanitizer fixed point: {residual}"

    @pytest.mark.parametrize("prop", TIER2_INTENTIONALLY_PENDING)
    def test_tier2_property_currently_stripped(self, sanitizer, prop) -> None:
        # If this fails, {prop} was added to the allowlist — update the
        # tier1 list above and confirm the url()/danger checks still hold.
        html = f"<style>.x{{{prop}:testvalue}}</style>"
        result = sanitizer.sanitize(html)
        assert any(prop in r for r in result.report.removed_css), (
            f"{prop} unexpectedly kept — if intended, promote it to TIER1_KEPT")


class TestStrips:
    def test_script_removed_with_content(self, sanitizer: HtmlSanitizer) -> None:
        html = '<div>x</div><script>alert(1)</script><div>y</div>'
        result = sanitizer.sanitize(html)
        assert "alert(1)" not in result.html
        assert "script" not in result.html
        assert "script" in result.report.removed_tags

    def test_event_handler_stripped(self, sanitizer: HtmlSanitizer) -> None:
        html = '<img src="topos-asset://x" onerror="alert(1)">'
        result = sanitizer.sanitize(html)
        assert "onerror" not in result.html
        assert any("onerror" in a for a in result.report.removed_attrs)

    def test_iframe_removed(self, sanitizer: HtmlSanitizer) -> None:
        html = '<iframe src="https://evil"></iframe>'
        result = sanitizer.sanitize(html)
        assert "iframe" in result.report.removed_tags
        assert "evil" not in result.html

    def test_remote_src_removed(self, sanitizer: HtmlSanitizer) -> None:
        html = '<img src="https://evil.example/track.png">'
        result = sanitizer.sanitize(html)
        assert "evil.example" not in result.html
        assert any("src" in a for a in result.report.removed_attrs)

    def test_network_profile_keeps_cdn_styles_fonts_images_and_external_scripts(self) -> None:
        network = HtmlSanitizer(allow_network_resources=True)
        html = (
            '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">'
            '<script src="https://cdn.tailwindcss.com"></script>'
            '<style>@import url("https://fonts.googleapis.com/css2?family=Inter");'
            'body{background:url("https://images.example/hero.png");}</style>'
            '<img src="https://images.example/hero.png">'
        )
        result = network.sanitize(html)
        assert "cdnjs.cloudflare.com" in result.html
        assert "cdn.tailwindcss.com" in result.html
        assert "fonts.googleapis.com" in result.html
        assert "images.example" in result.html
        assert "<script" in result.html and "</script>" in result.html
        assert "onerror" not in result.html

    def test_motion_graphic_freeform_profile_keeps_author_code_canvas_and_new_css(self) -> None:
        freeform = HtmlSanitizer(
            allow_network_resources=True,
            allow_inline_scripts=True,
            allow_event_handlers=True,
            allow_freeform_css=True,
        )
        html = (
            '<style>@property --progress{syntax:"<number>";inherits:false;initial-value:0}'
            '.art{view-timeline-name:--page;animation-timeline:--page;paint-order:stroke;'
            'background:linear-gradient(135deg,#111,#f0a)}</style>'
            '<canvas id="art" width="1920" height="1080"></canvas>'
            '<script>document.querySelector("#art").dataset.ready="yes"</script>'
            '<button onclick="this.dataset.clicked=\'yes\'">Run</button>'
        )
        result = freeform.sanitize(html)
        assert '<canvas id="art" width="1920" height="1080"></canvas>' in result.html
        assert '<script>document.querySelector("#art")' in result.html
        assert 'onclick="this.dataset.clicked=\'yes\'"' in result.html
        assert "animation-timeline:--page" in result.html
        assert "@property --progress" in result.html
        assert result.report.removed_tags == ()
        assert result.report.removed_attrs == ()
        assert result.report.removed_css == ()

    def test_default_profile_still_removes_author_code(self, sanitizer: HtmlSanitizer) -> None:
        result = sanitizer.sanitize(
            '<canvas width="10" height="10"></canvas>'
            '<script>window.x=1</script><button onclick="window.x=2">x</button>'
        )
        assert "<script" not in result.html
        assert "onclick" not in result.html
        assert "canvas" in result.html

    def test_freeform_profile_keeps_bounded_visual_data_and_blob_urls(self) -> None:
        freeform = HtmlSanitizer(
            allow_network_resources=True,
            allow_inline_scripts=True,
            allow_event_handlers=True,
            allow_freeform_css=True,
            allow_inline_visual_resources=True,
        )
        html = (
            '<style>.hero{background-image:url("data:image/png;base64,AAAA")}'
            '@font-face{font-family:Inline;src:url("data:font/woff2;base64,AAAA")}</style>'
            '<img src="data:image/png;base64,AAAA">'
            '<video poster="blob:https://example.test/poster"></video>'
            '<img src="data:image/svg+xml,<svg></svg>">'
            '<script src="data:application/javascript,alert(1)"></script>'
        )
        result = freeform.sanitize(html)
        assert 'data:image/png;base64,AAAA' in result.html
        assert 'data:font/woff2;base64,AAAA' in result.html
        assert 'blob:https://example.test/poster' in result.html
        assert 'data:image/svg+xml' not in result.html
        assert 'data:application/javascript' not in result.html

    def test_javascript_uri_removed(self, sanitizer: HtmlSanitizer) -> None:
        html = '<a href="javascript:alert(1)">x</a>'
        result = sanitizer.sanitize(html)
        assert "javascript:" not in result.html

    def test_css_import_removed(self, sanitizer: HtmlSanitizer) -> None:
        html = '<style>@import url("https://evil/x.css");body{color:red}</style>'
        result = sanitizer.sanitize(html)
        assert "evil" not in result.html
        assert "color:red" in result.html  # safe rule kept

    def test_css_expression_removed(self, sanitizer: HtmlSanitizer) -> None:
        html = '<style>body{width:expression(alert(1));color:red}</style>'
        result = sanitizer.sanitize(html)
        assert "expression" not in result.html
        assert "color:red" in result.html

    def test_css_remote_url_removed(self, sanitizer: HtmlSanitizer) -> None:
        html = '<style>body{background:url(https://evil/x.png);color:red}</style>'
        result = sanitizer.sanitize(html)
        assert "evil" not in result.html
        assert "color:red" in result.html

    def test_disallowed_css_property_dropped(self, sanitizer: HtmlSanitizer) -> None:
        html = '<style>div{color:red;-moz-binding:url(x)}</style>'
        result = sanitizer.sanitize(html)
        assert "color:red" in result.html
        assert "binding" not in result.html


class TestAttackCorpus:
    @pytest.mark.parametrize("fixture", ATTACK_FIXTURES, ids=[f.fixture_id for f in ATTACK_FIXTURES])
    def test_corpus_produces_no_executable_or_network(self, fixture, sanitizer: HtmlSanitizer) -> None:
        result = sanitizer.sanitize(fixture.html)
        out = result.html.lower()
        # No script content may survive, no remote URL, no event handler.
        assert "<script" not in out
        assert "javascript:" not in out
        assert "onerror" not in out and "onclick" not in out and "onload" not in out
        # No remote network/file URL survives (topos-asset:// is the only allowed URL).
        for bad in ("https://", "http://", "file:", "evil.example"):
            assert bad not in out, f"{fixture.fixture_id} leaked {bad!r}"


class TestNodeIdentity:
    def test_extract_declared_nodes(self) -> None:
        html = ('<div data-topos-node-id="title" data-topos-edit-mode="content">Hi</div>'
                '<div data-topos-node-id="badge" data-topos-edit-mode="absolute_transform">x</div>'
                '<img data-topos-node-id="logo" src="topos-asset://logo">')
        nodes = extract_declared_nodes(html)
        assert set(nodes) == {"title", "badge", "logo"}
        assert nodes["title"].edit_mode == "content"
        assert nodes["badge"].edit_mode == "absolute_transform"
        assert nodes["logo"].asset_slot is True

    def test_extract_tokens_and_slots(self) -> None:
        html = '<style>:root{--brand:#06c;--gap:8px}</style><div data-topos-editable="card1">x</div>'
        assert "brand" in extract_tokens(html)
        assert "card1" in extract_slots(html)

    def test_unmarked_node_not_addressable(self) -> None:
        html = '<div>anonymous</div>'
        assert extract_declared_nodes(html) == {}
