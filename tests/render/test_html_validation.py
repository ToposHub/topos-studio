"""Regression tests for the shared HTML renderability gate."""

from types import SimpleNamespace

import pytest

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.render.html_validation import validate_html_scene


def _scene(*, duration_frames: int = 48):
    return SimpleNamespace(
        scene_id="html-gate",
        fps=24.0,
        width=320,
        height=180,
        duration_frames=duration_frames,
        html_layer=SimpleNamespace(
            sanitized_html="<html><body><h1>Deck</h1></body></html>",
            metadata={},
            viewport_width=320,
            viewport_height=180,
        ),
    )


def _page_scene(*, duration_frames: int = 100):
    scene = _scene(duration_frames=duration_frames)
    scene.html_layer.sanitized_html = (
        '<html><body>'
        '<section data-topos-page="1"><h1>第一页</h1></section>'
        '<section data-topos-page="2"><h1>第二页</h1></section>'
        '</body></html>'
    )
    return scene


def test_gate_retries_legitimate_uniform_intro_frame():
    calls: list[tuple[int, str | None]] = []

    def render(_scene_id, frame, *, context):
        calls.append((frame, context.html_validation_background))
        if frame == 0:
            raise RuntimeError("capture returned uniform frame (0, 0, 0, 0)")
        return SimpleNamespace(data=b"rgba", width=320, height=180)

    result, frame = validate_html_scene(
        _scene(), context=RenderContext(fps=24, width=64, height=36, transparent=True),
        render_frame=render,
    )
    assert frame == 24
    assert result.width == 320
    assert calls[:2] == [(0, None), (24, None)]


def test_gate_rejects_a_page_that_is_uniform_at_every_candidate():
    def render(_scene_id, _frame, *, context):
        raise RuntimeError(
            "capture returned uniform frame (16, 24, 39, 255); "
            "DOM节点=0，可见节点=0，文字长度=0"
        )

    with pytest.raises(RuntimeError, match="HTML 验证捕获为空"):
        validate_html_scene(
            _scene(duration_frames=24),
            context=RenderContext(fps=24, width=320, height=180, transparent=True),
            render_frame=render,
        )


def test_gate_does_not_hide_timeout_or_browser_crash():
    calls: list[int] = []

    def render(_scene_id, frame, *, context):
        del context
        calls.append(frame)
        raise RuntimeError("playwright timeout")

    with pytest.raises(RuntimeError, match="playwright timeout"):
        validate_html_scene(_scene(), render_frame=render)
    assert calls == [0]


def test_gate_checks_every_page_and_rejects_near_uniform_later_page():
    """A valid first page must not hide a blank/near-blank later page."""
    from src.features.teaching_animation.render.html_renderer_spike.capture_backend import (
        is_effectively_blank,
    )

    page_scene = _page_scene()
    good = bytes((18, 32, 52, 255)) * (320 * 180)
    # Add enough varied pixels to represent a real composition on page 1.
    good = bytearray(good)
    for offset in range(0, len(good), 17):
        good[offset:offset + 4] = bytes((240, 120, 32, 255))
    good = bytes(good)
    near_blank = bytes((248, 250, 252, 255)) * (320 * 180)
    near_blank = bytearray(near_blank)
    near_blank[:4] = bytes((30, 90, 180, 255))
    near_blank = bytes(near_blank)
    assert is_effectively_blank(near_blank, 320, 180)[0]

    def render(_scene_id, frame, *, context):
        del context
        if frame >= 50:
            return SimpleNamespace(data=near_blank, width=320, height=180)
        return SimpleNamespace(data=good, width=320, height=180)

    with pytest.raises(RuntimeError, match="页面质量门"):
        validate_html_scene(
            page_scene,
            context=RenderContext(fps=24, width=320, height=180, transparent=True),
            render_frame=render,
        )


def test_gate_allows_first_page_to_paint_after_an_entrance_animation():
    """Frame zero may be an intentional opacity-zero intro, not a blank deck."""
    page_scene = _page_scene(duration_frames=100)
    calls: list[int] = []

    def render(_scene_id, frame, *, context):
        del context
        calls.append(frame)
        if frame == 0:
            raise RuntimeError("capture returned uniform frame (16, 24, 39, 255)")
        return SimpleNamespace(
            data=bytes((i % 251 for i in range(320 * 180 * 4))),
            width=320,
            height=180,
        )

    rendered, frame = validate_html_scene(
        page_scene,
        context=RenderContext(fps=24, width=320, height=180, transparent=True),
        render_frame=render,
    )

    assert rendered.width == 320
    assert frame == 24
    # The first page quality probe is now a settled frame instead of frame 0.
    assert 2 in calls


def test_gate_tries_later_frames_when_a_page_reveals_slowly():
    """A delayed page reveal is checked within its own interval, not rejected."""
    page_scene = _page_scene(duration_frames=100)

    def render(_scene_id, frame, *, context):
        del context
        if frame < 20:
            raise RuntimeError("capture returned uniform frame (16, 24, 39, 255)")
        return SimpleNamespace(
            data=bytes((i % 251 for i in range(320 * 180 * 4))),
            width=320,
            height=180,
        )

    rendered, _frame = validate_html_scene(
        page_scene,
        context=RenderContext(fps=24, width=320, height=180, transparent=True),
        render_frame=render,
    )

    assert rendered.width == 320
