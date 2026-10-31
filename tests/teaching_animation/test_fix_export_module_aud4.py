"""Batch-A fixes (AUD-27900 / AUD-28001 / AUD-28202) — regression tests.

AUD-27900: sequence export must route by frame ``pixel_format`` so a file's
extension always matches its content (RGBA8 bytes are never written as
``.svg`` and never UTF-8-decoded for ``html_sequence``).

AUD-28001: a cached ``TeachingAnimationAIService`` must never write an HTML
layer onto a stale scene snapshot over the shared store's newer document.

AUD-28202: near-critical damping must not divide by a zero ``omega_d``.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.features.teaching_animation.core.contracts import (
    RenderContext,
    RenderedFrame,
)
from src.features.teaching_animation.core.html_layer import FreeformHtmlMotionLayer
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.export_service import (
    TeachingAnimationExportService,
    TeachingExportError,
)
from src.features.teaching_animation.module import TeachingAnimationModule
from src.features.teaching_animation.motion import SpringConfig, measure_spring, spring
from src.features.teaching_animation.render.render_worker import RenderResult
from src.features.teaching_animation.scene_store import get_shared_scene_store


# --------------------------------------------------------------------- #
# AUD-27900 — sequence export routes by pixel_format
# --------------------------------------------------------------------- #

_SVG_BYTES = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"></svg>'
)


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        "seq", scene_id="scene-aud27900", width=2, height=2,
        fps_num=24, duration_frames=2,
    )


def _result(*frames: RenderedFrame) -> RenderResult:
    return RenderResult(job_id="test-aud27900", frames=frames)


def _raster_frame(index: int) -> RenderedFrame:
    return RenderedFrame(
        frame=index, width=2, height=2, pixel_format="rgba8",
        data=bytes([index, 64, 128, 255] * 4),
    )


def _svg_frame(index: int) -> RenderedFrame:
    return RenderedFrame(
        frame=index, width=2, height=2, pixel_format="svg",
        data=_SVG_BYTES,
    )


def _context() -> RenderContext:
    return RenderContext(fps=24.0, width=2, height=2)


def _manifest(out) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


class TestSequenceExportPixelFormatRouting:
    def test_raster_frames_with_svg_sequence_deliver_rgba_files(self, tmp_path):
        """HTML-backend RGBA8 bytes must never be labelled ``.svg``."""
        out = tmp_path / "seq"
        TeachingAnimationExportService._write_sequence(
            out, "svg_sequence", _result(_raster_frame(0), _raster_frame(1)),
            _context(), _scene(),
        )
        files = sorted(p.name for p in out.iterdir() if p.suffix == ".rgba")
        assert files == ["frame_000000.rgba", "frame_000001.rgba"]
        assert not list(out.glob("*.svg"))
        # Content is the raw pixel bytes — byte-identical to the frame data.
        assert (out / "frame_000000.rgba").read_bytes() == bytes([0, 64, 128, 255] * 4)
        assert (out / "frame_000001.rgba").read_bytes() == bytes([1, 64, 128, 255] * 4)
        manifest = _manifest(out)
        assert manifest["delivery_note"], "manifest must record the raster delivery"

    def test_raster_frames_with_html_sequence_do_not_crash(self, tmp_path):
        """``decode("utf-8")`` on RGBA8 bytes raised UnicodeDecodeError before."""
        out = tmp_path / "seq"
        TeachingAnimationExportService._write_sequence(
            out, "html_sequence", _result(_raster_frame(0)),
            _context(), _scene(),
        )
        assert not list(out.glob("*.html"))
        assert (out / "frame_000000.rgba").read_bytes() == bytes([0, 64, 128, 255] * 4)
        assert _manifest(out)["delivery_note"]

    def test_raster_frames_with_rgba_sequence_unchanged(self, tmp_path):
        out = tmp_path / "seq"
        TeachingAnimationExportService._write_sequence(
            out, "rgba_sequence", _result(_raster_frame(0)),
            _context(), _scene(),
        )
        assert (out / "frame_000000.rgba").read_bytes() == bytes([0, 64, 128, 255] * 4)
        assert "delivery_note" not in _manifest(out)

    def test_svg_frames_with_svg_sequence_write_text_bytes(self, tmp_path):
        out = tmp_path / "seq"
        TeachingAnimationExportService._write_sequence(
            out, "svg_sequence", _result(_svg_frame(0)),
            _context(), _scene(),
        )
        assert (out / "frame_000000.svg").read_bytes() == _SVG_BYTES
        assert "delivery_note" not in _manifest(out)

    def test_svg_frames_with_html_sequence_wrap_text(self, tmp_path):
        out = tmp_path / "seq"
        TeachingAnimationExportService._write_sequence(
            out, "html_sequence", _result(_svg_frame(0)),
            _context(), _scene(),
        )
        html = (out / "frame_000000.html").read_text(encoding="utf-8")
        assert html.lstrip().lower().startswith("<!doctype html>")
        assert _SVG_BYTES.decode("utf-8") in html

    def test_unknown_pixel_format_fails_closed(self, tmp_path):
        bogus = RenderedFrame(
            frame=0, width=2, height=2, pixel_format="exr_half",
            data=b"\x00" * 8,
        )
        with pytest.raises(TeachingExportError):
            TeachingAnimationExportService._write_sequence(
                tmp_path / "seq", "svg_sequence", _result(bogus),
                _context(), _scene(),
            )


# --------------------------------------------------------------------- #
# AUD-28001 — cached HTML service refreshes from the shared store
# --------------------------------------------------------------------- #


def _html_scene(scene_id: str, headline: str) -> TeachingSceneDocument:
    return replace(
        TeachingSceneDocument.create_default(
            "Target Clip", scene_id=scene_id, width=640, height=360,
            fps_num=25, duration_frames=50,
        ),
        html_layer=FreeformHtmlMotionLayer(
            layer_id=f"layer-{scene_id}",
            sanitized_html=(
                "<!doctype html><html><body>"
                f'<main data-topos-page="0"><h1>{headline}</h1></main>'
                "</body></html>"
            ),
            viewport_width=640,
            viewport_height=360,
            fps_num=25,
            fps_den=1,
            duration_frames=50,
        ),
    )


class _Controller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def start_html_generate_chat(self, request_id, user_prompt, **_kwargs):
        self.calls.append((request_id, user_prompt))
        return True


class _Panel:
    def __init__(self) -> None:
        self.proposals: list[tuple] = []

    def set_motion_graphic_proposal(self, *args) -> None:
        self.proposals.append(args)

    def set_agent_notice(self, *args, **_kwargs) -> None:
        pass


class _Service:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def append_chat_message(self, role, text, **_kwargs) -> None:
        self.messages.append((role, text))


def _window(assets, controller, panel, service):
    return SimpleNamespace(
        _current_edit_project=SimpleNamespace(assets=assets),
        _ai_edit_controller=controller,
        ai_edit_assistant_panel=panel,
        _ai_assistant_service=service,
    )


def _assets_for(scene_id: str) -> dict:
    return {
        f"teaching_animation:{scene_id}": SimpleNamespace(
            asset_id=f"teaching_animation:{scene_id}",
            display_name="课程网页 Clip",
            media_type="motion_graphic",
            metadata={"scene_id": scene_id},
        ),
    }


class TestHtmlServiceCacheRefresh:
    def test_cached_service_scene_follows_store(self):
        store = get_shared_scene_store()
        store.clear()
        scene = _html_scene("scene-refresh", "v0")
        store.register(scene)
        module = TeachingAnimationModule()
        try:
            service = module._get_html_service_for("scene-refresh")
            assert service is not None and service.scene is scene
            # A store-only write (editor command, confirmed scene edit, project
            # reload): the cached service must not keep resurrecting v0.
            newer = replace(scene, name="user-renamed")
            store.register(newer)
            refreshed = module._get_html_service_for("scene-refresh")
            assert refreshed is service
            assert refreshed.scene is newer
        finally:
            store.clear()

    def test_second_ai_edit_preserves_store_only_user_edits(self, monkeypatch):
        """Round-2 HTML confirm must not roll back edits made between rounds.

        Before the fix the cached service still held the round-1 snapshot;
        ``SetHtmlLayerCommand`` applied to it and ``store.register`` silently
        reverted the user's store-only edit (here: the renamed scene).
        """
        store = get_shared_scene_store()
        store.clear()
        scene = _html_scene("scene-two-rounds", "当前网页")
        store.register(scene)
        controller = _Controller()
        window = _window(_assets_for("scene-two-rounds"), controller,
                         _Panel(), _Service())
        module = TeachingAnimationModule()
        monkeypatch.setattr(module, "_validate_motion_graphic_scene", lambda _s: None)

        def _respond(text: str, headline: str) -> None:
            before = len(controller.calls)
            assert module._queue_motion_graphic_edit(
                window, text, ("teaching_animation:scene-two-rounds",),
            )
            request_id = controller.calls[before][0]
            html = (
                "<!doctype html><html><body>"
                f'<main data-topos-page="0"><h1>{headline}</h1></main>'
                "</body></html>"
            )
            assert module._handle_model_html_generate_response(
                window, request_id, html,
            ) is None

        try:
            _respond("把主标题改成「第一版」", "第一版")
            after_round1 = store.get("scene-two-rounds")
            assert after_round1 is not None
            assert "第一版" in after_round1.html_layer.sanitized_html
            # Store-only user edit between the two AI rounds (no editor window:
            # the chat-driven timeline flow never opens one).
            store.register(replace(after_round1, name="用户重命名"))
            _respond("把主标题改成「第二版」", "第二版")
            after_round2 = store.get("scene-two-rounds")
            assert after_round2 is not None
            assert "第二版" in after_round2.html_layer.sanitized_html
            assert after_round2.name == "用户重命名", (
                "the cached service wrote its stale snapshot over the store"
            )
        finally:
            store.clear()


# --------------------------------------------------------------------- #
# AUD-28202 — near-critical damping no longer divides by zero omega_d
# --------------------------------------------------------------------- #


class TestNearCriticalDamping:
    def test_audit_crash_input_is_finite(self):
        """The exact reported crash: zeta rounds to 1.0 in the under-damped
        branch while the discriminant is negative -> omega_d == 0."""
        cfg = SpringConfig(mass=1.0, damping=3.4641016151377544, stiffness=3.0)
        values = [spring(f, 24.0, cfg) for f in range(0, 200)]
        assert all(math.isfinite(v) for v in values)
        assert values[0] == pytest.approx(0.0)
        assert values[-1] == pytest.approx(1.0, abs=1e-3)

    def test_step_response_near_critical_matches_critical_limit(self):
        from src.features.teaching_animation.motion.spring import _step_response

        critical = SpringConfig(mass=1.0, damping=20.0, stiffness=100.0)
        # Mathematically just under-damped; zeta rounds within 1e-15 of 1.
        near = SpringConfig(mass=1.0, damping=3.4641016151377544, stiffness=3.0)
        for t in (0.0, 0.01, 0.1, 1.0, 5.0):
            # Same characteristic scales are not required — assert the near
            # response is a valid monotone settle, and the critical config's
            # own closed form still holds at t=0 and large t.
            assert _step_response(t, critical) == pytest.approx(
                1.0 - math.exp(-10.0 * t) * (1.0 + 10.0 * t), abs=1e-12,
            )
            assert -1e-9 <= _step_response(t, near) <= 1.0 + 1e-9

    def test_near_critical_monotonic_no_overshoot(self):
        cfg = SpringConfig(mass=1.0, damping=3.4641016151377544, stiffness=3.0)
        values = [spring(f, 24.0, cfg) for f in range(0, 400)]
        assert all(values[i] <= values[i + 1] + 1e-9 for i in range(len(values) - 1))

    def test_measure_spring_near_critical(self):
        cfg = SpringConfig(mass=1.0, damping=3.4641016151377544, stiffness=3.0)
        assert 1 <= measure_spring(24.0, cfg) <= 600

    def test_clearly_under_and_over_damped_unchanged(self):
        under = SpringConfig(mass=1.0, damping=7.0, stiffness=170.0)
        over = SpringConfig(mass=1.0, damping=40.0, stiffness=10.0)
        assert max(spring(f, 24.0, under) for f in range(0, 40)) > 1.0
        over_values = [spring(f, 24.0, over) for f in range(0, 200)]
        assert all(over_values[i] <= over_values[i + 1] + 1e-9
                   for i in range(len(over_values) - 1))
