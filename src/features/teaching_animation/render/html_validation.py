"""Shared browser renderability gate for HTML Motion Graphic scenes.

The gate deliberately has stricter semantics than final frame delivery:
uniform captures are retried at deterministic authored frames and once over a
host-owned contrast background.  A scene is accepted only when the real host
renderer returns pixels for at least one candidate.  Final export can then
preserve a legitimate uniform frame (for example an opacity-zero intro) via
``RenderContext.allow_uniform_frames`` without weakening this check.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from ..core.contracts import RenderContext


RenderFrame = Callable[[str, int], Any]


def _is_blank_capture_error(exc: BaseException) -> bool:
    """Whether ``exc`` is the retryable blank-capture outcome (AUD-30304).

    The structural contract first: the HTML V2 renderer raises
    :class:`HtmlBlankCaptureError` for captures the backend explicitly
    reported as blank/uniform, so classification survives message rewording
    or localization.  Structured non-blank errors (plain
    :class:`HtmlRenderError`) are NEVER classified by their message — a
    timeout/crash that happens to mention "uniform frame" must surface.
    The legacy substring fallback remains only for raisers that carry no
    structured marker (injected test doubles, older render paths).
    """
    from .html_scene_renderer_v2 import HtmlBlankCaptureError, HtmlRenderError

    if isinstance(exc, HtmlBlankCaptureError):
        return True
    if isinstance(exc, HtmlRenderError):
        return False
    message = str(exc).lower()
    return "uniform frame" in message or "near-uniform" in message


def _candidate_frames(scene: Any) -> list[int]:
    duration = max(1, int(getattr(scene, "duration_frames", 1) or 1))
    candidates = [0]
    layer = getattr(scene, "html_layer", None)
    if layer is None or duration <= 1:
        return candidates
    try:
        from ..core.html_page_deck import (
            detect_page_count, page_start_frame, page_timing_for,
        )

        page_count = detect_page_count(getattr(layer, "sanitized_html", ""))
        timing = page_timing_for(
            getattr(layer, "metadata", {}) or {},
            page_count=page_count,
            fallback_default_seconds=(
                max(0.001, duration / max(0.001, float(scene.fps)))
                / max(1, page_count)
            ),
        )
        first_page_frames = max(
            1, round(float(timing.durations[0]) * float(scene.fps))
        )
        page_probe_frames = []
        for index, duration_seconds in enumerate(timing.durations):
            start = page_start_frame(index, float(scene.fps), timing)
            page_frames = max(1, round(float(duration_seconds) * float(scene.fps)))
            # Every page gets a deterministic settle frame.  Page 0 used to
            # be probed at frame 0, but that is the exact moment at which a
            # model-authored entrance animation is commonly still at
            # ``opacity: 0``.  The browser can render that composition
            # correctly; rejecting it here was a false negative, not a real
            # empty page.  Keep the offset inside the page's own time window.
            offset = min(
                max(1, round(float(scene.fps) * 0.1)), page_frames - 1,
            )
            page_probe_frames.append(min(duration - 1, start + offset))
    except Exception:  # noqa: BLE001 - malformed legacy metadata is repairable
        first_page_frames = duration
        page_probe_frames = []
    fps = max(1, round(float(getattr(scene, "fps", 24.0) or 24.0)))
    candidates.extend([
        min(duration - 1, first_page_frames - 1, fps),
        min(duration - 1, max(1, round(duration * 0.1))),
        min(duration - 1, max(1, duration // 2)),
        duration - 1,
    ])
    candidates.extend(page_probe_frames)
    return list(dict.fromkeys(max(0, min(duration - 1, int(frame))) for frame in candidates))


def _page_probe_candidates(scene: Any) -> list[list[int]]:
    """Return deterministic visible-content candidates for every HTML page.

    A freeform HTML page may choose a long entrance animation or a delayed
    reveal.  One fixed probe is therefore still capable of false negatives.
    These candidates stay within each page's own interval and are tried in a
    deterministic order; a page passes when any one of them paints content.
    """
    layer = getattr(scene, "html_layer", None)
    duration = max(1, int(getattr(scene, "duration_frames", 1) or 1))
    if layer is None:
        return [[0]]
    try:
        from ..core.html_page_deck import (
            detect_page_count, page_start_frame, page_timing_for,
        )

        page_count = detect_page_count(getattr(layer, "sanitized_html", ""))
        timing = page_timing_for(
            getattr(layer, "metadata", {}) or {},
            page_count=page_count,
            fallback_default_seconds=(
                max(0.001, duration / max(0.001, float(scene.fps)))
                / max(1, page_count)
            ),
        )
        fps = max(1.0, float(getattr(scene, "fps", 24.0) or 24.0))
        probes: list[list[int]] = []
        for index, seconds in enumerate(timing.durations):
            start = page_start_frame(index, fps, timing)
            page_frames = max(1, round(float(seconds) * fps))
            # Probe the start, then progressively later settled points.  The
            # first page is allowed to animate in just like every later page;
            # frame 0 remains a normal render candidate, but is not a page
            # quality verdict by itself.
            offsets = (
                0,
                min(max(1, round(fps * 0.1)), page_frames - 1),
                min(max(1, round(page_frames * 0.25)), page_frames - 1),
                min(max(1, round(page_frames * 0.5)), page_frames - 1),
                page_frames - 1,
            )
            page_candidates = list(dict.fromkeys(
                max(0, min(duration - 1, start + offset))
                for offset in offsets
            ))
            probes.append(page_candidates or [0])
        return probes or [[0]]
    except Exception:  # noqa: BLE001 - malformed legacy metadata is repairable
        return [[0]]


def validate_html_scene(
    scene: Any,
    *,
    context: RenderContext | Any | None = None,
    render_frame: Callable[..., Any] | None = None,
) -> tuple[Any, int]:
    """Render deterministic candidates and return the first usable frame.

    ``render_frame`` is injectable for tests and staged scenes.  Its signature
    is ``(scene_id, frame, *, context)`` and it must use the real host renderer
    in production.  Only the browser's explicit uniform-frame diagnostic is
    retried; timeouts, crashes, policy violations and other failures surface
    immediately so they cannot be hidden by a later lucky frame.
    """
    from ..host_adapters import render_teaching_scene_frame

    if context is None:
        context = RenderContext(
            fps=float(scene.fps), width=int(scene.width), height=int(scene.height),
            transparent=True,
        )
    layer = getattr(scene, "html_layer", None)
    render_context = context
    if layer is not None and hasattr(context, "width") and hasattr(context, "height"):
        try:
            width = int(getattr(layer, "viewport_width", 0) or getattr(scene, "width", 0) or context.width)
            height = int(getattr(layer, "viewport_height", 0) or getattr(scene, "height", 0) or context.height)
            if width > 0 and height > 0:
                render_context = replace(context, width=width, height=height)
        except (TypeError, ValueError):
            render_context = context

    if render_frame is None:
        render_frame = lambda scene_id, frame, *, context: render_teaching_scene_frame(
            scene_id, frame, context=context, scenes={scene.scene_id: scene}
        )

    candidates = _candidate_frames(scene)
    page_probe_candidates = _page_probe_candidates(scene)
    page_probes = [candidates[0] for candidates in page_probe_candidates]
    attempted: list[int] = []
    last_uniform: Exception | None = None
    captured: dict[tuple[int, str | None], Any] = {}

    def _capture_one(frame: int, probe_context: Any) -> Any | None:
        nonlocal last_uniform
        background = getattr(probe_context, "html_validation_background", None)
        key = (int(frame), background)
        if key in captured:
            return captured[key]
        if frame not in attempted:
            attempted.append(frame)
        try:
            rendered = render_frame(scene.scene_id, frame, context=probe_context)
        except Exception as exc:  # noqa: BLE001 - classify only blank captures
            if not _is_blank_capture_error(exc):
                raise
            last_uniform = exc
            return None
        data = getattr(rendered, "data", b"")
        width = int(getattr(rendered, "width", 0) or 0)
        height = int(getattr(rendered, "height", 0) or 0)
        if data and width > 0 and height > 0:
            # The browser backend already rejects exact uniform captures.  A
            # multi-page deck also rejects a capture with only a few sampled
            # colours and a dominant background: that is the signature seen in
            # the real runs where a later page was technically non-empty but
            # visually blank or pushed outside the viewport.
            if len(page_probes) > 1:
                from .html_renderer_spike.capture_backend import is_effectively_blank

                near_blank, stats = is_effectively_blank(data, width, height)
                if near_blank:
                    last_uniform = RuntimeError(
                        f"near-uniform frame {frame}: {stats}"
                    )
                    return None
            captured[key] = rendered
            return rendered
        last_uniform = RuntimeError(f"rendered frame {frame} is empty")
        return None

    def _attempt(probe_context: Any) -> tuple[Any, int] | None:
        for frame in candidates:
            rendered = _capture_one(frame, probe_context)
            if rendered is not None:
                return rendered, frame
        return None

    def _validate_page_probes(probe_context: Any) -> str | None:
        if len(page_probe_candidates) <= 1:
            return None
        for index, frames in enumerate(page_probe_candidates):
            if not any(_capture_one(frame, probe_context) is not None for frame in frames):
                return (
                    f"第 {index + 1}/{len(page_probe_candidates)} 页在候选帧"
                    f" {frames} 没有可见内容"
                )
        return None

    result = _attempt(render_context)
    page_failure = _validate_page_probes(render_context)
    if result is not None and page_failure is None:
        return result

    # Transparent-only content is valid, but a contrast probe distinguishes it
    # from a page whose DOM never painted.  The background is host-owned and
    # never changes the authored document or final alpha output.
    if layer is not None and hasattr(render_context, "transparent"):
        probe_context = replace(render_context, html_validation_background="#101827")
        result = _attempt(probe_context)
        page_failure = _validate_page_probes(probe_context)
        if result is not None and page_failure is None:
            return result

    detail = f"候选帧={attempted}，视口={getattr(render_context, 'width', '?')}x{getattr(render_context, 'height', '?')}"
    if page_failure:
        detail += f"；页面质量门：{page_failure}"
    if last_uniform is not None:
        raise RuntimeError(f"HTML 验证捕获为空：{detail}；最后一次捕获：{last_uniform}") from last_uniform
    raise RuntimeError(f"HTML 验证没有可用的渲染帧：{detail}")


__all__ = ["validate_html_scene"]
