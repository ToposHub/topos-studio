"""Batch-D fixes (wd14-g73) — regression tests.

AUD-27901: ``_write_video`` drains FFmpeg stderr concurrently with the stdin
frame writes, so a stats-heavy encode can no longer deadlock both processes.

AUD-27910: ``export_scene_document`` maps ``HtmlRenderError`` to a structured
``FacadeResult`` instead of letting the raw RuntimeError escape the facade.

AUD-27911 / AUD-27920: ``prepare()`` lives inside the try/finally, so a
partial renderer allocation (browser session) is always cleaned up.

AUD-28002: scene-edit confirmation refuses stale proposals via the store
revision captured when the plan was parsed.

AUD-28003: text "确认" routes html_generate pending requests to the html
confirm handler, and unknown requests never clear a preview card.

AUD-28004: a build-failed plan is retired when the user re-describes the
requirement instead of swallowing the request or treating it as a revision
of the failed document.

AUD-28100/28101: elastic easing curves follow the Penner formulas and are
continuous at 0 / 0.5 / 1.

AUD-28102: ``_mix_colors`` clamps channels, so overshooting easings always
produce a parseable hex colour.

AUD-28103: invalid extrapolate modes raise ``ValueError``.

AUD-28107: integer ``**``/``pow`` cannot blow past the complexity budget.

AUD-28108: missing marker keys surface as ``ExpressionError``.

AUD-28111: ``format_timecode`` is exact for rational frame rates.

AUD-28113: ``motion_semantics`` records the spring config the compiler
actually used for partial dict ``spring_config`` params.
"""

from __future__ import annotations

import math
import os
import stat
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.features.teaching_animation.core.contracts import (
    RenderContext,
    RenderedFrame,
)
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.export_service import (
    ExportOptions,
    TeachingAnimationExportService,
)
from src.features.teaching_animation.facade import export_scene_document
from src.features.teaching_animation.host_adapters import TimelineSceneFrameProvider
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.motion.beat import format_timecode
from src.features.teaching_animation.motion.easing import (
    elastic_in,
    elastic_in_out,
    elastic_out,
)
from src.features.teaching_animation.motion.expression import (
    ExpressionContext,
    ExpressionError,
    evaluate_expression,
)
from src.features.teaching_animation.motion.interpolate import (
    _mix_colors,
    _parse_color,
    interpolate,
)
from src.features.teaching_animation.motion.motion_commands import motion_semantics
from src.features.teaching_animation.render.render_worker import RenderResult
from src.features.teaching_animation.scene_store import get_shared_scene_store


# --------------------------------------------------------------------- #
# AUD-28100 / AUD-28101 — Penner elastic curves
# --------------------------------------------------------------------- #


class TestElasticCurves:
    def test_elastic_in_continuous_at_start(self):
        # The old formula dropped to ≈-0.993 immediately after t=0.
        assert elastic_in(0.0) == 0.0
        assert abs(elastic_in(0.001)) < 0.01

    def test_elastic_in_continuous_at_end(self):
        # The old formula sat at ≈0.0005 before the t>=1 guard jumped to 1.
        assert abs(elastic_in(1.0 - 1e-9) - 1.0) < 1e-6
        assert elastic_in(1.0) == 1.0

    def test_elastic_in_mirrors_elastic_out(self):
        for t in (0.05, 0.2, 0.3, 0.5, 0.7, 0.95):
            assert elastic_in(t) == pytest.approx(1.0 - elastic_out(1.0 - t), abs=1e-12)

    def test_elastic_in_dip_is_small(self):
        # Penner easeInElastic undershoots by only ~10%, never ≈-1.
        assert min(elastic_in(i / 1000) for i in range(1001)) > -0.5

    def test_elastic_in_out_continuous_at_midpoint(self):
        left = elastic_in_out(0.5 - 1e-9)
        right = elastic_in_out(0.5 + 1e-9)
        assert left == pytest.approx(0.5, abs=1e-6)
        assert right == pytest.approx(0.5, abs=1e-6)

    def test_elastic_in_out_continuous_at_endpoints(self):
        assert elastic_in_out(0.0) == 0.0
        assert abs(elastic_in_out(0.001)) < 0.01
        # Penner's exponential envelope settles to within ~1e-4 of 1 (the
        # t>=1 guard snaps the exact endpoint); no 0.75→1 jump as before.
        assert abs(elastic_in_out(1.0 - 1e-9) - 1.0) < 1e-3
        assert elastic_in_out(1.0) == 1.0

    def test_elastic_in_out_symmetric(self):
        for t in (0.1, 0.25, 0.4, 0.6, 0.9):
            assert elastic_in_out(t) == pytest.approx(
                1.0 - elastic_in_out(1.0 - t), abs=1e-9,
            )

    def test_elastic_monotone_envelope_bounds(self):
        samples = [elastic_in_out(i / 200) for i in range(201)]
        assert min(samples) > -0.5
        assert max(samples) < 1.5
        assert all(math.isfinite(v) for v in samples)


# --------------------------------------------------------------------- #
# AUD-28102 / AUD-28103 — interpolate colour clamp + extrapolate validation
# --------------------------------------------------------------------- #


class TestInterpolateGuards:
    def test_mix_colors_overshoot_clamps_to_valid_hex(self):
        mixed = _mix_colors("#000000", "#ffffff", 2.0)
        assert mixed == "#ffffff"
        _parse_color(mixed)  # must round-trip

    def test_mix_colors_negative_t_clamps(self):
        mixed = _mix_colors("#336699", "#ffffff", -1.5)
        assert mixed == "#000000"
        _parse_color(mixed)

    def test_mix_colors_alpha_clamps_and_round_trips(self):
        for t in (-3.0, -0.5, 1.5, 42.0):
            mixed = _mix_colors("#11223344", "#55667788", t)
            assert len(mixed) == 9
            _parse_color(mixed)

    @pytest.mark.parametrize("mode", ["cmap", "", "EXTEND", "none", 1])
    def test_invalid_extrapolate_mode_raises(self, mode):
        with pytest.raises(ValueError):
            interpolate(0.5, [0.0, 1.0], [0.0, 1.0], extrapolate_left=mode)
        with pytest.raises(ValueError):
            interpolate(0.5, [0.0, 1.0], [0.0, 1.0], extrapolate_right=mode)

    def test_valid_extrapolate_modes_still_work(self):
        assert interpolate(-1.0, [0.0, 1.0], [0.0, 1.0], extrapolate_left="clamp") == 0.0
        assert interpolate(-1.0, [0.0, 1.0], [0.0, 1.0], extrapolate_left="identity") == -1.0
        assert interpolate(2.0, [0.0, 1.0], [0.0, 1.0], extrapolate_right="extend") == 2.0


# --------------------------------------------------------------------- #
# AUD-28107 / AUD-28108 — expression sandbox
# --------------------------------------------------------------------- #


class TestExpressionSandbox:
    def test_integer_power_tower_rejected_fast(self):
        # ``9 ** 9 ** 9`` computes a 3.7e8-digit int without the guard.
        with pytest.raises(ExpressionError):
            evaluate_expression("9 ** 9 ** 9", ExpressionContext())

    def test_pow_function_guarded(self):
        with pytest.raises(ExpressionError):
            evaluate_expression("pow(999999999, 999999999)", ExpressionContext())

    def test_literal_huge_exponent_rejected_at_validation(self):
        with pytest.raises(ExpressionError):
            evaluate_expression("2 ** 100000", ExpressionContext())

    def test_legitimate_powers_still_work(self):
        assert evaluate_expression("2 ** 10", ExpressionContext()) == 1024
        assert evaluate_expression("pow(2, 8)", ExpressionContext()) == 256
        assert evaluate_expression("2 ** 0.5", ExpressionContext()) == pytest.approx(
            math.sqrt(2.0))
        assert evaluate_expression("frame ** 2", ExpressionContext(frame=7)) == 49

    def test_missing_marker_key_raises_expression_error(self):
        with pytest.raises(ExpressionError):
            evaluate_expression('markers["missing"]', ExpressionContext(markers={"a": 1}))

    def test_present_marker_key_still_works(self):
        assert evaluate_expression(
            'markers["a"] * 2', ExpressionContext(markers={"a": 5}),
        ) == 10

    def test_zero_division_translation_unchanged(self):
        with pytest.raises(ExpressionError):
            evaluate_expression("1 / 0", ExpressionContext())


# --------------------------------------------------------------------- #
# AUD-28111 — rational-fps timecode
# --------------------------------------------------------------------- #


class TestFormatTimecode:
    def test_integer_fps_unchanged(self):
        assert format_timecode(0) == "00:00:00:00"
        assert format_timecode(23, 24, 1) == "00:00:00:23"
        assert format_timecode(24, 24, 1) == "00:00:01:00"
        assert format_timecode(2501, 24, 1) == "00:01:44:05"

    def test_ntsc_frame_rate_exact(self):
        # 1798 frames at 30000/1001 = 59.9933...s → :59:29 (the old integer
        # rounding showed 00:00:59:28 against a true position of ≈01:00:00).
        assert format_timecode(1798, 30000, 1001) == "00:00:59:29"
        # Exactly one NTSC minute of frames (1800) is 60.06 wall seconds.
        assert format_timecode(1800, 30000, 1001) == "00:01:00:01"
        assert format_timecode(1500, 30000, 1001) == "00:00:50:01"
        assert format_timecode(29, 30000, 1001) == "00:00:00:29"
        assert format_timecode(30, 30000, 1001) == "00:00:01:00"

    def test_drift_is_bounded_over_ten_minutes(self):
        # Wall-clock frame count for 10 NTSC minutes: 10 * 60 * 30000/1001 =
        # 17982.02 frames; 17982 frames = 599.9933s → 00:09:59:29 (within a
        # frame of ten minutes, not the ~36-frame error of the old code).
        assert format_timecode(17982, 30000, 1001) == "00:09:59:29"
        assert format_timecode(17983, 30000, 1001) == "00:10:00:00"

    def test_rejects_bad_inputs(self):
        with pytest.raises(ValueError):
            format_timecode(-1)
        with pytest.raises(ValueError):
            format_timecode(1, 0, 1)
        with pytest.raises(ValueError):
            format_timecode(1, 24, 0)


# --------------------------------------------------------------------- #
# AUD-28113 — spring_config semantics match the compiler
# --------------------------------------------------------------------- #


class TestSpringConfigSemantics:
    def test_partial_dict_uses_compiler_defaults(self):
        sem = motion_semantics("pop", {"spring_config": {"mass": 2.0}}, 24.0)
        # presets._coerce_spring_config defaults: damping=10, stiffness=100.
        assert sem["mass"] == 2.0
        assert sem["damping"] == 10.0
        assert sem["stiffness"] == 100.0

    def test_absent_config_keeps_preset_base(self):
        sem = motion_semantics("pop", {}, 24.0)
        assert (sem["mass"], sem["damping"], sem["stiffness"]) == (1.0, 12.0, 180.0)
        sem = motion_semantics("overshoot", {}, 24.0)
        assert (sem["mass"], sem["damping"], sem["stiffness"]) == (1.0, 7.0, 170.0)

    def test_full_dict_and_spring_config_object(self):
        sem = motion_semantics(
            "pop", {"spring_config": {"mass": 1.0, "damping": 12.0, "stiffness": 180.0}},
            24.0,
        )
        assert (sem["damping"], sem["stiffness"]) == (12.0, 180.0)

    def test_non_spring_preset_kind_unchanged(self):
        assert motion_semantics("fade", {}, 24.0) == {"kind": "easing", "preset_id": "fade"}


# --------------------------------------------------------------------- #
# AUD-27901 — ffmpeg stderr drained concurrently
# --------------------------------------------------------------------- #


def _fake_ffmpeg_script(tmp_path: Path) -> Path:
    """Fake ffmpeg: floods stderr BEFORE draining stdin, then succeeds.

    With the old "write all stdin, then read stderr" behaviour the 64KB
    stderr pipe fills, the fake blocks writing stderr, stops reading stdin,
    and both processes deadlock (stdin payload here is 256KB > pipe buffer).
    """
    script = tmp_path / "fake_ffmpeg.sh"
    script.write_text(
        "#!/bin/sh\n"
        # ~80KB of stderr (built with one subprocess pair, emitted with shell
        # builtins) before touching stdin — beyond the 64KB OS pipe buffer,
        # so a parent that does not drain stderr concurrently deadlocks.
        'chunk="$(head -c 64 /dev/zero | tr \'\\0\' \'x\')"\n'
        "i=0; while [ $i -lt 1200 ]; do printf '%s\\n' \"$chunk\" >&2; i=$((i+1)); done\n"
        "cat > /dev/null\n"
        'out=""; for arg in "$@"; do out=$arg; done; printf "video-bytes" > "$out"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _video_writes(tmp_path: Path) -> tuple[Path, Path]:
    frames = tuple(
        RenderedFrame(frame=i, width=64, height=64, pixel_format="rgba8",
                      data=bytes([16 * i, 32, 64, 255] * (64 * 64)))
        for i in range(16)  # 16 * 16KB = 256KB stdin payload
    )
    result = RenderResult(job_id="test-aud27901", frames=frames)
    scene = TeachingSceneDocument.create_default(
        "aud27901", scene_id="scene-aud27901", width=64, height=64,
        fps_num=24, duration_frames=16,
    )
    context = RenderContext(fps=24.0, width=64, height=64)
    output = tmp_path / "out" / "video.mp4"
    return output, tmp_path / "unused"


class TestFfmpegStdinStderrNoDeadlock:
    def test_write_video_completes_when_stderr_floods_first(self, tmp_path):
        script = _fake_ffmpeg_script(tmp_path)
        output, _ = _video_writes(tmp_path)
        options = ExportOptions(format="mp4", ffmpeg_binary=str(script))

        done = threading.Event()
        error: list[BaseException] = []

        def _run() -> None:
            try:
                TeachingAnimationExportService._write_video(
                    output, "mp4",
                    RenderResult(job_id="test-aud27901", frames=tuple(
                        RenderedFrame(frame=i, width=64, height=64, pixel_format="rgba8",
                                      data=bytes([16 * i, 32, 64, 255] * (64 * 64)))
                        for i in range(16)
                    )),
                    RenderContext(fps=24.0, width=64, height=64),
                    options,
                    TeachingSceneDocument.create_default(
                        "aud27901", scene_id="scene-aud27901", width=64, height=64,
                        fps_num=24, duration_frames=16,
                    ),
                )
            except BaseException as exc:  # noqa: BLE001 - propagate to the test
                error.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        assert done.wait(timeout=60.0), (
            "export deadlocked: the parent blocked writing stdin while ffmpeg "
            "blocked writing a full stderr pipe"
        )
        worker.join(timeout=5.0)
        assert not error, error
        assert output.is_file()
        assert output.with_suffix(".mp4.manifest.json").is_file()


# --------------------------------------------------------------------- #
# AUD-27910 — facade maps HtmlRenderError on export
# --------------------------------------------------------------------- #


class TestExportFacadeHtmlRenderError:
    def test_html_render_error_becomes_structured_result(self, monkeypatch):
        from src.features.teaching_animation.core.contracts import (
            RenderError,
            RenderErrorCode,
        )
        from src.features.teaching_animation.render.html_scene_renderer_v2 import (
            HtmlRenderError,
        )
        import src.features.teaching_animation.export_service as export_service_module

        render_error = RenderError(
            code=RenderErrorCode.RENDER_FAILED, message="browser capture died",
            recoverable=True,
        )

        class _FailingService:
            def export(self, *_args, **_kwargs):
                raise HtmlRenderError(render_error)

            def close(self) -> None:
                pass

        monkeypatch.setattr(
            export_service_module, "TeachingAnimationExportService", _FailingService,
        )
        scene = TeachingSceneDocument.create_default(scene_id="scene-aud27910")
        result = export_scene_document(scene, "/tmp/unused-aud27910.mp4", {})
        assert result.ok is False
        assert result.error is not None
        assert result.error.code == RenderErrorCode.RENDER_FAILED
        assert "browser capture died" in result.error.message

    def test_plain_errors_still_map_to_render_failed(self, monkeypatch):
        import src.features.teaching_animation.export_service as export_service_module

        class _FailingService:
            def export(self, *_args, **_kwargs):
                raise ValueError("bad options")

            def close(self) -> None:
                pass

        monkeypatch.setattr(
            export_service_module, "TeachingAnimationExportService", _FailingService,
        )
        scene = TeachingSceneDocument.create_default(scene_id="scene-aud27910b")
        result = export_scene_document(scene, "/tmp/unused.mp4", {})
        assert result.ok is False
        assert result.error is not None


# --------------------------------------------------------------------- #
# AUD-27911 / AUD-27920 — prepare inside try/finally
# --------------------------------------------------------------------- #


class _LeakyRenderer:
    """Renderer whose prepare() allocates then fails — cleanup must run."""

    def __init__(self) -> None:
        self.cleaned = 0

    def prepare(self, *_args) -> None:
        self.cleaned = -1  # "session allocated"
        raise RuntimeError("session setup failed after allocation")

    def render_frame(self, *_args):
        raise AssertionError("render_frame must not run when prepare fails")

    def cleanup(self) -> None:
        self.cleaned += 1


class TestPrepareCleanupContract:
    def test_facade_preview_frame_cleans_up_when_prepare_fails(self, monkeypatch):
        import src.features.teaching_animation.render.scene_renderer_router as router

        renderer = _LeakyRenderer()
        monkeypatch.setattr(router, "select_scene_renderer", lambda _scene: renderer)
        from src.features.teaching_animation.facade import preview_frame
        # Non-HtmlRenderError RuntimeErrors intentionally escape the facade;
        # the contract under test is that cleanup still ran.
        with pytest.raises(RuntimeError):
            preview_frame({"scene_id": "s"}, 0, RenderContext(fps=24.0, width=2, height=2))
        assert renderer.cleaned == 0, "cleanup must run exactly once after prepare raised"

    def test_host_provider_render_frame_cleans_up_when_prepare_fails(
        self, monkeypatch, tmp_path,
    ):
        import src.features.teaching_animation.render.scene_renderer_router as router
        import src.features.teaching_animation.render.cache as cache_module
        import src.features.teaching_animation.scene_cache as scene_cache_module

        scene = TeachingSceneDocument.create_default(
            "aud27920", scene_id="scene-aud27920", width=4, height=4,
            fps_num=24, duration_frames=8,
        )
        renderer = _LeakyRenderer()
        monkeypatch.setattr(router, "select_scene_renderer", lambda _scene: renderer)
        manifest = SimpleNamespace(
            renderer_build="", frame=0,
            key=("k", 0), payload=b"",
        )
        monkeypatch.setattr(
            cache_module, "build_cache_manifest",
            lambda *_a, **_k: manifest,
        )
        coordinator = SimpleNamespace(cache=SimpleNamespace(get=lambda _m: None))
        monkeypatch.setattr(
            scene_cache_module, "get_shared_scene_cache_coordinator",
            lambda: coordinator,
        )
        provider = TimelineSceneFrameProvider({scene.scene_id: scene})
        clip = SimpleNamespace(
            asset_id=f"teaching_animation:{scene.scene_id}",
            timeline_start=0, timeline_end=8, source_start=0, source_duration=8,
        )
        with pytest.raises(RuntimeError):
            provider.render_frame(clip, 0)
        assert renderer.cleaned == 0, "cleanup must run exactly once after prepare raised"


# --------------------------------------------------------------------- #
# module.py — AUD-28002 / AUD-28003 / AUD-28004
# --------------------------------------------------------------------- #


class _RecorderPanel:
    def __init__(self) -> None:
        self.proposals: list[tuple] = []
        self.cleared: list[str] = []

    def set_motion_graphic_proposal(self, *args) -> None:
        self.proposals.append(args)

    def clear_motion_graphic_proposal(self, request_id) -> None:
        self.cleared.append(str(request_id))

    def set_agent_notice(self, *args, **_kwargs) -> None:
        pass


class _RecorderService:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def append_chat_message(self, role, text, **_kwargs) -> None:
        self.messages.append((role, str(text)))


def _window(panel=None, service=None, controller=None):
    return SimpleNamespace(
        ai_edit_assistant_panel=panel if panel is not None else _RecorderPanel(),
        _ai_assistant_service=service if service is not None else _RecorderService(),
        _ai_edit_controller=controller,
    )


def _plain_scene(scene_id: str) -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        "AUD Scene", scene_id=scene_id, width=64, height=36,
        fps_num=24, duration_frames=12,
    )


class TestSceneEditStaleGuard:
    def test_stale_proposal_refused_and_scene_untouched(self, monkeypatch):
        store = get_shared_scene_store()
        store.clear()
        scene = _plain_scene("scene-aud28002")
        store.register(scene)
        window = _window()
        module = TeachingAnimationModule()
        try:
            revision = store.revision(scene.scene_id)
            # User edits the scene while the proposal card is visible.
            store.register(replace(scene, name="用户改过"))
            module._pending_scene_edit_requests["scene-edit-x"] = {
                "scene_id": scene.scene_id,
                "scene_revision": revision,
                "scene": scene,
                "proposal": SimpleNamespace(),
                "element_id": "elem-1",
                "updates": {"style": {"color": "#ff0000"}},
                "changed_summary": "style",
            }
            applied: list[str] = []
            import src.features.teaching_animation.ai.command_catalog as catalog
            monkeypatch.setattr(
                catalog, "ExtendedCommandRegistry",
                lambda: SimpleNamespace(apply=lambda *_a, **_k: applied.append("apply")),
            )
            assert module._confirm_scene_edit_request(window, "scene-edit-x") is False
            assert applied == [], "stale proposal must not reach the command registry"
            assert store.get(scene.scene_id).name == "用户改过"
            assert any("assistant" == role for role, _ in window._ai_assistant_service.messages)
        finally:
            store.clear()
            module._pending_scene_edit_requests.clear()

    def test_matching_revision_confirms(self, monkeypatch):
        store = get_shared_scene_store()
        store.clear()
        scene = _plain_scene("scene-aud28002-ok")
        store.register(scene)
        window = _window()
        module = TeachingAnimationModule()
        try:
            module._pending_scene_edit_requests["scene-edit-ok"] = {
                "scene_id": scene.scene_id,
                "scene_revision": store.revision(scene.scene_id),
                "scene": scene,
                "proposal": SimpleNamespace(),
                "element_id": "elem-1",
                "updates": {"content": "新文字"},
                "changed_summary": "content",
            }
            import src.features.teaching_animation.ai.command_catalog as catalog
            import src.features.teaching_animation.host_adapters as host_adapters
            monkeypatch.setattr(
                catalog, "ExtendedCommandRegistry",
                lambda: SimpleNamespace(apply=lambda *_a, **_k: scene),
            )
            monkeypatch.setattr(
                host_adapters, "render_teaching_scene_frame",
                lambda *_a, **_k: SimpleNamespace(data=b"frame", width=64, height=36),
            )
            assert module._confirm_scene_edit_request(window, "scene-edit-ok") is True
        finally:
            store.clear()
            module._pending_scene_edit_requests.clear()


class TestTextConfirmationRouting:
    def test_confirm_text_routes_html_generate_to_html_handler(self, monkeypatch):
        module = TeachingAnimationModule()
        window = _window()
        module._pending_html_generate_requests["html-generate-abc"] = {
            "scene_id": "scene-x", "raw_html": "<html></html>",
        }
        routed: list[tuple] = []
        monkeypatch.setattr(
            module, "_confirm_html_generate_request",
            lambda _mw, rid: routed.append((rid,)) or True,
        )
        try:
            assert module._handle_ai_request(window, "确认") is True
            assert routed == [("html-generate-abc",)]
        finally:
            module._pending_html_generate_requests.clear()

    def test_confirm_text_routes_scene_edit_to_scene_edit_handler(self, monkeypatch):
        module = TeachingAnimationModule()
        window = _window()
        module._pending_scene_edit_requests["scene-edit-1"] = {
            "scene_id": "scene-y",
        }
        routed: list[tuple] = []
        monkeypatch.setattr(
            module, "_confirm_scene_edit_request",
            lambda _mw, rid: routed.append((rid,)) or True,
        )
        try:
            assert module._handle_ai_request(window, "可以") is True
            assert routed == [("scene-edit-1",)]
        finally:
            module._pending_scene_edit_requests.clear()

    def test_unknown_request_confirm_does_not_clear_card(self):
        module = TeachingAnimationModule()
        panel = _RecorderPanel()
        window = _window(panel=panel)
        assert module._confirm_motion_graphic_request(window, "missing-id") is False
        assert panel.cleared == []
        assert module._confirm_scene_edit_request(window, "missing-id") is False
        assert panel.cleared == []


class TestBuildFailedPlanRouting:
    def _failed_plan(self) -> dict:
        return {
            "phase": "build",
            "build_failed": True,
            "requires_button_confirmation": True,
            "render_mode": "html",
            "raw_html": "<html>上次失败的文档</html>",
            "seconds": 5.0,
            "fps": 25.0,
            "frame": 0,
        }

    def test_new_creative_request_retires_failed_plan(self, monkeypatch):
        module = TeachingAnimationModule()
        panel = _RecorderPanel()
        service = _RecorderService()
        window = _window(panel=panel, service=service)
        module._pending_motion_graphic_requests["motion-1"] = self._failed_plan()
        rebuilt: list[str] = []
        monkeypatch.setattr(
            module, "_trigger_motion_graphic_llm_build",
            lambda _mw, rid: rebuilt.append(rid) or True,
        )
        try:
            module._handle_ai_request(
                window, "重新帮我做一个全新的15秒产品介绍动画",
                # not a motion-graphic *edit* of an existing clip: no target
            )
            assert "motion-1" not in module._pending_motion_graphic_requests, (
                "the failed plan must be retired so the fresh request is not "
                "swallowed or treated as a revision of the failed document"
            )
            assert rebuilt == [], "failed document must not be used as edit context"
            swallowed = [t for _r, t in service.messages if "已保留为草稿" in t]
            assert not swallowed, "the fresh request must not be swallowed"
        finally:
            module._pending_motion_graphic_requests.clear()

    def test_confirmation_on_failed_plan_reports_and_keeps_guidance(self):
        module = TeachingAnimationModule()
        service = _RecorderService()
        window = _window(service=service)
        module._pending_motion_graphic_requests["motion-2"] = self._failed_plan()
        try:
            assert module._handle_ai_request(window, "确认") is True
            assistant_messages = [t for r, t in service.messages if r == "assistant"]
            assert assistant_messages, "user must be told the build failed"
        finally:
            module._pending_motion_graphic_requests.clear()

    def test_cancellation_cleans_up_failed_plan(self):
        module = TeachingAnimationModule()
        service = _RecorderService()
        window = _window(service=service)
        module._pending_motion_graphic_requests["motion-3"] = self._failed_plan()
        try:
            assert module._handle_ai_request(window, "取消") is True
            assert "motion-3" not in module._pending_motion_graphic_requests
        finally:
            module._pending_motion_graphic_requests.clear()
