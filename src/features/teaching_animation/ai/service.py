"""Shared AI Assistant service for Teaching Animation."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core.contracts import AnimationFrame, RenderContext, RenderedFrame
from ..core.html_layer import FreeformHtmlMotionLayer, HtmlEditSlot
from ..core.html_layer_commands import SetHtmlLayerCommand, build_html_layer_from_pipeline
from ..core.html_template import HtmlTemplate, HtmlTemplateRegistry
from ..core.model import TeachingSceneDocument
from ..host_adapters import insert_motion_graphic_clip
from ..render.html_layer import (
    promote_html_layer,
    apply_html_template as _apply_html_template_engine,
)
from .command_catalog import ExtendedCommandRegistry
from .commands import TeachingAICommand, TeachingAIProposal
from .context_provider import TeachingSceneContextProvider
from .html_generation_protocol import HtmlGenerationConstraints, HtmlGenerationOutcome
from .html_llm_generator import LlmGenerationRequest, generate_html_with_llm


class TeachingAnimationAIService:
    def __init__(self, scene: TeachingSceneDocument, *, project_id: str | None = None) -> None:
        self.context_provider = TeachingSceneContextProvider(scene, project_id=project_id)
        # Extended registry so service.dry_run accepts the full MG-09 vocabulary
        # (base 4 + 16 extended); it is a drop-in superset of the base registry.
        self.commands = ExtendedCommandRegistry()
        # Session-scoped registry for FH-09 promoted HTML templates.  Promoted
        # templates live here so a later apply_html_template call (same session)
        # can find them — they are not scene state and not global builtins.
        self.html_templates = HtmlTemplateRegistry()

    @property
    def scene(self) -> TeachingSceneDocument:
        return self.context_provider.scene

    def dry_run(self, commands: Sequence[TeachingAICommand]) -> TeachingAIProposal:
        return self.commands.dry_run(self.scene, commands)

    def create_scene(self, name: str = "Teaching Scene", **kwargs: Any) -> TeachingSceneDocument:
        return self.commands.create_scene(name, **kwargs)

    def update_scene(self, scene: TeachingSceneDocument) -> None:
        self.context_provider.set_scene(scene)

    def render_preview(self, frame: int, context: RenderContext | None = None) -> RenderedFrame:
        context = context or RenderContext(fps=self.scene.fps, width=self.scene.width, height=self.scene.height)
        from ..render.scene_renderer_router import select_scene_renderer
        renderer = select_scene_renderer(self.scene)
        renderer.prepare(self.scene, context)
        try:
            return renderer.render_frame(AnimationFrame(frame, context.fps))
        finally:
            renderer.cleanup()

    def insert_into_timeline(self, timeline: Any, *, timeline_start: int = 0,
                             timeline_duration: int | None = None, command_stack: Any = None,
                             confirmed: bool = False) -> Any:
        if not confirmed:
            raise PermissionError("timeline insertion requires explicit confirmation")
        result = insert_motion_graphic_clip(timeline, self.scene, timeline_start=timeline_start,
                                            timeline_duration=timeline_duration, command_stack=command_stack)
        return result

    def replace_clip(self, timeline: Any, clip_id: str, *, timeline_duration: int | None = None,
                     command_stack: Any = None, confirmed: bool = False) -> Any:
        if not confirmed:
            raise PermissionError("clip replacement requires explicit confirmation")
        clip = timeline.get_clip_by_id(clip_id)
        if clip is None:
            raise KeyError(f"Clip not found: {clip_id}")
        # Do NOT pre-delete the original clip here (AUD-27018): deleting it
        # before the insert command runs leaves the removal outside the undo
        # stack, so undo dropped the original clip (and its manifest) forever.
        # ``mode="overwrite"`` makes the insert command itself remove every
        # overlapped clip — including this one — recording it so undo restores
        # the full prior timeline state.
        return insert_motion_graphic_clip(
            timeline,
            self.scene,
            timeline_start=clip.timeline_start,
            timeline_duration=timeline_duration or clip.timeline_duration,
            track_id=clip.track_id,
            mode="overwrite",
            command_stack=command_stack,
        )

    # ------------------------------------------------------------------ #
    # Freeform HTML generation loop (W2 / AQ-09 → FH-01)
    # ------------------------------------------------------------------ #
    #
    # The generation pipeline (sanitize → static-analysis → bind-assets →
    # preview → approve) was built and tested but had NO production caller:
    # a user could not reach Freeform HTML through the assistant at all.  This
    # pair closes that loop.  ``generate_html`` runs the controlled pipeline
    # and returns an outcome whose ``writable`` flag is the single gate; the
    # host previews the staged page, and on approval calls ``apply_html_layer``
    # to build a FreeformHtmlMotionLayer and attach it to the scene through the
    # immutable SetHtmlLayerCommand (so the write is undoable and round-trips
    # with the project).

    def generate_html(
        self,
        client: Any,
        *,
        request: LlmGenerationRequest | None = None,
        constraints: HtmlGenerationConstraints | None = None,
        known_asset_ids: tuple[str, ...] = (),
        approved: bool = False,
        preview_backend: Any = None,
        preview_frame: int = 0,
        preview_surface: Any = None,
    ) -> HtmlGenerationOutcome:
        """Run the controlled HTML generation pipeline (AQ-09).

        Returns an :class:`HtmlGenerationOutcome` whose ``writable`` flag is the
        single decision point for "may this become a project layer".  With
        ``approved=False`` (default) the page is staged for preview but not
        writable; the host re-runs with ``approved=True`` after the user accepts
        the preview, then calls :meth:`apply_html_layer`.

        ``preview_backend`` is a pinned headless-browser capture backend (ADR
        0002); when provided the PREVIEW stage captures a frame for evidence.
        """
        return generate_html_with_llm(
            client, request=request, constraints=constraints,
            known_asset_ids=known_asset_ids, approved=approved,
            renderer_available=preview_backend is not None,
            preview_backend=preview_backend, preview_frame=preview_frame,
            preview_surface=preview_surface,
        )

    def apply_html_layer(
        self,
        outcome: HtmlGenerationOutcome,
        *,
        layer_id: str | None = None,
        bound_asset_ids: tuple[str, ...] = (),
        editable_slots: tuple[HtmlEditSlot, ...] = (),
        editable_tokens: tuple[str, ...] = (),
        sanitizer_version: str = "",
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        duration_frames: int | None = None,
        fps_num: int | None = None,
        fps_den: int = 1,
        page_timing: Any | None = None,
        metadata: Mapping[str, Any] | None = None,
        command_stack: Any = None,
    ) -> FreeformHtmlMotionLayer:
        """Build a layer from a **writable** outcome and attach it to the scene.

        The layer's viewport/duration/fps default to the scene's, so a layer
        matches the clip it lives in.  ``SetHtmlLayerCommand`` makes the write
        reversible and recorded **when a ``command_stack`` is supplied** (the
        command is pushed with its undo state; AUD-27019 — without a stack the
        command instance and its previous-layer snapshot are discarded, so that
        path is *not* undoable); ``authored_source`` is pulled from the outcome
        for FH-01 provenance (auditing what the sanitizer changed).  Raises
        ``ValueError`` for a non-writable outcome — only an approved, sanitized,
        static-analysis-clean page may become a project layer.
        """
        if not getattr(outcome, "writable", False):
            reasons = getattr(outcome, "blocking_reasons", ())
            raise ValueError(
                "cannot apply a non-writable HTML layer (approve + pass "
                f"sanitize/static-analysis first): {('; '.join(reasons)) if reasons else 'not writable'}"
            )
        import uuid
        layer_metadata: dict[str, Any] = dict(metadata or {})
        if page_timing is None:
            from ..core.html_page_deck import PageDeckTiming, detect_page_count
            page_count = detect_page_count(outcome.sanitized_html)
            total_seconds = max(0.001, self.scene.duration_frames / self.scene.fps)
            page_timing = PageDeckTiming(
                page_count=page_count,
                mode="default",
                default_seconds=total_seconds / page_count,
            )
        from ..core.html_page_deck import timing_metadata
        layer_metadata.update(timing_metadata(page_timing))
        layer = build_html_layer_from_pipeline(
            outcome,
            layer_id=layer_id or f"html_{uuid.uuid4().hex}",
            authored_source=getattr(outcome, "authored_source", "") or outcome.sanitized_html,
            bound_asset_ids=bound_asset_ids,
            editable_slots=editable_slots,
            editable_tokens=editable_tokens,
            sanitizer_version=sanitizer_version,
            viewport_width=int(viewport_width if viewport_width is not None else self.scene.width),
            viewport_height=int(viewport_height if viewport_height is not None else self.scene.height),
            duration_frames=int(duration_frames if duration_frames is not None else self.scene.duration_frames),
            fps_num=int(round(fps_num if fps_num is not None else self.scene.fps)),
            fps_den=fps_den,
            metadata=layer_metadata,
        )
        self._apply_layer_command(SetHtmlLayerCommand(layer), command_stack=command_stack)
        return layer

    def _apply_layer_command(self, command: SetHtmlLayerCommand, *, command_stack: Any = None) -> TeachingSceneDocument:
        """Apply a scene HTML-layer command, through the undo stack when given.

        AUD-27019: ``SetHtmlLayerCommand`` records its undo snapshot on the
        command *instance*; applying it directly and dropping the instance lost
        the undo state.  When ``command_stack`` is provided the command goes
        through ``command_stack.execute`` so it is recorded and reversible.
        """
        if command_stack is None:
            updated = command.apply(self.scene)
        else:
            updated = command_stack.execute(command, self.scene)
        self.update_scene(updated)
        return updated

    # ------------------------------------------------------------------ #
    # HTML template promotion / re-application (FH-09)
    # ------------------------------------------------------------------ #
    #
    # promote_html_template distils the scene's *current, verified* HTML layer
    # into a reusable HtmlTemplate and registers it on this session's registry;
    # apply_html_template re-instantiates one with new copy / brand / assets and
    # attaches the result as a fresh FreeformHtmlMotionLayer (via the same
    # SetHtmlLayerCommand path as apply_html_layer, so the write is undoable).
    # The original layer is untouched by promotion — it stays as the audited
    # fallback (FH-09).

    def promote_html_template(
        self,
        *,
        template_id: str,
        display_name: str,
        description: str = "",
        version: int = 1,
        publish_to_effects: bool = True,
    ) -> HtmlTemplate:
        """Freeze the scene's current HTML layer into a reusable template.

        The template is registered in :attr:`html_templates` (session-scoped)
        so a subsequent :meth:`apply_html_template` can reach it.  By default
        it is also published to the Edit Effects ``MG 生成器`` palette; this is
        a recipe registration, not a Media Pool asset.  Set
        ``publish_to_effects=False`` for a private assistant/session template.
        The scene's layer is not modified — promotion is a pure distillation,
        and the layer remains the audited fallback.  Raises ``ValueError`` when
        the scene has no HTML layer to promote.
        """
        if self.scene.html_layer is None:
            raise ValueError("scene has no HTML layer to promote")
        template = promote_html_layer(
            self.scene.html_layer,
            template_id=template_id,
            display_name=display_name,
            description=description,
            version=version,
        )
        self.html_templates.register(template)
        if publish_to_effects:
            from ..core.motion_graphic_generator_registry import (
                register_motion_graphic_generator,
            )
            register_motion_graphic_generator(template)
        return template

    def apply_html_template(
        self,
        template_id: str,
        params: Mapping[str, Any] | None = None,
        *,
        layer_id: str | None = None,
        version: int | None = None,
        command_stack: Any = None,
    ) -> FreeformHtmlMotionLayer:
        """Re-instantiate a registered HTML template onto the scene.

        Looks up ``template_id`` in :attr:`html_templates`, merges ``params``
        over the template defaults, builds a fresh layer (substitute markers →
        re-sanitize → rebuild), and attaches it through ``SetHtmlLayerCommand``
        so the write round-trips with the project.  The write is **undoable and
        recorded only when ``command_stack`` is supplied** (AUD-27019); without
        a stack the command's undo snapshot is discarded.  Raises ``KeyError``
        for an unknown template id and ``ValueError`` for a missing required
        parameter.
        """
        import uuid
        template = self.html_templates.get(template_id, version=version)
        if template is None:
            raise KeyError(f"unknown HTML template: {template_id!r}")
        errors = template.validate_params(dict(params or {}))
        if errors:
            raise ValueError(
                f"invalid params for template {template_id!r}: {'; '.join(errors)}")
        layer = _apply_html_template_engine(
            template, params, layer_id=layer_id or f"html_{uuid.uuid4().hex}")
        self._apply_layer_command(SetHtmlLayerCommand(layer), command_stack=command_stack)
        return layer

    def list_html_templates(self) -> tuple[HtmlTemplate, ...]:
        """Return the templates promoted in this session (FH-09 reachability)."""
        return self.html_templates.list_templates()
