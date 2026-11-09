"""CV-B01 域件测试：Color Viewer 状态／能力注册表（纯领域）。"""

from __future__ import annotations

import pytest

from src.features.color.core.color_viewer_capabilities import (
    PRESENTATION_COMPOSITE,
    PRESENTATION_IMAGE,
    PRESENTATION_NONE,
    PRESENTATION_SOURCE_GL,
    PRESENTATION_TIMELINE_PREVIEW,
    TIME_DOMAIN_CLIP_LOCAL,
    TIME_DOMAIN_NONE,
    TIME_DOMAIN_TIMELINE,
    ColorViewerCapabilityContext,
    ColorViewerState,
    color_capability_by_action,
    default_time_domain_for,
    evaluate_color_viewer_capabilities,
    presentation_supports_video,
)


def _caps(**context_overrides):
    context = ColorViewerCapabilityContext(**context_overrides)
    return color_capability_by_action(
        evaluate_color_viewer_capabilities(context))


def test_no_media_honestly_disables_monitor_actions() -> None:
    caps = _caps()
    assert caps["view.highlight"].supported is False
    assert caps["view.highlight"].reason == "No media is open in the viewer."
    for action in ("view.split_wipe", "view.ab_toggle",
                   "view.capture_reference", "view.capture_still",
                   "view.auto_color", "overlay.power_windows"):
        assert caps[action].supported is False
        assert caps[action].reason


def test_canvas_transform_actions_do_not_require_media() -> None:
    caps = _caps()
    for action in ("view.zoom_in", "view.zoom_out", "view.fit",
                   "view.actual_size", "view.guides", "view.fullscreen"):
        assert caps[action].supported is True
        assert caps[action].reason is None


def test_image_mode_keeps_grading_but_disables_transport() -> None:
    state = ColorViewerState(
        presentation_path=PRESENTATION_IMAGE, has_media=True,
        show_mask_mode=True, split_view_active=True)
    caps = _caps(state=state, engine_available=True)
    # 图片调色同样有 qualifier/matte 与 wipe。
    assert caps["view.highlight"].supported is True
    assert caps["view.highlight"].checked is True
    assert caps["view.split_wipe"].checked is True
    assert caps["view.capture_still"].supported is True
    # 图片呈现不承载视频 transport。
    for action in ("transport.play", "transport.first", "transport.last",
                   "transport.next", "transport.prev", "transport.scrub",
                   "transport.loop"):
        assert caps[action].supported is False
        assert caps[action].reason == (
            "The current view is not presenting video.")


def test_missing_engine_disables_display_actions_with_no_engine() -> None:
    state = ColorViewerState(
        presentation_path=PRESENTATION_IMAGE, has_media=True)
    caps = _caps(state=state, engine_available=False)
    assert caps["view.highlight"].supported is False
    assert caps["view.highlight"].reason == (
        "The rendering engine is not available yet.")
    assert caps["view.split_wipe"].supported is False


def test_source_gl_enables_transport_and_backfills_loop_checked() -> None:
    state = ColorViewerState(
        presentation_path=PRESENTATION_SOURCE_GL, has_media=True,
        loop_enabled=True, time_domain=TIME_DOMAIN_CLIP_LOCAL)
    caps = _caps(state=state, engine_available=True,
                 video_controller_available=True)
    for action in ("transport.play", "transport.scrub", "transport.loop"):
        assert caps[action].supported is True
    assert caps["transport.loop"].checked is True
    assert state.time_domain == TIME_DOMAIN_CLIP_LOCAL


def test_video_presentation_without_controller_reports_no_controller() -> None:
    state = ColorViewerState(
        presentation_path=PRESENTATION_SOURCE_GL, has_media=True)
    caps = _caps(state=state, engine_available=True,
                 video_controller_available=False)
    assert caps["transport.play"].supported is False
    assert caps["transport.play"].reason == (
        "No video playback controller is available.")


@pytest.mark.parametrize("path", [PRESENTATION_TIMELINE_PREVIEW,
                                  PRESENTATION_COMPOSITE])
def test_timeline_presentations_use_timeline_frame_domain(path: str) -> None:
    assert default_time_domain_for(path) == TIME_DOMAIN_TIMELINE
    assert presentation_supports_video(path) is True


def test_none_and_image_presentations_have_no_time_domain() -> None:
    assert default_time_domain_for(PRESENTATION_NONE) == TIME_DOMAIN_NONE
    assert default_time_domain_for(PRESENTATION_IMAGE) == TIME_DOMAIN_NONE
    assert presentation_supports_video(PRESENTATION_IMAGE) is False


def test_clear_reference_requires_loaded_still() -> None:
    state = ColorViewerState(presentation_path=PRESENTATION_IMAGE,
                             has_media=True)
    caps = _caps(state=state, engine_available=True)
    assert caps["view.clear_reference"].supported is False
    assert caps["view.clear_reference"].reason == (
        "No reference still is loaded.")
    state = ColorViewerState(presentation_path=PRESENTATION_IMAGE,
                             has_media=True, has_reference_still=True)
    caps = _caps(state=state, engine_available=True)
    assert caps["view.clear_reference"].supported is True


def test_undesigned_services_report_stable_reasons() -> None:
    caps = _caps()
    expected = {
        "compare.split_screen": "presenter is not wired",
        "monitor.proxy": "not wired",
        "monitor.resolution": "not wired",
        "monitor.bypass": "not wired",
        "monitor.unmix": "not designed",
        "monitor.mute": "not wired",
        "monitor.alpha": "no alpha channel",
        "grade.freeze": "no independent snapshot",
    }
    for action, fragment in expected.items():
        assert caps[action].supported is False, action
        assert fragment in caps[action].reason, action


def test_focus_workspace_and_timeline_switch_gates() -> None:
    caps = _caps()
    assert caps["view.focus_mode"].supported is False
    assert caps["view.workspace"].supported is False
    assert caps["timeline.switch"].supported is False
    caps = _caps(focus_host_available=True,
                 workspace_service_available=True, timeline_choice_count=2)
    assert caps["view.focus_mode"].supported is True
    assert caps["view.workspace"].supported is True
    assert caps["timeline.switch"].supported is True


def test_overlay_capability_reflects_overlay_visibility() -> None:
    state = ColorViewerState(presentation_path=PRESENTATION_IMAGE,
                             has_media=True, overlays_visible=False)
    caps = _caps(state=state, engine_available=True)
    assert caps["overlay.power_windows"].supported is True
    assert caps["overlay.power_windows"].checked is False


def test_snapshots_are_serializable() -> None:
    state = ColorViewerState(
        presentation_path=PRESENTATION_SOURCE_GL, context_revision=7,
        clip_id="c1", timeline_id="t1",
        time_domain=default_time_domain_for(PRESENTATION_SOURCE_GL))
    data = state.to_dict()
    assert data["presentation_path"] == PRESENTATION_SOURCE_GL
    assert data["context_revision"] == 7
    assert data["time_domain"] == TIME_DOMAIN_CLIP_LOCAL
    caps = _caps(state=state)
    cap_dict = caps["transport.loop"].to_dict()
    assert cap_dict["action"] == "transport.loop"
    assert set(cap_dict) == {"action", "supported", "reason", "checked"}
