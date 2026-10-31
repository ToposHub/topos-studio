"""W2: the LLM entry point that produces a Freeform HTML motion layer.

The controlled pipeline (:func:`run_html_generation_pipeline`) is the *downstream*
half of AQ-09 — it takes already-generated ``raw_html`` and decides whether it is
safe to write.  This module is the missing *upstream* half: it asks a language
model to design an HTML/CSS motion page, under the exact constraints the
pipeline enforces, then runs the result straight through the pipeline so a
caller only ever sees a typed :class:`HtmlGenerationOutcome`.

Design rules:

* **constraints become the prompt.**  :class:`HtmlGenerationConstraints` is
  surfaced to the model verbatim (allowed/forbidden subset, viewport, duration)
  AND re-enforced by the sanitizer + quality gate — so a model that ignores the
  prompt is still caught.  Defence in depth, never trust the model alone.
* **motion is browser-authored.**  The pinned capture backend pauses/ seeks
  browser animations per frame via a privileged bridge (it also sets
  ``--topos-frame`` / ``--topos-time`` / ``--topos-progress``).  The generic
  profile remains conservative; Motion Graphic's explicit freeform profile may
  use JavaScript, Canvas and Web Animations as well as CSS.
* **injectable client.**  The :class:`~src.features.edit.ai_editing.llm_client.LLMClient`
  is passed in, so this module has no LLM-vendor dependency and is unit-testable
  with a stub.  The caller builds the client with
  :func:`~src.features.edit.ai_editing.llm_client.make_client` from the active
  profile (e.g. DeepSeek-v4-flash).
* **fail-closed.**  An LLM error (auth / rate-limit / network / invalid response)
  is surfaced as an :class:`HtmlGenerationOutcome` whose ``writable`` is False
  with the reason in ``blocking_reasons`` — never as a raised exception that
  could bypass the pipeline gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..render.html_layer.sanitizer import HtmlSanitizer
from .html_generation_protocol import (
    HtmlGenerationConstraints,
    HtmlGenerationOutcome,
    HtmlPipelineStage,
    StageResult,
    run_html_generation_pipeline,
)
from .motion_quality_gate import QualityResult


def _failed_outcome(reason: str, *, renderer_available: bool) -> HtmlGenerationOutcome:
    """A non-writable outcome for an LLM/generation failure (fail-closed)."""
    return HtmlGenerationOutcome(
        sanitized_html="",
        stages=(StageResult(
            stage=HtmlPipelineStage.CONSTRAIN,
            passed=False,
            detail=reason,
        ),),
        quality=QualityResult(),   # no issues: the failure is in the CONSTRAIN stage
        approved=False,
        renderer_available=renderer_available,
        writable=False,
    )


# A system prompt that tells the model exactly what the pipeline will accept.
# It mirrors HtmlGenerationConstraints so the model is set up to succeed, while
# the sanitizer/quality-gate still catch anything it gets wrong.
def build_html_system_prompt(
    constraints: HtmlGenerationConstraints,
    *,
    known_asset_ids: tuple[str, ...] = (),
) -> str:
    """Render the generation constraints as the model's system prompt."""
    if constraints.allow_network_resources:
        forbidden_items = list(constraints.forbidden)
        if constraints.allow_inline_scripts:
            forbidden_items = [
                item for item in forbidden_items
                if item not in {"script", "event handlers (on*)", "remote urls", "external fonts", "@import"}
            ]
            resource_clause = (
                "This is the Motion Graphic freeform browser profile: HTTPS CDN resources, "
                "inline JavaScript, Canvas, Web Animations, event handlers, CSS Houdini, "
                "Tailwind CDN, Font Awesome, Google Fonts, external CSS, fonts, images and "
                "external scripts are available. GPU compositing/WebGL, CSS 3D, and "
                "data/blob-backed visual assets are available when enabled by the clip. "
                "Only file://, host/file access, popups, downloads, executable data URLs "
                "and persistent local storage remain isolated."
            )
        else:
            forbidden_items = [
                item for item in forbidden_items
                if item not in {"script", "remote urls", "external fonts", "@import"}
            ] + ["inline scripts"]
            resource_clause = (
                "HTTPS CDN resources are enabled in this isolated browser profile: you may use "
                "Tailwind CDN, Font Awesome, Google Fonts, external CSS, fonts, images and "
                "external scripts. Inline scripts, event handlers, file://, data://, popups, "
                "downloads and host/file access remain unavailable."
            )
        forbidden = ", ".join(forbidden_items)
    else:
        forbidden = ", ".join(constraints.forbidden)
        resource_clause = (
            "The capture is offline: do not depend on remote CSS, fonts, images or scripts; "
            "use inline HTML/CSS/SVG and bound topos-asset:// resources."
        )
    assets_clause = ""
    if known_asset_ids:
        ids = ", ".join(sorted(known_asset_ids))
        assets_clause = (
            f"\n- You MAY reference bound project assets with "
            f"src=\"topos-asset://<id>\". The only valid asset ids are: {ids}. "
            f"Use no other image source.\n"
        )
    if constraints.page_count is not None and int(constraints.page_count) > 0:
        page_count_clause = (
            f'Create exactly {int(constraints.page_count)} marked page container(s).\n'
        )
    else:
        page_count_clause = (
            "Choose any number of pages that serves the idea, and mark each page container so the"
            " editor can control its timing.\n"
        )
    runtime_contract = f"""You are the website designer inside Topos Color's browser-backed
Motion Graphic Clip. The user owns the idea; you own the complete website
design. Decide the content, narrative, number of pages, HTML structure, CSS,
layout, typography, colors, imagery, SVG, animation and visual language
yourself. The host provides no template or art direction.

The following is the browser integration contract, not a design brief:
- Begin the output with <!DOCTYPE html>. Output ONLY the HTML document — no
  markdown fences, no commentary before or after, no explanations.
- HTML + an inline <style> block + inline style="" CSS; external HTTPS CSS
  and browser resources follow the profile described below.
- FORBIDDEN: {forbidden}.
- {resource_clause}
- Fonts: authored font-family choices are honored in the freeform browser
  profile. Choose typography deliberately instead of assuming a host template
  will replace it.
- {page_count_clause.rstrip()}
  Each data-topos-page container is one independent full-canvas webpage. The
  editor controls page order, durations, transitions. The editor owns page timing and page-to-page transitions, including total clip timing
  after import; do not encode page timing in JavaScript or page-level timers.
  CSS @keyframes, animation, transition and layout techniques remain available
  inside every page. The capture bridge exposes --topos-frame, --topos-time and
  --topos-progress for deterministic page-internal motion.
- Delivery viewport: {int(constraints.viewport_width)}x{int(constraints.viewport_height)} CSS px.
  This is the capture/export viewport, not a fixed-pixel layout prescription.
  Build a responsive page: use ``width:100vw; min-height:100vh`` (or equivalent),
  fluid Grid/Flex layouts, relative units and ``clamp()`` where appropriate. Do not
  hard-code html/body to this pixel size or rely on absolute positioning for the
  entire composition; the same document must remain well composed when embedded
  in a differently sized browser view while preserving this delivery aspect.
- Page richness is a delivery requirement, not a visual template: every page
  must advance the narrative with one subject-specific hero visual, diagram or
  spatial composition plus at least two substantive supporting information
  groups. Choose the groups freely—flows, comparisons, timelines, data, labels,
  examples, interfaces, annotations or states—as the idea calls for. An opener
  or closer may use less copy, but never leave a page as only a headline or
  isolated icon; preserve hierarchy and breathing room instead of cramming text.
- You MAY mark important text nodes with data-topos-node-id=\"<stable-id>\" so
  the host can expose them for inline editing; this is optional.{assets_clause}

Return only the HTML document."""
    # Keep the visual authorship contract in the folder-backed Motion Graphic
    # plugin so users can refine the default design direction without editing
    # Python.  The constraint block above remains generated from the actual
    # scene and therefore cannot be weakened by a prompt file.
    try:
        from src.features.edit.ai_editing.assistant_plugins import assistant_plugin_prompt

        plugin_prompt = assistant_plugin_prompt("motion_graphic", "html_build")
    except (ImportError, AttributeError):
        plugin_prompt = ""
    return f"{plugin_prompt}\n\n{runtime_contract}" if plugin_prompt else runtime_contract


_DEFAULT_USER_PROMPT = (
    "Design an original 6-second HTML/CSS motion-graphic essay about how a "
    "seed becomes a tree. Invent the visual metaphor, narrative beats, "
    "typography, palette and page compositions yourself. Use subject-specific "
    "geometry and CSS motion. "
    "1920x1080.")


@dataclass(frozen=True)
class LlmGenerationRequest:
    """The caller's intent for one HTML generation call."""

    user_prompt: str = _DEFAULT_USER_PROMPT
    temperature: float = 0.4
    max_tokens: int = 6000


_FENCE_RE = re.compile(r"^\s*```(?:html|HTML)?[^\n]*\n|\n```\s*$", re.DOTALL)


def strip_markdown_fence(text: str) -> str:
    """Normalize a model response into the HTML document itself.

    Providers occasionally ignore the ``output only HTML`` instruction and
    add a markdown fence or one sentence before the document.  The old
    regex-only implementation handled the ideal fence but left a leading
    `````html`` marker in the Motion Graphic path, where it was then rendered
    as visible text.  Strip one outer fence, discard harmless preamble text
    before ``<!doctype``/``<html``, and trim a trailing fence.  This does not
    relax the sanitizer: the resulting document still goes through the full
    HTML/CSS allowlist and quality gate.
    """
    value = str(text or "").strip()
    if not value:
        return ""
    match = re.match(r"^```(?:html)?\s*\n?(.*?)\n?```\s*$", value, re.IGNORECASE | re.DOTALL)
    if match:
        value = match.group(1).strip()
    start_candidates = [
        index for index in (
            value.lower().find("<!doctype html"),
            value.lower().find("<html"),
        ) if index >= 0
    ]
    if start_candidates:
        value = value[min(start_candidates):].lstrip()
    # AUD-26910：仅当残留围栏位于文档末尾（其后仅空白）才剥离。旧的
    # ``rfind("```")`` 无条件从最后一个围栏截断——正文含代码围栏的教学
    # 页面（如 <pre> 内的示例代码）会被静默腰斩，只剩围栏前的半篇文档。
    trailing_fence = re.search(r"```\s*$", value)
    if trailing_fence is not None:
        value = value[:trailing_fence.start()].rstrip()
    # Keep the historical regex as a final compatibility pass for unusual
    # whitespace/newline combinations.
    return _FENCE_RE.sub("", value).strip()


def generate_raw_html(
    client: Any,
    request: LlmGenerationRequest,
    *,
    constraints: HtmlGenerationConstraints,
    known_asset_ids: tuple[str, ...] = (),
) -> str:
    """Call the LLM for raw HTML, stripping any stray markdown fence.

    Raises whatever ``client.complete`` raises (e.g. ``LLMError``); callers that
    want fail-closed behaviour use :func:`generate_html_with_llm`, which maps
    errors into a non-writable outcome.
    """
    messages = [
        {"role": "system", "content": build_html_system_prompt(
            constraints, known_asset_ids=known_asset_ids)},
        {"role": "user", "content": request.user_prompt},
    ]
    raw = client.complete(
        messages,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )
    return strip_markdown_fence(raw)


def generate_html_with_llm(
    client: Any,
    *,
    request: LlmGenerationRequest | None = None,
    constraints: HtmlGenerationConstraints | None = None,
    known_asset_ids: tuple[str, ...] = (),
    approved: bool = False,
    renderer_available: bool = False,
    sanitizer: HtmlSanitizer | None = None,
    preview_backend: Any = None,
    preview_frame: int = 0,
    preview_surface: Any = None,
) -> HtmlGenerationOutcome:
    """Generate HTML with the LLM and run it through the controlled pipeline.

    This is the full upstream→downstream AQ-09 loop for one model attempt.  An
    LLM failure (auth / rate-limit / network / empty content) is reported as a
    non-writable outcome with the reason in ``blocking_reasons`` rather than
    raised, so the host's ``outcome.writable`` gate is the single decision point.

    Set ``approved=True`` only after the user accepts the result; by default the
    outcome is staged-but-not-approved (writable stays False).
    """
    request = request or LlmGenerationRequest()
    constraints = constraints or HtmlGenerationConstraints()
    try:
        raw_html = generate_raw_html(
            client, request,
            constraints=constraints, known_asset_ids=known_asset_ids,
        )
    except Exception as exc:  # LLM error → fail-closed outcome, never raise.
        code = getattr(exc, "code", None)
        reason = f"{type(exc).__name__}({code}): {exc}" if code else str(exc)
        return _failed_outcome(f"llm generation failed: {reason}",
                               renderer_available=renderer_available)
    if not raw_html or not raw_html.strip():
        return _failed_outcome("llm returned empty content",
                               renderer_available=renderer_available)
    return run_html_generation_pipeline(
        raw_html,
        constraints=constraints,
        known_asset_ids=known_asset_ids,
        approved=approved,
        renderer_available=renderer_available,
        sanitizer=sanitizer,
        preview_backend=preview_backend,
        preview_frame=preview_frame,
        preview_surface=preview_surface,
    )


__all__ = [
    "LlmGenerationRequest",
    "build_html_system_prompt",
    "generate_html_with_llm",
    "generate_raw_html",
    "strip_markdown_fence",
]
