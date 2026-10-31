"""W0-A spike: no-browser tests for the runtime sandbox + security corpus.

These run in every environment (no Qt, no display).  They assert that the
runtime policy (CSP + URL deny-by-default) denies every fixture in the attack
corpus, and that the resource/complexity budgets detect runaway CSS.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.render.html_renderer_spike import (
    ATTACK_FIXTURES,
    BENIGN_FIXTURES,
    CspPolicy,
    ResourceBudget,
    ToposAssetRegistry,
    UrlPolicy,
    UrlRequest,
    check_stylesheet_budget,
)
from src.features.teaching_animation.render.html_renderer_spike import (
    HtmlRendererSpike,
    QtWebEngineGrabBackend,
)


class TestUrlPolicyDenyByDefault:
    """Every request that is not a bound topos-asset:// is denied."""

    def test_network_scheme_blocked(self) -> None:
        policy = UrlPolicy()
        decision = policy.evaluate(UrlRequest(url="https://evil.example/x.png"))
        assert decision.decision.value == "deny"
        assert decision.reason == "network_scheme_blocked"

    def test_motion_graphic_network_profile_allows_https_cdn(self) -> None:
        policy = UrlPolicy(allow_network_resources=True)
        decision = policy.evaluate(UrlRequest(url="https://cdn.tailwindcss.com"))
        assert decision.decision.value == "allow"
        assert decision.reason == "https_network_resource"

    def test_network_profile_can_still_allowlist_hosts(self) -> None:
        policy = UrlPolicy(
            allow_network_resources=True,
            allowed_network_hosts=frozenset({"fonts.googleapis.com"}),
        )
        assert policy.evaluate(UrlRequest(url="https://fonts.googleapis.com/css2")).decision.value == "allow"
        denied = policy.evaluate(UrlRequest(url="https://evil.example/x"))
        assert denied.decision.value == "deny"
        assert denied.reason == "network_host_not_allowed"

    def test_http_scheme_blocked(self) -> None:
        policy = UrlPolicy()
        decision = policy.evaluate(UrlRequest(url="http://evil.example/x"))
        assert decision.decision.value == "deny"

    def test_file_scheme_blocked(self) -> None:
        policy = UrlPolicy()
        decision = policy.evaluate(UrlRequest(url="file:///etc/passwd"))
        assert decision.decision.value == "deny"
        assert decision.reason == "file_scheme_blocked"

    def test_data_uri_blocked(self) -> None:
        policy = UrlPolicy()
        decision = policy.evaluate(UrlRequest(url="data:image/png;base64,AAAA"))
        assert decision.decision.value == "deny"
        assert decision.reason == "data_uri_blocked"

    def test_blob_uri_blocked(self) -> None:
        policy = UrlPolicy()
        decision = policy.evaluate(UrlRequest(url="blob:https://evil.example/uuid"))
        assert decision.decision.value == "deny"
        assert decision.reason == "blob_uri_blocked"

    def test_freeform_profile_allows_visual_data_and_blob_but_not_script_data(self) -> None:
        policy = UrlPolicy(allow_inline_visual_resources=True)
        image = policy.evaluate(UrlRequest(
            url="data:image/png;base64,AAAA", kind="image",
        ))
        assert image.decision.value == "allow"
        blob = policy.evaluate(UrlRequest(
            url="blob:https://example.test/clip", kind="media",
        ))
        assert blob.decision.value == "allow"
        script = policy.evaluate(UrlRequest(
            url="data:application/javascript,alert(1)", kind="script",
        ))
        assert script.decision.value == "deny"
        assert script.reason == "data_uri_blocked"

    def test_namespaced_asset_ids_are_not_truncated(self) -> None:
        policy = UrlPolicy(allowed_asset_ids=frozenset({"image/logo"}))
        decision = policy.evaluate(UrlRequest(url="topos-asset://image/logo"))
        assert decision.decision.value == "allow"

    def test_javascript_uri_blocked(self) -> None:
        policy = UrlPolicy()
        decision = policy.evaluate(UrlRequest(url="javascript:alert(1)"))
        assert decision.decision.value == "deny"
        assert decision.reason == "javascript_uri_blocked"

    def test_unbound_asset_denied(self) -> None:
        policy = UrlPolicy(allowed_asset_ids=frozenset({"logo"}))
        decision = policy.evaluate(UrlRequest(url="topos-asset://other"))
        assert decision.decision.value == "deny"
        assert decision.reason == "asset_id_not_bound"

    def test_bound_asset_allowed(self) -> None:
        policy = UrlPolicy(allowed_asset_ids=frozenset({"logo"}))
        decision = policy.evaluate(UrlRequest(url="topos-asset://logo"))
        assert decision.decision.value == "allow"
        assert decision.reason == "bound_asset"

    def test_asset_id_with_path_allowed(self) -> None:
        policy = UrlPolicy(allowed_asset_ids=frozenset({"logo"}))
        decision = policy.evaluate(UrlRequest(url="topos-asset://logo/variant?x=1"))
        assert decision.decision.value == "allow"

    def test_empty_asset_id_denied(self) -> None:
        policy = UrlPolicy(allowed_asset_ids=frozenset({"logo"}))
        decision = policy.evaluate(UrlRequest(url="topos-asset://"))
        assert decision.decision.value == "deny"
        assert decision.reason == "asset_id_empty"

    def test_relative_path_denied(self) -> None:
        policy = UrlPolicy()
        decision = policy.evaluate(UrlRequest(url="/etc/passwd"))
        assert decision.decision.value == "deny"
        assert decision.reason == "relative_or_missing_scheme"

    def test_whitespace_url_denied(self) -> None:
        # Internal whitespace is a real parsing-bypass vector (leading/trailing
        # whitespace is legitimately trimmed); embedded spaces must be denied.
        policy = UrlPolicy(allowed_asset_ids=frozenset({"logo"}))
        decision = policy.evaluate(UrlRequest(url="topos-asset://lo go"))
        assert decision.decision.value == "deny"
        assert decision.reason == "url_contains_whitespace"


class TestCspPolicy:
    def test_default_deny_everything(self) -> None:
        header = CspPolicy().to_header()
        assert "default-src 'none'" in header
        assert "script-src 'none'" in header
        assert "connect-src 'none'" in header
        assert "frame-src 'none'" in header

    def test_inline_styles_allowed_for_css_animation(self) -> None:
        header = CspPolicy().to_header()
        assert "style-src 'unsafe-inline'" in header

    def test_assets_appear_only_when_bound(self) -> None:
        empty = CspPolicy().to_header()
        assert "topos-asset://" not in empty
        bound = CspPolicy(allowed_asset_ids=frozenset({"logo", "font"})).to_header()
        assert "topos-asset://font" in bound
        assert "topos-asset://logo" in bound

    def test_network_profile_adds_https_sources_without_opening_files(self) -> None:
        header = CspPolicy(allow_network_resources=True).to_header()
        assert "script-src https:" in header
        assert "style-src 'unsafe-inline' https:" in header
        assert "file" not in header

    def test_freeform_profile_allows_inline_author_scripts(self) -> None:
        header = CspPolicy(
            allow_network_resources=True,
            allow_inline_scripts=True,
        ).to_header()
        assert "script-src 'unsafe-inline' https:" in header
        assert "frame-src 'none'" in header
        assert "object-src 'none'" in header

    def test_freeform_profile_allows_visual_inline_sources_only(self) -> None:
        header = CspPolicy(
            allow_inline_scripts=True,
            allow_inline_visual_resources=True,
        ).to_header()
        assert "img-src data: blob:" in header
        assert "font-src data: blob:" in header
        assert "media-src data: blob:" in header
        assert "script-src 'unsafe-inline'" in header
        assert "worker-src 'none'" in header


class TestSecurityCorpus:
    """The full attack corpus must be denied by the runtime policy."""

    @pytest.mark.parametrize("fixture", ATTACK_FIXTURES, ids=[f.fixture_id for f in ATTACK_FIXTURES])
    def test_every_attack_is_denied(self, fixture) -> None:
        spike = HtmlRendererSpike(QtWebEngineGrabBackend())
        verdicts = spike.evaluate_security_corpus()
        verdict = next(v for v in verdicts if v.fixture_id == fixture.fixture_id)
        assert verdict.denied, (
            f"attack {fixture.fixture_id} (vector={fixture.vector}) was NOT denied "
            f"(reason={verdict.reason}); it must be blocked by the runtime sandbox"
        )

    def test_corpus_covers_all_vectors(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import attack_vectors
        vectors = attack_vectors()
        # Every documented abuse vector is represented in the corpus.
        for required in ("script", "network", "file", "data_uri", "frame", "popup", "storage"):
            assert required in vectors, f"corpus missing vector: {required}"

    def test_corpus_has_at_least_twenty_attacks(self) -> None:
        assert len(ATTACK_FIXTURES) >= 20

    def test_three_benign_fixtures_present(self) -> None:
        categories = {f.category for f in BENIGN_FIXTURES}
        assert "benign_static" in categories
        assert "benign_animated" in categories
        assert "benign_asset" in categories


class TestResourceBudget:
    def test_oversized_stylesheet_flagged(self) -> None:
        big_css = "a{color:red}" + " " * (ResourceBudget().max_stylesheet_chars + 1)
        violation, _ = check_stylesheet_budget(big_css, ResourceBudget())
        assert violation is not None
        assert violation.budget_field == "max_stylesheet_chars"

    def test_too_many_keyframes_flagged(self) -> None:
        css = "@keyframes x {} " * (ResourceBudget().max_keyframes + 1)
        violation, keyframes = check_stylesheet_budget(css, ResourceBudget())
        assert violation is not None
        assert violation.budget_field == "max_keyframes"
        assert keyframes == ResourceBudget().max_keyframes + 1

    def test_within_budget_passes(self) -> None:
        css = "@keyframes slide { from { left: 0 } to { left: 100px } }"
        violation, _ = check_stylesheet_budget(css, ResourceBudget())
        assert violation is None


class TestToposAssetRegistry:
    def test_resolve_bound_asset(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import ToposAssetBinding
        registry = ToposAssetRegistry(bindings={"logo": ToposAssetBinding("logo", "/tmp/logo.png", "image/png")})
        assert registry.resolve("logo") is not None
        assert registry.resolve("missing") is None
        assert registry.allowed_ids() == frozenset({"logo"})


class TestEnsureFontLockCsp:
    """The render-time CSP merge that lets the font lock's data: fonts load.

    The font lock inlines packaged fonts as base64 data: URIs, so the CSP's
    font-src must allow ``data:``; a layer CSP without it would block the
    @font-face and fall back to a system font (breaking the cross-machine
    determinism lock).  The renderer merges ``data:`` into font-src regardless
    of the authored CSP.
    """

    def test_adds_font_src_data_when_absent(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import (
            ensure_font_lock_csp,
        )
        csp = "default-src 'none'; style-src 'unsafe-inline'"
        out = ensure_font_lock_csp(csp)
        assert "font-src data:" in out
        # Authored directives survive.
        assert "default-src 'none'" in out
        assert "style-src 'unsafe-inline'" in out

    def test_merges_data_into_existing_font_src(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import (
            ensure_font_lock_csp,
        )
        csp = "default-src 'none'; font-src topos-asset://font/x"
        out = ensure_font_lock_csp(csp)
        # Authored source preserved, data: added; only one font-src directive.
        assert "topos-asset://font/x" in out
        assert "data:" in out
        assert out.lower().count("font-src") == 1

    def test_noop_when_data_already_allowed(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import (
            ensure_font_lock_csp,
        )
        csp = "default-src 'none'; font-src data: topos-asset://font/x"
        assert ensure_font_lock_csp(csp) == csp

    def test_adds_directive_when_no_font_src(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import (
            ensure_font_lock_csp,
        )
        out = ensure_font_lock_csp("default-src 'none'")
        assert out == "default-src 'none'; font-src data:"
