"""FH-04: a SceneRendererV2 adapter that renders a scene's HTML layer.

A scene whose ``html_layer`` is set is an HTML scene; this renderer turns each
``FrameContext`` into a deterministic RGBA frame by capturing the layer's
sanitized HTML through the pooled :class:`HtmlRendererService`, and returns the
frozen ``RendererResult`` (frame + ``RenderEvidence``) that preview and export
consume.  It implements the same ``SceneRendererV2`` Protocol as the SVG and
(GPU) backends, so the host's render-plan dispatch is backend-neutral.

Failure is fail-closed: a capture that times out, cancels or crashes raises a
:class:`HtmlRenderError` carrying the structured :class:`RenderErrorCode` — the
caller shows a diagnostic placeholder (preview) or blocks (export), never a
stale or backend-generated blank frame.  A uniform authored frame is accepted
only when the caller explicitly opted in after a scene-level validation gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import logging
import re
from typing import Any, Mapping

from ..core.contracts import (
    AlphaMode, FrameContext, RenderErrorCode, RenderError, RenderEvidence,
    RenderedFrame, RendererResult, RendererSession,
)
from .html_renderer_service import HtmlRendererService
from .html_renderer_spike import (
    CancelToken, CaptureFailure, CaptureFailureCode, CaptureSurface, UrlPolicy,
)
from .html_renderer_spike.security import ensure_font_lock_csp
from ..core.html_page_deck import (
    detect_page_count, ensure_page_markers, page_deck_capture_html, page_timing_for,
    scene_background_css,
)


_RENDERER_ID = "html"
_LOGGER = logging.getLogger(__name__)

#: AUD-30301: closing-tag search must run on the ORIGINAL document.  Indices
#: taken from ``document.lower()`` slice the original at the wrong offset
#: because ``str.lower()`` changes length for some characters (``'İ'`` U+0130
#: lowercases to two characters), corrupting the injected markup.  A case-
#: insensitive regex keeps the legacy case-insensitive matching while every
#: match index is a valid index into the original string.
_CLOSING_HEAD_RE = re.compile(r"</head\s*>", re.IGNORECASE)


def _append_validation_background(document: str, background: str | None) -> str:
    """Append a host-owned contrast background for a validation-only capture.

    The value is supplied by the host validation gate, never by model HTML.
    Keeping it as a final ``!important`` rule makes the probe observable even
    when the authored page sets ``body { background: transparent !important }``.
    The normal path passes ``None`` and returns the document byte-for-byte.
    """
    if not background:
        return document
    style = (
        "<style id=\"topos-html-validation-background\">"
        f"html,body{{background:{background} !important;}}"
        "</style>"
    )
    closing = None
    for closing in _CLOSING_HEAD_RE.finditer(document):
        pass  # last occurrence, matching the historical ``rfind`` semantics
    if closing is not None:
        closing_head = closing.start()
        return document[:closing_head] + style + document[closing_head:]
    return style + document


class HtmlRenderError(RuntimeError):
    """Raised when an HTML capture fails; carries the structured RenderError."""

    def __init__(self, render_error: RenderError) -> None:
        super().__init__(f"[{render_error.code.value}] {render_error.message}")
        self.render_error = render_error


class HtmlBlankCaptureError(HtmlRenderError):
    """A blank/uniform capture — the retryable outcome the validation gate probes.

    AUD-30304: the gate classified these failures by matching English message
    substrings ("uniform frame"/"near-uniform"), which breaks when a message
    is reworded or localized and can misclassify unrelated failures that
    happen to contain the substring.  This structural marker replaces that
    string contract: only a capture that the backend explicitly reported as
    ``BLANK`` raises it, and only this type is retried at other frames.
    """

    blank_capture = True

    def __init__(self, render_error: RenderError) -> None:
        super().__init__(render_error)


@dataclass(frozen=True)
class HtmlRendererSession:
    """Per-prepare state for an HTML scene render (closed exactly once)."""

    html: str
    bound_asset_ids: tuple[str, ...]
    csp: str
    viewport_width: int
    viewport_height: int
    duration_frames: int
    asset_resolver: Mapping[str, bytes] = field(default_factory=dict, repr=False)
    #: The forced-alias CSS prepended to ``html`` at render time so the captured
    #: bytes are font-locked to the packaged OFL font set (W0-A determinism).
    font_lock_css: str = ""
    font_asset_ids: tuple[str, ...] = ()
    font_set_hash: str = ""
    network_access: bool = False
    network_hosts: tuple[str, ...] = ()
    allow_inline_visual_resources: bool = False
    allow_gpu_apis: bool = False
    _closed: bool = False

    def close(self) -> None:
        # Stateless beyond held data; close is idempotent and exists to satisfy
        # the RendererSession contract (resources are owned by the service pool).
        object.__setattr__(self, "_closed", True)


def _scene_html_layer(scene: Any) -> Any:
    """Return the scene's html_layer from a document or its JSON dict."""
    layer = getattr(scene, "html_layer", None)
    if layer is not None:
        return layer
    if isinstance(scene, Mapping):
        layer_data = scene.get("html_layer")
        if isinstance(layer_data, Mapping) and layer_data:
            # Late import: build the layer from its dict only when needed.
            from ..core.html_layer import FreeformHtmlMotionLayer
            return FreeformHtmlMotionLayer.from_dict(layer_data)
    return None


def resolve_bound_image_bytes(scene: Any, context: Any) -> dict[str, bytes]:
    """Resolve the HTML layer's bound image assets to real file bytes (W2 P2).

    The capture backend fulfils ``topos-asset://<asset_id>`` from the
    ``asset_resolver`` mapping; until now only the packaged *fonts* were
    injected there, so every bound *image* fell through to the deterministic
    1×1 placeholder.  This reads the project's declared image assets — those the
    layer's ``bound_asset_ids`` reference and that exist under
    ``context.asset_root`` — into bytes so rendered frames show the real
    artwork instead of a stand-in.

    Resolution is best-effort and never raises: preflight (the strict-assets
    gate) is enforced upstream by the export service *before* render, so here a
    missing or undeclared asset simply stays absent and the backend serves its
    placeholder — a visible degradation, not a crash.  Fonts are skipped because
    the font lock injects those from the packaged OFL set.
    """
    layer = _scene_html_layer(scene)
    bound = tuple(getattr(layer, "bound_asset_ids", ()) or ())
    if not bound or not getattr(context, "asset_root", None):
        return {}
    from .asset_resolver import AssetResolver
    out: dict[str, bytes] = {}
    try:
        resolver = AssetResolver(context)
        # Preflight populates `resolved` (and raises under strict+missing, but
        # `_result` is set before the raise, so resolve() still works for the
        # assets that did resolve).  Export already preflew, so this is a no-op
        # raise there; for non-strict preview it never raises.
        resolver.preflight(scene)
        for asset_id in bound:
            ref = resolver.asset_by_id(scene, asset_id)
            if ref is None or getattr(ref, "kind", "") != "image":
                continue
            path = resolver.resolve(asset_id)
            if path is not None:
                out[asset_id] = path.read_bytes()
    except (ValueError, TypeError, OSError) as exc:
        # AUD-30300: best-effort, but not silent.  Only the degradation-class
        # errors are downgraded to placeholders — a strict preflight failure
        # (AssetPreflightError is a ValueError), a dict-shaped scene
        # (TypeError) or an unreadable asset (OSError).  Anything else is a
        # real defect and must surface instead of masking every image loss.
        _LOGGER.warning(
            "bound image resolution degraded to placeholders for %d asset(s): %s: %s",
            len(bound), type(exc).__name__, exc,
        )
    return out


class HtmlSceneRendererV2:
    """Renders a scene's FreeformHtmlMotionLayer via the pinned browser pool.

    Not a ``Protocol`` subclass — it structurally satisfies ``SceneRendererV2``
    (prepare/render/close).  ``renderer_build`` delegates to the service so the
    pinned chromium revision participates in the cache key.
    """

    def __init__(self, service: HtmlRendererService, *, renderer_id: str = _RENDERER_ID) -> None:
        self._service = service
        self.renderer_id = renderer_id
        # AUD-30302: whether the service accepts ``allow_uniform`` is a
        # per-service property — probe the signature once here instead of
        # re-running inspect.signature on every rendered frame. The probe is
        # kept as a compatibility shim for third-party/test services that
        # implement the pre-flag protocol (the built-in service always does).
        try:
            params = inspect.signature(self._service.render_frame).parameters
        except (TypeError, ValueError):
            params = {}
        self._service_accepts_allow_uniform = bool(
            "allow_uniform" in params
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        )

    @property
    def renderer_build(self) -> str:
        return self._service.backend_build

    # ------------------------------------------------------------------
    # SceneRendererV2
    # ------------------------------------------------------------------
    def prepare(
        self,
        scene: Any,
        context: FrameContext,
        *,
        asset_bytes: Mapping[str, bytes] | None = None,
    ) -> RendererSession:
        layer = _scene_html_layer(scene)
        if layer is None:
            raise HtmlRenderError(RenderError(
                RenderErrorCode.UNSUPPORTED_BACKEND,
                "scene has no html_layer; HtmlSceneRendererV2 only renders HTML scenes",
                frame=context.frame, fallback_available=True))
        # The default HTML layer keeps a packaged font lock for deterministic
        # interchange.  A Motion Graphic Clip may explicitly opt into the
        # freeform browser profile (font_lock=False), where authored
        # font-family declarations are part of the design and must not be
        # overwritten by the host.
        from ..core.html_fonts import (
            DEFAULT_FONT_ASSET_IDS, default_font_lock_css, default_font_set_hash,
        )
        layer_metadata = getattr(layer, "metadata", {}) or {}
        font_lock_enabled = layer_metadata.get("font_lock", True) is not False
        network_access = layer_metadata.get("network_access", False) is True
        allow_inline_visual_resources = layer_metadata.get(
            "allow_inline_visual_resources", False,
        ) is True
        allow_gpu_apis = layer_metadata.get("allow_gpu_apis", False) is True
        raw_network_hosts = layer_metadata.get("network_hosts", ())
        network_hosts = tuple(
            str(host).strip().lower().rstrip(".")
            for host in (raw_network_hosts if isinstance(raw_network_hosts, (list, tuple, set)) else ())
            if str(host).strip()
        )
        # Layers created before the page-deck pipeline (or imported from an
        # older project) may have conventional ``.page``/``.slide`` wrappers
        # without stable markers.  Normalize at the renderer boundary so all
        # preview, timeline and export paths select the same page.
        document = ensure_page_markers(layer.sanitized_html)
        page_count = detect_page_count(document)
        timing = page_timing_for(getattr(layer, "metadata", {}), page_count=page_count,
                                 fallback_default_seconds=max(0.001, layer.duration_frames / max(0.001, context.fps)) / page_count)
        return HtmlRendererSession(
            html=_append_validation_background(
                page_deck_capture_html(
                    document,
                    timing,
                    global_background=scene_background_css(getattr(scene, "background", {})),
                    page_backgrounds=tuple(
                        getattr(page, "background", "")
                        for page in (getattr(scene, "pages", ()) or ())
                    ),
                ),
                getattr(context, "html_validation_background", None),
            ),
            bound_asset_ids=tuple(getattr(layer, "bound_asset_ids", ())),
            csp=str(getattr(layer, "csp", "")) or _default_csp(),
            viewport_width=int(getattr(layer, "viewport_width", context.canvas.design_width)),
            viewport_height=int(getattr(layer, "viewport_height", context.canvas.design_height)),
            duration_frames=int(getattr(layer, "duration_frames", 1)),
            # Bound image assets resolved to real bytes by the host (W2 P2);
            # fonts are merged in at render time.  Empty when the scene declares
            # no bound images or has no asset_root (e.g. the W0-A gate).
            asset_resolver=dict(asset_bytes) if asset_bytes else {},
            font_lock_css=default_font_lock_css() if font_lock_enabled else "",
            font_asset_ids=tuple(DEFAULT_FONT_ASSET_IDS) if font_lock_enabled else (),
            font_set_hash=(str(getattr(layer, "font_set_hash", "")) or default_font_set_hash())
            if font_lock_enabled else "",
            network_access=network_access,
            network_hosts=network_hosts,
            allow_inline_visual_resources=allow_inline_visual_resources,
            allow_gpu_apis=allow_gpu_apis,
        )

    def render(
        self,
        session: RendererSession,
        context: FrameContext,
        cancel_token: Any | None = None,
    ) -> RendererResult:
        if not isinstance(session, HtmlRendererSession):
            raise HtmlRenderError(RenderError(
                RenderErrorCode.INVALID_SCENE,
                f"session must be HtmlRendererSession, got {type(session).__name__}",
                frame=context.frame))
        surface = CaptureSurface(
            int(context.surface.pixel_width),
            int(context.surface.pixel_height),
            float(context.surface.dpr),
        )
        # Font lock: prepend the trusted CSS (fonts embedded as base64 data:
        # URIs — Chromium won't fetch @font-face from topos-asset://, so the lock
        # inlines the bytes) and guarantee the CSP lets those data: fonts load.
        # This makes captured pixels a pure function of the packaged font set,
        # independent of the host OS's installed fonts.  Fonts no longer flow
        # through the asset resolver / URL policy (only bound images do).
        from ..core.html_fonts import wrap_with_font_lock
        capture_html = (wrap_with_font_lock(session.html, session.font_lock_css)
                        if session.font_lock_css else session.html)
        url_policy = UrlPolicy(
            frozenset(session.bound_asset_ids),
            allow_network_resources=session.network_access,
            allowed_network_hosts=frozenset(session.network_hosts),
            allow_inline_visual_resources=session.allow_inline_visual_resources,
            allow_gpu_apis=session.allow_gpu_apis,
        )
        resolver = dict(session.asset_resolver)
        # The font lock's @font-face uses inline data: URIs, so its CSP need
        # (font-src data:) is a render-time concern: guarantee it here regardless
        # of what the layer's authored CSP declared.  A layer without it would
        # block the @font-face and fall back to a system font, silently breaking
        # the cross-machine determinism lock.
        csp = ensure_font_lock_csp(session.csp) if session.font_lock_css else session.csp
        token = cancel_token if isinstance(cancel_token, CancelToken) else None
        # Transparent capture follows the surface's alpha intent.  The V2
        # convention (FrameContext.for_scene) maps transparent=True to
        # AlphaMode.PREMULT, so PREMULT signals "capture with alpha".
        transparent = context.surface.alpha_mode == AlphaMode.PREMULT
        service_kwargs = dict(
            frame=int(context.frame),
            fps_num=int(context.fps_num),
            fps_den=int(context.fps_den),
            csp=csp,
            url_policy=url_policy,
            asset_resolver=resolver or None,
            cancel_token=token,
            transparent=transparent,
        )
        # AUD-30302: decision cached in __init__ — no per-frame reflection.
        if self._service_accepts_allow_uniform:
            service_kwargs["allow_uniform"] = bool(getattr(context, "allow_uniform_frames", False))
        result = self._service.render_frame(capture_html, surface, **service_kwargs)
        if isinstance(result, CaptureFailure):
            # Fail-closed: never return a blank/stale frame as a success.
            render_error = RenderError(
                _failure_to_code(result.code),
                result.message,
                frame=context.frame,
                recoverable=result.code in (CaptureFailureCode.TIMEOUT, CaptureFailureCode.CANCELLED),
                fallback_available=True,
                cause=result.backend_id,
            )
            # A BLANK capture is retryable at a different authored frame by the
            # validation gate, which detects it structurally (AUD-30304)
            # instead of parsing this message.
            if result.code == CaptureFailureCode.BLANK:
                raise HtmlBlankCaptureError(render_error) from None
            raise HtmlRenderError(render_error)
        evidence = RenderEvidence(
            renderer_id=self.renderer_id,
            renderer_build=self.renderer_build,
            font_set_hash=session.font_set_hash,
            render_duration_ms=float(result.evidence.duration_ms),
            frame_hash=result.content_hash,
            diagnostics={
                "backend_build": result.evidence.backend_build,
                "viewport": list(result.evidence.viewport),
                "capture_method": result.evidence.capture_method,
                "transparent": transparent,
                "dpr": float(context.surface.dpr),
                "font_locked": bool(session.font_lock_css),
                "uniform_frame_allowed": bool(getattr(result.evidence, "notes", {}).get("uniform_frame_allowed", False)),
            },
        )
        frame = RenderedFrame(
            frame=int(context.frame),
            width=int(result.surface.width),
            height=int(result.surface.height),
            pixel_format="rgba8",
            data=bytes(result.data),
            backend=self.renderer_id,
        )
        return RendererResult(frame=frame, evidence=evidence)

    def close(self, session: RendererSession) -> None:
        close = getattr(session, "close", None)
        if callable(close):
            close()


def _failure_to_code(code: CaptureFailureCode) -> RenderErrorCode:
    return {
        CaptureFailureCode.TIMEOUT: RenderErrorCode.RENDER_FAILED,
        CaptureFailureCode.CANCELLED: RenderErrorCode.CANCELLED,
        CaptureFailureCode.CRASH: RenderErrorCode.RENDER_FAILED,
        CaptureFailureCode.BLANK: RenderErrorCode.RENDER_FAILED,
        CaptureFailureCode.BLOCKED_RESOURCE: RenderErrorCode.MISSING_ASSET,
        CaptureFailureCode.INVALID_INPUT: RenderErrorCode.INVALID_SCENE,
        CaptureFailureCode.UNSUPPORTED: RenderErrorCode.UNSUPPORTED_BACKEND,
    }.get(code, RenderErrorCode.RENDER_FAILED)


def _default_csp() -> str:
    from .html_renderer_spike.security import CspPolicy
    return CspPolicy().to_header()


__all__ = [
    "HtmlBlankCaptureError",
    "HtmlRenderError",
    "HtmlRendererSession",
    "HtmlSceneRendererV2",
    "resolve_bound_image_bytes",
]
