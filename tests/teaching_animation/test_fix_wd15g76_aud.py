"""Regression tests for remediation batch D (wd15-g76, teaching-render HTML domain).

Covers the Major findings fixed in this batch:

* AUD-30100  CSS animation names never interpolate a raw element_id
* AUD-30101  --topos-* token declarations are validated before entering CSS
* AUD-30120  VP8 ffmpeg stderr is drained (quiet flags + dedicated thread)
* AUD-30121  bounded enqueue fails fast when the VP8 writer thread is dead
* AUD-30122  recording-guard removal is verified, fail-closed
* AUD-30130  a failed replacement factory never escapes release()/finally
* AUD-30131  release() vs close() cannot leak a worker into the idle queue
* AUD-30203  security corpus consumes expected_denial_reason; no about:blank
             placeholders; capability vectors verified against the sandbox
* AUD-30205  _inject_csp_meta no longer matches <header>
* AUD-30206  non-representable dpr surfaces are rejected as INVALID_INPUT
* AUD-30207  virtual-clock jumps are reported and fail the capture
* AUD-30208  DOM diagnostics run only for frames classified uniform
* AUD-30212  Qt backend enforces the URL policy at the network layer
* AUD-30213  Qt backend seeks via the shared bridge and keeps authored <head>
* AUD-30214  Qt profile is off-the-record with a memory HTTP cache
* AUD-30218  backslash/control-char URLs denied; host parsed with urlsplit
* AUD-30300  resolve_bound_image_bytes swallows only degradation-class errors
"""

from __future__ import annotations

import inspect
import logging
import queue
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.features.teaching_animation.core.design_tokens import DesignTokenSet
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.render.html_native_overlay import (
    native_animation_css,
    native_overlay_css_variables,
)
from src.features.teaching_animation.render.html_realtime_recorder import (
    _drain_pipe_lines,
    _enqueue_with_writer_check,
    _remove_recording_guard,
    _vp8_encode_command,
)
from src.features.teaching_animation.render.html_renderer_service import (
    HtmlRendererService,
)
from src.features.teaching_animation.render.html_renderer_spike import (
    ATTACK_FIXTURES,
    CaptureEvidence,
    CaptureFailure,
    CaptureFailureCode,
    CaptureSurface,
    CapturedFrame,
    UrlPolicy,
    UrlRequest,
)
from src.features.teaching_animation.render.html_renderer_spike import (
    html_spike as spike_module,
)
from src.features.teaching_animation.render.html_renderer_spike.html_spike import (
    HtmlRendererSpike,
    _requests_for_attack,
    _sandbox_denial_for_vector,
)
from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
    PlaywrightChromiumBackend,
    _inject_csp_meta,
)
from src.features.teaching_animation.render.html_renderer_spike.security import (
    CspPolicy,
    ResourceDecision,
)
from src.features.teaching_animation.render.html_renderer_spike.qt_capture_backend import (
    _qt_request_allowed,
)
from src.features.teaching_animation.render.html_scene_renderer_v2 import (
    resolve_bound_image_bytes,
)
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _scene(*elements: SceneElement, duration: int = 24) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="wd15", schema_version=3, name="wd15", width=640, height=360,
        fps_num=24, fps_den=1, duration_frames=duration, background={"color": "#000000"},
        animations=(), elements=elements,
    )


def _animated_element(element_id: str) -> SceneElement:
    return SceneElement(
        element_id, "text", transform=SceneTransform(10, 10, 300, 60),
        content={"text": "hello"}, style={"font_size": 40.0},
        local_animation_tracks=(
            KeyframeTrack("opacity", (
                Keyframe(0, 0.0, InterpolationType.LINEAR),
                Keyframe(11, 1.0),
            )),
        ),
    )


class _FakeCaptureBackend:
    """Minimal pooled backend for service tests (no chromium)."""

    backend_id = "fake"
    backend_build = "fake/test"

    def __init__(self, *, fail_code: CaptureFailureCode | None = None) -> None:
        self._fail_code = fail_code
        self.closed = False

    def is_available(self) -> bool:
        return True

    def capture(self, html, surface, *, frame, fps_num, fps_den, csp, url_policy,
                font_set=(), timeout_ms=10000, cancel_token=None, transparent=False,
                allow_uniform=False):
        if self._fail_code is not None:
            return CaptureFailure(self._fail_code, "simulated", self.backend_id)
        data = bytes([(frame + 1) % 256]) * (surface.width * surface.height * 4)
        evidence = CaptureEvidence(self.backend_id, self.backend_build, "fake",
                                   (surface.width, surface.height), surface.dpr)
        return CapturedFrame(data=data, surface=surface, evidence=evidence)

    def close(self) -> None:
        self.closed = True


class _DummySpikeBackend:
    """evaluate_security_corpus never touches the backend; a stub suffices."""

    backend_id = "dummy"
    backend_build = "dummy/test"

    def is_available(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# AUD-30100 — animation names are CSS-safe regardless of element_id
# ---------------------------------------------------------------------------

class TestAud30100AnimationNameSafety:
    def test_hostile_element_id_cannot_inject_into_animation_names(self) -> None:
        hostile = 'evil"} *{animation:none}'
        css = native_animation_css(_scene(_animated_element(hostile)))
        names = re.findall(r"@keyframes ([^{\s]+)\{", css)
        assert names, "expected at least one keyframes rule"
        for name in names:
            assert re.fullmatch(r"[A-Za-z0-9_-]+", name), name
        # Match the exact shorthand the renderer emits, so id text that merely
        # appears inside the (escaped) selector attribute cannot masquerade as
        # an animation reference.
        refs = re.findall(r"animation:([A-Za-z0-9_-]+) [\d.]+s linear both paused", css)
        assert set(refs) == set(names)

    def test_colliding_ids_get_distinct_animation_names(self) -> None:
        # "a b" and "a_b" both sanitize to "a_b"; they must not share a name.
        css = native_animation_css(
            _scene(_animated_element("a b"), _animated_element("a_b")),
        )
        names = re.findall(r"@keyframes ([^{\s]+)\{", css)
        assert len(names) == 2
        assert len(set(names)) == 2
        refs = re.findall(r"animation:([A-Za-z0-9_-]+) [\d.]+s linear both paused", css)
        assert set(refs) == set(names)

    def test_clean_ids_keep_the_legacy_name_shape(self) -> None:
        css = native_animation_css(_scene(_animated_element("title")))
        assert "@keyframes topos-a-title-opacity{" in css
        assert re.search(r"animation:topos-a-title-opacity [\d.]+s linear both paused", css)


# ---------------------------------------------------------------------------
# AUD-30101 — token declarations are validated
# ---------------------------------------------------------------------------

class TestAud30101CssVariableValidation:
    def test_hostile_token_names_and_values_are_dropped(self) -> None:
        tokens = DesignTokenSet(
            palette={
                "accent": "#00ff88",
                "bad name}": "#123456",           # name escapes the ident rule
                "inject": "red} *{color:#fff}",   # value escapes the declaration
                "plain": "#abcdef",
            },
            spacing={"md": 16.0, "zap": 8.0},
        )
        css = native_overlay_css_variables(tokens)
        assert "--topos-color-accent:#00ff88" in css
        assert "--topos-color-plain:#abcdef" in css
        assert "--topos-spacing-md:16px" in css
        assert "bad" not in css
        assert "red}" not in css
        # No injected rule can follow the declaration block.
        assert " *{" not in css
        assert css.rstrip().endswith("}")

    def test_all_invalid_declarations_yield_empty_rule(self) -> None:
        tokens = DesignTokenSet(palette={"x": "javascript:alert(1)"})
        assert native_overlay_css_variables(tokens) == ""


# ---------------------------------------------------------------------------
# AUD-30120/30121/30122 — realtime recorder pipeline robustness
# ---------------------------------------------------------------------------

class TestAud30120StderrDrain:
    def test_vp8_command_suppresses_stats_and_limits_verbosity(self) -> None:
        cmd = _vp8_encode_command(
            "/usr/bin/ffmpeg", Path("/tmp/out.webm"),
            target_fps=24.0, bitrate_kbps=40_000, expected_frames=48,
        )
        assert "-nostats" in cmd
        assert cmd[cmd.index("-v") + 1] == "error"
        assert cmd[cmd.index("-frames:v") + 1] == "48"

    def test_drain_pipe_lines_consumes_until_eof(self) -> None:
        class _FakePipe:
            def __init__(self, lines: list[bytes]) -> None:
                self._lines = list(lines)

            def readline(self) -> bytes:
                return self._lines.pop(0) if self._lines else b""

        sink: list[bytes] = []
        _drain_pipe_lines(_FakePipe([b"a\n", b"b\n"]), sink)
        assert sink == [b"a\n", b"b\n"]
        _drain_pipe_lines(None, sink)  # no pipe — no-op
        assert sink == [b"a\n", b"b\n"]

    def test_drain_pipe_lines_survives_closed_stream(self) -> None:
        class _ClosedPipe:
            def readline(self) -> bytes:
                raise ValueError("closed file")

        sink: list[bytes] = []
        _drain_pipe_lines(_ClosedPipe(), sink)  # must not raise
        assert sink == []


class _FakePage:
    def __init__(self, *, result: object = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def evaluate(self, script: str) -> object:
        if self._error is not None:
            raise self._error
        return self._result


class TestAud30122RecordingGuardFailClosed:
    def test_guard_removed_returns_normally(self) -> None:
        _remove_recording_guard(_FakePage(result=False))  # re-read: node gone

    def test_guard_still_present_aborts(self) -> None:
        with pytest.raises(RuntimeError, match="still attached"):
            _remove_recording_guard(_FakePage(result=True))

    def test_unhealthy_page_aborts(self) -> None:
        with pytest.raises(RuntimeError, match="cannot verify removal"):
            _remove_recording_guard(_FakePage(error=RuntimeError("page crashed")))


class TestAud30121BoundedEnqueue:
    def test_dead_writer_fails_fast(self) -> None:
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        with pytest.raises(RuntimeError, match="stopped consuming"):
            _enqueue_with_writer_check(
                queue.Queue(maxsize=1), b"x",
                writer_thread=dead, writer_errors=[], timeout_s=1.0,
            )

    def test_writer_error_surfaces(self) -> None:
        alive = threading.Thread(target=lambda: time.sleep(1.0))
        alive.start()
        try:
            with pytest.raises(RuntimeError, match="writer failed"):
                _enqueue_with_writer_check(
                    queue.Queue(maxsize=1), b"x",
                    writer_thread=alive,
                    writer_errors=[BrokenPipeError("ffmpeg died")],
                    timeout_s=1.0,
                )
        finally:
            alive.join(timeout=2.0)

    def test_full_queue_times_out_with_live_writer(self) -> None:
        alive = threading.Thread(target=lambda: time.sleep(1.0))
        alive.start()
        try:
            full = queue.Queue(maxsize=1)
            full.put(b"frame")
            with pytest.raises(RuntimeError, match="timed out queueing"):
                _enqueue_with_writer_check(
                    full, b"x", writer_thread=alive, writer_errors=[],
                    timeout_s=0.3,
                )
        finally:
            alive.join(timeout=2.0)

    def test_enqueue_succeeds_when_consumer_drains(self) -> None:
        q: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
        stop = threading.Event()

        def consume() -> None:
            while not stop.is_set():
                try:
                    q.get(timeout=0.05)
                except queue.Empty:
                    continue

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        # A live (never-finishing) writer thread plus a draining consumer.
        writer = threading.Thread(target=lambda: time.sleep(2.0), daemon=True)
        writer.start()
        try:
            _enqueue_with_writer_check(
                q, None, writer_thread=writer, writer_errors=[], timeout_s=2.0,
            )
        finally:
            stop.set()
            writer.join(timeout=2.0)
            consumer.join(timeout=2.0)


# ---------------------------------------------------------------------------
# AUD-30130/30131 — pool lifecycle
# ---------------------------------------------------------------------------

class TestAud30130FactoryFailureInFinally:
    def test_failed_replacement_returns_failure_and_records_debt(self) -> None:
        calls = {"n": 0}

        def factory() -> _FakeCaptureBackend:
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeCaptureBackend(fail_code=CaptureFailureCode.CRASH)
            raise RuntimeError("browser launch failed")

        svc = HtmlRendererService(factory, pool_size=1)
        # The first worker crashes; the replacement factory then fails inside
        # release() — which runs in render_frame's finally and must not raise
        # or replace the structured CaptureFailure result.
        result = svc.render_frame(
            "<p>x</p>", CaptureSurface(4, 4, 1.0), frame=0,
            fps_num=24, fps_den=1, csp="default-src 'none'", url_policy=UrlPolicy(),
        )
        assert isinstance(result, CaptureFailure)
        assert result.code == CaptureFailureCode.CRASH
        assert svc.pending_rebuilds == 1
        assert isinstance(svc.last_factory_error, RuntimeError)
        svc.close()

    def test_debt_is_repaid_lazily_on_next_acquire(self) -> None:
        calls = {"n": 0}

        def factory() -> _FakeCaptureBackend:
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeCaptureBackend(fail_code=CaptureFailureCode.CRASH)
            if calls["n"] == 2:
                raise RuntimeError("browser launch failed")
            return _FakeCaptureBackend()

        svc = HtmlRendererService(factory, pool_size=1)
        svc.render_frame(
            "<p>x</p>", CaptureSurface(4, 4, 1.0), frame=0,
            fps_num=24, fps_den=1, csp="default-src 'none'", url_policy=UrlPolicy(),
        )
        assert svc.pending_rebuilds == 1
        rebuilt = svc.acquire(timeout=0.1)
        assert rebuilt is not None
        assert svc.pending_rebuilds == 0
        svc.release(rebuilt)
        svc.close()


class TestAud30131ReleaseCloseRace:
    def test_release_after_close_discards_instead_of_parking(self) -> None:
        svc = HtmlRendererService(lambda: _FakeCaptureBackend(), pool_size=1)
        worker = svc.acquire()
        svc.close()
        svc.release(worker)
        assert worker.closed is True
        assert svc._idle.qsize() == 0  # type: ignore[attr-defined]

    def test_concurrent_close_never_leaks_a_worker(self) -> None:
        created: list[_FakeCaptureBackend] = []
        guard = threading.Lock()

        def factory() -> _FakeCaptureBackend:
            backend = _FakeCaptureBackend()
            with guard:
                created.append(backend)
            return backend

        svc = HtmlRendererService(factory, pool_size=2)
        closer = threading.Thread(target=lambda: (time.sleep(0.02), svc.close()))
        closer.start()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not svc._closed:  # type: ignore[attr-defined]
            try:
                worker = svc.acquire(timeout=0.01)
            except (TimeoutError, RuntimeError):
                break
            svc.release(worker)
        closer.join(timeout=2.0)
        # close() drains the idle queue; every worker ever created must be
        # either handed out-and-closed or closed by close()/release().
        idle: list[object] = []
        while True:
            try:
                idle.append(svc._idle.get_nowait())  # type: ignore[attr-defined]
            except queue.Empty:
                break
        idle_ids = {id(w) for w in idle}
        leaked = [b for b in created if id(b) not in idle_ids and not b.closed]
        assert leaked == []


# ---------------------------------------------------------------------------
# AUD-30203 — security corpus verdicts are non-vacuous
# ---------------------------------------------------------------------------

class TestAud30203SecurityCorpus:
    def test_every_attack_is_still_denied(self) -> None:
        verdicts = HtmlRendererSpike(_DummySpikeBackend()).evaluate_security_corpus()
        by_id = {v.fixture_id: v for v in verdicts}
        assert len(verdicts) == len(ATTACK_FIXTURES)
        for fixture in ATTACK_FIXTURES:
            assert by_id[fixture.fixture_id].denied, (
                f"{fixture.fixture_id}: {by_id[fixture.fixture_id].reason}")

    def test_no_url_fixtures_have_no_fabricated_placeholder_request(self) -> None:
        for fixture in ATTACK_FIXTURES:
            requests = _requests_for_attack(fixture)
            assert all(r.url != "about:blank" for r in requests), fixture.fixture_id

    def test_capability_vectors_verify_the_sandbox_profile(self) -> None:
        verdicts = {
            v.fixture_id: v
            for v in HtmlRendererSpike(_DummySpikeBackend()).evaluate_security_corpus()
        }
        for fixture_id in (
            "attack_script_inline", "attack_event_handler", "attack_network_xhr",
            "attack_storage_persist", "attack_permission_api", "attack_webgl_canvas",
            "attack_css_expression",
        ):
            reason = verdicts[fixture_id].reason
            assert reason.startswith(("csp_", "disabled_feature:")), (
                f"{fixture_id}: {reason}")

    def test_url_reason_expectation_is_enforced(self) -> None:
        verdicts = {
            v.fixture_id: v
            for v in HtmlRendererSpike(_DummySpikeBackend()).evaluate_security_corpus()
        }
        # Fixtures documenting a URL-policy reason are denied with exactly it.
        assert verdicts["attack_script_external"].reason == "network_scheme_blocked"
        assert verdicts["attack_file_scheme_img"].reason == "file_scheme_blocked"
        assert verdicts["attack_unbound_asset"].reason == "asset_id_not_bound"

    def test_reason_mismatch_flips_the_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.teaching_animation.render.html_renderer_spike.fixtures import (
            AttackFixture,
        )
        fixture = AttackFixture(
            fixture_id="crafted_reason_mismatch",
            category="attack_script",
            vector="script",
            expected_denial_reason="file_scheme_blocked",  # will not match
            description="crafted: incidental denial must fail the verdict",
            html='<html><body><script src="https://evil.example/a.js"></script></body></html>',
        )
        monkeypatch.setattr(spike_module, "ATTACK_FIXTURES", (fixture,))
        verdict = HtmlRendererSpike(_DummySpikeBackend()).evaluate_security_corpus()[0]
        assert verdict.denied is False
        assert "expected_denial_reason" in verdict.reason

    def test_unmapped_capability_vector_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.teaching_animation.render.html_renderer_spike.fixtures import (
            AttackFixture,
        )
        fixture = AttackFixture(
            fixture_id="crafted_unknown_vector",
            category="attack_misc",
            vector="telepathy",
            expected_denial_reason="telepathy_blocked",
            description="crafted: no sandbox mapping",
            html="<html><body><script>x</script></body></html>",
        )
        monkeypatch.setattr(spike_module, "ATTACK_FIXTURES", (fixture,))
        verdict = HtmlRendererSpike(_DummySpikeBackend()).evaluate_security_corpus()[0]
        assert verdict.denied is False
        assert verdict.reason == "no_sandbox_mapping_for_vector"

    def test_sandbox_denial_for_vector_csp_branches(self) -> None:
        strict = CspPolicy().to_header()
        assert _sandbox_denial_for_vector("script", strict) == (True, "csp_script_src_none")
        assert _sandbox_denial_for_vector("frame", strict)[0] is True
        assert _sandbox_denial_for_vector("form", strict)[0] is True
        # A permissive CSP must not count as coverage.
        permissive = CspPolicy(allow_network_resources=True, allow_inline_scripts=True)
        assert _sandbox_denial_for_vector("script", permissive.to_header())[0] is False


# ---------------------------------------------------------------------------
# AUD-30205 — CSP meta injection point
# ---------------------------------------------------------------------------

class TestAud30205CspMetaInjection:
    def test_header_tag_is_not_mistaken_for_head(self) -> None:
        document = "<html><body><header>h</header></body></html>"
        out = _inject_csp_meta(document, "default-src 'none'")
        assert "<head>" in out
        assert out.index("<head>") < out.index("<header>")
        assert out.index("<head>") < out.index('<meta http-equiv="Content-Security-Policy"')

    def test_real_head_still_receives_the_meta_first(self) -> None:
        document = "<html><head lang=\"en\"><title>t</title></head><body><header>x</header></body></html>"
        out = _inject_csp_meta(document, "default-src 'none'")
        meta_at = out.index('<meta http-equiv="Content-Security-Policy"')
        assert out.index("<head ") < meta_at < out.index("<title>")
        # The <header> in body must not have attracted the injection.
        assert meta_at < out.index("<header>")


# ---------------------------------------------------------------------------
# AUD-30206 — dpr/surface representability + CSS-pixel clip
# ---------------------------------------------------------------------------

class TestAud30206ClipUnits:
    def test_non_representable_surface_rejected_before_launch(self) -> None:
        backend = PlaywrightChromiumBackend()
        result = backend.capture(
            "<p>x</p>", CaptureSurface(1081, 721, 2.0),  # odd px at dpr 2
            frame=0, fps_num=24, fps_den=1,
            csp="default-src 'none'", url_policy=UrlPolicy(),
        )
        assert isinstance(result, CaptureFailure)
        assert result.code == CaptureFailureCode.INVALID_INPUT
        assert "not representable" in result.message

    def test_capture_uses_css_pixel_clip(self) -> None:
        source = inspect.getsource(PlaywrightChromiumBackend.capture)
        assert '"width": css_w' in source and '"height": css_h' in source
        assert '"width": surface.width' not in source


# ---------------------------------------------------------------------------
# AUD-30207 — virtual clock jump is reported and fails the capture
# ---------------------------------------------------------------------------

class TestAud30207ClockJump:
    def test_pump_reports_jump(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
            _CLOCK_SHIM,
        )
        assert "jumped = true" in _CLOCK_SHIM
        assert "jumped: jumped" in _CLOCK_SHIM

    def test_jumped_clock_fails_the_capture_structurally(self) -> None:
        source = inspect.getsource(PlaywrightChromiumBackend.capture)
        assert "virtual clock budget exhausted" in source
        assert "jumped" in source


# ---------------------------------------------------------------------------
# AUD-30208 — DOM diagnostics only for uniform frames
# ---------------------------------------------------------------------------

class TestAud30208DiagnosticsOrdering:
    def test_uniform_classification_precedes_dom_diagnostics(self) -> None:
        source = inspect.getsource(PlaywrightChromiumBackend.capture)
        assert source.index("is_uniform_frame") < source.index("_DOM_DIAGNOSTICS")
        # The diagnostics evaluate is guarded by the uniform classification.
        assert source.index("if uniform:") < source.index("page.evaluate(_DOM_DIAGNOSTICS)")


# ---------------------------------------------------------------------------
# AUD-30212/30213/30214 — Qt WebEngine backend contract
# ---------------------------------------------------------------------------

class TestAud30212QtUrlPolicy:
    def test_page_origin_navigation_is_allowed(self) -> None:
        assert _qt_request_allowed(
            UrlPolicy(), "about:blank", "navigation", is_page_origin=True,
        ) is True

    def test_authored_about_blank_is_denied(self) -> None:
        assert _qt_request_allowed(
            UrlPolicy(), "about:blank", "navigation", is_page_origin=False,
        ) is False

    def test_network_denied_in_strict_policy(self) -> None:
        assert _qt_request_allowed(
            UrlPolicy(), "https://evil.example/a.js", "script", is_page_origin=False,
        ) is False

    def test_bound_asset_allowed_unbound_denied(self) -> None:
        policy = UrlPolicy(frozenset({"logo"}))
        assert _qt_request_allowed(
            policy, "topos-asset://logo", "image", is_page_origin=False,
        ) is True
        assert _qt_request_allowed(
            policy, "topos-asset://other", "image", is_page_origin=False,
        ) is False

    def test_interceptor_is_wired_into_the_profile(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import (
            qt_capture_backend,
        )
        source = inspect.getsource(qt_capture_backend)
        assert "setUrlRequestInterceptor" in source
        assert "info.block(True)" in source
        assert "QWebEngineUrlRequestInterceptor" in source


class TestAud30213QtSeekAndHead:
    def test_body_extraction_shell_is_gone(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import (
            qt_capture_backend,
        )
        assert not hasattr(qt_capture_backend, "_extract_body")

    def test_backend_uses_shared_csp_injection_and_seek_bridge(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import (
            qt_capture_backend,
        )
        source = inspect.getsource(qt_capture_backend)
        assert "_inject_csp_meta(html, csp)" in source
        assert "_SEEK_BRIDGE" in source
        assert "page.runJavaScript" in source


class TestAud30214QtOffTheRecordProfile:
    def test_profile_is_off_the_record_with_memory_cache(self) -> None:
        from src.features.teaching_animation.render.html_renderer_spike import (
            qt_capture_backend,
        )
        source = inspect.getsource(qt_capture_backend)
        assert 'QWebEngineProfile("topos-spike-ephemeral")' not in source
        assert "QWebEngineProfile()" in source
        assert "MemoryHttpCache" in source


# ---------------------------------------------------------------------------
# AUD-30218 — host allow-list parsing hardening
# ---------------------------------------------------------------------------

class TestAud30218HostParsing:
    def _network_policy(self) -> UrlPolicy:
        return UrlPolicy(
            frozenset(),
            allow_network_resources=True,
            allowed_network_hosts=frozenset({"allowed.com"}),
        )

    def test_backslash_confusion_is_denied(self) -> None:
        decision = self._network_policy().evaluate(
            UrlRequest(url="https://evil.example\\@allowed.com/x", kind="resource"))
        assert decision.decision is ResourceDecision.DENY
        assert decision.reason == "url_contains_backslash_or_control"

    def test_control_characters_are_denied(self) -> None:
        for url in ("https://allowed.com/a\x00b", "https://allowed.com/a\nb",
                    "https://allowed.com/a\rb", "https://allowed.com/a\x7fb"):
            decision = self._network_policy().evaluate(UrlRequest(url=url, kind="resource"))
            assert decision.decision is ResourceDecision.DENY, url
            assert decision.reason == "url_contains_backslash_or_control", url

    def test_userinfo_port_and_case_are_parsed_like_a_browser(self) -> None:
        decision = self._network_policy().evaluate(
            UrlRequest(url="https://user:pw@ALLOWED.com.:8443/x", kind="resource"))
        assert decision.decision is ResourceDecision.ALLOW
        assert decision.reason == "https_network_resource"

    def test_trailing_dot_subdomain_not_in_allowlist_is_denied(self) -> None:
        decision = self._network_policy().evaluate(
            UrlRequest(url="https://allowed.com.evil.example/x", kind="resource"))
        assert decision.decision is ResourceDecision.DENY
        assert decision.reason == "network_host_not_allowed"

    def test_missing_or_malformed_host_is_denied(self) -> None:
        for url in ("https:///x", "https://[bad/x"):
            decision = self._network_policy().evaluate(UrlRequest(url=url, kind="resource"))
            assert decision.decision is ResourceDecision.DENY, url
            assert decision.reason == "network_host_missing", url

    def test_strict_policy_still_blocks_network_entirely(self) -> None:
        decision = UrlPolicy().evaluate(
            UrlRequest(url="https://allowed.com/x", kind="resource"))
        assert decision.decision is ResourceDecision.DENY
        assert decision.reason == "network_scheme_blocked"


# ---------------------------------------------------------------------------
# AUD-30300 — bounded degradation in bound-image resolution
# ---------------------------------------------------------------------------

class _DegradedResolver:
    def __init__(self, context: object) -> None:
        pass

    def preflight(self, scene: object) -> None:
        raise ValueError("strict preflight: missing asset")


class _UnexpectedResolver:
    def __init__(self, context: object) -> None:
        pass

    def preflight(self, scene: object) -> None:
        raise RuntimeError("api contract changed")


class TestAud30300BoundedDegradation:
    def _scene_and_context(self) -> tuple[SimpleNamespace, SimpleNamespace]:
        scene = SimpleNamespace(html_layer=SimpleNamespace(bound_asset_ids=("img1",)))
        context = SimpleNamespace(asset_root=Path("/tmp"))
        return scene, context

    def test_degradation_class_errors_return_empty_with_warning(
            self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        from src.features.teaching_animation.render import asset_resolver
        monkeypatch.setattr(asset_resolver, "AssetResolver", _DegradedResolver)
        scene, context = self._scene_and_context()
        with caplog.at_level(logging.WARNING):
            out = resolve_bound_image_bytes(scene, context)
        assert out == {}
        assert any("degraded" in record.message for record in caplog.records)

    def test_unexpected_errors_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.teaching_animation.render import asset_resolver
        monkeypatch.setattr(asset_resolver, "AssetResolver", _UnexpectedResolver)
        scene, context = self._scene_and_context()
        with pytest.raises(RuntimeError, match="api contract changed"):
            resolve_bound_image_bytes(scene, context)
