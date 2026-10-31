"""Regression tests for remediation batch D (wd14-g75, AUD-298xx…AUD-301xx).

Covers the Major findings fixed in the teaching-render domain:

* AUD-29805  benchmark GPU-effect setup must never leak GL objects
* AUD-29808  ffprobe invocations need a timeout and must degrade to None
* AUD-29810  asset file hashing is memoized by (path, size, mtime_ns)
* AUD-29903  non-numeric element style ``opacity`` degrades to 1.0
* AUD-29904  ``style: null`` degrades to "no style" instead of TypeError
* AUD-29908  QApplication lazy init is serialized across worker threads
* AUD-30001  measure_dom fails closed when the DOM walk aborts
* AUD-30002  data-URI budget compares the largest single URI, not the sum
* AUD-30005  replace_text is forbidden on token_only nodes
* AUD-30006  set_animation_timing duration_ms is validated (structured error)
* AUD-30009  timing edits rewrite only the duration component of the shorthand
* AUD-30010  post-patch source_hash keeps the authored (pre-sanitize) semantics
* AUD-30011  set_slot_attr locates slots via the tag scanner, not string find
* AUD-30014  CSS custom properties cannot smuggle a url() past the whitelist
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import types
from dataclasses import replace

import pytest

from src.features.teaching_animation.core.contracts import RenderedFrame
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render.evaluator import evaluate
from src.features.teaching_animation.render.html_layer.html_patch import (
    DeclaredNode,
    HtmlLayerState,
    HtmlPatch,
    HtmlPatchError,
    HtmlPatchErrorCode,
    HtmlPatchOperation,
    validate_patch,
)
from src.features.teaching_animation.render.html_layer.html_patch_apply import (
    _op_set_slot_attr,
    apply_html_patch,
)
from src.features.teaching_animation.render.html_layer.sanitizer import HtmlSanitizer


# --------------------------------------------------------------------------- #
# AUD-29805: GL resource ownership in _measure_gpu_effect
# --------------------------------------------------------------------------- #

def _fake_moderngl(monkeypatch: pytest.MonkeyPatch, ctx: object) -> None:
    fake = types.ModuleType("moderngl")
    fake.create_standalone_context = lambda: ctx  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "moderngl", fake)


def test_gpu_effect_releases_context_when_texture_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.render import benchmark

    released: list[str] = []
    ctx = types.SimpleNamespace(
        texture=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("texture boom")),
        release=lambda: released.append("context"),
    )
    _fake_moderngl(monkeypatch, ctx)
    segment = benchmark._measure_gpu_effect(None, iterations=1)  # type: ignore[arg-type]
    assert segment.status == "skipped"
    assert "texture boom" in segment.reason
    assert released == ["context"]


def test_gpu_effect_releases_texture_and_context_when_bridge_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.render import benchmark

    released: list[str] = []
    texture = types.SimpleNamespace(
        glo=1, write=lambda data: None, release=lambda: released.append("texture"))
    ctx = types.SimpleNamespace(
        texture=lambda *a, **k: texture,
        release=lambda: released.append("context"),
    )
    _fake_moderngl(monkeypatch, ctx)
    monkeypatch.setattr(
        benchmark, "CompositeEffectBridge",
        lambda gl: (_ for _ in ()).throw(RuntimeError("bridge boom")))
    segment = benchmark._measure_gpu_effect(None, iterations=1)  # type: ignore[arg-type]
    assert segment.status == "skipped"
    assert "bridge boom" in segment.reason
    assert sorted(released) == ["context", "texture"]


def test_gpu_effect_success_path_cleans_everything_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.render import benchmark

    released: list[str] = []
    texture = types.SimpleNamespace(
        glo=1, write=lambda data: None, release=lambda: released.append("texture"))
    ctx = types.SimpleNamespace(
        texture=lambda *a, **k: texture,
        release=lambda: released.append("context"),
    )
    _fake_moderngl(monkeypatch, ctx)

    class _FakeBridge:
        def __init__(self, gl: object) -> None:
            pass

        def apply(self, *a: object, **k: object) -> object:
            return types.SimpleNamespace(texture_id=1)

        def cleanup(self) -> None:
            released.append("bridge")

    monkeypatch.setattr(benchmark, "CompositeEffectBridge", _FakeBridge)
    segment = benchmark._measure_gpu_effect(None, iterations=2)  # type: ignore[arg-type]
    assert segment.status == "passed"
    assert len(segment.values) == 2
    assert released == ["bridge", "texture", "context"]


# --------------------------------------------------------------------------- #
# AUD-29808: ffprobe timeout
# --------------------------------------------------------------------------- #

def test_probe_video_metrics_times_out_to_none(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from src.features.teaching_animation.render import bitrate_probe as bp

    video = tmp_path / "v.mp4"  # type: ignore[operator]
    video.write_bytes(b"not really a video")

    def fake_run(cmd: list, **kwargs: object) -> object:
        assert kwargs.get("timeout") is not None, "ffprobe must run with a timeout"
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(bp, "_ffprobe_binary", lambda: "/fake/ffprobe")
    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    assert bp.probe_video_metrics(video) is None


def test_probe_packet_count_timeout_degrades_to_zero_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    from src.features.teaching_animation.render import bitrate_probe as bp

    video = tmp_path / "v.webm"  # type: ignore[operator]
    video.write_bytes(b"webm-ish")
    payload = {
        "streams": [{
            "codec_name": "vp9", "pix_fmt": "yuv420p", "width": "64", "height": "48",
            "r_frame_rate": "24/1", "nb_frames": "0", "bit_rate": "8000",
        }],
        "format": {"duration": "1.0", "size": "1000", "bit_rate": "8000"},
    }

    def fake_run(cmd: list, **kwargs: object) -> object:
        assert kwargs.get("timeout") is not None, "ffprobe must run with a timeout"
        if "-count_packets" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
        return types.SimpleNamespace(stdout=json.dumps(payload))

    monkeypatch.setattr(bp, "_ffprobe_binary", lambda: "/fake/ffprobe")
    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    metrics = bp.probe_video_metrics(video)
    assert metrics is not None
    assert metrics.nb_frames == 0  # degraded, not raised, not hung


# --------------------------------------------------------------------------- #
# AUD-29810: memoized file hashing
# --------------------------------------------------------------------------- #

def test_file_hash_memo_hit_does_not_rehash(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.render import cache as render_cache

    render_cache._file_hash_memo.clear()
    path = tmp_path / "a.bin"  # type: ignore[operator]
    path.write_bytes(b"hello")
    first = render_cache._file_hash(path)
    assert first == hashlib.sha256(b"hello").hexdigest()

    def _no_rehash(*a: object, **k: object) -> None:
        raise AssertionError("memoized file must not be re-hashed")

    monkeypatch.setattr(render_cache.hashlib, "sha256", _no_rehash)
    assert render_cache._file_hash(path) == first
    render_cache._file_hash_memo.clear()


def test_file_hash_rehashes_when_stat_identity_changes(tmp_path: object) -> None:
    from src.features.teaching_animation.render import cache as render_cache

    render_cache._file_hash_memo.clear()
    path = tmp_path / "b.bin"  # type: ignore[operator]
    path.write_bytes(b"one")
    first = render_cache._file_hash(path)
    # Different size (and mtime) → different stat key → fresh digest.
    path.write_bytes(b"two-different-content")
    second = render_cache._file_hash(path)
    assert first != second
    assert second == hashlib.sha256(b"two-different-content").hexdigest()
    render_cache._file_hash_memo.clear()


# --------------------------------------------------------------------------- #
# AUD-29903 / AUD-29904: dirty style data must not crash evaluate()
# --------------------------------------------------------------------------- #

def _scene_with_style(style: object) -> TeachingSceneDocument:
    doc = TeachingSceneDocument.create_default("s", width=64, height=48, duration_frames=6)
    dirty = replace(doc.elements[0], style=style)
    return replace(doc, elements=(dirty,) + tuple(doc.elements[1:]))


@pytest.mark.parametrize("style", [{"opacity": None}, {"opacity": "50%"}, {"opacity": [1]}])
def test_non_numeric_opacity_degrades_to_one(style: object) -> None:
    result = evaluate(_scene_with_style(style), 0)
    assert result.elements, "the dirty element must still evaluate"
    assert result.elements[0].opacity == 1.0


def test_style_null_degrades_to_empty_style() -> None:
    result = evaluate(_scene_with_style(None), 0)
    assert result.elements
    assert result.elements[0].opacity == 1.0
    assert isinstance(result.elements[0].style, dict)


def test_valid_opacity_still_applies() -> None:
    result = evaluate(_scene_with_style({"opacity": 0.25}), 0)
    assert result.elements[0].opacity == 0.25


# --------------------------------------------------------------------------- #
# AUD-29908: QApplication lazy init is lock-serialized
# --------------------------------------------------------------------------- #

_MINIMAL_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
    b'<rect width="8" height="8" fill="#ff0000"/></svg>'
)


def test_qt_application_creation_is_serialized_across_threads() -> None:
    pytest.importorskip("PyQt6.QtSvg")
    from PyQt6.QtWidgets import QApplication

    from src.features.teaching_animation.render import frame_adapter as fa

    assert isinstance(fa._QT_APPLICATION_LOCK, type(threading.Lock()))
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            rendered = RenderedFrame(
                frame=0, width=8, height=8, pixel_format="svg", data=_MINIMAL_SVG)
            frame = fa.FrameAdapter.from_rendered_frame(rendered)
            assert (frame.width, frame.height) == (8, 8)
        except Exception as exc:  # collect for the main-thread assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    # Exactly one QApplication exists and the module cached it.
    assert QApplication.instance() is not None
    assert fa._QT_APPLICATION is QApplication.instance()


# --------------------------------------------------------------------------- #
# AUD-30001 / AUD-30002: budget measurement
# --------------------------------------------------------------------------- #

def test_measure_dom_fails_closed_when_parser_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.render.html_layer import budget

    def _boom(self: object, html: str) -> None:
        raise ValueError("parser boom")

    monkeypatch.setattr(budget._DomCounter, "feed", _boom)
    report = budget.enforce_budget("<div><span>hi</span></div>")
    assert report.stats.count_complete is False
    assert "parser boom" in report.stats.count_error
    assert not report.ok, "a partially counted page must never read as within budget"
    assert any(v.budget_field == "dom_count_incomplete" for v in report.exceeded)


def test_data_uri_budget_compares_largest_single_uri() -> None:
    from src.features.teaching_animation.render.html_layer import budget

    # 40 × ~1KB data URIs: every individual payload is legal; the old sum
    # (≈40KB) tripped the 32KB per-item limit and rejected the page.
    many_small = "<div>" + "".join(
        f'<img src="data:image/png;base64,{"A" * 1000}">' for _ in range(40)) + "</div>"
    report = budget.enforce_budget(many_small)
    assert not any(v.budget_field == "max_data_uri_bytes" for v in report.exceeded)

    # One oversized payload must still be rejected.
    one_big = f'<img src="data:image/png;base64,{"A" * 40000}">'
    report2 = budget.enforce_budget(one_big)
    assert any(v.budget_field == "max_data_uri_bytes" for v in report2.exceeded)
    assert report2.stats.data_uri_bytes > 32000


# --------------------------------------------------------------------------- #
# AUD-30005 / AUD-30006 / AUD-30009 / AUD-30010 / AUD-30011: patch contract
# --------------------------------------------------------------------------- #

def _layer(html: str) -> object:
    return build_html_layer(
        layer_id="L", sanitized_html=html, authored_source=html,
        viewport_width=64, viewport_height=48, fps_num=24, fps_den=1, duration_frames=6,
    )


def _patch(op: HtmlPatchOperation, target: str, value: object, layer: object,
           sub_target: str = "") -> HtmlPatch:
    return HtmlPatch(op, target, value, layer.revision, layer.source_hash, sub_target)  # type: ignore[attr-defined]


def test_replace_text_forbidden_on_token_only_node_validate() -> None:
    state = HtmlLayerState(
        revision=1, source_hash="h",
        declared_nodes={"t": DeclaredNode(node_id="t", edit_mode="token_only")})
    patch = HtmlPatch(HtmlPatchOperation.REPLACE_TEXT, "t", "hacked", 1, "h")
    with pytest.raises(HtmlPatchError) as exc:
        validate_patch(patch, state)
    assert exc.value.code == HtmlPatchErrorCode.EDIT_MODE_FORBIDS


def test_replace_text_forbidden_on_token_only_node_apply() -> None:
    layer = _layer('<div data-topos-node-id="t" data-topos-edit-mode="token_only">secret</div>')
    with pytest.raises(HtmlPatchError) as exc:
        apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "t", "hacked", layer))
    assert exc.value.code == HtmlPatchErrorCode.EDIT_MODE_FORBIDS


def test_replace_text_still_allowed_for_content_nodes() -> None:
    layer = _layer('<div data-topos-node-id="t" data-topos-edit-mode="content">old</div>')
    new = apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "t", "new", layer))
    assert "new" in new.sanitized_html


@pytest.mark.parametrize("bad", ["abc", [1], -5, 1.5, True, None])
def test_set_animation_timing_rejects_invalid_duration(bad: object) -> None:
    layer = _layer(
        '<style>[data-topos-node-id="t"]{animation:slide 2s ease-in infinite}</style>'
        '<div data-topos-node-id="t">x</div>')
    for value in (bad, {"duration_ms": bad}):
        patch = _patch(HtmlPatchOperation.SET_ANIMATION_TIMING, "t", value, layer)
        with pytest.raises(HtmlPatchError) as exc:
            apply_html_patch(layer, patch)
        assert exc.value.code == HtmlPatchErrorCode.ILLEGAL_VALUE


def test_set_animation_timing_shorthand_keeps_name_easing_iteration() -> None:
    layer = _layer(
        '<style>[data-topos-node-id="t"]{animation:slide 2s ease-in infinite}</style>'
        '<div data-topos-node-id="t">x</div>')
    new = apply_html_patch(
        layer, _patch(HtmlPatchOperation.SET_ANIMATION_TIMING, "t", {"duration_ms": 500}, layer))
    css = new.sanitized_html
    assert "slide" in css, "animation name must survive a timing edit"
    assert "ease-in" in css, "easing must survive a timing edit"
    assert "infinite" in css, "iteration count must survive a timing edit"
    assert "0.5s" in css
    assert "2s" not in css, "old duration must be gone"


def test_set_animation_timing_shorthand_preserves_delay() -> None:
    layer = _layer(
        '<style>[data-topos-node-id="t"]{animation:slide 2s 1s linear infinite}</style>'
        '<div data-topos-node-id="t">x</div>')
    new = apply_html_patch(
        layer, _patch(HtmlPatchOperation.SET_ANIMATION_TIMING, "t", {"duration_ms": 500}, layer))
    assert "0.5s 1s" in new.sanitized_html.replace("slide ", "")


def test_set_animation_timing_longhand_replaces_whole_value() -> None:
    layer = _layer(
        '<style>[data-topos-node-id="t"]{animation-duration:2s}</style>'
        '<div data-topos-node-id="t">x</div>')
    new = apply_html_patch(
        layer, _patch(HtmlPatchOperation.SET_ANIMATION_TIMING, "t", {"duration_ms": 250}, layer))
    assert "animation-duration:0.25s" in new.sanitized_html.replace(" ", "")


def test_set_animation_timing_inline_style_fallback_keeps_shorthand() -> None:
    layer = _layer('<div data-topos-node-id="t" style="animation:slide 2s linear">x</div>')
    new = apply_html_patch(
        layer, _patch(HtmlPatchOperation.SET_ANIMATION_TIMING, "t", {"duration_ms": 500}, layer))
    css = new.sanitized_html
    assert "slide" in css
    assert "0.5s" in css
    assert "2s" not in css


def test_patch_result_keeps_authored_source_hash_semantics() -> None:
    html = '<div data-topos-node-id="t">Hello</div>'
    layer = _layer(html)
    new = apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "t", "World", layer))
    # source_hash: the authored (pre-sanitization) text the patch produced.
    expected_authored = '<div data-topos-node-id="t">World</div>'
    assert new.source_hash == hashlib.sha256(expected_authored.encode("utf-8")).hexdigest()
    # sanitizer_hash: the sanitized text actually stored.
    assert new.sanitizer_hash == hashlib.sha256(
        new.sanitized_html.encode("utf-8")).hexdigest()


def test_patch_chain_still_validates_against_new_revision() -> None:
    layer = _layer('<style>:root{--brand:#06c}</style><div data-topos-node-id="t">Hello</div>')
    first = apply_html_patch(layer, _patch(HtmlPatchOperation.REPLACE_TEXT, "t", "World", layer))
    # A second patch authored against the evolved layer applies cleanly.
    second = apply_html_patch(
        first, _patch(HtmlPatchOperation.SET_TOKEN, "brand", "#ff0000", first))
    assert "--brand" in second.sanitized_html
    assert second.revision == first.revision + 1


def test_set_slot_attr_ignores_css_selector_and_hits_real_element() -> None:
    html = ('<style>[data-topos-editable="cap"]{color:#ffffff}</style>'
            '<div data-topos-editable="cap">T</div>')
    patch = HtmlPatch(HtmlPatchOperation.SET_SLOT_ATTR, "cap", "nid", 1, "h", "data-topos-node-id")
    out = _op_set_slot_attr(html, patch, None)  # type: ignore[arg-type]
    assert out.startswith('<style>[data-topos-editable="cap"]{color:#ffffff}</style>')
    assert '<div data-topos-editable="cap" data-topos-node-id="nid">' in out


def test_set_slot_attr_updates_every_element_carrying_the_slot() -> None:
    html = ('<div data-topos-editable="cap" data-k="0">A</div>'
            '<span data-topos-editable="cap" data-k="0">B</span>')
    patch = HtmlPatch(HtmlPatchOperation.SET_SLOT_ATTR, "cap", "9", 1, "h", "data-k")
    out = _op_set_slot_attr(html, patch, None)  # type: ignore[arg-type]
    assert out.count('data-k="9"') == 2


def test_set_slot_attr_unknown_slot_is_structured_error() -> None:
    patch = HtmlPatch(HtmlPatchOperation.SET_SLOT_ATTR, "ghost", "v", 1, "h", "data-k")
    with pytest.raises(HtmlPatchError) as exc:
        _op_set_slot_attr("<div data-topos-editable=\"cap\">x</div>", patch, None)  # type: ignore[arg-type]
    assert exc.value.code == HtmlPatchErrorCode.UNKNOWN_SLOT


def test_set_slot_attr_apply_moves_real_element_not_stylesheet() -> None:
    html = ('<style>[data-topos-editable="cap"]{color:#ffffff}</style>'
            '<div data-topos-editable="cap">T</div>')
    layer = _layer(html)
    new = apply_html_patch(
        layer, _patch(HtmlPatchOperation.SET_SLOT_ATTR, "cap", "nid", layer, "data-topos-node-id"))
    assert 'data-topos-node-id="nid"' in new.sanitized_html
    assert '[data-topos-editable="cap"]{color:#ffffff}' in new.sanitized_html.replace(" ", "")


# --------------------------------------------------------------------------- #
# AUD-30014: custom properties cannot bypass the url() policy
# --------------------------------------------------------------------------- #

def test_custom_property_remote_url_is_stripped() -> None:
    result = HtmlSanitizer().sanitize(
        '<style>:root{--evil:url(https://evil.example/x);--ok:#ffffff}</style><div>x</div>')
    assert "--evil" not in result.html
    assert "--ok" in result.html.replace(" ", "")


def test_custom_property_topos_asset_url_is_kept() -> None:
    result = HtmlSanitizer().sanitize(
        '<style>:root{--bg:url(topos-asset://pic)}</style><div>x</div>')
    assert "--bg" in result.html.replace(" ", "")
    assert "topos-asset://pic" in result.html


def test_custom_property_network_url_respects_profile() -> None:
    result = HtmlSanitizer(allow_network_resources=True).sanitize(
        '<style>:root{--bg:url(https://cdn.example/x.png)}</style><div>x</div>')
    assert "--bg" in result.html.replace(" ", "")
    assert "https://cdn.example/x.png" in result.html


def test_regular_property_url_policy_unchanged() -> None:
    result = HtmlSanitizer().sanitize(
        '<style>.a{background:url(https://evil.example/x)}</style><div>x</div>')
    assert "evil.example" not in result.html
